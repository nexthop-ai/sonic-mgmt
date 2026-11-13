"""EVPN multihoming: a protodown Ethernet segment member is down in the ASIC, so the dual-homed device moves its traffic
to the ES peer at once instead of waiting for its LACP timeout.

zebra holds ES members down by setting kernel protodown on them (`evpn mh startup-delay` after it starts, `evpn mh
uplink` tracking when every uplink is down). The DUT must turn that into a port down in the ASIC: the attached device
sees the link drop and takes it out of its bundle, and while the hold lasts the link cannot bundle again. Reboot and
config reload use the same hold (`evpn-mh-es-shutdown`) to take the ES members down before forwarding stops.

Rig: test_vxlan_evpn_mh.py's (mh_rig). The DUT and the FRR leaf l1 share ES1; its dual-homed host's bond0 has legs
hdut (through the DUT) and hl1 (through l1). The DUT is DF.

Cases:
  test_mh_uplink_down_protodown_holds_es_member
      zebra's uplink tracking on a dummy netdev the case creates (`evpn mh uplink`, then the netdev down) sets
      protodown on the ES member. Under a stream from H0 to the host: the member is down in the ASIC (APPL_DB
      admin and oper status down, STATE_DB protodown on, the kernel admin state kept), the host bond drops the DUT
      leg, l1 becomes DF, and the stream keeps flowing through l1 with loss bounded as for a member shutdown. The
      uplink back up clears the hold: the member rejoins only after LACP (no frame lost), the DUT is DF again.
  test_mh_es_shutdown_step
      `evpn-mh-es-shutdown` takes the ES member down the same way and returns once it is oper down; `--clear` brings
      it back. The step sets protodown with its own reason, which zebra's clear cannot undo.
  test_mh_time_to_ready_after_bgp_restart, test_mh_time_to_ready_after_cold_reboot
      measure, with a READY_HOLD_S startup delay, how long the DUT needs after zebra starts until it is ready to
      carry the ES: every BGP EVPN peer Established, the EVPN route counts (types 1, 2, 4) stable, a remote MAC behind
      ES2 on its bridge-port next hop group in ASIC_DB, and the bridge-port next hop groups back. The delay is set
      only in CONFIG_DB, so frrcfgd is its one source, and it is not the default: the member must stay held for it
      and rejoin within RELEASE_SLACK_S after zebra's start plus the delay, so one that did not reach zebra fails.
      The rig's FRR config lives only in FRR, so the case re-applies it, without the startup delay, as soon as FRR
      answers (a switch in production loads its own); the timeline logs when. Logged as "TIMELINE {...}".
      After the bgp restart, once the DUT has its EVPN state back but is still held, a stream from H0 reaches the
      host only through l1 with nothing lost: the ES peer carries the traffic while the restarted switch is held.

Needs a DUT whose front-panel netdevs accept protodown (the kernel refuses it otherwise and the member stays up: the
cases fail), the DUT side that folds protodown into the port's ASIC admin state, `evpn-mh-es-shutdown`, and
everything test_vxlan_evpn_mh.py needs, including the ES backup group (the stream's loss bound). A virtual switch's
netdevs do not take protodown, so the cases skip there.
"""

import json
import logging
import os
import time

import pytest

from tests.vxlan.vxlan_l2_helpers import Steps, db_hget, sh, wait_for, wait_state
from tests.vxlan.vxlan_evpn_l2_helpers import vtysh
from tests.vxlan.vxlan_evpn_mh_helpers import frr_es, lag_up
from tests.vxlan.test_vxlan_evpn_mh import (  # noqa: F401 (fixtures)
    CONVERGE,
    ES1_SYS_MAC,
    STREAM_FRAMES,
    LEG_DUT,
    LEG_L1,
    MH_HOST_MAC,
    RHOST_MAC,
    VLAN_A,
    UP_STREAM_FRAMES,
    access,
    asic_mac_vteps,
    bond_ready,
    es_backup_attached,
    es_stream,
    ignore_known_harmless,
    learn_mh_host,
    mh_rig,
    stream_ok,
    wait_mh_ready,
)

