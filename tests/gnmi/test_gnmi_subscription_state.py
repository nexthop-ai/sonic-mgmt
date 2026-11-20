# Copyright 2026 Nexthop Systems Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""gNMI subscription state, dial-in: the rows the gNMI server publishes to STATE_DB.

The server writes GNMI_SERVER_STATE|gnmi at start and refreshes it every
publish interval, writes one GNMI_SUBSCRIPTION row with its
GNMI_SUBSCRIPTION_PATH rows for every subscription it accepts, refreshes the
subscription counters on the same interval, and removes the rows when the
subscription ends. These tests open real subscriptions from the test host
with PygnmiClient and read the rows straight from STATE_DB; the CLI is not
involved. They check only the rows they caused and the change they caused
in the server's totals, so rows another collector left are ignored; the
totals are compared before and after, so no other collector may subscribe
while they run.
"""
import logging
import re

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.gnmi_substate import (
    DEFAULT_PUBLISH_INTERVAL,
    MIN_PUBLISH_INTERVAL,
    PROCESS_EXIT_TIMEOUT,
    SERVER_GNMI,
    SERVER_ROW_TIMEOUT,
    SERVER_TABLE,
    SUBSCRIPTION_TABLE,
    DutShell,
    PublishInterval,
    Subscribers,
    expect,
    int_field,
    path_row_keys,
    path_rows,
    publishes_server_row,
    pygnmi_conn,
    server_row,
    subscription_ids,
    subscription_row,
    subscription_rows,
    wait_for_field,
    wait_for_growth,
    wait_for_server_interval,
    wait_for_stamp_advance,
)
from tests.common.helpers.gnmi_utils import GNMIEnvironment, dump_gnmi_debug_state, gnmi_state_dumped
from tests.common.utilities import wait_until
from .helper import apply_cert_config, build_pygnmi_client, open_dut_ssh

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer,
    pytest.mark.usefixtures("setup_gnmi_ntp_client_server", "setup_gnmi_server"),
]

SERVER_KEY = "{}|{}".format(SERVER_TABLE, SERVER_GNMI)
# What the subscriptions read: entries every image has, switch or BMC.
TARGET = "CONFIG_DB"
PATHS = ("DEVICE_METADATA/localhost", "FEATURE/gnmi")
SAMPLE_INTERVAL_S = 1
# A SAMPLE interval under 30 s is refused on a YANG path.
YANG_SAMPLE_INTERVAL_S = 30
YANG_UPDATE_TIMEOUT = YANG_SAMPLE_INTERVAL_S * 2 + 15
# A subscription's rows can appear shortly after it is accepted, not in the
# same instant.
ROW_TIMEOUT = 30
# One tick at the 1 s interval the module runs at, with slack for the DUT.
TICK_TIMEOUT = 10
# The interval the change test moves to, and the gap that shows it moved off
# 1 s. An event between ticks (a connection opening or closing) also
# rewrites last_updated, so only the widest of the gaps has to reach it.
CHANGED_INTERVAL_S = 3
CHANGED_GAP_MIN = 2
CHANGED_TICKS = 2
# A POLL subscription that outlives the test; the teardown stops it.
POLL_COUNT, POLL_INTERVAL_S = 600, 1
SUBSCRIBE_TIMEOUT = 30
# A request the server refuses never answers; its subscriber gives up after this.
REFUSED_RESPONSE_SECONDS = 5


@pytest.fixture(scope="module")
def substate(request, duthosts, rand_one_dut_hostname, vrf_config):
    """The DUT, how to reach its gNMI server from the test host, and a 1 s
    publish interval for the whole module, put back as it was afterwards.
    Skips where the server does not publish its state. "duthost" reads the
    DUT over one ssh session, so a test may read it while a subscription is
    open; "ansible" is the duthost itself, for what must not run then."""
    ansible = duthosts[rand_one_dut_hostname]
    if ansible.is_supervisor_node() or ansible.is_multi_asic:
        pytest.skip("needs a single-ASIC DUT")
    ssh = open_dut_ssh(ansible)
    request.addfinalizer(ssh.close)
    duthost = DutShell(ansible, ssh)
    env = GNMIEnvironment(ansible, GNMIEnvironment.GNMI_MODE)
    if not publishes_server_row(duthost, SERVER_GNMI):
        pytest.skip("gNMI server on {} does not publish {}".format(duthost.hostname, SERVER_KEY))
    interval = PublishInterval(duthost)
    request.addfinalizer(interval.restore)  # registered first, so a failed check below still restores
    interval.set(MIN_PUBLISH_INTERVAL)
    pytest_assert(wait_for_server_interval(duthost, SERVER_GNMI, MIN_PUBLISH_INTERVAL),
                  "server row did not pick up state_publish_interval={}".format(MIN_PUBLISH_INTERVAL))
    host = vrf_config.get("dut_ip") or ansible.mgmt_ip
    return {"duthost": duthost, "ansible": ansible, "env": env, "interval": interval, "vrf": vrf_config.get("vrf"),
            "conn": pygnmi_conn(build_pygnmi_client(ansible), host)}


def front_panel_ports(duthost):
    """The DUT's port names in COUNTERS_DB, in port order, since port
    numbering differs between platforms."""
    res = duthost.shell("sonic-db-cli COUNTERS_DB HKEYS COUNTERS_PORT_NAME_MAP", module_ignore_errors=True)
    pytest_assert(res.get("rc", 1) == 0, "reading COUNTERS_PORT_NAME_MAP failed: {}".format(res.get("stderr", "")))
    names = [n.strip() for n in res["stdout_lines"] if re.match(r"^Ethernet\d+$", n.strip())]
    return sorted(names, key=lambda n: int(n[len("Ethernet"):]))


def state_path(i=0):
    return PATHS[i]


def oc_counters(port):
    """The OpenConfig counters path for port, as requested and as the path
    row renders it."""
    row = "/interfaces/interface[name={}]/state/counters".format(port)
    return "openconfig:/" + row, row


@pytest.fixture(scope="function")
def subs(substate):
    """The subscriptions one test opens. On teardown they are stopped and
    every row they caused must be gone, so a leak shows on its own test."""
    duthost = substate["duthost"]
    baseline = subscription_ids(duthost)
    path_baseline = path_row_keys(duthost)
    s = Subscribers(substate["conn"])
    s.baseline = baseline
    yield s
    s.stop_all()

    def left():
        return sorted(subscription_ids(duthost) - baseline) + sorted(path_row_keys(duthost) - path_baseline)

    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: not left()),
           lambda: "rows left after the test's subscriptions stopped: {}".format(left()))


def new_rows(duthost, subs):
    """The dial-in rows that appeared during this test."""
    return subscription_rows(duthost, subscription_ids(duthost) - subs.baseline)


def wait_for_new_rows(duthost, subs, count):
    """The test's own rows once exactly count of them exist."""
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: len(new_rows(duthost, subs)) == count),
           lambda: "expected {} new dial-in rows, have {}".format(count, new_rows(duthost, subs)))
    return new_rows(duthost, subs)


