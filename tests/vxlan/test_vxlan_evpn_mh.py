"""EVPN multihoming (all-active ESI-LAG) control plane + SAI: the DUT and a remote FRR leaf share an Ethernet segment.

The DUT is one of the two leaves of a dual-homed host, and a remote VTEP for a second, remote Ethernet segment. Every
case asserts per-port (or per-leg) frame counts and ASIC_DB state; FRR and APPL_DB are read only as gates. The FRR
leaves are not under test: what they forward (l1's own DF filter and local bias toward the host's other leg) is not
read, and every DUT decision shows in what the DUT itself sends.

Rig (module fixture), topology t0 or single-ASIC t2 or lt2, on the first VLAN with four oper-up members (e.g.
Vlan1000; on a t2, neighbor-facing ports that vxlan_l2_helpers.access_ports borrows and restores). It is
test_vxlan_evpn_l2.py's rig (vxlan_evpn_l2_helpers.RigBuild and the leaf script) with Ethernet segments added:
  * ports[0] = H0: a single-homed host, untagged in VLAN A.
  * ports[1]: the only member of the DUT's ES PortChannel (untagged in VLAN A). Its PTF port is joined to the
    dual-homed host on the test server (bond0, 802.3ad, legs hdut through the DUT and hl1 through l1), so the PTF
    still sees every frame the DUT sends to the host and can send as the host toward the DUT.
  * ports[2]/[3]: routed /31s to the remote leaves l1 and l2 (FRR containers, eBGP ipv4 + l2vpn evpn, VNI A).
  * ES1 = the DUT's PortChannel + l1's es1bond (Type-3 ESI from the PortChannel's system MAC and number, the
    upstream `config interface evpn-esi add <pc> auto-system-mac` form). The DUT is DF (preference 50000 vs 10000).
  * ES2 = l1's and l2's es2bond, with a second dual-homed host behind them: the DUT is remote to it.
  * DUT: vtep1 / nvo1, VLAN A <-> VNI A, BGP to the leaves via vtysh, `evpn mh startup-delay 0` via vtysh.

Cases:
  test_mh_df_bum_from_remote_vtep     BUM from a remote VTEP (broadcast and unknown unicast) reaches the ES only
                                      through the DF: DUT DF -> one copy on its ES member; DUT non-DF (the peer's
                                      preference raised) -> none, while local BUM still floods to the ES; ASIC VLAN
                                      member TUNNEL_TERM_BUM_TX_DROP follows
  test_mh_local_bias_from_es_peer     BUM (broadcast and unknown unicast) the ES peer sends as the ES host's is not
                                      sent back onto the ES; the same BUM from a VTEP that is not on the ES is; ASIC
                                      isolation group on the peer's tunnel bridge port holds the ES bridge port
  test_mh_aliasing_to_remote_es       known unicast to a host behind ES2 spreads over both ES2 leaves, each frame once,
                                      each flow on one leaf; ASIC FDB -> bridge-port next hop group {l1, l2}
  test_mh_es_leg_down_switchover      the ES member goes down: traffic to the dual-homed host moves to the backup
                                      group (VxLAN to l1), loss bounded, no flood, no duplicates; back up: 0 lost;
                                      ASIC SET_SWITCHOVER and the FDB entry kept on the ES bridge port
  test_mh_es_detach_while_down        with the member down and switched over, zebra's backup group entry is deleted:
                                      no loss, no flood, ASIC protection and switchover kept (held) until the leg is up
  test_mh_vni_mapped_after_es_port_joined
                                      a VLAN given its VNI after the ES port joined it has no DF entry from zebra: the
                                      DUT (non-DF by election) still forwards remote BUM to the ES, the duplicate both
                                      leaves send until zebra writes the VLAN's DF role; then it follows the role
  test_mh_remote_es_group_after_fdbsyncd_restart
                                      after an fdbsyncd restart, a leaf leaving the remote ES2 leaves the next hop
                                      group of the MACs behind ES2 (APPL_DB and ASIC)

What the image must carry (the module skips, naming what is missing, when the EVPN-MH tables or the CLI are absent):
  * FRR with EVPN-MH and its SONiC dataplane messages (sonic-net/sonic-buildimage#27544, #27683);
  * sonic-net/sonic-swss#4608 and #4615 (orchagent/fdbsyncd/fpmsyncd EVPN-MH tables), with sonic-net/sonic-swss#4841
    (or a SAI that accepts SAI_BRIDGE_PORT_ATTR_EGRESS_FILTERING at create) and the DF polarity fix
    (TUNNEL_TERM_BUM_TX_DROP = non-DF);
  * sonic-net/sonic-utilities#4247 (`config interface evpn-esi`, `config interface evpn-df-pref`);
  * a SAI with DF (VLAN member TUNNEL_TERM_BUM_TX_DROP), local bias (isolation group on a tunnel bridge port) and
    bridge-port next hop groups;
  * the two ES-down cases also need orchagent's EVPN_ES_BACKUP_NHG_TABLE consumer (bridge-port protection group and
    switchover; skipped without it), and pass only with the switchover at the last member's distribution disable
    (the loss bound) and the held backup-group delete (the detach case).
Not detected (the cases fail rather than skip): zebra's SONiC dataplane without the MH messages (APPL_DB
EVPN_DF_TABLE stays empty), an orchagent without the DF polarity fix (upstream sends
TUNNEL_TERM_BUM_TX_DROP = the DF role), one that drops tunnel BUM on an ES port that has no DF entry for the VLAN,
one without sonic-swss#4841 where the SAI rejects EGRESS_FILTERING, a SAI
without the MH objects (DF, isolation groups, bridge-port next hop groups, protection and switchover; the failure
quotes orchagent's capability notice when it logged one).
Not covered: an ES PortChannel with more than one member (the rig's has one, so the switchover cases cannot tell a
switchover at the last member's distribution disable from one at the first member's), EVPN-MH state across a warm
restart or warm reboot, and, on broadcom-dnx, the switchover's timing: its loss bound there (measured 146-147 frames
per event) only catches a switchover that does not happen.
Optional: zebra --kernel-mac-ext-learn for DEVICE_METADATA subtype EvpnMH (sonic-net/sonic-buildimage#29312): when the
bgp template carries it and zebra runs without it, the fixture sets the subtype and restarts bgp once (the teardown puts
the subtype back; zebra keeps the flag until tests/vxlan/conftest.py's config reload after the module restarts bgp).
frrcfgd's EVPN_ETHERNET_SEGMENT handler (sonic-net/sonic-buildimage#27885) is not needed (the CLI pushes the ES to FRR
itself).
"""

import collections
import logging
import re
import statistics
import time
from types import SimpleNamespace

import ptf.packet as packet
import ptf.testutils as testutils
import pytest
from scapy.packet import Raw

from tests.common.utilities import get_inventory_files, wait_until
from tests.vxlan.vxlan_l2_helpers import (
    BROADCAST_MAC,
    GOOD,
    KNOWN_HARMLESS_LOG,
    L2NHG_DEL_NOISE_LOG,
    N,
    Steps,
    VTEP_NAME,
    access_ports,
    acl_rebind,
    acl_unbind,
    asic_counts,
    asic_objects_naming,
    capture,
    collect,
    db_hget,
    db_keys,
    delivered,
    established_bgp_peers,
    flush_vlan_fdb,
    kernel_path,
    kernel_rx_packets,
    kernel_vxlan_netdev,
    learn_mac,
    log_clean,
    make_tagger,
    member_delivered,
    member_flood,
    native_on,
    pid_of,
    probe_frame,
    probe_tagged_path,
    raw_payloads,
    send_and_count,
    sh,
    swss_apply,
    summarize,
    vxlan_frame,
    vxlan_to,
    wait_for,
    wait_state,
)
from tests.vxlan.vxlan_evpn_l2_helpers import (
    BASELINE_TYPES,
    EthernetSegment,
    Leaf,
    Leaves,
    RigBuild,
    appl_remote_vnis,
    assert_clean_start,
    check_ingress_once,
    check_leaf_vteps_unrouted,
    dut_has_advertise_all_vni,
    pick_rig_ports,
    resolve_leaf_image,
    rig_preflight,
    type3_esi,
    vtysh,
)
from tests.vxlan.vxlan_evpn_mh_helpers import (
    NULL_OID,
    MhAsicView,
    appl_df,
    appl_es_backup,
    appl_l2nhg_vteps,
    appl_remote_mac_nhg,
    appl_shl_vteps,
    es_backup_consumer,
    es_portchannel_add,
    es_vteps,
    frr_es,
    lag_up,
    mh_support_missing,
    orch_capability_notice,
    zebra_mac_ext_learn,
)

pytestmark = [
    pytest.mark.topology("t0", "t2", "lt2"),
]

logger = logging.getLogger(__name__)

