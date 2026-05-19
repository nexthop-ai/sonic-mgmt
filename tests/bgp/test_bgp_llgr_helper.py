"""
Test BGP Long-Lived Graceful Restart (RFC 9494) helper behavior on SONiC.

Topology: any
Device type: vs

The DUT acts as an LLGR helper; a neighbor acts as the LLGR speaker.

Per RFC 9494, the LLGR capability MUST be advertised alongside the GR
capability (RFC 4724); a receiver MUST ignore LLGR if GR is absent. So the
retention scenarios below are not "LLGR with GR" vs "LLGR without GR" — they're
both LLGR+GR, differing only in the advertised GR restart-time.

Test scope is split into two layers:

  Layer 1 — DUT-side, peer-independent (runs on any testbed):
    1. After llgr_stale_time is HSET on BGP_GLOBALS, bgpcfgd translates the
       knob to FRR's running-config AND FRR advertises the LLGR capability
       outward on each BGP session. No peer LLGR support required.

  Layer 2 — End-to-end retention, requires LLGR-capable speaker (skipped
  cleanly otherwise):
    2. Zero GR restart-time (RFC 9494 section 4.1's "the conventional GR phase
       can be skipped by ... advertising a Restart Time of zero"): when the
       peer's bgpd is killed, the DUT transitions retained routes to
       llgr-stale essentially immediately.
    3. Non-zero GR restart-time: routes are first held as plain stale during
       the GR restart window, then tagged with llgr-stale once that window
       expires.
    4. When the peer stays down past the LLGR stale-time, retained routes
       are withdrawn from the RIB (RFC 9494 section 4.4).

  Layer 2 tests also verify that when the peer reconnects within the LLGR
  stale-time, routes are re-learned fresh and the llgr-stale community is
  cleared.
"""

import json
import logging
from typing import Any
import pytest

from tests.bgp.bgp_helpers import get_route_communities
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.bgp import get_bgp_neighbors_from_config_facts
from tests.common.devices.eos import EosHost
from tests.common.devices.sonic import SonicHost
from tests.common.utilities import is_ipv4_address, wait_until

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.device_type('vs'),
    # kill_bgpd on the speaker drives the DUT into NSF and produces session-down /
    # hold-timer-expired syslogs that the log analyzer would flag as unexpected.
    pytest.mark.disable_loganalyzer,
]

logger = logging.getLogger(__name__)

# Stale-time advertised by the LLGR helper (DUT) and by the speaker (neighbor)
LLGR_STALE_TIME = 120
# GR restart-time advertised by the neighbor in the combined GR+LLGR scenario
GR_RESTART_TIME = 30
# Well-known FRR community-string token mapped from the LLGR_STALE numeric value (0xFFFF0006)
LLGR_COMMUNITY = "llgr-stale"
# Bound on the number of prefixes inspected during community presence/absence checks —
# a full sweep would multiply test runtime by the size of the advertised RIB
MAX_PREFIXES_TO_CHECK = 5

# Time to wait for the DUT to enter NSF state after the peer's bgpd dies
NSF_ENTRY_TIMEOUT = 60
# Time to wait for the llgr-stale community to appear when no GR was negotiated
LLGR_COMMUNITY_APPEAR_TIMEOUT = 60
# Time to wait for the llgr-stale community to clear after the peer reconnects
LLGR_COMMUNITY_CLEAR_TIMEOUT = 120
# Time to wait for BGP sessions to settle after a config change
BGP_SESSION_RECOVERY_TIMEOUT = 300
# Time to wait for bgpcfgd to translate the BGP_GLOBALS knob into FRR running-config
LLGR_CONFIG_PROPAGATION_TIMEOUT = 30


def _frontend_namespaces(host: Any) -> list[Any]:
    """Return the list of frontend-ASIC namespaces to iterate (or `[None]` for single-ASIC).

    MultiAsicSonicHost exposes `get_frontend_asic_namespace_list()`; a plain SonicHost
    (single-ASIC DUT or any SONiC neighbor in `nbrhosts`) does not, in which case we
    issue a single un-namespaced call. The `None` sentinel matches the contract of
    `SonicHost.get_route(namespace=...)` and `sonic-db-cli` (omit `-n` when None).
    """
    if hasattr(host, "get_frontend_asic_namespace_list"):
        return list(host.get_frontend_asic_namespace_list())
    return [None]