def start_sample(subs, paths, target=TARGET, interval=SAMPLE_INTERVAL_S):
    s = subs.start(paths=paths, target=target, sample_interval=interval)
    pytest_assert(s.wait_ready(SUBSCRIBE_TIMEOUT),
                  "no response to the subscription within {} s; subscriber log:\n{}".format(SUBSCRIBE_TIMEOUT, s.log()))
    pytest_assert(s.outcome() is None, "the subscription ended at once: {}".format(s.outcome()))
    return s


def gnmi_pid(sub):
    """The gNMI server's pid, None when it is not running. Any pidof result
    but "found" and "no such process" fails the test, so a failed read never
    stands for a stopped server."""
    env, duthost = sub["env"], sub["duthost"]
    res = duthost.shell("docker exec {} pidof {}".format(env.gnmi_container, env.gnmi_process),
                        module_ignore_errors=True)
    out = res["stdout"].strip()
    if res["rc"] == 0 and out:
        return int(out.split()[0])
    pytest_assert(res["rc"] == 1 and not out and not res["stderr"].strip(),
                  "reading the {} pid failed: {}".format(env.gnmi_process, res))
    return None


def dut_now(duthost):
    """The DUT's clock: the test host's is not kept in sync with it."""
    return int(duthost.shell("date +%s")["stdout"].strip())