pytestmark = [
    pytest.mark.topology("t0", "t2", "lt2"),
]

logger = logging.getLogger(__name__)

UPLINK = "mhuplink0"        # dummy netdev the uplink-tracking case creates on the DUT
HOLD_S = 30                 # how long a hold must keep the member down
SONIC_REASON = "6"          # evpn-mh-es-shutdown's protodown reason bit
UP_MAX_LOST = 10            # the member rejoining: 0 expected (it only distributes after LACP sync)
HELD_MAX_LOST = 10         # a stream while the member is held: l1 carries it, 0 expected
READY_HOLD_S = 120         # the startup delay while time-to-ready is measured; FRR's and the boot hold's default is 180
RELEASE_SLACK_S = 30       # the member is back within this long after zebra's start + READY_HOLD_S
ROUTES_STABLE_S = 20       # route counts unchanged this long count as stable
SAMPLER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "files", "vxlan_evpn_mh_ready_sampler.py")
SAMPLER_DUT = "/tmp/vxlan_evpn_mh_ready_sampler.py"
SAMPLER_UNIT = "vxlan-evpn-mh-ready-sampler"
FRR_SAVED = "/host/vxlan_evpn_mh_frr.conf"
# zebra re-asserts its own protodown state on an ES member, which the kernel refuses while another holder's reason
# (here evpn-mh-es-shutdown's) is set
PROTODOWN_HARMLESS_LOG = [
    r".*zebra.*Cannot clear protodown, active reasons.*",
    r".*zebra.*netlink.*(EBUSY|Device or resource busy).*",
]


@pytest.fixture(autouse=True)
def ignore_protodown_harmless(duthost, loganalyzer):
    if loganalyzer:
        loganalyzer[duthost.hostname].ignore_regex.extend(PROTODOWN_HARMLESS_LOG)


def kernel_link(r):
    """(admin up, protodown, protodown reasons) of the ES member's netdev."""
    out = sh(r.duthost, "ip -d -j link show dev {}".format(r.es_port))
    link = json.loads(out)[0]
    return "UP" in link.get("flags", []), bool(link.get("proto_down")), [str(x) for x in link.get(
        "proto_down_reason", [])]


def member_state(r):
    d = r.duthost
    up, pd, reasons = kernel_link(r)
    return {"kernel_admin_up": up, "protodown": pd, "reasons": reasons,
            "state_db": db_hget(d, "STATE_DB", "PORT_TABLE|{}".format(r.es_port), "protodown"),
            "appl_admin": db_hget(d, "APPL_DB", "PORT_TABLE:{}".format(r.es_port), "admin_status"),
            "appl_oper": db_hget(d, "APPL_DB", "PORT_TABLE:{}".format(r.es_port), "oper_status"),
            "lag": lag_up(d, r.pc, r.es_port), "host_bond": r.leaves.host_bond("es1"),
            "l1_es1": r.leaves.es_detail(r.l1.name).get(r.esi1, {}).get("flags"),
            "dut_es1": frr_es(d).get(r.esi1, {}).get("flags")}


def held_down(r):
    """The member is down in the ASIC, the kernel keeps it admin up, and the host bond distributes on hl1 only."""
    s = member_state(r)
    legs = s["host_bond"]
    return s["protodown"] and s["state_db"] == "on" and s["appl_admin"] == "down" and s["appl_oper"] == "down" and \
        s["kernel_admin_up"] and not s["lag"] and LEG_L1 in legs and legs[LEG_L1]["state"] & 0x30 == 0x30 and \
        (LEG_DUT not in legs or legs[LEG_DUT]["state"] & 0x30 != 0x30)


def l1_df(r):
    flags = r.leaves.es_detail(r.l1.name).get(r.esi1, {}).get("flags", [])
    return bool(flags) and "nonDF" not in flags


def released(r):
    """Not protodown and up in the ASIC (an image without the fix publishes no STATE_DB protodown)."""
    s = member_state(r)
    return not s["protodown"] and s["state_db"] in ("off", "") and s["appl_admin"] == "up" and s["appl_oper"] == "up"


