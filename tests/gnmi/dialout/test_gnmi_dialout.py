"""gNMI dial-out tests.

SONiC telemetry dial-out: the DUT's dialout client watches the
TELEMETRY_CLIENT table in CONFIG_DB and pushes gNMI SubscribeResponse
messages over the gnmi.sonic.gNMIDialOut/Publish gRPC stream to a
configured collector. These tests run a collector on the PTF host and
verify the full path: config ingestion via redis keyspace events, the
periodic/stream/once report types, multi-destination failover,
unconfiguration, and dialing through the management VRF.
"""
import logging
import time
from urllib.parse import unquote

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.gnmi_dialout import assert_dialout_running
from tests.common.helpers.syslog_helpers import is_mgmt_vrf_enabled
from tests.common.reboot import SONIC_SSH_PORT, SONIC_SSH_REGEX
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer,
]

REPORT_INTERVAL_MS = 2000
RETRY_INTERVAL_S = 5
MGMT_VRF_ROW = "MGMT_VRF_CONFIG|vrf_global"


def configure_dialout(config, dst_addrs, report_type, paths="COUNTERS_PORT_NAME_MAP",
                      report_interval=REPORT_INTERVAL_MS, vrf=None):
    """Write a full Global + DestinationGroup + Subscription config set.

    report_interval is always set explicitly: the client's fallback for an
    omitted field is currently a microseconds-scale busy loop, not the 5s
    its code comment intends.
    """
    global_fields = {"retry_interval": RETRY_INTERVAL_S}
    if vrf is not None:
        global_fields["vrf"] = vrf
    config.hset("Global", **global_fields)
    config.hset("DestinationGroup_NHTEST", dst_addr=",".join(dst_addrs))
    fields = {
        "dst_group": "NHTEST",
        "path_target": "COUNTERS_DB",
        "paths": paths,
        "report_type": report_type,
        "report_interval": report_interval,
    }
    config.hset("Subscription_NHTEST", **fields)


def wait_for_updates(collector, minimum, timeout=90):
    return wait_until(timeout, 3, 0, lambda: collector.count(kind="update") >= minimum)


def config_db_row_exists(duthost, row):
    return duthost.shell("sonic-db-cli CONFIG_DB EXISTS '%s'" % row)["stdout"].strip() == "1"


def tacacs_blocks_mgmt_vrf(duthost):
    """Logins go through TACACS+ servers that are not bound to the mgmt VRF.

    Once eth0 moves into that VRF such servers are unreachable, so every
    later SSH session, the teardown's included, would fail to authenticate.
    """
    facts = duthost.config_facts(host=duthost.hostname, source="running")["ansible_facts"]
    login = facts.get("AAA", {}).get("authentication", {}).get("login", "")
    if "tacacs+" not in login:
        return False
    return any(server.get("vrf") != "mgmt" for server in facts.get("TACPLUS_SERVER", {}).values())


def set_mgmt_vrf(duthost, localhost, enable):
    """Enable or disable the management VRF and wait for the DUT to be back.

    hostcfgd acts on the row by restarting interfaces-config, which moves
    eth0 in or out of the mgmt VRF and drops every SSH session, so the
    command is issued asynchronously and SSH is waited for to go away and
    come back before the state is read, as tests/syslog does.
    """
    duthost.shell("sudo config vrf %s mgmt" % ("add" if enable else "del"), module_async=True)
    localhost.wait_for(host=duthost.mgmt_ip, port=SONIC_SSH_PORT, search_regex=SONIC_SSH_REGEX,
                       state="absent", delay=1, connect_timeout=1, timeout=30,
                       module_ignore_errors=True)
    localhost.wait_for(host=duthost.mgmt_ip, port=SONIC_SSH_PORT, search_regex=SONIC_SSH_REGEX,
                       state="started", delay=2, timeout=180)
    pytest_assert(wait_until(60, 5, 0, lambda: is_mgmt_vrf_enabled(duthost) == enable),
                  "management VRF did not become %s" % ("enabled" if enable else "disabled"))