def test_server_row(substate):
    """The server row describes this process: its pid, start time, listen
    address, VRF and interval, every field it should carry, and live counts
    that agree with the rows on the DUT."""
    duthost, env = substate["duthost"], substate["env"]
    row = server_row(duthost, SERVER_GNMI)
    for field in ("pid", "start_time", "last_updated", "publish_interval", "threshold", "listen_address",
                  "unix_socket", "vrf", "active_subscriptions", "admitted_subscriptions", "waiting_subscriptions",
                  "active_connections", "subscriptions_accepted", "subscriptions_refused", "subscriptions_rejected",
                  "subscriptions_closed", "subscriptions_failed", "failed_send", "failed_source", "failed_encode",
                  "failed_other"):
        pytest_assert(field in row, "server row has no {}: {}".format(field, row))
    # subscriptions_refused, admitted_subscriptions and waiting_subscriptions
    # are checked for presence only: they move at the server's admission
    # threshold, and driving them here would mean restarting the gNMI server
    # with a changed config.
    pid = gnmi_pid(substate)
    pytest_assert(pid and int_field(row, "pid") == pid,
                  "server row pid {} is not the {} process {}".format(row["pid"], env.gnmi_process, pid))
    pytest_assert(0 < int_field(row, "start_time") <= dut_now(duthost),
                  "start_time {} is not in the past".format(row["start_time"]))
    pytest_assert(row["listen_address"].endswith(":{}".format(env.gnmi_port)),
                  "listen_address {} does not carry port {}".format(row["listen_address"], env.gnmi_port))
    want_vrf = substate["vrf"] or "default"  # the server names the default VRF when none is configured
    pytest_assert(row["vrf"] == want_vrf, "vrf {!r}, want {!r}".format(row["vrf"], want_vrf))
    pytest_assert(row["publish_interval"] == str(MIN_PUBLISH_INTERVAL),
                  "publish_interval {}".format(row["publish_interval"]))
    # The count and the rows are read at different moments, so they are
    # compared until both reads agree.

    def counts_agree():
        return int_field(server_row(duthost, SERVER_GNMI), "active_subscriptions") == len(subscription_ids(duthost))
    expect(wait_until(TICK_TIMEOUT, 1, 0, counts_agree),
           lambda: "active_subscriptions {} does not match the {} dial-in rows".format(
               server_row(duthost, SERVER_GNMI).get("active_subscriptions"), len(subscription_ids(duthost))))


def test_server_row_heartbeat(substate):
    """last_updated is rewritten every publish interval: that stamp is what
    tells a live server from a dead one."""
    duthost = substate["duthost"]
    for _ in range(2):
        pytest_assert(wait_for_stamp_advance(duthost, SERVER_GNMI, TICK_TIMEOUT),
                      "last_updated did not advance within {} s at a {} s interval".format(
                          TICK_TIMEOUT, MIN_PUBLISH_INTERVAL))