def check_held(r, what, problems):
    """The hold is in place within 10 s and lasts HOLD_S; l1 takes DF."""
    try:
        wait_state(10, 1, lambda: held_down(r), "{}: the ES member held down".format(what), lambda: member_state(r))
        wait_state(CONVERGE, 3, lambda: l1_df(r), "{}: l1 DF for ES1 while the DUT's member is held".format(what),
                   lambda: member_state(r))
        end = time.time() + HOLD_S
        while time.time() < end:
            if not held_down(r):
                problems.append("{}: the hold did not last {} s: {}".format(what, HOLD_S, member_state(r)))
                break
            time.sleep(3)
    except AssertionError as e:
        problems.append(str(e))


def check_rejoin(r, name, release, problems):
    """release() under a stream: the member rejoins with no frame lost, and the rig is ready with the DUT DF."""
    up = es_stream(r, name, release, UP_STREAM_FRAMES)
    if not (stream_ok(up, UP_MAX_LOST) and up["at_" + LEG_DUT] > 0):
        problems.append("{} (released): {}".format(name, up))
    try:
        wait_state(60, 2, lambda: released(r), "{}: the member released".format(name), lambda: member_state(r))
        wait_mh_ready(r, CONVERGE, when="after the hold of {} ended".format(name))
    except AssertionError as e:
        problems.append(str(e))


def skip_vs(r):
    if r.vs:
        pytest.skip("a virtual switch's front-panel netdevs do not take protodown; hardware runs it")


def test_mh_uplink_down_protodown_holds_es_member(mh_rig, ptfadapter):  # noqa: F811
    """zebra's uplink-down protodown holds the ES member down in the ASIC; the host moves to l1; release rejoins."""
    r = mh_rig
    skip_vs(r)
    d = r.duthost
    wait_mh_ready(r, CONVERGE, when="before the case")
    es_backup_attached(r)
    learn_mh_host(r, ptfadapter)
    before = kernel_link(r)
    assert before[0] and not before[1], "ES member {} admin {}, protodown {} before the case".format(
        r.es_port, before[0], before[1])
    _median, cap = r.loss_budget
    problems = []
    sh(d, "sudo ip link add {} type dummy && sudo ip link set {} up".format(UPLINK, UPLINK))
    try:
        vtysh(d, ["configure terminal", "interface {}".format(UPLINK), "evpn mh uplink", "end"])
        # one uplink, up: nothing held
        time.sleep(5)
        assert released(r) and not kernel_link(r)[1], "held with its uplink up: {}".format(member_state(r))

        down = es_stream(r, "pdup", lambda: sh(d, "sudo ip link set {} down".format(UPLINK)))
        if not (stream_ok(down, cap) and down["vx_l1"] > 0):
            problems.append("uplink down: stream {}".format(down))
        check_held(r, "uplink down", problems)
        reasons = kernel_link(r)[2]
        if not reasons or SONIC_REASON in reasons:
            problems.append("protodown reasons {}: expected zebra's only".format(reasons))
        check_rejoin(r, "pdupback", lambda: sh(d, "sudo ip link set {} up".format(UPLINK)), problems)
    finally:
        back = Steps("uplink-tracking cleanup")
        back("no evpn mh uplink", lambda: vtysh(
            d, ["configure terminal", "interface {}".format(UPLINK), "no evpn mh uplink", "end"], check=False))
        back("dummy uplink", lambda: sh(d, "sudo ip link del {}".format(UPLINK), check=False))
        back("ready", lambda: wait_mh_ready(r, CONVERGE, when="after the uplink-tracking case"))
    assert not back.errors and not problems, "uplink-down protodown: {}; cleanup: {}".format(problems, back.errors)


def dut_now(d):
    return float(sh(d, "date +%s.%N").strip())