def _vtysh_per_asic(host: Any, vtysh_cmd: str) -> list[str]:
    """Run `vtysh_cmd` once per frontend ASIC and return each stdout."""
    outputs = []
    for namespace in _frontend_namespaces(host):
        if namespace and hasattr(host, "get_vtysh_cmd_for_namespace"):
            cmd = host.get_vtysh_cmd_for_namespace(vtysh_cmd, namespace)
        else:
            cmd = vtysh_cmd
        outputs.append(host.shell(cmd, verbose=False)["stdout"])
    return outputs


def _sonic_db_cli_per_asic(duthost: Any, db_args: str) -> None:
    """Run a sonic-db-cli mutation (HSET / HDEL / ...) on every frontend ASIC CONFIG_DB.

    bgpcfgd runs per namespace on multi-ASIC devices; a global write to the default
    CONFIG_DB is not seen by per-namespace daemons, so for `BGP_GLOBALS` we must
    target each frontend CONFIG_DB explicitly. On single-ASIC devices we fall back
    to the un-namespaced form.
    """
    for namespace in _frontend_namespaces(duthost):
        ns_arg = "-n {} ".format(namespace) if namespace else ""
        duthost.shell("sonic-db-cli {}{}".format(ns_arg, db_args), module_ignore_errors=True)


def is_llgr_configured(host: Any) -> bool:
    """Return True if LLGR_STALE_TIME is present in FRR running-config on all frontend ASICs.
    Coupled to LLGR_STALE_TIME by design — confirms bgpcfgd pushed our specific value.

    The `bool(outputs)` guard avoids a vacuous-all pass when `_vtysh_per_asic` returns
    an empty list (e.g. chassis linecard with no frontend ASICs) — otherwise the
    propagation `wait_until` would succeed without actually verifying anything.
    """
    target = "long-lived-graceful-restart stale-time {}".format(LLGR_STALE_TIME)
    outputs = _vtysh_per_asic(host, "vtysh -c 'show running-config'")
    return bool(outputs) and all(target in cfg for cfg in outputs)


def _prefix_route_data(duthost: Any, prefix: str) -> dict[str, Any]:
    """Return `show bgp <afi> <prefix> json` output merged across all frontend ASICs.

    Wraps `SonicHost.get_route` because that helper is single-namespace and we need
    to aggregate paths across ASICs. We tolerate JSON parse failures (vtysh emits
    `% ...` plain-text on unknown-prefix / mid-state queries) and surface them so an
    empty result is debuggable in CI instead of looking like a quiet RIB miss.
    """
    paths: list[Any] = []
    for namespace in _frontend_namespaces(duthost):
        try:
            data = duthost.get_route(prefix, namespace=namespace)
        except json.JSONDecodeError as exc:
            logger.warning("_prefix_route_data(%s, ns=%s): JSON parse failed: %s",
                           prefix, namespace, exc)
            continue
        paths.extend(data.get("paths", []))
    return {"paths": paths}


def prefix_has_llgr_stale_community(duthost: Any, prefix: str) -> bool:
    """Return True if any path for `prefix` carries the llgr-stale community.

    Uses `bgp_helpers.get_route_communities` to extract the per-path community list,
    then exact-matches the `llgr-stale` token (FRR returns space-separated tokens in
    the `string` field, so a plain substring check could false-positive on a future
    community whose name contains "llgr-stale").
    """
    route_data = _prefix_route_data(duthost, prefix)
    if not route_data["paths"]:
        return False
    communities = get_route_communities(duthost, route_data, prefix)
    return any(LLGR_COMMUNITY in c.split() for c in communities)


def prefix_in_rib_from_neighbor(duthost: Any, prefix: str, neighbor_ips: list[str]) -> bool:
    """Return True if `prefix` has a path on the DUT whose nexthop is one of `neighbor_ips`.

    The naive `prefix in show bgp` check passes vacuously when a *different* live
    neighbor is still advertising the same prefix — making "LLGR retained the route"
    indistinguishable from "another neighbor never went away". This stricter check
    walks the per-path peer IP so retention from the *dead* neighbor is what we
    actually verify.
    """
    neighbor_set = set(neighbor_ips)
    for path in _prefix_route_data(duthost, prefix).get("paths", []):
        # FRR's per-path schema exposes the learning peer as `peerId` (IPv4/IPv6)
        # in newer versions and as `peer` in older ones; check both.
        peer = path.get("peerId") or path.get("peer")
        if peer and peer in neighbor_set:
            return True
    return False