@pytest.fixture(scope="function")
def mgmt_vrf(request, dialout_setup, localhost):
    """Management VRF enabled for the duration of one test, then put back.

    A DUT that already runs with it is left as it is. Otherwise the VRF is
    enabled here and the finalizer, registered before anything changes,
    disables it again and drops the MGMT_VRF_CONFIG row `config vrf del`
    leaves behind when the DUT had none. Either way the finalizer checks the
    VRF state and the row are as they were found.
    """
    duthost, container = dialout_setup["duthost"], dialout_setup["container"]
    was_enabled = is_mgmt_vrf_enabled(duthost)
    had_row = config_db_row_exists(duthost, MGMT_VRF_ROW)
    if not was_enabled and tacacs_blocks_mgmt_vrf(duthost):
        pytest.skip("TACACS+ login servers are not bound to the mgmt VRF; enabling it would "
                    "lock the test out of the DUT")

    def restore():
        if not was_enabled:
            try:
                set_mgmt_vrf(duthost, localhost, False)
            finally:
                if not had_row:
                    duthost.shell("sonic-db-cli CONFIG_DB DEL '%s'" % MGMT_VRF_ROW)
        pytest_assert(is_mgmt_vrf_enabled(duthost) == was_enabled,
                      "management VRF not back to %s" % ("enabled" if was_enabled else "disabled"))
        pytest_assert(config_db_row_exists(duthost, MGMT_VRF_ROW) == had_row,
                      "%s not back to %s" % (MGMT_VRF_ROW, "present" if had_row else "absent"))

    request.addfinalizer(restore)
    if not was_enabled:
        set_mgmt_vrf(duthost, localhost, True)
    assert_dialout_running(duthost, container)
    yield


def test_dialout_process_running(dialout_setup):
    """The dialout client program comes up by default in the gnmi/telemetry container.

    Asserts on the supervisorctl status captured before dialout_setup's own
    restart, so what is verified is the state the image booted into — i.e.
    the dependent_startup wiring (the program is autostart=false and started
    only via dependent_startup_wait_for), not this suite's restart.
    """
    initial = dialout_setup["initial_dialout_status"]
    pytest_assert(initial is not None and "RUNNING" in initial,
                  "dialout was not RUNNING in %s before this suite restarted it "
                  "(initial status: %s)" % (dialout_setup["container"], initial))