def sampler(r, op, out=""):
    """A systemd unit, so it outlives the ssh session that starts it."""
    d = r.duthost
    sh(d, "sudo systemctl stop {u}; sudo systemctl reset-failed {u}; true".format(u=SAMPLER_UNIT), check=False)
    if op == "start":
        d.copy(src=SAMPLER, dest=SAMPLER_DUT, mode="0755")
        sh(d, "sudo systemd-run --unit {u} python3 {s} {m} {p} {o}".format(
            u=SAMPLER_UNIT, s=SAMPLER_DUT, m=RHOST_MAC, p=r.es_port, o=out))


def samples(r, out):
    rows = []
    for line in sh(r.duthost, "sudo cat {}".format(out), check=False).splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def frr_running_config(r):
    """The rig's FRR config without the startup delay, which only CONFIG_DB (frrcfgd) may set."""
    cfg = sh(r.duthost, "sudo vtysh -c 'show running-config'")
    lines = ["configure terminal"] + [ln for ln in cfg.splitlines()
                                      if not ln.startswith(("Building configuration", "Current configuration"))
                                      and "evpn mh startup-delay" not in ln]
    return "\n".join(lines + ["end"]) + "\n"


def frr_answers(d):
    """zebra and bgpd both answer vtysh."""
    res = d.shell("docker exec bgp vtysh -c 'show bgp summary json' -c 'show evpn json'", module_ignore_errors=True)
    return res["rc"] == 0 and "is not running" not in res.get("stdout", "") + res.get("stderr", "")


FRR_SKIP = ("!", "frr version", "frr defaults", "hostname", "log ", "service ", "password", "enable password")


def frr_reapply(d):
    """Replays the saved running-config in one vtysh session (one -c per line keeps the config nodes)."""
    lines = [ln.rstrip() for ln in sh(d, "sudo cat {}".format(FRR_SAVED)).splitlines()]
    cmds = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(FRR_SKIP)]
    neighbors = [c for c in cmds if c.startswith("neighbor ")][:1]
    for attempt in range(5):
        res = d.shell("sudo vtysh " + " ".join("-c '{}'".format(c) for c in cmds), module_ignore_errors=True)
        logger.info("FRR config re-applied (%d lines, attempt %d): rc %s %s", len(cmds), attempt, res["rc"],
                    (res.get("stderr", "") or res.get("stdout", ""))[-300:])
        if not neighbors or neighbors[0] in sh(d, "sudo vtysh -c 'show running-config'", check=False):
            return
        time.sleep(5)
    raise AssertionError("the rig's FRR config did not re-apply after the restart")


def remote_mac_ready(r):
    want = sorted({r.l1.vtep, r.l2.vtep})
    r.leaves.fdb("add", r.l1.name, RHOST_MAC, VLAN_A, "es2bond")
    # l1 also knows the dual-homed host (an attached router is never silent), so while the DUT is held its own
    # traffic to the host has the ES peer's route to follow
    r.leaves.fdb("add", r.l1.name, MH_HOST_MAC, VLAN_A, "es1bond")
    wait_state(CONVERGE, 3, lambda: asic_mac_vteps(r.duthost, RHOST_MAC) == want,
               "ASIC FDB {} on a bridge-port next hop group {}".format(RHOST_MAC, want),
               lambda: asic_mac_vteps(r.duthost, RHOST_MAC))


def zebra_started(rows, t_cmd):
    """When the zebra started after the restart command did, from the sampler's rows."""
    return next((x["zebra_start"] for x in rows if x.get("t", 0) >= t_cmd and x.get("zebra_start", 0) >= t_cmd - 5),
                None)