def test_publish_interval_follows_config(substate):
    """A change to state_publish_interval takes effect in the running server,
    without a restart, and removing it restores the default."""
    duthost, interval = substate["duthost"], substate["interval"]
    before = server_row(duthost, SERVER_GNMI)
    try:
        interval.set(CHANGED_INTERVAL_S)
        pytest_assert(wait_for_server_interval(duthost, SERVER_GNMI, CHANGED_INTERVAL_S),
                      "server row did not move to a {} s interval".format(CHANGED_INTERVAL_S))
        stamps = [wait_for_stamp_advance(duthost, SERVER_GNMI, TICK_TIMEOUT)]
        pytest_assert(stamps[0], "no tick after the interval change")
        for _ in range(CHANGED_TICKS):
            # Each wait starts from the stamp the last one saw, so no tick
            # is skipped between them.
            stamps.append(wait_for_stamp_advance(duthost, SERVER_GNMI, TICK_TIMEOUT, after=stamps[-1]))
            pytest_assert(stamps[-1], "no further tick at the {} s interval".format(CHANGED_INTERVAL_S))
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        pytest_assert(max(gaps) >= CHANGED_GAP_MIN,
                      "ticks {} s apart at a {} s interval".format(gaps, CHANGED_INTERVAL_S))

        interval.clear()
        pytest_assert(wait_for_server_interval(duthost, SERVER_GNMI, DEFAULT_PUBLISH_INTERVAL),
                      "server row did not fall back to the {} s default".format(DEFAULT_PUBLISH_INTERVAL))
        after = server_row(duthost, SERVER_GNMI)
        pytest_assert(after["pid"] == before["pid"] and after["start_time"] == before["start_time"],
                      "the server restarted to apply the interval: {} -> {}".format(
                          (before["pid"], before["start_time"]), (after["pid"], after["start_time"])))
    finally:
        interval.set(MIN_PUBLISH_INTERVAL)
        pytest_assert(wait_for_server_interval(duthost, SERVER_GNMI, MIN_PUBLISH_INTERVAL),
                      "server row did not return to the {} s interval".format(MIN_PUBLISH_INTERVAL))


def test_publish_interval_restore(substate):
    """PublishInterval.restore() puts back the value it found, and removes
    the field when there was none, so a run leaves an operator's setting as
    it was."""
    duthost, module_interval = substate["duthost"], substate["interval"]

    def current():
        res = duthost.shell("sonic-db-cli CONFIG_DB HGET 'GNMI|gnmi' state_publish_interval", module_ignore_errors=True)
        pytest_assert(res.get("rc", 1) == 0, "reading state_publish_interval failed: {}".format(res))
        return res["stdout"].strip()
    try:
        # A value set before: the module's MIN_PUBLISH_INTERVAL stands in for an operator's.
        operator = PublishInterval(duthost)
        pytest_assert(operator.saved == str(MIN_PUBLISH_INTERVAL), "saved {!r}".format(operator.saved))
        operator.set(CHANGED_INTERVAL_S)
        operator.restore()
        expect(current() == str(MIN_PUBLISH_INTERVAL),
               lambda: "restore() left {!r}, want {}".format(current(), MIN_PUBLISH_INTERVAL))
        # No value before: restore() removes the field.
        module_interval.clear()
        absent = PublishInterval(duthost)
        pytest_assert(absent.saved == "", "saved {!r} for an absent field".format(absent.saved))
        absent.set(CHANGED_INTERVAL_S)
        absent.restore()
        expect(current() == "", lambda: "restore() left {!r}, want no field".format(current()))
    finally:
        module_interval.set(MIN_PUBLISH_INTERVAL)
        pytest_assert(wait_for_server_interval(duthost, SERVER_GNMI, MIN_PUBLISH_INTERVAL),
                      "server row did not return to the {} s interval".format(MIN_PUBLISH_INTERVAL))