VLAN_A, VNI_A = 2100, 12100
VLAN_A_NAME = "Vlan{}".format(VLAN_A)
RT_ASN = 65000
# name, ASN, VTEP, leaf /31 address, DUT /31 address, (VLAN, VNI) served
LEAF_PLAN = [
    ("l1", 65211, "10.255.93.1", "10.99.0.1", "10.99.0.0", [(VLAN_A, VNI_A)]),
    ("l2", 65213, "10.255.94.1", "10.99.0.3", "10.99.0.2", [(VLAN_A, VNI_A)]),
]
# the ES PortChannel; `config interface evpn-esi ... auto-system-mac` takes its number as the ES id
ES_PC, ES_ID = "PortChannel22", 22
# Logged by the MH rig itself:
# - teammgrd sets the ES PortChannel's system MAC ("err 0" is success; the common "kernel:.*\serr" rule matches it);
# - the ES teardown: fdbsyncd's kernel delete of the host MAC that already moved to the backup group, and L2NhgOrch's
#   DEL of the member next hop of a group it has already removed.
MH_HARMLESS_LOG = KNOWN_HARMLESS_LOG + L2NHG_DEL_NOISE_LOG + [
    r".*teammgrd: :- update_kernel: ifindex \d+, mac [0-9a-f:]+, err 0\b.*",
    (r".*fdbsyncd: :- updateLocalMac: Failed cmd: bridge fdb del [0-9a-f:]+ dev PortChannel\d+ master dynamic "
     r"extern_learn vlan \d+(?: proto \w+)?, res=, ret=255.*"),
]

ES1_SYS_MAC, ES2_SYS_MAC, ES2_ID = "02:4c:45:e5:00:01", "02:4c:45:e5:00:02", 2
DUT_DF_PREF, L1_DF_PREF, L1_DF_PREF_WINS = 50000, 10000, 60000   # FRR: the higher preference is DF
ES2_DF_PREFS = {"l1": 20000, "l2": 10000}
MH_HOST_MAC = "02:4c:45:e5:10:01"     # the host on ES1 (DUT + l1)
RHOST_MAC = "02:4c:45:e5:20:01"       # the host on ES2 (l1 + l2), remote to the DUT
MAC_L2_HOST = "02:4c:45:a2:00:09"     # a host behind l2 (inner source of BUM injected as l2)
MAC_UNKNOWN = "02:4c:45:ff:00:01"     # never learned or advertised anywhere
# BUM injected from a remote VTEP: a broadcast and an unknown unicast, which a DUT may filter by separate rules (group
# destination vs lookup miss). A multicast destination is a group address like the broadcast.
REMOTE_BUM = (("bc", "broadcast", BROADCAST_MAC), ("uu", "unknown unicast", MAC_UNKNOWN))
STREAM_SRC_MAC = "02:4c:45:00:00:71"  # H0's source in the ES streams
ALIAS_SRC_MAC = "02:4c:45:00:00:72"   # H0's source in the aliasing flows
INJ_OUTER_SMAC = "02:4c:45:ee:00:01"  # outer source MAC of VxLAN the PTF injects

CONVERGE = 120             # seconds for BGP / EVPN / LACP state to settle

ALIAS_FLOWS = 128          # UDP flows to the ES2 host, each sent twice
ALIAS_MIN_SHARE = ALIAS_FLOWS // 8   # each ES2 leaf carries at least this many flows

# Streams to the dual-homed host: H0 -> DUT -> ES member (or, switched over, VxLAN to l1) -> host. 1 frame = 0.5 ms.
# The event (leg down/up, delete) is issued EVENT_AT_S after the stream started; it lands 3-6 s in (ansible round
# trips), so a stream runs 10 s, and 12 s across a leg coming up (LACP, then orchagent's 1 s switchback hold).
PPS, STREAM_FLOWS = 2000, 64
STREAM_FRAMES, UP_STREAM_FRAMES = 20000, 40000
EVENT_AT_S = 2.0
AFTER_UP_S = 8             # an up stream must run this long after the event (link, LACP, 1 s hold, then direct)
SWITCHOVER_RUNS = 3        # leg down/up cycles; the bound applies to the median, the cap to every run
# Frames lost at the ES member's shutdown at 2000 pps, (median bound, cap for any one run). With the switchover at the
# last member's distribution disable (here the only member), 6-9 per run (one run 30) were measured, and 34-51 without
# it. broadcom-dnx (keyed by platform_asic) loses 146-147 per run (one run 659), so there the bound only catches a
# switchover that does not happen (several thousand frames, the rest of the stream), not its timing.
SWITCHOVER_LOST = (16, 50)
SWITCHOVER_LOST_BY_PLATFORM_ASIC = {"broadcom-dnx": (200, 1000)}
UP_MAX_LOST = 10           # leg back up: 0 measured
DETACH_MAX_LOST = 10       # backup group deleted while switched over: 0 measured

# the dual-homed host's bond legs: through the DUT, through l1
LEG_DUT, LEG_L1 = "hdut", "hl1"

tag = make_tagger("VXMH")


# --------------------------------------------------------------------------------------------------------------------
# rig
# --------------------------------------------------------------------------------------------------------------------
def mh_baseline(duthost):
    counts = asic_counts(duthost, BASELINE_TYPES + ("LAG", "LAG_MEMBER", "ISOLATION_GROUP", "ISOLATION_GROUP_MEMBER"))
    counts["NEXT_HOP_GROUP(BRIDGE_PORT)"] = asic_objects_naming(duthost, "NEXT_HOP_GROUP",
                                                                "SAI_NEXT_HOP_GROUP_TYPE_BRIDGE_PORT")
    return counts


def enable_mac_ext_learn(duthost):
    """DEVICE_METADATA subtype EvpnMH (zebra --kernel-mac-ext-learn) where the image renders it and zebra runs without
    it; one bgp restart. Returns the previous subtype to restore, or None when nothing was changed."""
    running, template = zebra_mac_ext_learn(duthost)
    prev = db_hget(duthost, "CONFIG_DB", "DEVICE_METADATA|localhost", "subtype")
    if running or not template or prev not in ("", "EvpnMH"):
        logger.info("zebra --kernel-mac-ext-learn: running %s, template %s, subtype '%s': left as is",
                    running, template, prev)
        return None
    bgp = established_bgp_peers(duthost)
    logger.info("DEVICE_METADATA subtype EvpnMH and a bgp restart, for zebra --kernel-mac-ext-learn")
    sh(duthost, "sudo sonic-db-cli CONFIG_DB HSET 'DEVICE_METADATA|localhost' subtype EvpnMH")
    sh(duthost, "sudo systemctl reset-failed bgp; sudo systemctl restart bgp")
    wait_for(300, 10, lambda: zebra_mac_ext_learn(duthost)[0], "zebra back with --kernel-mac-ext-learn")
    wait_for(600, 10, lambda: duthost.check_bgp_session_state(bgp), "BGP back after the bgp restart")
    return prev


def restore_subtype(duthost, prev):
    if prev:
        sh(duthost, "sudo sonic-db-cli CONFIG_DB HSET 'DEVICE_METADATA|localhost' subtype {}".format(prev))
    else:
        sh(duthost, "sudo sonic-db-cli CONFIG_DB HDEL 'DEVICE_METADATA|localhost' subtype")


def bond_ready(legs, want_legs, partner):
    """The host bond has exactly want_legs, all in one aggregator, collecting and distributing, partnered by the
    ES system MAC (the DUT PortChannel and the leaf bonds present it)."""
    if set(legs) != set(want_legs):
        return False
    aggs = {legs[g]["agg"] for g in want_legs}
    return len(aggs) == 1 and "" not in aggs and all(
        legs[g]["state"] & 0x30 == 0x30 and legs[g]["partner"] == partner for g in want_legs)


def mh_now(r):
    """The MH state the gates read, for failure messages."""
    d = r.duthost
    es = frr_es(d)
    a = MhAsicView(d)
    bp, _ = a.lag_bridge_port(r.pc)
    _vm, vma = a.vlan_member(VLAN_A, bp)
    return {"lag": lag_up(d, r.pc, r.es_port), "es1_host": r.leaves.host_bond("es1"),
            "es2_host": r.leaves.host_bond("es2"), "frr_es1": es.get(r.esi1), "frr_es2": es.get(r.esi2),
            "df": appl_df(d, VLAN_A, r.pc), "shl": sorted(appl_shl_vteps(d, r.pc)), "bridge_port": bp,
            "vlan_member": vma, "l1_es1": r.leaves.es_detail(r.l1.name).get(r.esi1, {}).get("flags")}


def mh_ready(r, dut_df):
    d = r.duthost
    if not lag_up(d, r.pc, r.es_port):
        return False
    if not bond_ready(r.leaves.host_bond("es1"), (LEG_DUT, LEG_L1), ES1_SYS_MAC) or \
            not bond_ready(r.leaves.host_bond("es2"), ("hl1", "hl2"), ES2_SYS_MAC):
        return False
    es = frr_es(d)
    e1, e2 = es.get(r.esi1, {}), es.get(r.esi2, {})
    flags1 = set(e1.get("flags", []))
    if not {"local", "remote"} <= flags1 or r.l1.vtep not in es_vteps(e1) or ("nonDF" in flags1) == dut_df:
        return False
    if "remote" not in e2.get("flags", []) or es_vteps(e2) != {r.l1.vtep, r.l2.vtep}:
        return False
    if appl_df(d, VLAN_A, r.pc) != ("true" if dut_df else "false") or r.l1.vtep not in appl_shl_vteps(d, r.pc):
        return False
    a = MhAsicView(d)
    bp, _ = a.lag_bridge_port(r.pc)
    return a.vlan_member(VLAN_A, bp)[0] is not None