def timeline(rows, t_cmd, t_cfg, nhg_base):
    """When each readiness condition was first met after the restart (seconds after zebra started)."""
    after = [x for x in rows if x.get("t", 0) >= t_cmd]
    z = zebra_started(after, t_cmd)
    tl = {"zebra_start": z, "cmd": t_cmd, "frr_config": t_cfg, "samples": len(after),
          "first_sample": after[0]["t"] if after else None}

    def first(cond):
        x = next((x for x in after if cond(x)), None)
        return x["t"] if x else None
    tl["evpn_established"] = first(lambda x: x.get("evpn_peers", 0) > 0 and x.get("evpn_est") == x.get("evpn_peers"))
    last_change, prev, stable = None, None, False
    for x in after:
        if "routes" not in x:
            continue
        if x["routes"] != prev:
            last_change, prev = x["t"], x["routes"]
        elif x["t"] - last_change >= ROUTES_STABLE_S:
            stable = True
    tl["routes_stable"] = last_change if stable else None
    tl["routes"] = prev
    tl["remote_mac_on_nhg"] = first(lambda x: x.get("fdb") == 1 and
                                    x.get("fdb_bp") == "SAI_BRIDGE_PORT_TYPE_BRIDGE_PORT_NEXT_HOP_GROUP")
    tl["bp_nhg_back"] = first(lambda x: x.get("bp_nhg", 0) >= nhg_base)
    # released: up again after the member was last seen held, so a sample before the hold starts does not count
    held_t = [x["t"] for x in after if x.get("pd") == 1 and x["t"] > (t_cfg or t_cmd) + 5]
    tl["member_released"] = first(lambda x: x.get("pd") == 0 and x.get("oper") == "up" and
                                  held_t and x["t"] > held_t[-1])
    marks = [tl[k] for k in ("evpn_established", "routes_stable", "remote_mac_on_nhg", "bp_nhg_back")]
    tl["ready"] = max(marks) if None not in marks else None
    if z:
        for k in ("frr_config", "first_sample", "evpn_established", "routes_stable", "remote_mac_on_nhg",
                  "bp_nhg_back", "ready", "member_released"):
            if tl.get(k):
                tl[k + "_s"] = round(tl[k] - z, 1)
    return tl


def check_hold(rows, tl, problems, what):
    """Held (protodown, oper down) from 20 s after the rig's ES config is back until 15 s before the delay ends, then
    released within RELEASE_SLACK_S of its end. The delay runs from zebra's start (frrcfgd's delivery re-arms it),
    not from vtysh answering."""
    z = tl.get("zebra_start")
    if not z:
        problems.append("{}: zebra's start not seen: {}".format(what, tl))
        return
    start = max(z, tl.get("frr_config") or z) + 20
    window = [x for x in rows if start <= x.get("t", 0) <= z + READY_HOLD_S - 15 and "pd" in x]
    if not window or any(x.get("pd") != 1 or x.get("oper") != "down" for x in window):
        bad = [x for x in window if x.get("pd") != 1 or x.get("oper") != "down"][:3]
        problems.append("{}: the member was not held through the delay ({} samples; not held: {})".format(
            what, len(window), bad))
    if not tl.get("member_released"):
        problems.append("{}: the member did not come back after the delay: {}".format(what, tl))
    elif tl["member_released"] - z > READY_HOLD_S + RELEASE_SLACK_S:
        problems.append("{}: the member came back too late for a {} s delay: {}".format(what, READY_HOLD_S, tl))


def evpn_back_held(r):
    """Held, l1 DF, and the DUT sees ES1 on l1 again and ES2 on both leaves (its EVPN state is back)."""
    es = frr_es(r.duthost)
    e1, e2 = es.get(r.esi1) or {}, es.get(r.esi2) or {}
    return held_down(r) and l1_df(r) and "remote" in e1.get("flags", []) and \
        {v.get("vtep") for v in e2.get("vteps", [])} == {r.l1.vtep, r.l2.vtep}


def held_path_state(r):
    """Where the DUT sends the dual-homed host's MAC while its ES member is held (failure messages)."""
    d = r.duthost
    backup = db_hget(d, "APPL_DB", "EVPN_ES_BACKUP_NHG_TABLE:{}".format(r.pc), "nexthop_group")
    groups = {k: sh(d, "sonic-db-cli APPL_DB HGETALL '{}'".format(k), check=False).strip()
              for k in sh(d, "sonic-db-cli APPL_DB KEYS 'L2_NEXTHOP_GROUP_TABLE:*'", check=False).split()}
    fdb = sh(d, "sonic-db-cli APPL_DB HGETALL 'VXLAN_FDB_TABLE:{}:{}'".format("Vlan{}".format(VLAN_A), MH_HOST_MAC),
             check=False).strip()
    zebra = sh(d, "sudo vtysh -c 'show evpn mac vni all' | grep -i {}".format(MH_HOST_MAC), check=False).strip()
    return {"backup_nhg": backup, "l2_nhgs": groups, "vxlan_fdb": fdb, "asic_vteps": asic_mac_vteps(d, MH_HOST_MAC),
            "zebra_mac": zebra, "es": frr_es(d).get(r.esi1)}