def test_sample_subscription_row(substate, subs):
    """A STREAM/SAMPLE subscription gets a row and a path row at accept, its
    counters move on the tick, the server totals follow, and closing the
    stream removes the rows."""
    duthost = substate["duthost"]
    totals = server_row(duthost, SERVER_GNMI)
    accepted, closed = int_field(totals, "subscriptions_accepted"), int_field(totals, "subscriptions_closed")
    active = int_field(totals, "active_subscriptions")

    path = state_path()
    s = start_sample(subs, [path])
    (sid, row), = wait_for_new_rows(duthost, subs, 1).items()
    pytest_assert(sid.isdigit(), "dial-in id {!r} is not the server's numeric client id".format(sid))
    for field, want in (("direction", "dial-in"), ("server", SERVER_GNMI), ("target", TARGET),
                        ("origin", ""), ("list_mode", "STREAM"), ("encoding", "JSON_IETF")):
        pytest_assert(row.get(field) == want, "{} = {!r}, want {!r}: {}".format(field, row.get(field), want, row))
    pytest_assert(":" in row.get("peer", ""), "peer {!r} is not address:port".format(row.get("peer")))
    pytest_assert(":" in row.get("local_address", ""),
                  "local_address {!r} is not address:port".format(row.get("local_address")))
    pytest_assert(0 < int_field(row, "start_time") <= dut_now(duthost), "start_time {!r}".format(row.get("start_time")))
    pytest_assert("user" in row, "row has no user field: {}".format(row))

    paths = path_rows(duthost, sid)
    pytest_assert(list(paths) == [0], "path rows for {}: {}".format(sid, paths))
    # A database target renders first in the path, as the collector addressed it.
    for field, want in (("path", "{}/{}".format(TARGET, path)), ("mode", "SAMPLE"),
                        ("sample_interval_ns", str(SAMPLE_INTERVAL_S * 10 ** 9)), ("suppress_redundant", "false")):
        pytest_assert(paths[0].get(field) == want,
                      "path row {} = {!r}, want {!r}: {}".format(field, paths[0].get(field), want, paths[0]))

    expect(wait_for_field(duthost, SERVER_KEY, "subscriptions_accepted", accepted + 1, timeout=TICK_TIMEOUT),
           lambda: "subscriptions_accepted did not move from {}: {}".format(accepted, server_row(duthost, SERVER_GNMI)))
    pytest_assert(wait_for_field(duthost, SERVER_KEY, "active_subscriptions", active + 1, timeout=TICK_TIMEOUT),
                  "active_subscriptions did not move from {}".format(active))

    key = "{}|{}".format(SUBSCRIPTION_TABLE, sid)
    pytest_assert(wait_for_growth(duthost, key, "tx_msgs", TICK_TIMEOUT * 2), "tx_msgs did not grow across a tick")
    pytest_assert(wait_for_growth(duthost, key, "tx_bytes", TICK_TIMEOUT * 2), "tx_bytes did not grow across a tick")
    row = subscription_row(duthost, sid)
    pytest_assert(int_field(row, "rx_msgs") >= 1 and int_field(row, "rx_bytes") > 0,
                  "the SubscribeRequest was not counted: {}".format(row))
    pytest_assert(all(f in row for f in ("last_updated", "error_count", "queue_depth")),
                  "tick fields missing: {}".format(row))

    s.stop()
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: not subscription_row(duthost, sid)),
           lambda: "row survived the close: {}".format(subscription_row(duthost, sid)))
    expect(not path_rows(duthost, sid), lambda: "path rows survived the close: {}".format(path_rows(duthost, sid)))
    expect(wait_for_field(duthost, SERVER_KEY, "subscriptions_closed", closed + 1, timeout=TICK_TIMEOUT),
           lambda: "subscriptions_closed did not move: {}".format(server_row(duthost, SERVER_GNMI)))
    pytest_assert(wait_for_field(duthost, SERVER_KEY, "active_subscriptions", active, timeout=TICK_TIMEOUT),
                  "active_subscriptions did not return to {}".format(active))