def wait_mh_ready(r, timeout, dut_df=True, when=""):
    """Gates: LACP (DUT LAG and member up; each host bond on one aggregator with both legs distributing), zebra's ES
    view (ES1 local + remote via l1 with the expected DF role, ES2 remote via l1 and l2), APPL_DB DF and split
    horizon for the ES port, and its ASIC bridge port and VLAN member (needs sonic-net/sonic-swss#4841 where the SAI
    rejects EGRESS_FILTERING at create)."""
    wait_state(timeout, 3, lambda: mh_ready(r, dut_df),
               "the EVPN-MH rig ready (DUT {}){}; APPL_DB EVPN_DF_TABLE (\"df\") stays empty when zebra's SONiC "
               "dataplane lacks the EVPN-MH messages (sonic-net/sonic-buildimage#27544)".format(
                   "DF" if dut_df else "non-DF", " " + when if when else ""),
               lambda: mh_now(r))


@pytest.fixture(scope="module")
def access(duthost, tbinfo, request):
    with access_ports(duthost, tbinfo, request, need=4, want=4) as a:
        yield a


@pytest.fixture(scope="module")
def mh_rig(duthost, tbinfo, ptfadapter, vmhost, ansible_adhoc, request):
    missing = mh_support_missing(duthost)
    if missing:
        pytest.skip("the image has no EVPN multihoming support: {}".format("; ".join(missing)))
    pre = rig_preflight(vmhost, ansible_adhoc, get_inventory_files(request))
    # after the preflight, so a testbed without a test server borrows no ports
    pick = pick_rig_ports(duthost, request.getfixturevalue("access"))
    idx, ports = pick.idx, pick.ports
    h0, es_port = ports[0], ports[1]
    leaves = [Leaf(name, "eth{}".format(idx[port]), idx[port], port, asn, vtep, ip, dut_ip, vv)
              for (name, asn, vtep, ip, dut_ip, vv), port in zip(LEAF_PLAN, ports[2:])]
    l1, l2 = leaves
    check_leaf_vteps_unrouted(duthost, leaves)
    mh_tables = (("CONFIG_DB", "EVPN_ETHERNET_SEGMENT|*"), ("CONFIG_DB", "PORTCHANNEL|{}".format(ES_PC)),
                 ("APPL_DB", "EVPN_DF_TABLE:*"), ("APPL_DB", "EVPN_SPLIT_HORIZON_TABLE:*"),
                 ("APPL_DB", "EVPN_ES_BACKUP_NHG_TABLE:*"), ("APPL_DB", "L2_NEXTHOP_GROUP_TABLE:*"))
    assert_clean_start(duthost, leaves, (VLAN_A_NAME,), (VNI_A,), more=mh_tables)
    check_ingress_once(duthost, ptfadapter, {p: idx[p] for p in (h0, ports[2], ports[3])}, ports, tag("rx"))

    es1 = EthernetSegment("es1", ES_ID, ES1_SYS_MAC, {l1.name: L1_DF_PREF}, VLAN_A, MH_HOST_MAC,
                          "eth{}".format(idx[es_port]))
    es2 = EthernetSegment("es2", ES2_ID, ES2_SYS_MAC, ES2_DF_PREFS, VLAN_A, RHOST_MAC, "")
    group = tbinfo["group-name"]
    lv = Leaves(pre.server, "ptf_{}".format(group), pre.image, leaves, pick.peer_asn, pick.lo, RT_ASN,
                re.sub(r"[^A-Za-z0-9_.-]", "_", group) + "_mh", segments=(es1, es2))
    lv.install()
    try:
        # before the rig changes the DUT: an image that cannot be had is a missing test-server prerequisite
        image_used = resolve_leaf_image(lv)
        rb = RigBuild(request, duthost, pick, leaves, lv, RT_ASN, ((VLAN_A, VNI_A),), baseline=mh_baseline,
                      log_ignore=MH_HARMLESS_LOG, leftovers=mh_tables)
        added_aav = not dut_has_advertise_all_vni(duthost)
    except BaseException:
        lv.remove_files()   # nothing on the DUT yet, so no teardown runs
        raise
    r = SimpleNamespace(
        duthost=duthost, leaves=lv, l1=l1, l2=l2, h0=h0, p_h0=idx[h0], es_port=es_port, p_es=idx[es_port], pc=ES_PC,
        esi1=type3_esi(ES1_SYS_MAC, ES_ID), esi2=type3_esi(ES2_SYS_MAC, ES2_ID), dut_vtep=pick.lo, dut_asn=pick.dut_asn,
        router_mac=duthost.facts["router_mac"], kvx=None, vs=duthost.facts.get("asic_type") == "vs",
        loss_budget=SWITCHOVER_LOST_BY_PLATFORM_ASIC.get(duthost.facts.get("platform_asic"), SWITCHOVER_LOST))
    logger.info("EVPN-MH rig: base %s, H0 %s (ptf %s), ES member %s (ptf %s) in %s, ESI %s, leaves %s, ES2 ESI %s, "
                "DUT VTEP %s AS %s, image %s, ASIC_DB baseline %s", pick.base_vlan, h0, r.p_h0, es_port, r.p_es,
                ES_PC, r.esi1, leaves, r.esi2, r.dut_vtep, pick.dut_asn, image_used, rb.base)

    raw_payloads(ptfadapter)
    try:
        # before the setup LogAnalyzer window: what a bgp restart logs is not the rig's
        prev_subtype = enable_mac_ext_learn(duthost)
        if prev_subtype is not None:
            rb.on_teardown("after_members", "DEVICE_METADATA subtype", lambda: restore_subtype(duthost, prev_subtype))
        with log_clean(request, duthost, "setup", MH_HARMLESS_LOG):
            rb.ports_out_of_base()
            # restore registered first, so a failure partway still puts back what changed (none if no ACL binding)
            acl = {}
            rb.on_teardown("after_members", "ACL bindings of {}".format(es_port), lambda: acl_rebind(duthost, acl))
            acl_unbind(duthost, [es_port], acl)
            rb.leaf_ips()
            rb.vlans()
            rb.member(VLAN_A, h0, True)
            rb.vxlan()
            rb.wait_connected()
            vtysh(duthost, ["configure terminal", "evpn mh startup-delay 0", "end"])
            # `no` needs a value; it restores the default (180 s)
            rb.on_teardown("first", "evpn mh startup-delay", lambda: vtysh(
                duthost, ["configure terminal", "no evpn mh startup-delay 180", "end"], check=False))
            # the same delay in CONFIG_DB, which the DUT's ES boot hold reads after a swss restart
            prev_delay = db_hget(duthost, "CONFIG_DB", "EVPN_MH_GLOBAL|default", "startup_delay")
            sh(duthost, "sudo sonic-db-cli CONFIG_DB HSET 'EVPN_MH_GLOBAL|default' startup_delay 0")
            mh_global = "sudo sonic-db-cli CONFIG_DB {} 'EVPN_MH_GLOBAL|default' startup_delay"
            restore_delay = (mh_global.format("HSET") + " " + prev_delay) if prev_delay else mh_global.format("HDEL")
            rb.on_teardown("first", "EVPN_MH_GLOBAL startup_delay", lambda: sh(duthost, restore_delay, check=False))
            rb.bgp(added_aav)
            rb.leaves_up()
            rb.converge(CONVERGE)

            # the dual-homed host aggregates its legs only if both partners present one system MAC and key
            key = lv.es_key(l1.name, "es1")
            if key is None:
                pytest.fail("no LACP actor key on {}'s es1bond".format(l1.name))
            es_portchannel_add(duthost, ES_PC, ES1_SYS_MAC, key)

            def pc_del():
                sh(duthost, "sudo config portchannel member del {} {}".format(ES_PC, es_port), check=False)
                sh(duthost, "sudo config portchannel del {}".format(ES_PC))
                # the ACL bindings go back on the port next: aclorch refuses a port that is still a LAG member
                wait_for(60, 2, lambda: not db_keys(duthost, "APPL_DB", "LAG_MEMBER_TABLE:{}:*".format(ES_PC)) and
                         not db_keys(duthost, "APPL_DB", "LAG_TABLE:{}".format(ES_PC)),
                         "APPL_DB LAG_TABLE / LAG_MEMBER_TABLE of {} to go".format(ES_PC))
            rb.on_teardown("after_members", "PortChannel {}".format(ES_PC), pc_del)
            wait_for(60, 2, lambda: bool(db_keys(duthost, "APPL_DB", "LAG_TABLE:{}".format(ES_PC))),
                     "APPL_DB LAG_TABLE:{}".format(ES_PC))
            sh(duthost, "sudo config portchannel member add {} {}".format(ES_PC, es_port))
            rb.member(VLAN_A, ES_PC, True)
            sh(duthost, "sudo config interface evpn-esi add {} auto-system-mac".format(ES_PC))
            rb.on_teardown("first", "ES {}".format(ES_PC), lambda: sh(
                duthost, "sudo config interface evpn-esi del {}".format(ES_PC), check=False))
            sh(duthost, "sudo config interface evpn-df-pref {} {}".format(ES_PC, DUT_DF_PREF))
            wait_mh_ready(r, CONVERGE, when="after the ES came up")
            time.sleep(5)
        r.kvx = kernel_vxlan_netdev(duthost, VNI_A)
        if r.kvx is None or kernel_rx_packets(duthost, r.kvx) is None:
            pytest.fail("no readable kernel VxLAN netdev for VNI {}: the hardware-termination gate would be "
                        "off".format(VNI_A))
        logger.info("EVPN-MH rig up; LACP key %s, kernel VxLAN netdev %s, zebra --kernel-mac-ext-learn %s", key, r.kvx,
                    zebra_mac_ext_learn(duthost)[0])
        yield r
    finally:
        rb.teardown()