def held_stream(r, problems):
    """While the restarted DUT is held, H0's stream reaches the host only through l1, nothing lost."""
    try:
        wait_state(READY_HOLD_S - 40, 3, lambda: evpn_back_held(r), "EVPN state back while the member is held",
                   lambda: member_state(r))
    except AssertionError as e:
        problems.append(str(e))
        return
    held = es_stream(r, "pdbgpheld", lambda: None, STREAM_FRAMES)
    if not (stream_ok(held, HELD_MAX_LOST) and held["vx_l1"] > 0 and held["at_" + LEG_DUT] == 0):
        problems.append("stream while held after the bgp restart: {}; DUT state: {}".format(held, held_path_state(r)))
    if not kernel_link(r)[1]:
        problems.append("the member was released before the held stream ended: {}".format(member_state(r)))


def measure_restart(r, kind, localhost):
    d = r.duthost
    wait_mh_ready(r, CONVERGE, when="before the case")
    es_backup_attached(r)
    remote_mac_ready(r)
    nhg_base = int(sh(d, "redis-cli -n 1 --scan --pattern 'ASIC_STATE:SAI_OBJECT_TYPE_NEXT_HOP_GROUP:*' | "
                         "xargs -r -n1 -I{} redis-cli -n 1 HGET {} SAI_NEXT_HOP_GROUP_ATTR_TYPE | "
                         "grep -c SAI_NEXT_HOP_GROUP_TYPE_BRIDGE_PORT", check=False).strip() or 0)
    out = "/host/vxlan_evpn_mh_ready_{}.jsonl".format(kind)
    d.copy(content=frr_running_config(r), dest=FRR_SAVED)
    problems, tl = [], {}
    backup = "/etc/sonic/config_db.json.vxlan_evpn_mh_bak"
    try:
        # frrcfgd renders EVPN_MH_GLOBAL when bgp starts; zebra only re-arms a delay that is still running
        sh(d, "sudo sonic-db-cli CONFIG_DB HSET 'EVPN_MH_GLOBAL|default' startup_delay {}".format(READY_HOLD_S))
        if kind == "reboot":
            sh(d, "sudo cp /etc/sonic/config_db.json {}".format(backup))
            sh(d, "sudo config save -y")
        sh(d, "sudo rm -f {}".format(out))
        sampler(r, "start", out)
        time.sleep(5)
        t_cmd = dut_now(d)
        if kind == "bgp":
            sh(d, "sudo systemctl restart bgp")
        else:
            d.shell("nohup sudo reboot >/dev/null 2>&1 &", module_ignore_errors=True)
            localhost.wait_for(host=d.mgmt_ip, port=22, state="stopped", delay=5, timeout=300)
            localhost.wait_for(host=d.mgmt_ip, port=22, state="started", delay=10, timeout=900)
        wait_for(600, 1, lambda: frr_answers(d), "FRR answering vtysh")
        t_cfg = dut_now(d)
        frr_reapply(d)
        if kind == "reboot":
            sampler(r, "start", out)
        if kind == "bgp":
            held_stream(r, problems)
        time.sleep(max(0, t_cfg + READY_HOLD_S + 60 - dut_now(d)))
        rows = samples(r, out)
        tl = timeline(rows, t_cmd, t_cfg, nhg_base)
        tl["kind"] = kind
        logger.info("TIMELINE %s", json.dumps(tl, sort_keys=True))
        check_hold(rows, tl, problems, kind)
        if not tl.get("ready"):
            problems.append("{}: not ready within the window: {}".format(kind, tl))
        try:
            wait_mh_ready(r, CONVERGE, when="after the {} and the startup delay".format(kind))
        except AssertionError as e:
            problems.append(str(e))
    finally:
        back = Steps("{} time-to-ready cleanup".format(kind))
        back("sampler", lambda: sampler(r, "stop"))
        back("startup delay 0", lambda: vtysh(
            d, ["configure terminal", "evpn mh startup-delay 0", "end"], check=False))
        back("CONFIG_DB delay 0", lambda: sh(
            d, "sudo sonic-db-cli CONFIG_DB HSET 'EVPN_MH_GLOBAL|default' startup_delay 0", check=False))
        if kind == "reboot":
            back("config_db.json", lambda: sh(d, "sudo test -f {b} && sudo mv {b} /etc/sonic/config_db.json".format(
                b=backup), check=False))
        back("ready", lambda: wait_mh_ready(r, CONVERGE, when="after the {} case".format(kind)))
    assert not back.errors and not problems, "time to ready after {}: {}; cleanup: {}; timeline {}".format(
        kind, problems, back.errors, tl)