def test_two_paths_one_subscription(substate, subs):
    """One subscription with two paths is one row with two path rows, in request order."""
    duthost = substate["duthost"]
    requested = [state_path(0), state_path(1)]
    start_sample(subs, requested)
    (sid, _), = wait_for_new_rows(duthost, subs, 1).items()
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: len(path_rows(duthost, sid)) == 2),
           lambda: "path rows for {}: {}".format(sid, path_rows(duthost, sid)))
    paths = path_rows(duthost, sid)
    want = ["{}/{}".format(TARGET, p) for p in requested]
    pytest_assert([paths[0]["path"], paths[1]["path"]] == want, "path rows out of order: {}".format(paths))


def test_two_clients_two_rows(substate, subs):
    """Two connections are two rows with distinct ids and peers, and two more
    active connections on the server row; closing one removes exactly its
    row and leaves the other's as it was."""
    duthost = substate["duthost"]
    connections = int_field(server_row(duthost, SERVER_GNMI), "active_connections")
    first = start_sample(subs, [state_path()])
    (first_id, first_row), = wait_for_new_rows(duthost, subs, 1).items()
    start_sample(subs, [state_path()])
    rows = wait_for_new_rows(duthost, subs, 2)
    (second_id, second_row), = [(k, v) for k, v in rows.items() if k != first_id]
    pytest_assert(first_row["peer"] != second_row["peer"],
                  "two connections share a peer: {} / {}".format(first_row["peer"], second_row["peer"]))
    expect(wait_for_field(duthost, SERVER_KEY, "active_connections", lambda v: int(v) >= connections + 2,
                          timeout=TICK_TIMEOUT),
           lambda: "active_connections {} with two clients open, was {}".format(
               server_row(duthost, SERVER_GNMI).get("active_connections"), connections))

    with_both = int_field(server_row(duthost, SERVER_GNMI), "active_connections")
    first.stop()
    expect(wait_for_field(duthost, SERVER_KEY, "active_connections", lambda v: int(v) <= with_both - 1,
                          timeout=TICK_TIMEOUT),
           lambda: "active_connections {} after closing one of two clients, was {}".format(
               server_row(duthost, SERVER_GNMI).get("active_connections"), with_both))
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: set(new_rows(duthost, subs)) == {second_id}),
           lambda: "after closing {}, rows are {}, want only {}".format(
               first_id, sorted(new_rows(duthost, subs)), second_id))
    survivor = new_rows(duthost, subs)[second_id]
    for field in ("peer", "start_time", "list_mode", "target"):
        before, after = second_row.get(field), survivor.get(field)
        pytest_assert(after == before, "the surviving row's {} changed: {!r} -> {!r}".format(field, before, after))


def test_poll_subscription_row(substate, subs):
    """A POLL subscription from a remote collector is a row with list_mode
    POLL whose path row carries the path and an empty mode, since SAMPLE and
    ON_CHANGE are STREAM notions."""
    duthost, path = substate["duthost"], state_path()
    s = subs.start(paths=[path], target=TARGET, mode="poll", poll_count=POLL_COUNT,
                   poll_interval=POLL_INTERVAL_S)
    pytest_assert(s.wait_ready(SUBSCRIBE_TIMEOUT) and s.outcome() is None,
                  "the POLL subscription did not start or ended at once: {}; subscriber log:\n{}".format(
                      s.outcome(), s.log()))
    (sid, row), = wait_for_new_rows(duthost, subs, 1).items()
    pytest_assert(row.get("list_mode") == "POLL", "list_mode {!r}: {}".format(row.get("list_mode"), row))
    peer_host = row.get("peer", "").rsplit(":", 1)[0].strip("[]")
    pytest_assert(":" in row.get("peer", "") and peer_host != substate["conn"]["host"],
                  "peer {!r} is not a remote collector's address:port (the DUT is {})".format(
                      row.get("peer"), substate["conn"]["host"]))
    paths = path_rows(duthost, sid)
    pytest_assert(paths and paths[0].get("path") == "{}/{}".format(TARGET, path)
                  and paths[0].get("mode") == "" and "sample_interval_ns" not in paths[0],
                  "POLL path row: {}; subscriber log:\n{}".format(paths, s.log()))