def all_prefixes_have_llgr_stale(duthost: Any, prefixes: list[str]) -> bool:
    return all(prefix_has_llgr_stale_community(duthost, p) for p in prefixes)


def no_prefixes_have_llgr_stale(duthost: Any, prefixes: list[str]) -> bool:
    return not any(prefix_has_llgr_stale_community(duthost, p) for p in prefixes)


def no_prefixes_in_rib_from_neighbor(
    duthost: Any, prefixes: list[str], neighbor_ips: list[str]
) -> bool:
    return not any(prefix_in_rib_from_neighbor(duthost, p, neighbor_ips) for p in prefixes)


def routes_from_neighbor(duthost: Any, neighbor_ip: str) -> dict[str, Any]:
    """Return all routes received from `neighbor_ip` keyed by prefix, across all ASICs."""
    afi = "ipv4" if is_ipv4_address(neighbor_ip) else "ipv6"
    cmd = "vtysh -c 'show bgp {} neighbor {} routes json'".format(afi, neighbor_ip)
    routes: dict[str, Any] = {}
    for stdout in _vtysh_per_asic(duthost, cmd):
        try:
            routes.update(json.loads(stdout).get("routes", {}))
        except json.JSONDecodeError as exc:
            logger.warning("routes_from_neighbor(%s): JSON parse failed: %s",
                           neighbor_ip, exc)
    return routes


def neighbor_bgp_ips(duthost: Any, neighbor_name: str) -> list[str]:
    """Return all BGP peer IPs (v4 and v6) on the DUT for the given neighbor name."""
    config_facts = duthost.config_facts(host=duthost.hostname, source="running")["ansible_facts"]
    bgp_neighbors = get_bgp_neighbors_from_config_facts(duthost, config_facts)
    return [ip for ip, info in bgp_neighbors.items() if info.get("name") == neighbor_name]


def dut_facing_peer_ips(node: dict[str, Any]) -> list[str]:
    """Return the DUT-facing peer IPs (as configured on the neighbor) from the topology.

    `node['conf']['bgp']['peers']` is a dict keyed by remote ASN. In single-DUT
    topologies it carries exactly one entry — the DUT's ASN. We assert that
    invariant explicitly rather than silently picking `next(iter(...))`, so a
    topology change (multi-DUT, transit-ASN convention drift) fails loudly here
    instead of silently configuring the wrong neighbor downstream.
    """
    bgp_peers = node["conf"]["bgp"]["peers"]
    pytest_assert(
        len(bgp_peers) == 1,
        "dut_facing_peer_ips: expected exactly one remote ASN in node bgp.peers, "
        "got {}".format(list(bgp_peers.keys()))
    )
    dut_asn = next(iter(bgp_peers.keys()))
    return list(bgp_peers[dut_asn])


def apply_neighbor_llgr_config(node: dict[str, Any], with_gr: bool) -> None:
    """Configure LLGR + graceful-restart on a neighbor.

    GR is always advertised because RFC 9494 sections 3.1 / 4.5 require it: a receiver
    MUST ignore LLGR if GR is not also present. `with_gr=False` therefore does *not*
    mean "no GR advertised" — it means GR advertised with restart-time=0 (the RFC
    section 4.1-prescribed way to express "skip the conventional GR retention phase, jump
    straight to LLGR"). FRR implements LLGR strictly as a continuation of the GR
    helper state machine; absent GR negotiation, none of the LLGR retention paths
    run (see bgpd/bgp_open.c and bgpd/bgp_fsm.c gating on PEER_CAP_RESTART_*).

    The neighbor is the LLGR *speaker* — what we're testing on the DUT is the
    BGP_GLOBALS/bgpcfgd path (see setup_llgr). On the neighbor we push directly via
    vtysh/eos_config, so the test is independent of the neighbor image version.
    Sessions are reset afterward so the updated OPEN is re-exchanged.
    """
    host = node["host"]
    asn = node["conf"]["bgp"]["asn"]
    gr_restart_time = GR_RESTART_TIME if with_gr else 0

    if isinstance(host, EosHost):
        # Global LLGR enable + stale-time on the speaker. This is the bit FRR/SONiC
        # actually needs to receive in the OPEN, and is supported across EOS images
        # that have any LLGR support at all.
        host.config(
            lines=['bgp long-lived-graceful-restart stale-time {}'.format(LLGR_STALE_TIME)],
            parents=['router bgp {}'.format(asn)],
            module_ignore_errors=True,
        )
        # Per-peer enable. The exact keyword varies across EOS versions — some
        # accept `... capable`, some only the bare form. Ignore failures so a
        # cEOS without per-peer LLGR support degrades to a clean negotiation
        # check rather than aborting setup; if the speaker can't advertise LLGR
        # at all, test_bgp_llgr_capability_negotiated will fail with a clear
        # "LLGR capability not found" assertion downstream.
        for peer_ip in dut_facing_peer_ips(node):
            host.config(
                lines=['neighbor {} long-lived-graceful-restart'.format(peer_ip)],
                parents=['router bgp {}'.format(asn)],
                module_ignore_errors=True,
            )
        host.config(
            lines=['graceful-restart restart-time {}'.format(gr_restart_time)],
            parents=['router bgp {}'.format(asn)],
        )
        for af in ("ipv4", "ipv6"):
            host.config(
                lines=['graceful-restart'],
                parents=['router bgp {}'.format(asn), 'address-family {}'.format(af)],
            )
        host.eos_command(commands=["clear bgp *"])
    elif isinstance(host, SonicHost):
        vtysh_lines = [
            'conf t',
            'router bgp {}'.format(asn),
            'bgp long-lived-graceful-restart stale-time {}'.format(LLGR_STALE_TIME),
            'bgp graceful-restart',
            'bgp graceful-restart restart-time {}'.format(gr_restart_time),
        ]
        cmd = 'sudo vtysh ' + ' '.join('-c "{}"'.format(line) for line in vtysh_lines)
        host.shell(cmd)
        host.shell("sudo vtysh -c 'clear bgp *'", module_ignore_errors=True)
    else:
        pytest.fail("Unsupported neighbor host type for LLGR config: {}".format(type(host).__name__))