@pytest.fixture(autouse=True)
def ignore_known_harmless(duthost, loganalyzer):
    if loganalyzer:
        loganalyzer[duthost.hostname].ignore_regex.extend(MH_HARMLESS_LOG)


# --------------------------------------------------------------------------------------------------------------------
# measurements
# --------------------------------------------------------------------------------------------------------------------
def inject_bum(r, ptfadapter, leaf, name, src, dst=BROADCAST_MAC):
    """N VxLAN frames to dst toward the DUT as leaf (the PTF on leaf's port, outer source leaf's VTEP, VNI A).

    Returns (copies, ES member copies, H0 copies, DUT VxLAN copies). On a virtual switch the kernel is the dataplane,
    so the decap is not judged by its path there."""
    t = tag(name)
    pkt = vxlan_frame(probe_frame(t, dst, src), r.router_mac, INJ_OUTER_SMAC, leaf.vtep, r.dut_vtep, VNI_A)
    before = kernel_rx_packets(r.duthost, r.kvx)
    copies = send_and_count(ptfadapter, leaf.ptf_port, pkt, N, t)
    why = "" if r.vs else kernel_path(r.duthost, r.kvx, before, N)
    assert not why, "BUM from {}: {}".format(leaf.name, why)
    return (copies, native_on(copies, r.p_es, None), native_on(copies, r.p_h0, None),
            len(vxlan_to(copies, r.dut_vtep)))


def check_remote_bum(r, ptfadapter, leaf, name, src, to_es, what):
    """inject_bum for each REMOTE_BUM kind: H0 gets every copy, the DUT sends no VxLAN copy, and the ES member gets
    every copy (to_es) or none. The none (DF drop, local bias) is the SAI's VLAN member and isolation group filtering,
    which a virtual switch's kernel bridge does not apply: there the ASIC_DB attributes the cases read stand for it,
    and hardware reads the frames."""
    for suffix, kind, dst in REMOTE_BUM:
        copies, es, h0, emitted = inject_bum(r, ptfadapter, leaf, name + suffix, src, dst)
        es_ok = delivered(es) if to_es else (es == 0 or (r.vs and es <= N))
        assert es_ok and delivered(h0) and emitted == 0, (
            "{}: {} {} frames from {}: {} on the ES member {} (expected {}), {} on {}, {} DUT VxLAN copies. "
            "Copies: {}".format(what, N, kind, leaf.name, es, r.es_port, N if to_es else 0, h0, r.h0, emitted,
                                summarize(copies)))


def es_member_drop(r, vid=VLAN_A):
    a = MhAsicView(r.duthost)
    bp, _ = a.lag_bridge_port(r.pc)
    _vm, vma = a.vlan_member(vid, bp)
    return vma.get("SAI_VLAN_MEMBER_ATTR_TUNNEL_TERM_BUM_TX_DROP", "")


def learn_mh_host(r, ptfadapter):
    """The DUT learns the dual-homed host's MAC on the ES PortChannel (frames from it on the ES member)."""
    learn_mac(ptfadapter, r.duthost, r.p_es, r.pc, VLAN_A, MH_HOST_MAC, tag("learn"))


def es_bridge_port_state(r):
    """(ES bridge port oid, its protection group oid, switchover, the host MAC's FDB bridge ports, the group)."""
    a = MhAsicView(r.duthost)
    bp, attrs = a.lag_bridge_port(r.pc)
    prot = attrs.get("SAI_BRIDGE_PORT_ATTR_BRIDGE_PORT_PROTECTION_NEXT_HOP_GROUP_ID", NULL_OID)
    return SimpleNamespace(
        bp=bp, prot=prot, switchover=attrs.get("SAI_BRIDGE_PORT_ATTR_BRIDGE_PORT_SET_SWITCHOVER", "false"),
        fdb=[e[0] for e in a.fdb_entries(MH_HOST_MAC, VLAN_A)], group=a.nhg(prot) if prot != NULL_OID else {})


def es_backup_attached(r):
    """Skip without orchagent's ES backup consumer; else wait for zebra's backup group {l1} on the ES bridge port.

    Returns (APPL group id, ASIC protection group oid)."""
    d = r.duthost
    if not es_backup_consumer(d):
        pytest.skip("orchagent has no EVPN_ES_BACKUP_NHG_TABLE consumer: the ES backup group (bridge port protection "
                    "and switchover) is not in this image")
    wait_for(CONVERGE, 3, lambda: appl_es_backup(d, r.pc) != "", "APPL_DB EVPN_ES_BACKUP_NHG_TABLE:{}".format(r.pc))
    gid = appl_es_backup(d, r.pc)
    wait_for(CONVERGE, 3, lambda: appl_l2nhg_vteps(d, gid) == {r.l1.vtep},
             "APPL_DB L2_NEXTHOP_GROUP_TABLE:{} == {{{}}}".format(gid, r.l1.vtep))

    def attached():
        s = es_bridge_port_state(r)
        return s.prot != NULL_OID and s.group.get("vteps") == [r.l1.vtep] and \
            s.group.get("type") == "SAI_NEXT_HOP_GROUP_TYPE_BRIDGE_PORT"
    try:
        wait_state(60, 3, attached, "the ES bridge port's protection group = zebra's backup group {{{}}}".format(
            r.l1.vtep), lambda: es_bridge_port_state(r))
    except AssertionError as e:
        raise AssertionError("{}; orchagent's capability notice: '{}'".format(e, orch_capability_notice(
            d, "ES backup next hop group (bridge port protection)") or "none logged")) from e
    return gid, es_bridge_port_state(r).prot


def es_stream(r, name, event, frames=STREAM_FRAMES):
    """A stream H0 -> the dual-homed host with event() issued EVENT_AT_S into it; the probe's summary plus the
    VxLAN copies to each leaf (VNI A) and when the event ran."""
    t = tag(name)
    r.leaves.stream_start(t, "es1", "eth{}".format(r.p_h0), STREAM_SRC_MAC, PPS, frames, STREAM_FLOWS)
    t0 = time.time()
    time.sleep(EVENT_AT_S)
    try:
        event()
    finally:
        took = time.time() - t0
        res = r.leaves.stream_result(t)
    res["event_s"] = round(took, 2)
    res["frames"] = frames
    # each counter writes its result only when it ran: a missing one is no reading, not a zero
    assert set(res["vx"]) == {r.l1.ptf_if, r.l2.ptf_if} and {LEG_DUT, LEG_L1} <= set(res["legs"]), (
        "stream {}: not every counter reported (VxLAN per leaf port, receiver per bond leg): {}".format(t, res))
    for lf in (r.l1, r.l2):
        res["vx_" + lf.name] = res["vx"][lf.ptf_if].get("{}/{}".format(lf.vtep, VNI_A), 0)
    res["at_" + LEG_DUT] = res["legs"][LEG_DUT]["n"]
    res["at_" + LEG_L1] = res["legs"][LEG_L1]["n"]
    logger.info("stream %s: %s", t, res)
    return res


def stream_ok(res, max_lost):
    """Common stream checks: all sent, at most max_lost lost, no duplicate, nothing flooded to l2."""
    return res.get("sent") == res["frames"] and res["lost"] <= max_lost and res["dups"] == 0 and res["vx_l2"] == 0


def leg(r, op):
    (r.duthost.shutdown if op == "shutdown" else r.duthost.no_shutdown)(r.es_port)


def up_stream(r, name, problems):
    """The ES member comes back up under a stream; checks it and appends what is wrong to problems."""
    up = es_stream(r, name, lambda: leg(r, "startup"), UP_STREAM_FRAMES)
    after = UP_STREAM_FRAMES / float(PPS) - up["event_s"]
    if after < AFTER_UP_S:
        problems.append("{}: the stream ran only {:.1f} s after the startup (instrument: too short)".format(
            name, after))
    elif not (stream_ok(up, UP_MAX_LOST) and up["at_" + LEG_DUT] > 0):
        problems.append("{} (leg up): {}".format(name, up))
    return up