def test_failed_subscription_counted(substate, subs):
    """A subscription the data client refuses after the server accepted it
    (a SAMPLE interval under the minimum) counts as failed under
    failed_other, and leaves no row."""
    duthost = substate["duthost"]
    before = server_row(duthost, SERVER_GNMI)
    # The refusal reaches the subscriber only when its first-response window
    # ends, so the outcome is read from the server's count.
    s = subs.start(paths=[state_path()], target=TARGET, sample_interval=0.1,
                   first_response_seconds=REFUSED_RESPONSE_SECONDS)

    def counted():
        row = server_row(duthost, SERVER_GNMI)
        return all(int_field(row, f) == int_field(before, f) + 1 for f in ("subscriptions_failed", "failed_other"))
    expect(wait_until(TICK_TIMEOUT, 1, 0, counted),
           lambda: "the refused subscription was not counted: before {}, now {}; subscriber log:\n{}".format(
               before, server_row(duthost, SERVER_GNMI), s.log()))
    after = server_row(duthost, SERVER_GNMI)
    for field in ("failed_send", "failed_source", "failed_encode"):
        pytest_assert(int_field(after, field) == int_field(before, field),
                      "{} moved from {} to {}".format(field, before.get(field), after.get(field)))
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: not new_rows(duthost, subs)),
           lambda: "the failed subscription left rows: {}".format(new_rows(duthost, subs)))


def test_rejected_subscriptions_leave_no_row(substate, subs):
    """A subscription the server cannot serve, a SHOW target or an unknown
    origin, counts as rejected and writes no row."""
    duthost = substate["duthost"]
    totals = server_row(duthost, SERVER_GNMI)
    rejected, accepted = int_field(totals, "subscriptions_rejected"), int_field(totals, "subscriptions_accepted")

    # A refusal reaches the subscriber only when its first-response window
    # ends, so it is read from the server's count, one request at a time.
    for kwargs in ({"paths": ["reboot-cause"], "target": "SHOW"},
                   {"paths": ["no-such-origin://" + state_path()], "target": TARGET}):
        s = subs.start(sample_interval=SAMPLE_INTERVAL_S, first_response_seconds=REFUSED_RESPONSE_SECONDS, **kwargs)
        rejected += 1
        expect(wait_for_field(duthost, SERVER_KEY, "subscriptions_rejected", rejected, timeout=ROW_TIMEOUT),
               lambda: "{} was not counted as rejected: subscriptions_rejected {}; subscriber log:\n{}".format(
                   kwargs, server_row(duthost, SERVER_GNMI).get("subscriptions_rejected"), s.log()))
        s.stop()
    pytest_assert(int_field(server_row(duthost, SERVER_GNMI), "subscriptions_accepted") == accepted,
                  "a rejected subscription was counted as accepted")
    expect(not new_rows(duthost, subs),
           lambda: "a rejected subscription left a row: {}".format(new_rows(duthost, subs)))