@pytest.mark.disable_loganalyzer
def test_mh_time_to_ready_after_bgp_restart(mh_rig, ptfadapter, localhost):  # noqa: F811
    """After `systemctl restart bgp`: time to ready; the member held for the delay, then back."""
    skip_vs(mh_rig)
    measure_restart(mh_rig, "bgp", localhost)


@pytest.mark.disable_loganalyzer
def test_mh_time_to_ready_after_cold_reboot(mh_rig, ptfadapter, localhost):  # noqa: F811
    """After a cold reboot: time to ready; the member held (boot hold, then zebra) for the delay, then back."""
    skip_vs(mh_rig)
    measure_restart(mh_rig, "reboot", localhost)


def test_mh_es_shutdown_step(mh_rig, ptfadapter):  # noqa: F811
    """evpn-mh-es-shutdown holds the ES member down before it returns; --clear releases it."""
    r = mh_rig
    skip_vs(r)
    d = r.duthost
    if sh(d, "command -v evpn-mh-es-shutdown", check=False).strip() == "":
        pytest.fail("evpn-mh-es-shutdown is not on the DUT: reboot and config reload stop forwarding with the ES "
                    "members up")
    wait_mh_ready(r, CONVERGE, when="before the case")
    es_backup_attached(r)
    learn_mh_host(r, ptfadapter)
    _median, cap = r.loss_budget
    problems = []
    took = {}

    def step():
        t0 = time.time()
        sh(d, "sudo evpn-mh-es-shutdown")
        took["s"] = time.time() - t0
        took["after"] = member_state(r)

    try:
        down = es_stream(r, "pdstep", step)
        if not (stream_ok(down, cap) and down["vx_l1"] > 0):
            problems.append("shutdown step: stream {}".format(down))
        after = took.get("after", {})
        if after.get("appl_oper") != "down" or SONIC_REASON not in after.get("reasons", []):
            problems.append("the step returned ({} s) before the member was down with its reason: {}".format(
                round(took.get("s", -1), 2), after))
        check_held(r, "shutdown step", problems)
        check_rejoin(r, "pdstepback", lambda: sh(d, "sudo evpn-mh-es-shutdown --clear"), problems)
    finally:
        back = Steps("shutdown-step cleanup")
        back("clear", lambda: sh(d, "sudo evpn-mh-es-shutdown --clear", check=False))
        # a clear that failed must not leave the rig's ES member down for the cases after this one
        release = "sudo ip link set dev {p} protodown_reason {b} off; sudo ip link set dev {p} protodown off".format(
            p=r.es_port, b=SONIC_REASON)
        back("force release", lambda: sh(d, release, check=False))
        back("ready", lambda: wait_mh_ready(r, CONVERGE, when="after the shutdown-step case"))
    assert not back.errors and not problems, "evpn-mh-es-shutdown: {}; cleanup: {}".format(problems, back.errors)
    wait_for(10, 1, lambda: bond_ready(r.leaves.host_bond("es1"), (LEG_DUT, LEG_L1), ES1_SYS_MAC),
             "the host bond on both legs")