# --------------------------------------------------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------------------------------------------------
def test_mh_df_bum_from_remote_vtep(mh_rig, ptfadapter):
    """BUM from a remote VTEP (l2, not on the ES) reaches the dual-homed host only through the DF.

    Each reading sends a broadcast and an unknown unicast. DUT DF: one copy on the ES member, VLAN member
    TUNNEL_TERM_BUM_TX_DROP false. l1's preference raised above the DUT's: the DUT is non-DF, drops it
    (TUNNEL_TERM_BUM_TX_DROP true), and still floods local BUM to the ES. Restored: DF again. H0 gets every copy
    throughout and the DUT re-floods nothing to other VTEPs.
    """
    r = mh_rig
    wait_mh_ready(r, CONVERGE, when="before the case")
    drop = es_member_drop(r)
    assert drop in ("", "false"), (
        "DUT DF: ES VLAN member TUNNEL_TERM_BUM_TX_DROP is '{}' (true drops tunnel BUM on the DF: an orchagent that "
        "sends the DF role itself instead of its inverse)".format(drop))
    check_remote_bum(r, ptfadapter, r.l2, "dfA", MAC_L2_HOST, True, "DUT DF")

    restore = Steps("after the DF flip")

    def _cleanup():
        r.leaves.run("esdf", r.l1.name, "es1", L1_DF_PREF, check=False)
        restore("restore", lambda: wait_mh_ready(
            r, CONVERGE, when="after {}'s DF preference was restored".format(r.l1.name)))

    try:
        r.leaves.es_df(r.l1.name, "es1", L1_DF_PREF_WINS)
        wait_mh_ready(r, CONVERGE, dut_df=False, when="after {}'s DF preference rose".format(r.l1.name))
        # TUNNEL_TERM_BUM_TX_DROP=true drops tunnel-terminated BUM (saivlan.h), so the non-DF member has it set
        wait_state(30, 2, lambda: es_member_drop(r) == "true", "DUT non-DF: ES VLAN member TUNNEL_TERM_BUM_TX_DROP "
                   "true", lambda: es_member_drop(r))
        check_remote_bum(r, ptfadapter, r.l2, "dfN", MAC_L2_HOST, False, "DUT non-DF")
        t = tag("dfL")
        copies = send_and_count(ptfadapter, r.p_h0, probe_frame(t, BROADCAST_MAC, "02:4c:45:00:00:31"), N, t)
        local = native_on(copies, r.p_es, None)
        per_leaf = {lf.name: len(vxlan_to(copies, r.dut_vtep, lf.vtep, VNI_A, port=lf.ptf_port)) for lf in
                    (r.l1, r.l2)}
        assert delivered(local) and all(delivered(n) for n in per_leaf.values()), (
            "DUT non-DF: local BUM from {} must still flood to the ES: {} on {}, per leaf {}. Copies: {}".format(
                r.h0, local, r.es_port, per_leaf, summarize(copies)))
    finally:
        _cleanup()
    restore.finish()
    wait_state(30, 2, lambda: es_member_drop(r) in ("", "false"), "DF restored: ES VLAN member "
               "TUNNEL_TERM_BUM_TX_DROP false", lambda: es_member_drop(r))
    check_remote_bum(r, ptfadapter, r.l2, "dfR", MAC_L2_HOST, True, "DF restored")


def test_mh_local_bias_from_es_peer(mh_rig, ptfadapter):
    """BUM the ES peer (l1) floods for the dual-homed host is not sent back onto the ES (local bias).

    The PTF sends, as l1, broadcasts and unknown unicasts sourced by the ES host: the DUT (DF) delivers them to H0
    but not to its ES member. The same frames from l2 (not on the ES) reach the ES member. ASIC: l1's tunnel bridge
    port carries an isolation group that holds the ES bridge port; l2's does not.
    """
    r = mh_rig
    d = r.duthost
    wait_mh_ready(r, CONVERGE, when="before the case")
    shl = appl_shl_vteps(d, r.pc)
    assert r.l1.vtep in shl and r.l2.vtep not in shl, "APPL_DB split horizon peers of {}: {}".format(r.pc, shl)
    a = MhAsicView(d)
    bp, _ = a.lag_bridge_port(r.pc)
    assert a.isolating(r.l1.vtep, bp) and not a.isolating(r.l2.vtep, bp), (
        "ASIC: isolation of the ES bridge port {}: from {} {}, from {} {} (expected True, False); tunnel bridge "
        "ports {}".format(bp, r.l1.name, a.isolating(r.l1.vtep, bp), r.l2.name, a.isolating(r.l2.vtep, bp),
                          a.tunnel_bridge_ports()))
    check_remote_bum(r, ptfadapter, r.l1, "lbP", MH_HOST_MAC, False, "local bias, from the ES peer")
    check_remote_bum(r, ptfadapter, r.l2, "lbO", MAC_L2_HOST, True, "control, from a VTEP not on the ES")


def flow_frames(t, flows):
    return [packet.Ether(dst=RHOST_MAC, src=ALIAS_SRC_MAC) / packet.IP(src="10.99.254.1", dst="10.99.254.2", ttl=64) /
            packet.UDP(sport=10000 + i, dport=5001) / Raw("{}:{}:".format(t, i).encode() + b"." * 40)
            for i in range(flows)]


def send_flows(ptfadapter, port, pkts, t, batch=32):
    """Send pkts in batches (well under the PTF's per-port queue) and return [(Copy, flow)] of every copy seen."""
    found = []
    rx = re.compile(re.escape(t.encode()) + rb":(\d+):")
    for i in range(0, len(pkts), batch):
        found += [(c, int(m.group(1))) for c, m in collect(
            ptfadapter, lambda b=pkts[i:i + batch]: [testutils.send(ptfadapter, port, p) for p in b], rx.search)]
    return found


def test_mh_aliasing_to_remote_es(mh_rig, ptfadapter):
    """Known unicast to a host behind a remote ES (ES2 on l1 and l2) spreads over both ES2 leaves (aliasing).

    l1 advertises the ES2 host's MAC (Type-2 with ES2's ESI); the DUT installs it behind a bridge-port next hop
    group of both VTEPs. 128 UDP flows from H0, sent twice: each frame leaves the DUT exactly once as VxLAN, each
    leaf carries at least 1/8 of the flows, each flow uses one leaf both times, and nothing floods.
    """
    r = mh_rig
    d = r.duthost
    wait_mh_ready(r, CONVERGE, when="before the case")
    try:
        r.leaves.fdb("add", r.l1.name, RHOST_MAC, VLAN_A, "es2bond")
        wait_for(CONVERGE, 3, lambda: appl_remote_mac_nhg(d, VLAN_A_NAME, RHOST_MAC) != "",
                 "APPL_DB VXLAN_FDB_TABLE {} on a next hop group".format(RHOST_MAC))
        gid = appl_remote_mac_nhg(d, VLAN_A_NAME, RHOST_MAC)
        want = {r.l1.vtep, r.l2.vtep}
        wait_for(CONVERGE, 3, lambda: appl_l2nhg_vteps(d, gid) == want,
                 "APPL_DB L2_NEXTHOP_GROUP_TABLE:{} == {}".format(gid, sorted(want)))

        def asic_group():
            e = MhAsicView(d)
            entries = e.fdb_entries(RHOST_MAC, VLAN_A)
            if len(entries) != 1:
                return {"fdb": entries}
            _bp, bpa, _fa = entries[0]
            if bpa.get("SAI_BRIDGE_PORT_ATTR_TYPE") != "SAI_BRIDGE_PORT_TYPE_BRIDGE_PORT_NEXT_HOP_GROUP":
                return {"fdb": entries}
            return e.nhg(bpa.get("SAI_BRIDGE_PORT_ATTR_BRIDGE_PORT_NEXT_HOP_GROUP_ID", ""))

        def grouped(g):
            return g.get("vteps") == sorted(want) and g.get("type") == "SAI_NEXT_HOP_GROUP_TYPE_BRIDGE_PORT"
        try:
            wait_state(60, 3, lambda: grouped(asic_group()),
                       "ASIC FDB {} -> a bridge-port next hop group {}".format(RHOST_MAC, sorted(want)), asic_group)
        except AssertionError as e:
            raise AssertionError("{}; orchagent's capability notice: '{}'".format(e, orch_capability_notice(
                d, "SAI_NEXT_HOP_GROUP_TYPE_BRIDGE_PORT") or "none logged")) from e

        t = tag("al")
        pkts = flow_frames(t, ALIAS_FLOWS)
        found = send_flows(ptfadapter, r.p_h0, pkts + pkts, t)
        vx = [(c, f) for c, f in found if c.kind == "vxlan" and c.osrc == r.dut_vtep]
        # a copy counts only to a leaf's VTEP, in VNI A, on that leaf's port
        right = {(lf.vtep, lf.ptf_port): lf.name for lf in (r.l1, r.l2)}
        good = [(right[(c.odst, c.port)], f) for c, f in vx if c.vni == VNI_A and (c.odst, c.port) in right]
        seen = collections.Counter(f for _leaf, f in good)
        per_leaf = {lf.name: {f for leaf, f in good if leaf == lf.name} for lf in (r.l1, r.l2)}
        split = per_leaf["l1"] & per_leaf["l2"]
        flooded = native_on([c for c, _f in found], r.p_es)
        wrong = {f: n for f, n in seen.items() if n != 2}
        missing = sorted(set(range(ALIAS_FLOWS)) - set(seen))
        other = len(vx) - len(good)
        assert (not wrong and not missing and not split and other == 0 and flooded == 0 and
                all(len(s) >= ALIAS_MIN_SHARE for s in per_leaf.values())), (
            "{} flows x2 to {} behind ES2: {} DUT VxLAN copies; flows per leaf {}; flows not seen exactly twice {}; "
            "flows missing {}; {} flows on both leaves; {} copies to another VTEP, VNI or port; {} flooded to the ES "
            "member".format(ALIAS_FLOWS, RHOST_MAC, len(vx), {k: len(v) for k, v in per_leaf.items()},
                            dict(list(wrong.items())[:8]), missing[:8], len(split), other, flooded))
    finally:
        r.leaves.run("fdb", "del", r.l1.name, RHOST_MAC, VLAN_A, "es2bond", check=False)