def remove_neighbor_llgr_config(node: dict[str, Any]) -> None:
    """Inverse of apply_neighbor_llgr_config — strips all LLGR + GR config we may
    have added. Unconditional: apply always advertises GR (with restart-time=0 when
    the caller passed `with_gr=False`), so teardown is the same in either case."""
    host = node["host"]
    asn = node["conf"]["bgp"]["asn"]

    if isinstance(host, EosHost):
        for peer_ip in dut_facing_peer_ips(node):
            host.config(
                lines=['no neighbor {} long-lived-graceful-restart'.format(peer_ip)],
                parents=['router bgp {}'.format(asn)],
                module_ignore_errors=True,
            )
        # Mirror the global stale-time we set in apply.
        host.config(
            lines=['no bgp long-lived-graceful-restart stale-time'],
            parents=['router bgp {}'.format(asn)],
            module_ignore_errors=True,
        )
        for af in ("ipv4", "ipv6"):
            host.config(
                lines=['no graceful-restart'],
                parents=['router bgp {}'.format(asn), 'address-family {}'.format(af)],
                module_ignore_errors=True,
            )
        # apply() sets graceful-restart restart-time at router-bgp scope; remove it so
        # the timer doesn't linger in the EOS running-config for subsequent tests.
        host.config(
            lines=['no graceful-restart restart-time'],
            parents=['router bgp {}'.format(asn)],
            module_ignore_errors=True,
        )
        host.eos_command(commands=["clear bgp *"])
    elif isinstance(host, SonicHost):
        vtysh_lines = [
            'conf t',
            'router bgp {}'.format(asn),
            'no bgp long-lived-graceful-restart stale-time',
            'no bgp graceful-restart',
        ]
        cmd = 'sudo vtysh ' + ' '.join('-c "{}"'.format(line) for line in vtysh_lines)
        host.shell(cmd, module_ignore_errors=True)
        host.shell("sudo vtysh -c 'clear bgp *'", module_ignore_errors=True)
    else:
        # Asymmetric with apply_neighbor_llgr_config on purpose: apply runs in setup
        # where a hard fail is what we want (don't proceed against a half-configured
        # testbed). remove runs from finally blocks where a hard fail would mask the
        # original test failure, so best-effort warn-and-continue is correct.
        logger.warning("Unsupported neighbor host type for LLGR teardown: %s",
                       type(host).__name__)