def test_openconfig_subscription_row(substate, subs):
    """A YANG subscription is recorded with its origin and the path rendered
    with its keys, the way the request carried it."""
    duthost = substate["duthost"]
    ports = front_panel_ports(duthost)
    if not ports:
        pytest.skip("needs a front-panel port in COUNTERS_PORT_NAME_MAP")
    oc_path, oc_row = oc_counters(ports[0])
    start_sample(subs, [oc_path], target=None, interval=YANG_SAMPLE_INTERVAL_S)
    (sid, row), = wait_for_new_rows(duthost, subs, 1).items()
    pytest_assert(row.get("origin") == "openconfig", "origin {!r}: {}".format(row.get("origin"), row))
    paths = path_rows(duthost, sid)
    pytest_assert(paths and paths[0].get("path") == oc_row and paths[0].get("mode") == "SAMPLE"
                  and paths[0].get("sample_interval_ns") == str(YANG_SAMPLE_INTERVAL_S * 10 ** 9),
                  "YANG path row: {}".format(paths))
    # The first tick may only count the initial response; from the next one
    # on, growth is a sample sent on the 30 s interval.
    key = "{}|{}".format(SUBSCRIPTION_TABLE, sid)
    pytest_assert(wait_until(TICK_TIMEOUT * 2, 1, 0,
                             lambda: int_field(subscription_row(duthost, sid), "tx_msgs") >= 1),
                  "the initial response was never counted in tx_msgs")
    pytest_assert(wait_for_growth(duthost, key, "tx_msgs", YANG_UPDATE_TIMEOUT),
                  "no sample on the YANG subscription within {} s".format(YANG_UPDATE_TIMEOUT))


def test_rows_left_by_a_killed_server_are_flushed_at_start(substate, subs):
    """A server killed outright leaves its rows behind, and the next start
    removes them and writes a server row for the new process. (On SIGTERM the
    close of each stream may or may not land before the publisher stops, so
    only SIGKILL leaves the rows for certain.)"""
    duthost, env = substate["duthost"], substate["env"]
    start_sample(subs, [state_path()])
    (sid, _), = wait_for_new_rows(duthost, subs, 1).items()
    old_pid = gnmi_pid(substate)
    pytest_assert(old_pid, "could not read the {} pid".format(env.gnmi_process))
    old_start = int_field(server_row(duthost, SERVER_GNMI), "start_time")

    logger.info("killing %s (pid %d) with SIGKILL", env.gnmi_process, old_pid)
    duthost.shell("docker exec {} pkill -KILL -x {}".format(env.gnmi_container, env.gnmi_process),
                  module_ignore_errors=True)
    try:
        pytest_assert(wait_until(PROCESS_EXIT_TIMEOUT, 1, 0, lambda: gnmi_pid(substate) is None),
                      "{} did not stop".format(env.gnmi_process))
        pytest_assert(subscription_row(duthost, sid), "the dead server's row is already gone")
        pytest_assert(path_rows(duthost, sid), "the dead server's path rows are already gone")
    except (Exception, pytest.fail.Exception) as e:
        # Dump now: the restart below truncates the killed server's log.
        subs.stop_all()
        if not gnmi_state_dumped(e):
            dump_gnmi_debug_state(substate["ansible"], error=e)
        raise
    finally:
        # Closed before Ansible runs: it forks this process, which an open
        # gRPC channel does not survive.
        subs.stop_all()
        logger.info("starting %s again", env.gnmi_process)
        apply_cert_config(substate["ansible"], substate["vrf"])

    pytest_assert(wait_until(SERVER_ROW_TIMEOUT, 2, 0,
                             lambda: int_field(server_row(duthost, SERVER_GNMI), "pid") not in (0, old_pid)),
                  "server row still shows pid {} after the restart".format(old_pid))
    row = server_row(duthost, SERVER_GNMI)
    pytest_assert(int_field(row, "pid") == gnmi_pid(substate), "server row pid {} is not the new process".format(
        row["pid"]))
    pytest_assert(int_field(row, "start_time") >= old_start, "start_time went backwards: {}".format(row))
    pytest_assert(int_field(row, "subscriptions_accepted") == 0, "totals carried over the restart: {}".format(row))
    pytest_assert(wait_until(ROW_TIMEOUT, 1, 0, lambda: not subscription_row(duthost, sid)),
                  "the dead server's row survived the start")
    pytest_assert(not path_rows(duthost, sid), "the dead server's path rows survived the start")
    pytest_assert(wait_for_server_interval(duthost, SERVER_GNMI, MIN_PUBLISH_INTERVAL),
                  "the restarted server did not read state_publish_interval")