DETACH_SETTLE_S = 10    # orchagent consumes the delete asynchronously; the stream gives hardware as long


def test_mh_es_leg_down_switchover(mh_rig, ptfadapter):
    """The ES member goes down: traffic to the dual-homed host moves to the ES backup group (VxLAN to l1).

    SWITCHOVER_RUNS cycles under a 2000 pps stream from H0. Down: frames lost (median and every run bounded), no
    duplicate, nothing flooded to l2, the rest delivered through l1; ASIC: SET_SWITCHOVER true and the host's FDB entry
    still on the ES bridge port. Up: at most UP_MAX_LOST lost, no duplicate, no flood, switched back. The ES
    PortChannel has one member, so this does not tell a switchover at its last member from one at its first. On a
    virtual switch, whose kernel bridge has no bridge port protection, only the ASIC_DB readings are taken.
    """
    r = mh_rig
    wait_mh_ready(r, CONVERGE, when="before the case")
    _gid, prot = es_backup_attached(r)
    median_max, cap = r.loss_budget
    downs, problems = [], []
    for i in range(SWITCHOVER_RUNS):
        learn_mh_host(r, ptfadapter)
        pre = es_bridge_port_state(r)
        assert pre.switchover != "true" and pre.fdb == [pre.bp], "run {} before: {}".format(i, pre)

        back = Steps("ES member down/up run {}".format(i))

        def _cleanup():
            if r.vs or back("up", lambda: up_stream(r, "su{}".format(i), problems)) is None:
                back("leg startup", lambda: leg(r, "startup"))
            back("ready", lambda: wait_mh_ready(r, CONVERGE, when="after the ES member came back (run {})".format(i)))
            back("switched back", lambda: wait_state(
                30, 2, lambda: es_bridge_port_state(r).switchover != "true", "ASIC switched back after the leg came "
                "up", lambda: es_bridge_port_state(r)))

        try:
            if r.vs:     # the kernel bridge has no bridge port protection: no frames are read
                leg(r, "shutdown")
                down = None
            else:
                down = es_stream(r, "sd{}".format(i), lambda: leg(r, "shutdown"))
            wait_state(30, 2, lambda: es_bridge_port_state(r).switchover == "true",
                       "ASIC SET_SWITCHOVER true on the ES bridge port", lambda: es_bridge_port_state(r))
            st = es_bridge_port_state(r)
            stream_good = down is None or (stream_ok(down, cap) and down["at_" + LEG_DUT] > 0 and down["vx_l1"] > 0)
            if not (stream_good and st.fdb == [st.bp] and st.prot == prot):
                problems.append("run {} down: {} | ASIC {}".format(i, down or "no stream (virtual switch)", st))
            if down is not None:
                downs.append(down["lost"])
        finally:
            _cleanup()
        assert not back.errors, "ES member down/up run {}: {}; so far: {}".format(i, back.errors, problems)
    if r.vs:
        assert not problems, "ES member down/up, ASIC_DB (virtual switch): {}".format(" | ".join(problems))
        return
    median = statistics.median(downs)
    assert not problems and median <= median_max, (
        "ES member down/up at {} pps (1 frame = {} ms): lost at down {} (median {}, bound {}, cap {} per run); "
        "problems: {}".format(PPS, 1000.0 / PPS, downs, median, median_max, cap, problems or "none"))


def test_mh_es_detach_while_down(mh_rig, ptfadapter):
    """With the ES member down and switched over, zebra's backup group entry for the ES port is deleted (APPL_DB
    EVPN_ES_BACKUP_NHG_TABLE DEL, written as fpmsyncd would): nothing is lost or flooded and the ASIC keeps the
    protection group and the switchover. The leg comes back: switched back with no loss and no duplicate. On a
    virtual switch, whose kernel bridge has no bridge port protection, only the ASIC_DB readings are taken.
    """
    r = mh_rig
    d = r.duthost
    wait_mh_ready(r, CONVERGE, when="before the case")
    gid, prot = es_backup_attached(r)
    learn_mh_host(r, ptfadapter)
    problems = []

    back = Steps("detach while the ES member is down: bringing it back")

    def _cleanup():
        if r.vs or back("leg up", lambda: up_stream(r, "du", problems)) is None:
            back("leg startup", lambda: leg(r, "startup"))

        def reattach():
            # zebra rewrites the entry when the leg returns; if it did not, put zebra's group back for later cases
            if appl_es_backup(d, r.pc) == "":
                swss_apply(d, [{"EVPN_ES_BACKUP_NHG_TABLE:{}".format(r.pc): {"nexthop_group": gid}, "OP": "SET"}],
                           "vxlan_mh_dt")
        back("backup group entry", reattach)
        back("ready", lambda: wait_mh_ready(r, CONVERGE, when="after the ES member came back"))
        back("re-attached", lambda: wait_state(
            60, 3, lambda: es_bridge_port_state(r).prot == prot and es_bridge_port_state(r).switchover != "true",
            "ASIC: protection group attached again and switched back", lambda: es_bridge_port_state(r)))

    try:
        leg(r, "shutdown")
        wait_state(30, 2, lambda: es_bridge_port_state(r).switchover == "true",
                   "ASIC SET_SWITCHOVER true after the leg went down", lambda: es_bridge_port_state(r))
        row = "EVPN_ES_BACKUP_NHG_TABLE:{}".format(r.pc)
        delete = lambda: swss_apply(d, [{row: {"nexthop_group": gid}, "OP": "DEL"}], "vxlan_mh_dt")  # noqa: E731
        if r.vs:
            # no bridge port protection in a virtual switch's kernel bridge: only ASIC_DB is read, after the settle
            # time the hardware case gets from its stream
            delete()
            time.sleep(DETACH_SETTLE_S)
        else:
            res = es_stream(r, "dt", delete)
            if not (stream_ok(res, DETACH_MAX_LOST) and res["at_" + LEG_DUT] == 0 and res["vx_l1"] > 0):
                problems.append("stream across the delete: {}".format(res))
        st = es_bridge_port_state(r)
        if not (st.switchover == "true" and st.prot == prot and st.fdb == [st.bp]):
            problems.append("ASIC after the delete (expected switchover true, protection {}, the host on the ES "
                            "bridge port): {}".format(prot, st))
    finally:
        _cleanup()
    assert not problems and not back.errors, "detach while the ES member is down: {}".format(
        " | ".join(problems + back.errors))


# --------------------------------------------------------------------------------------------------------------------
# a VLAN given its VNI after the ES port joined it
# --------------------------------------------------------------------------------------------------------------------
VLAN_B, VNI_B = 2101, 12101
VLAN_B_NAME = "Vlan{}".format(VLAN_B)
B_PROBE_SRC_MAC = "02:4c:45:00:00:73"   # H0's source in VLAN B's tagged-path probe
DF_ENTRY_SETTLE_S = 10                  # zebra's DF writes for a VNI it just learned are in APPL_DB by then


def dut_vni(r, vni, add):
    """The DUT's l2vpn evpn vni with the rig's route targets, or its removal."""
    body = (["vni {}".format(vni), "route-target import {}:{}".format(RT_ASN, vni),
             "route-target export {}:{}".format(RT_ASN, vni), "exit-vni"] if add else ["no vni {}".format(vni)])
    vtysh(r.duthost, ["configure terminal", "router bgp {}".format(r.dut_asn), "address-family l2vpn evpn"] + body +
          ["exit-address-family", "exit", "end"], check=add)


def es_in_vlan(r, vid):
    a = MhAsicView(r.duthost)
    bp, _ = a.lag_bridge_port(r.pc)
    return a.vlan_member(vid, bp)[0] is not None


def vlan_b_readers(r, ptfadapter):
    """Readers for VLAN B's tagged members, the ES member and H0 (the namespace vxlan_l2_helpers.capture takes).

    VLAN B leaves the DUT C-tagged; where the fanout drops such frames, the members' DUT TX counters stand in. Probed
    before VLAN B has a VNI (probe_tagged_path): a tagged broadcast from H0 must leave on the ES member."""
    rd = SimpleNamespace(duthost=r.duthost, tagged_hosts=[r.es_port, r.h0], port={r.es_port: r.p_es, r.h0: r.p_h0},
                         canary=None, tagged_at_ptf=True)
    if r.vs:     # no fanout between a virtual switch and the PTF
        return rd
    rd.tagged_at_ptf, _why = probe_tagged_path(rd, ptfadapter, r.p_h0, VLAN_B, tag("ctagB"), B_PROBE_SRC_MAC,
                                               tagged=True, sender=r.h0)
    return rd