def setup_llgr(
    duthost: Any,
    nbrhosts: dict[str, Any],
    with_gr: bool,
    configured_out: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Enable LLGR on the DUT, configure neighbors as LLGR speakers (SONiC and cEOS),
    and wait for sessions to re-establish. Returns the DUT's bgp_neighbors dict.

    `configured_out` is appended to incrementally as each neighbor is configured, so a
    failure partway through still leaves the caller with the list of neighbors that
    were applied and need to be torn down. The DUT-side HSET is also done early — the
    caller's teardown should unconditionally HDEL on the DUT.

    Any neighbor whose host type is neither SonicHost nor EosHost causes a hard
    failure inside apply_neighbor_llgr_config — extend the dispatch there to add new
    NOS types.
    """
    logger.info("Enabling LLGR on DUT (llgr_stale_time=%d)", LLGR_STALE_TIME)
    # bgpcfgd runs per namespace on multi-ASIC devices; write to every frontend
    # CONFIG_DB so each instance picks the value up. Single-ASIC collapses to one
    # un-namespaced sonic-db-cli invocation.
    _sonic_db_cli_per_asic(
        duthost,
        'CONFIG_DB HSET "BGP_GLOBALS|default" llgr_stale_time {}'.format(LLGR_STALE_TIME),
    )
    pytest_assert(
        wait_until(LLGR_CONFIG_PROPAGATION_TIMEOUT, 2, 0, is_llgr_configured, duthost),
        "bgpcfgd failed to push LLGR config to FRR on DUT"
    )

    for name, node in nbrhosts.items():
        logger.info("Configuring LLGR on neighbor %s (type=%s, with_gr=%s)",
                    name, type(node["host"]).__name__, with_gr)
        try:
            apply_neighbor_llgr_config(node, with_gr=with_gr)
        except RunAnsibleModuleFail as exc:
            # An EOS/cEOS image without LLGR support rejects `bgp long-lived-
            # graceful-restart ...` with "% Invalid input". Surface that as a
            # clean skip rather than a cryptic Ansible abort. Note: relying on
            # try/except here because EosHost.config() in eos.py drops the
            # module_ignore_errors kwarg on the floor (see eos.py:603) — fixing
            # that is out of scope for this test.
            if "long-lived-graceful-restart" in str(exc):
                pytest.skip(
                    "Neighbor {} ({}) does not support LLGR — this test requires "
                    "an LLGR-capable speaker (FRR/SONiC neighbor or EOS 4.27+). "
                    "Underlying error: {}"
                    .format(name, type(node["host"]).__name__, str(exc).splitlines()[0])
                )
            raise
        configured_out.append((name, node))

    config_facts = duthost.config_facts(host=duthost.hostname, source="running")["ansible_facts"]
    bgp_neighbors = get_bgp_neighbors_from_config_facts(duthost, config_facts)
    pytest_assert(
        wait_until(BGP_SESSION_RECOVERY_TIMEOUT, 10, 0,
                   duthost.check_bgp_session_state, list(bgp_neighbors.keys())),
        "BGP sessions did not re-establish after enabling LLGR"
    )
    return bgp_neighbors


def teardown_llgr(
    duthost: Any,
    bgp_neighbors: dict[str, Any],
    configured_neighbors: list[tuple[str, dict[str, Any]]],
) -> None:
    """Reverse of setup_llgr: strip LLGR from neighbors and the DUT, wait for sessions.

    Safe to call after a partial setup: only neighbors actually in `configured_neighbors`
    are reversed, and the DUT HDEL is idempotent (so it's a no-op if HSET never ran).
    Called from finally blocks, so individual failures use module_ignore_errors so we
    don't mask the original setup exception.
    """
    for name, node in configured_neighbors:
        logger.info("Removing LLGR config from neighbor %s", name)
        remove_neighbor_llgr_config(node)

    logger.info("Disabling LLGR on DUT")
    _sonic_db_cli_per_asic(
        duthost, 'CONFIG_DB HDEL "BGP_GLOBALS|default" llgr_stale_time'
    )

    if bgp_neighbors:
        # Log-and-continue instead of pytest_assert: this runs from finally blocks, so
        # a hard assert here can mask the real test failure with a teardown error.
        if not wait_until(BGP_SESSION_RECOVERY_TIMEOUT, 10, 0,
                          duthost.check_bgp_session_state, list(bgp_neighbors.keys())):
            logger.warning("BGP sessions did not re-establish after disabling LLGR; "
                           "leaving testbed in best-effort state")


@pytest.fixture(scope='module')
def setup_dut_llgr(duthosts, rand_one_dut_hostname):
    """Enable LLGR on the DUT only — no peer LLGR config.

    Used by tests that only need to verify the DUT-as-helper config path
    (bgpcfgd → FRR running-config + outgoing LLGR capability). RFC 9494
    retention behavior cannot be exercised through this fixture because
    that requires the peer to also advertise LLGR — see setup_llgr_only
    for that. This fixture, by not touching the peer, lets at least the
    config-propagation/advertisement check run on testbeds whose
    neighbors don't support LLGR (e.g. older cEOS).
    """
    duthost = duthosts[rand_one_dut_hostname]
    try:
        logger.info("Enabling LLGR on DUT (llgr_stale_time=%d)", LLGR_STALE_TIME)
        _sonic_db_cli_per_asic(
            duthost,
            'CONFIG_DB HSET "BGP_GLOBALS|default" llgr_stale_time {}'.format(LLGR_STALE_TIME),
        )
        pytest_assert(
            wait_until(LLGR_CONFIG_PROPAGATION_TIMEOUT, 2, 0, is_llgr_configured, duthost),
            "bgpcfgd failed to push LLGR config to FRR on DUT"
        )
        yield duthost
    finally:
        logger.info("Disabling LLGR on DUT")
        _sonic_db_cli_per_asic(duthost, 'CONFIG_DB HDEL "BGP_GLOBALS|default" llgr_stale_time')


@pytest.fixture(scope='module')
def setup_llgr_only(duthosts, rand_one_dut_hostname, nbrhosts):
    """DUT with LLGR enabled; neighbors advertise LLGR + GR (restart-time=0).

    Per RFC 9494 the GR capability is mandatory whenever LLGR is advertised, so the
    "LLGR only" framing is expressed by zeroing the GR restart-time rather than by
    omitting GR. See apply_neighbor_llgr_config for the rationale.

    Yields the list of (name, node) tuples for neighbors that were successfully configured.
    """
    duthost = duthosts[rand_one_dut_hostname]
    bgp_neighbors: dict[str, Any] = {}
    configured_neighbors: list[tuple[str, dict[str, Any]]] = []
    try:
        bgp_neighbors = setup_llgr(duthost, nbrhosts, with_gr=False,
                                   configured_out=configured_neighbors)
        yield configured_neighbors
    finally:
        teardown_llgr(duthost, bgp_neighbors, configured_neighbors)


@pytest.fixture(scope='module')
def setup_llgr_with_gr(duthosts, rand_one_dut_hostname, nbrhosts):
    """DUT with LLGR enabled; neighbors advertise both graceful-restart and LLGR.

    Yields the list of (name, node) tuples for neighbors that were successfully configured.
    """
    duthost = duthosts[rand_one_dut_hostname]
    bgp_neighbors: dict[str, Any] = {}
    configured_neighbors: list[tuple[str, dict[str, Any]]] = []
    try:
        bgp_neighbors = setup_llgr(duthost, nbrhosts, with_gr=True,
                                   configured_out=configured_neighbors)
        yield configured_neighbors
    finally:
        teardown_llgr(duthost, bgp_neighbors, configured_neighbors)


def test_bgp_llgr_config_propagated_to_frr(setup_dut_llgr):
    """Verify bgpcfgd translates BGP_GLOBALS|default.llgr_stale_time into FRR's
    running-config (the NOS-7061 handler under test).

    Scope is DUT-side only — no peer config required:

      * The fixture HSETs `llgr_stale_time` on every frontend CONFIG_DB and
        waits up to LLGR_CONFIG_PROPAGATION_TIMEOUT for FRR's running-config
        to gain the matching `bgp long-lived-graceful-restart stale-time N`
        line. The test body re-asserts that line explicitly so the failure
        message points at the running-config gap rather than the fixture.

    FRR's per-session `show bgp neighbor X` output cannot be used here: FRR
    only emits the "Long-lived Graceful Restart Capability" section once the
    cap is *negotiated* (both sides advertised), so a DUT-side-only check
    against that text always fails on testbeds whose peer doesn't support
    LLGR. That's an explicit non-goal for this test — testing the full
    advertise-and-negotiate path is the job of the retention tests, which
    require an LLGR-capable speaker and pytest.skip otherwise.

    This test consequently runs on any testbed where bgpcfgd is functional,
    regardless of neighbor capabilities, and is the right place to catch a
    regression in the BGP_GLOBALS handler itself.
    """
    duthost = setup_dut_llgr
    target = "long-lived-graceful-restart stale-time {}".format(LLGR_STALE_TIME)
    outputs = _vtysh_per_asic(duthost, "vtysh -c 'show running-config'")
    pytest_assert(
        bool(outputs) and all(target in cfg for cfg in outputs),
        "FRR running-config does not contain '{}' on every frontend ASIC — "
        "bgpcfgd failed to translate BGP_GLOBALS|default.llgr_stale_time"
        .format(target)
    )
    logger.info("bgpcfgd translated llgr_stale_time=%d to FRR running-config on all ASICs",
                LLGR_STALE_TIME)


def run_llgr_stale_test(
    duthost: Any,
    neighbor_name: str,
    neighbor_node: dict[str, Any],
    wait_for_gr_expiry: bool,
) -> None:
    """Drive the LLGR lifecycle: kill bgpd, verify llgr-stale community on retained
    routes, restart bgpd, verify routes are refreshed and the community is removed.

    With wait_for_gr_expiry=True we additionally confirm the DUT enters NSF state and
    delay polling until the GR window has elapsed — by then routes have transitioned
    from plain stale to llgr-stale.
    """
    neighbor_host = neighbor_node["host"]

    neighbor_ips = neighbor_bgp_ips(duthost, neighbor_name)
    pytest_assert(neighbor_ips, "No BGP neighbor IPs found for neighbor {}".format(neighbor_name))

    routes_before = {}
    for neighbor_ip in neighbor_ips:
        routes_before.update(routes_from_neighbor(duthost, neighbor_ip))
    pytest_assert(routes_before, "No routes received from {} before test".format(neighbor_name))

    prefixes_to_check = list(routes_before.keys())[:MAX_PREFIXES_TO_CHECK]
    logger.info("Neighbor %s advertises %d prefixes; checking the first %d",
                neighbor_name, len(routes_before), len(prefixes_to_check))

    try:
        logger.info("Killing bgpd on neighbor %s", neighbor_name)
        neighbor_host.kill_bgpd()

        if wait_for_gr_expiry:
            # GR negotiated: DUT enters NSF (passiveNSF). Routes start as plain stale;
            # they only flip to llgr-stale once the GR restart window closes
            for neighbor_ip in neighbor_ips:
                pytest_assert(
                    wait_until(NSF_ENTRY_TIMEOUT, 5, 0, duthost.check_bgp_session_nsf, neighbor_ip),
                    "DUT did not enter NSF state for neighbor {}".format(neighbor_ip)
                )
            community_timeout = GR_RESTART_TIME + LLGR_COMMUNITY_APPEAR_TIMEOUT
            community_initial_delay = GR_RESTART_TIME
        else:
            # GR with restart-time=0: GR retention window is empty, so FRR transitions
            # straight to LLGR — no NSF state to observe, community appears right away
            community_timeout = LLGR_COMMUNITY_APPEAR_TIMEOUT
            community_initial_delay = 0

        pytest_assert(
            wait_until(community_timeout, 5, community_initial_delay,
                       all_prefixes_have_llgr_stale, duthost, prefixes_to_check),
            "Routes from {} did not receive the llgr-stale community".format(neighbor_name)
        )

        # Verify the *dead* neighbor's paths are what LLGR is preserving, not that
        # the prefix happens to be installed because some other live neighbor still
        # advertises it. Without this filter the assertion would pass vacuously on
        # any multi-neighbor topology with overlapping prefix advertisements.
        for prefix in prefixes_to_check:
            pytest_assert(
                prefix_in_rib_from_neighbor(duthost, prefix, neighbor_ips),
                "Route {} from {} was withdrawn from RIB during LLGR window"
                .format(prefix, neighbor_name)
            )
        logger.info("All sampled routes from %s still present in RIB with llgr-stale community",
                    neighbor_name)
    finally:
        logger.info("Restarting bgpd on neighbor %s", neighbor_name)
        neighbor_host.start_bgpd()

    pytest_assert(
        wait_until(BGP_SESSION_RECOVERY_TIMEOUT, 10, 0,
                   duthost.check_bgp_session_state, neighbor_ips),
        "BGP sessions {} did not come back after neighbor restart".format(neighbor_ips)
    )

    pytest_assert(
        wait_until(LLGR_COMMUNITY_CLEAR_TIMEOUT, 5, 0,
                   no_prefixes_have_llgr_stale, duthost, prefixes_to_check),
        "llgr-stale community not removed after {} reconnected".format(neighbor_name)
    )
    logger.info("Routes from %s are clean after reconnect", neighbor_name)


def test_bgp_llgr_routes_preserved_without_gr(
        duthosts, rand_one_dut_hostname, setup_llgr_only):
    """LLGR with GR restart-time=0 (RFC 9494 section 4.1 — conventional GR phase skipped):
    when bgpd dies the DUT transitions retained routes to the llgr-stale community
    essentially immediately, with no plain-stale GR window."""
    configured_neighbors = setup_llgr_only
    if not configured_neighbors:
        pytest.skip("No neighbors configured for LLGR")
    neighbor_name, neighbor_node = configured_neighbors[0]
    run_llgr_stale_test(duthosts[rand_one_dut_hostname], neighbor_name, neighbor_node,
                        wait_for_gr_expiry=False)


def test_bgp_llgr_routes_preserved_with_gr(
        duthosts, rand_one_dut_hostname, setup_llgr_with_gr):
    """LLGR with graceful-restart: routes are first held as plain stale during the GR
    restart window, then tagged with llgr-stale once that window expires."""
    configured_neighbors = setup_llgr_with_gr
    if not configured_neighbors:
        pytest.skip("No neighbors configured for LLGR")
    neighbor_name, neighbor_node = configured_neighbors[0]
    run_llgr_stale_test(duthosts[rand_one_dut_hostname], neighbor_name, neighbor_node,
                        wait_for_gr_expiry=True)


def test_bgp_llgr_routes_withdrawn_after_stale_time(
        duthosts, rand_one_dut_hostname, setup_llgr_only):
    """RFC 9494 section 4.4: if the speaker stays down past `llgr_stale_time`, retained
    routes must be removed from the RIB.

    Setup mirrors test_bgp_llgr_routes_preserved_without_gr — neighbor advertises
    GR restart-time=0 so LLGR retention kicks in immediately on session loss. We
    keep bgpd dead through the full LLGR window and then assert the dead neighbor's
    paths are no longer installed. bgpd is restarted in `finally` so the next
    module / cleanup path sees a healthy session.

    Total runtime ~LLGR_STALE_TIME + recovery: dominated by the stale timer; not
    feasible to shorten without a separate per-test stale-time fixture.
    """
    configured_neighbors = setup_llgr_only
    if not configured_neighbors:
        pytest.skip("No neighbors configured for LLGR")
    duthost = duthosts[rand_one_dut_hostname]
    neighbor_name, neighbor_node = configured_neighbors[0]
    neighbor_host = neighbor_node["host"]

    neighbor_ips = neighbor_bgp_ips(duthost, neighbor_name)
    pytest_assert(neighbor_ips, "No BGP neighbor IPs found for neighbor {}".format(neighbor_name))

    routes_before: dict[str, Any] = {}
    for neighbor_ip in neighbor_ips:
        routes_before.update(routes_from_neighbor(duthost, neighbor_ip))
    pytest_assert(routes_before, "No routes received from {} before test".format(neighbor_name))

    prefixes_to_check = list(routes_before.keys())[:MAX_PREFIXES_TO_CHECK]
    logger.info("Neighbor %s advertises %d prefixes; sampling %d for stale-expiry check",
                neighbor_name, len(routes_before), len(prefixes_to_check))

    try:
        logger.info("Killing bgpd on neighbor %s and waiting %ds for LLGR window to expire",
                    neighbor_name, LLGR_STALE_TIME)
        neighbor_host.kill_bgpd()

        # Poll past the LLGR stale-time: FRR's stale-route walker runs at a coarse
        # interval, so we add LLGR_COMMUNITY_APPEAR_TIMEOUT as slack to absorb the
        # post-expiry sweep before asserting the paths are gone.
        withdraw_timeout = LLGR_STALE_TIME + LLGR_COMMUNITY_APPEAR_TIMEOUT
        pytest_assert(
            wait_until(withdraw_timeout, 10, LLGR_STALE_TIME,
                       no_prefixes_in_rib_from_neighbor, duthost, prefixes_to_check, neighbor_ips),
            "Routes from {} were not withdrawn after the {}s LLGR stale-time elapsed"
            .format(neighbor_name, LLGR_STALE_TIME)
        )
        logger.info("All sampled routes from %s removed from RIB after LLGR window expiry",
                    neighbor_name)
    finally:
        logger.info("Restarting bgpd on neighbor %s", neighbor_name)
        neighbor_host.start_bgpd()

    pytest_assert(
        wait_until(BGP_SESSION_RECOVERY_TIMEOUT, 10, 0,
                   duthost.check_bgp_session_state, neighbor_ips),
        "BGP sessions {} did not come back after neighbor restart".format(neighbor_ips)
    )