def test_dialout_periodic(dialout_setup, telemetry_client_config, start_collector):
    """Periodic report: collector receives COUNTERS_DB snapshots, not a busy loop."""
    collector = start_collector()
    configure_dialout(telemetry_client_config, [collector.address], "periodic")

    pytest_assert(wait_for_updates(collector, 3),
                  "collector received %d update messages, want >= 3" % collector.count(kind="update"))

    updates = [r for r in collector.records() if r.get("kind") == "update"]
    pytest_assert(updates, "no update records readable from the collector on re-read "
                           "(records: %d)" % collector.count())
    for record in updates:
        pytest_assert(record.get("target") == "COUNTERS_DB",
                      "unexpected notification target: %s" % record)
        pytest_assert(record.get("update_count", 0) >= 1,
                      "notification carries no updates: %s" % record)

    # Busy-loop guard (lower bound only): within a single publish stream
    # (peer), consecutive messages must not arrive much faster than
    # report_interval. Group by peer so a reconnect (new stream) can not
    # produce a misleading small gap, and assert on the median gap: the
    # timestamps are receive-side, so a stalled read can stamp one message
    # late and the next on time, shortening a single gap on a healthy
    # client — while the busy-loop bug compresses every gap to microseconds.
    by_peer = {}
    for r in updates:
        by_peer.setdefault(r.get("peer"), []).append(r["ts"])
    peer, timestamps = max(by_peer.items(), key=lambda kv: len(kv[1]))
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    pytest_assert(len(gaps) >= 1,
                  "not enough same-stream samples to measure cadence "
                  "(peers: %s)" % {p: len(t) for p, t in by_peer.items()})
    median_gap = sorted(gaps)[len(gaps) // 2]
    pytest_assert(median_gap > REPORT_INTERVAL_MS / 1000.0 * 0.5,
                  "updates from %s arrived too fast (median gap %.3fs, gaps %s); "
                  "busy loop?" % (peer, median_gap, gaps))


def test_dialout_stream(dialout_setup, telemetry_client_config, start_collector):
    """Stream report: update notifications and a sync_response arrive at the collector."""
    collector = start_collector()
    configure_dialout(telemetry_client_config, [collector.address], "stream",
                      paths="COUNTERS/Ethernet*")

    pytest_assert(wait_until(90, 3, 0, lambda: collector.count(kind="sync") >= 1),
                  "no sync_response received; records: %d" % collector.count())
    pytest_assert(collector.count(kind="update") >= 1,
                  "no update notifications received")


@pytest.mark.xfail(strict=True,
                   reason="report_type 'once' is accepted by TELEMETRY_CLIENT config but "
                          "unimplemented in the dialout client: publishRun has no Once arm, "
                          "so the subscription is silently ignored (tracked internally)")
def test_dialout_once(dialout_setup, telemetry_client_config, start_collector):
    """Once report: a single snapshot should arrive after configuration."""
    collector = start_collector()
    configure_dialout(telemetry_client_config, [collector.address], "once")
    pytest_assert(wait_for_updates(collector, 1, timeout=60),
                  "no snapshot received for report_type=once")


def test_dialout_failover(dialout_setup, telemetry_client_config, start_collector):
    """With two destinations, killing the active collector moves the stream to the other."""
    first = start_collector()
    second = start_collector()
    configure_dialout(telemetry_client_config, [first.address, second.address], "periodic")

    # The client connects to one destination (round-robin from the first).
    pytest_assert(wait_until(90, 3, 0,
                             lambda: first.count(kind="update") + second.count(kind="update") >= 2),
                  "no updates on either collector")

    def last_update_ts(collector):
        stamps = [r["ts"] for r in collector.records() if r.get("kind") == "update"]
        return max(stamps) if stamps else 0.0

    # The live stream is wherever the most recent update landed — a
    # reconnect during the initial wait can leave old updates on both.
    active, standby = ((first, second) if last_update_ts(first) >= last_update_ts(second)
                       else (second, first))

    # Confirm the standby really is idle before the kill, so the rise
    # asserted below can only mean failover (not a mislabelled active).
    standby_before = standby.count(kind="update")
    time.sleep(1.5 * REPORT_INTERVAL_MS / 1000.0)
    pytest_assert(standby.count(kind="update") == standby_before,
                  "standby collector received updates before failover — "
                  "active/standby misidentified")

    active.stop()
    pytest_assert(wait_until(120, 5, 0,
                             lambda: standby.count(kind="update") > standby_before),
                  "no updates on standby collector after killing the active one")


def test_dialout_stream_failover_idle(dialout_setup, telemetry_client_config, start_collector):
    """An idle stream moves to the other collector when the active one is killed.

    COUNTERS_PORT_NAME_MAP does not change during the test, so after the
    initial dump nothing is sent on the stream. The client has to notice the
    closed connection without a failed send to trigger the reconnect.
    """
    first = start_collector()
    second = start_collector()
    configure_dialout(telemetry_client_config, [first.address, second.address], "stream")

    pytest_assert(wait_until(90, 3, 0,
                             lambda: first.count(kind="sync") + second.count(kind="sync") >= 1),
                  "no sync_response on either collector")

    def last_sync_ts(collector):
        stamps = [r["ts"] for r in collector.records() if r.get("kind") == "sync"]
        return max(stamps) if stamps else 0.0

    active, standby = ((first, second) if last_sync_ts(first) >= last_sync_ts(second)
                       else (second, first))

    # A send on a live stream would also expose the closed connection, so the
    # stream must be idle for the test to cover detection without a send.
    active_before = active.count()
    time.sleep(RETRY_INTERVAL_S)
    pytest_assert(active.count() == active_before,
                  "stream is not idle: active collector got %d new messages"
                  % (active.count() - active_before))

    standby_before = standby.count(kind="sync")
    active.stop()
    pytest_assert(wait_until(6 * RETRY_INTERVAL_S, 3, 0,
                             lambda: standby.count(kind="sync") > standby_before),
                  "idle stream did not move to the standby collector within %ds "
                  "of killing the active one" % (6 * RETRY_INTERVAL_S))


def test_dialout_unconfigure_stops_publishing(dialout_setup, telemetry_client_config,
                                              start_collector):
    """Deleting the Subscription row stops the publish stream."""
    collector = start_collector()
    configure_dialout(telemetry_client_config, [collector.address], "periodic")
    pytest_assert(wait_for_updates(collector, 2), "dial-out stream never started")

    telemetry_client_config.delete("Subscription_NHTEST")

    # Wait for the stream to quiesce first: the deletion propagates via
    # keyspace events and a message already in flight may still land, so a
    # fixed grace period would make the equality check below flaky.
    def quiesced():
        n = collector.count(kind="update")
        time.sleep(REPORT_INTERVAL_MS / 1000.0)
        return collector.count(kind="update") == n

    pytest_assert(wait_until(30, 1, 0, quiesced),
                  "updates never stopped arriving after Subscription was deleted")

    before = collector.count(kind="update")
    time.sleep(3 * REPORT_INTERVAL_MS / 1000.0)
    after = collector.count(kind="update")
    pytest_assert(after == before,
                  "updates kept arriving after Subscription was deleted (%d -> %d)"
                  % (before, after))


def peer_host(host):
    """host as gRPC peer strings and `ss` print it before the port: bare for IPv4, bracketed for IPv6."""
    return "[%s]" % host if ":" in host else host


def client_sockets(duthost, address):
    """TCP sockets the DUT holds towards the collector address, as `ss`
    prints them (state, counters, local and peer address)."""
    res = duthost.shell("ss -Htn state syn-sent state established 'dst %s'" % address,
                        module_ignore_errors=True)
    return [line for line in res["stdout_lines"] if line.strip()]


def test_dialout_mgmt_vrf(dialout_setup, telemetry_client_config, start_collector, mgmt_vrf):
    """Global vrf=mgmt makes the client dial collectors through the management VRF.

    The collector runs on the PTF and is reached over the DUT's management
    network. With the management VRF enabled, eth0 belongs to that VRF, so
    a client that dials in the default VRF cannot source from eth0: either
    it has no route to the collector at all (the production case), or a
    front-panel route leads there and the stream, if it comes up, carries
    a front-panel address. Setting vrf=mgmt on the Global row binds the
    dial-out sockets to the mgmt VRF, and the collector then sees the
    stream come from eth0.
    """
    duthost = dialout_setup["duthost"]
    collector = start_collector()
    mgmt_ip = str(duthost.mgmt_ip)

    def updates_from(host):
        # gRPC percent-encodes the brackets of an IPv6 peer (ipv6:%5B::1%5D:<port>).
        needle = ":%s:" % peer_host(host)
        return [r for r in collector.records()
                if r.get("kind") == "update" and needle in unquote(r.get("peer") or "")]

    def peers():
        return sorted({r.get("peer") for r in collector.records() if r.get("kind") == "update"})

    mgmt_route = duthost.shell("ip route get %s vrf mgmt" % collector.host, module_ignore_errors=True)
    pytest_assert(mgmt_route["rc"] == 0,
                  "collector %s is not reachable in the mgmt VRF: %s"
                  % (collector.host, mgmt_route["stderr"]))
    default_route = duthost.shell("ip route get %s" % collector.host, module_ignore_errors=True)
    logger.info("default VRF route to collector %s: rc=%d %s", collector.host,
                default_route["rc"], default_route["stdout"] or default_route["stderr"])

    # Without the vrf field the client dials in the default VRF. Whether
    # that reaches the collector depends on the testbed's front-panel
    # routes, but it can never source from the management address. The
    # client logs its attempts, but supervisord does not deliver its output
    # to syslog on these images, so the attempts are read from the kernel
    # instead: with a route in the default VRF they show up as sockets
    # towards the collector, with their source address.
    configure_dialout(telemetry_client_config, [collector.address], "periodic")
    if default_route["rc"] == 0:
        pytest_assert(wait_until(6 * RETRY_INTERVAL_S, 1, 0,
                                 lambda: client_sockets(duthost, collector.address)),
                      "the dial-out client made no connection attempt towards %s" % collector.address)
        sockets = client_sockets(duthost, collector.address)
        logger.info("without vrf: sockets towards the collector: %s", sockets)
        pytest_assert(not any(" %s:" % peer_host(mgmt_ip) in line for line in sockets),
                      "a client dialing in the default VRF connected from the management address %s: %s"
                      % (mgmt_ip, sockets))
    time.sleep(2 * RETRY_INTERVAL_S)
    assert_dialout_running(duthost, dialout_setup["container"])
    logger.info("without vrf: %d updates, peers %s", collector.count(kind="update"), peers())
    pytest_assert(not updates_from(mgmt_ip),
                  "a client dialing in the default VRF published from the management "
                  "address %s" % mgmt_ip)

    configure_dialout(telemetry_client_config, [collector.address], "periodic", vrf="mgmt")
    pytest_assert(wait_until(90, 3, 0, lambda: len(updates_from(mgmt_ip)) >= 3),
                  "collector received %d update messages from the management address %s "
                  "after setting vrf=mgmt, want >= 3 (peers seen: %s)"
                  % (len(updates_from(mgmt_ip)), mgmt_ip, peers()))