def check_remote_bum_b(r, ptfadapter, rd, kvx, name, to_es, what):
    """check_remote_bum for VNI B, as l2: H0 (tagged) gets every copy, the DUT sends no VxLAN copy, and the ES member
    gets every copy (to_es) or none (on a virtual switch the ASIC_DB drop attribute stands for none). Where the members
    are read from the DUT TX counters, the window stays open until the copies they should get reach COUNTERS_DB."""
    for suffix, kind, dst in REMOTE_BUM:
        t = tag(name + suffix)
        pkt = vxlan_frame(probe_frame(t, dst, MAC_L2_HOST), r.router_mac, INJ_OUTER_SMAC, r.l2.vtep, r.dut_vtep, VNI_B)
        before = None if r.vs else kernel_rx_packets(r.duthost, kvx)
        expect = {r.h0: GOOD, r.es_port: GOOD} if to_es else {r.h0: GOOD}
        copies, tx = capture(rd, lambda: send_and_count(ptfadapter, r.l2.ptf_port, pkt, N, t), expect=expect)
        why = "" if r.vs else kernel_path(r.duthost, kvx, before, N)
        assert not why, "{}: BUM from {} in VNI {}: {}".format(what, r.l2.name, VNI_B, why)
        h0_ok, h0_got = member_delivered(rd, copies, tx, r.h0)
        if to_es:
            es_ok, es_got = member_delivered(rd, copies, tx, r.es_port)
        else:
            es_got = member_flood(rd, copies, tx, [r.es_port])
            es_ok = es_got == 0 or r.vs
        emitted = len(vxlan_to(copies, r.dut_vtep))
        assert es_ok and h0_ok and emitted == 0, (
            "{}: {} {} frames from {} in VNI {}: {} on the ES member {} (expected {}), {} on {}, {} DUT VxLAN copies. "
            "Copies: {}".format(what, N, kind, r.l2.name, VNI_B, es_got, r.es_port, N if to_es else 0, h0_got, r.h0,
                                emitted, summarize(copies)))


def test_mh_vni_mapped_after_es_port_joined(mh_rig, ptfadapter):
    """A VLAN given its VNI after the ES port joined it has no DF entry from zebra, and its remote BUM still reaches the
    dual-homed host; both leaves forward it until zebra writes the VLAN's DF role.

    zebra writes an ES port's DF role for a VLAN when the port joins a VLAN that already has a VNI and, for every VLAN
    of the port, when the role changes. Mapping a VNI to a VLAN the port is already in writes nothing, so APPL_DB
    EVPN_DF_TABLE has no entry for that VLAN until the next of those events. Without an entry orchagent does not drop
    tunnel-terminated BUM to the port (TUNNEL_TERM_BUM_TX_DROP not true), whatever the elected role: the DF and the
    non-DF leaf both forward, and the host gets each remote BUM frame twice in that window. That is the accepted
    behaviour; dropping without an entry would make both leaves drop it and the host get none. The PTF injects the
    remote BUM to the DUT only, so the case reads the DUT's copy, with the DUT made non-DF so that its copy is the
    duplicate:

      1. l1's preference raised: the DUT is non-DF. VLAN B is created with the ES PortChannel (tagged) and H0
         (tagged), then mapped to VNI B, which l2 serves. No DF entry for VLAN B (the case fails as not exercised if
         one appears); the ES VLAN member's drop is off in VLAN B and on in VLAN A; BUM from l2 in VNI B (broadcast
         and unknown unicast) reaches the ES member and H0.
      2. The ES PortChannel leaves VLAN B and joins it again, now with VNI B mapped: zebra writes the non-DF entry,
         the drop goes on, and the ES member gets none while H0 still gets every copy.
      3. l1's preference restored (a DF change): VLAN B's entry turns DF, the drop goes off, and the ES member gets
         every copy.
    """
    r = mh_rig
    d = r.duthost
    wait_mh_ready(r, CONVERGE, when="before the case")
    built = []

    restore = Steps("undoing {} and the DF flip".format(VLAN_B_NAME))

    def _cleanup():
        if "df" in built:
            restore("DF preference", lambda: r.leaves.run("esdf", r.l1.name, "es1", L1_DF_PREF, check=False))
        if "vlan" in built:
            restore("fdb flush", lambda: flush_vlan_fdb(d, VLAN_B))
        if "members" in built:
            # while VNI B is still mapped: zebra then deletes the port's DF and split horizon rows for VLAN B
            for p in (r.pc, r.h0):
                restore("member del {}".format(p), lambda p=p: sh(
                    d, "sudo config vlan member del {} {}".format(VLAN_B, p), check=False))
            restore("DF rows of {} gone".format(VLAN_B_NAME), lambda: wait_for(
                60, 2, lambda: not db_keys(d, "APPL_DB", "EVPN_DF_TABLE:{}:*".format(VLAN_B_NAME)) and
                not db_keys(d, "APPL_DB", "EVPN_SPLIT_HORIZON_TABLE:{}:*".format(VLAN_B_NAME)),
                "APPL_DB EVPN_DF_TABLE / EVPN_SPLIT_HORIZON_TABLE rows of {} to go".format(VLAN_B_NAME)))
        if "bgp" in built:
            restore("bgp vni", lambda: dut_vni(r, VNI_B, False))
        if "map" in built:
            if db_keys(d, "CONFIG_DB", "VXLAN_TUNNEL_MAP|{}|map_{}_{}".format(VTEP_NAME, VNI_B, VLAN_B_NAME)):
                restore("map del", lambda: sh(d, "sudo config vxlan map del {} {} {}".format(
                    VTEP_NAME, VLAN_B, VNI_B)))
            restore("remote VNIs of {} gone".format(VLAN_B_NAME), lambda: wait_for(
                CONVERGE, 3, lambda: not [k for k in appl_remote_vnis(d) if k[0] == VLAN_B_NAME],
                "APPL_DB remote VNIs of {} to go".format(VLAN_B_NAME)))
        if "leaf" in built:
            restore("leaf vni", lambda: r.leaves.vni_del(r.l2.name, VLAN_B, VNI_B, check=False))
        if "vlan" in built and db_keys(d, "CONFIG_DB", "VLAN|{}".format(VLAN_B_NAME)):
            restore("vlan del", lambda: sh(d, "sudo config vlan del {}".format(VLAN_B)))
        if "df" in built:
            restore("ready", lambda: wait_mh_ready(r, CONVERGE, when="after {}'s DF preference was restored".format(
                r.l1.name)))

    try:
        # each step is marked before it runs, so that the undo also covers a step that failed half way
        built.append("df")
        r.leaves.es_df(r.l1.name, "es1", L1_DF_PREF_WINS)
        wait_mh_ready(r, CONVERGE, dut_df=False, when="after {}'s DF preference rose".format(r.l1.name))
        wait_state(30, 2, lambda: es_member_drop(r) == "true", "DUT non-DF: ES VLAN member TUNNEL_TERM_BUM_TX_DROP "
                   "true in {}".format(VLAN_A_NAME), lambda: es_member_drop(r))
        built.append("vlan")
        sh(d, "sudo config vlan add {}".format(VLAN_B))
        built.append("members")
        sh(d, "sudo config vlan member add {} {}".format(VLAN_B, r.pc))
        sh(d, "sudo config vlan member add {} {}".format(VLAN_B, r.h0))
        wait_for(60, 2, lambda: es_in_vlan(r, VLAN_B), "the ES PortChannel's VLAN member of {} in ASIC_DB".format(
            VLAN_B_NAME))
        rd = vlan_b_readers(r, ptfadapter)
        built.append("leaf")
        r.leaves.vni_add(r.l2.name, VLAN_B, VNI_B)
        built.append("map")
        sh(d, "sudo config vxlan map add {} {} {}".format(VTEP_NAME, VLAN_B, VNI_B))
        built.append("bgp")
        dut_vni(r, VNI_B, True)
        wait_for(CONVERGE, 3, lambda: (VLAN_B_NAME, r.l2.vtep) in appl_remote_vnis(d),
                 "APPL_DB remote VNI {} of {} in {}".format(VNI_B, r.l2.name, VLAN_B_NAME))
        kvx = None if r.vs else kernel_vxlan_netdev(d, VNI_B)
        if not r.vs and (kvx is None or kernel_rx_packets(d, kvx) is None):
            pytest.fail("no readable kernel VxLAN netdev for VNI {}: the hardware-termination gate would be "
                        "off".format(VNI_B))
        time.sleep(DF_ENTRY_SETTLE_S)
        if appl_df(d, VLAN_B, r.pc):
            pytest.fail("not exercised: zebra wrote a DF entry for {} ({}) although VNI {} was mapped after {} "
                        "joined it".format(VLAN_B_NAME, appl_df(d, VLAN_B, r.pc), VNI_B, r.pc))
        drops = {VLAN_A_NAME: es_member_drop(r), VLAN_B_NAME: es_member_drop(r, VLAN_B)}
        assert drops[VLAN_A_NAME] == "true" and drops[VLAN_B_NAME] in ("", "false"), (
            "DUT non-DF, no DF entry for {}: ES VLAN member TUNNEL_TERM_BUM_TX_DROP {} (expected true in {} from its "
            "non-DF entry, not true in {} without one)".format(VLAN_B_NAME, drops, VLAN_A_NAME, VLAN_B_NAME))
        check_remote_bum_b(r, ptfadapter, rd, kvx, "vbN", True, "DUT non-DF, no DF entry for {}".format(VLAN_B_NAME))

        sh(d, "sudo config vlan member del {} {}".format(VLAN_B, r.pc))
        wait_for(60, 2, lambda: not es_in_vlan(r, VLAN_B), "the ES PortChannel's VLAN member of {} to leave "
                 "ASIC_DB".format(VLAN_B_NAME))
        sh(d, "sudo config vlan member add {} {}".format(VLAN_B, r.pc))
        wait_state(CONVERGE, 3, lambda: appl_df(d, VLAN_B, r.pc) == "false", "zebra's non-DF entry for {} once {} "
                   "joined it with VNI {} mapped".format(VLAN_B_NAME, r.pc, VNI_B), lambda: appl_df(d, VLAN_B, r.pc))
        wait_state(30, 2, lambda: es_member_drop(r, VLAN_B) == "true", "ES VLAN member TUNNEL_TERM_BUM_TX_DROP true "
                   "in {} from its non-DF entry".format(VLAN_B_NAME), lambda: es_member_drop(r, VLAN_B))
        check_remote_bum_b(r, ptfadapter, rd, kvx, "vbJ", False, "DUT non-DF, non-DF entry for {}".format(VLAN_B_NAME))

        r.leaves.es_df(r.l1.name, "es1", L1_DF_PREF)
        built.remove("df")
        wait_mh_ready(r, CONVERGE, when="after {}'s DF preference was restored".format(r.l1.name))
        wait_state(CONVERGE, 3, lambda: appl_df(d, VLAN_B, r.pc) == "true", "zebra's DF entry for {} after the DF "
                   "change".format(VLAN_B_NAME), lambda: appl_df(d, VLAN_B, r.pc))
        wait_state(30, 2, lambda: es_member_drop(r, VLAN_B) in ("", "false"), "ES VLAN member "
                   "TUNNEL_TERM_BUM_TX_DROP not true in {} as DF".format(VLAN_B_NAME),
                   lambda: es_member_drop(r, VLAN_B))
        check_remote_bum_b(r, ptfadapter, rd, kvx, "vbD", True, "DUT DF, DF entry for {}".format(VLAN_B_NAME))
    finally:
        _cleanup()
    restore.finish()


def appl_mac_vteps(duthost, mac):
    """The remote VTEPs APPL_DB VXLAN_FDB_TABLE gives mac in VLAN A: its next hop group's, or its one remote VTEP."""
    key = "VXLAN_FDB_TABLE:{}:{}".format(VLAN_A_NAME, mac.lower())
    gid = db_hget(duthost, "APPL_DB", key, "nexthop_group")
    if gid:
        return appl_l2nhg_vteps(duthost, gid)
    vtep = db_hget(duthost, "APPL_DB", key, "remote_vtep")
    return {vtep} if vtep else set()


def asic_mac_vteps(duthost, mac):
    """Sorted remote VTEPs of mac's one ASIC FDB entry in VLAN A (a bridge-port next hop group's members, or a tunnel
    bridge port's destination), or the entries when there is not exactly one such entry."""
    e = MhAsicView(duthost)
    entries = e.fdb_entries(mac, VLAN_A)
    if len(entries) != 1:
        return {"fdb": entries}
    _bp, bpa, _fa = entries[0]
    if bpa.get("SAI_BRIDGE_PORT_ATTR_TYPE") == "SAI_BRIDGE_PORT_TYPE_BRIDGE_PORT_NEXT_HOP_GROUP":
        return e.nhg(bpa.get("SAI_BRIDGE_PORT_ATTR_BRIDGE_PORT_NEXT_HOP_GROUP_ID", "")).get("vteps")
    if bpa.get("SAI_BRIDGE_PORT_ATTR_TYPE") == "SAI_BRIDGE_PORT_TYPE_TUNNEL":
        return [e.tunnel_dip(bpa)]
    return {"fdb": entries}


def test_mh_remote_es_group_after_fdbsyncd_restart(mh_rig, ptfadapter):
    """After fdbsyncd restarts, a remote ES's next hop group still follows zebra: a leaf that leaves the ES leaves the
    group of the MACs behind it.

    l1 advertises the ES2 host's MAC (on the group {l1, l2}, as in test_mh_aliasing_to_remote_es). fdbsyncd is
    restarted (APPL_DB keeps the group). l2's link to the ES2 host then goes down, so l2 leaves ES2 and zebra moves
    the MAC to l1 alone. APPL_DB and the ASIC FDB entry must then send the MAC to l1 only: an fdbsyncd that did not
    learn the kernel's groups again at its start drops zebra's group update and keeps l2. Then l2's link comes back.
    This case runs last: on that failure the group stays stale until swss restarts.
    """
    r = mh_rig
    d = r.duthost
    both, l1_only = {r.l1.vtep, r.l2.vtep}, {r.l1.vtep}
    wait_mh_ready(r, CONVERGE, when="before the case")
    restore = Steps("l2 back on ES2")
    leg_down = False

    def _cleanup():
        if leg_down:
            restore("leg up", lambda: r.leaves.es_leg(r.l2.name, "es2", "up"))
            restore("ready", lambda: wait_mh_ready(r, CONVERGE, when="after l2's ES2 link came back"))
            # the MAC follows zebra back onto the group of both leaves before it is deleted
            restore("group back", lambda: wait_state(
                CONVERGE, 3, lambda: appl_mac_vteps(d, RHOST_MAC) == both and asic_mac_vteps(d, RHOST_MAC) == sorted(
                    both), "{} back on the group {} once l2 rejoined ES2".format(RHOST_MAC, sorted(both)),
                lambda: (appl_mac_vteps(d, RHOST_MAC), asic_mac_vteps(d, RHOST_MAC))))
        # l2 keeps advertising the MAC for FRR's ES peer MAC hold time after l1 deletes it; the rig teardown removes it
        r.leaves.run("fdb", "del", r.l1.name, RHOST_MAC, VLAN_A, "es2bond", check=False)

    try:
        r.leaves.fdb("add", r.l1.name, RHOST_MAC, VLAN_A, "es2bond")
        wait_state(CONVERGE, 3, lambda: appl_mac_vteps(d, RHOST_MAC) == both,
                   "APPL_DB VXLAN_FDB_TABLE {} on a group {}".format(RHOST_MAC, sorted(both)),
                   lambda: appl_mac_vteps(d, RHOST_MAC))
        wait_state(60, 3, lambda: asic_mac_vteps(d, RHOST_MAC) == sorted(both),
                   "ASIC FDB {} on a group {}".format(RHOST_MAC, sorted(both)), lambda: asic_mac_vteps(d, RHOST_MAC))
        before = pid_of(d, "fdbsyncd")
        sh(d, "docker exec swss supervisorctl restart fdbsyncd")
        wait_for(60, 2, lambda: pid_of(d, "fdbsyncd") not in ("", before) and "RUNNING" in sh(
            d, "docker exec swss supervisorctl status fdbsyncd; true", check=False),
            "fdbsyncd running again with a new pid (was {})".format(before))
        time.sleep(10)   # its startup dumps of the kernel
        if appl_mac_vteps(d, RHOST_MAC) != both:
            pytest.fail("the fdbsyncd restart alone changed {}'s APPL_DB VTEPs to {} (want {}): nothing else can be "
                        "read".format(RHOST_MAC, appl_mac_vteps(d, RHOST_MAC), sorted(both)))
        leg_down = True
        r.leaves.es_leg(r.l2.name, "es2", "down")
        wait_state(CONVERGE, 3, lambda: es_vteps(frr_es(d).get(r.esi2, {})) == l1_only,
                   "the DUT zebra's ES2 to hold l1 only once l2 left it", lambda: frr_es(d).get(r.esi2))
        appl_ok = wait_until(60, 3, 0, lambda: appl_mac_vteps(d, RHOST_MAC) == l1_only)
        asic_ok = wait_until(30, 3, 0, lambda: asic_mac_vteps(d, RHOST_MAC) == sorted(l1_only))
        assert appl_ok and asic_ok, (
            "after an fdbsyncd restart and l2 leaving ES2, {} still goes to APPL_DB VTEPs {} and ASIC {} (want {} "
            "only)".format(RHOST_MAC, sorted(appl_mac_vteps(d, RHOST_MAC)), asic_mac_vteps(d, RHOST_MAC),
                           sorted(l1_only)))
    finally:
        _cleanup()
    restore.finish()
