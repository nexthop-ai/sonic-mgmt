# Copyright 2026 Nexthop Systems Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""gNMI subscription state, dial-out: the rows the dial-out client publishes to STATE_DB.

The dial-out client writes GNMI_SERVER_STATE|dialout at start and one
GNMI_SUBSCRIPTION|dialout_<name> row for every TELEMETRY_CLIENT Subscription
entry, for as long as the entry exists. The row's state says whether the
collector is reachable: connecting while the client dials, connected while a
stream is up, misconfigured when the entry cannot be served as written. An
entry the client serves has its path rows too; a misconfigured one has only
its row. These tests configure real entries against a
collector on the PTF host and read the rows straight from STATE_DB.
"""
import logging
import time

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.gnmi_dialout import OWNED_KEYS, TelemetryClientConfig, dialout_running
from tests.common.helpers.gnmi_substate import (
    DIALOUT_ID_PREFIX,
    MIN_PUBLISH_INTERVAL,
    PROCESS_EXIT_TIMEOUT,
    SERVER_DIALOUT,
    SERVER_ROW_TIMEOUT,
    SERVER_TABLE,
    SUBSCRIPTION_TABLE,
    PublishInterval,
    delete_config_keys,
    expect,
    int_field,
    path_rows,
    publishes_server_row,
    server_row,
    subscription_ids,
    subscription_row,
    subscription_rows,
    wait_for_field,
    wait_for_growth,
    wait_for_server_interval,
)
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer,
]

SERVER_KEY = "%s|%s" % (SERVER_TABLE, SERVER_DIALOUT)
FAILED_FIELDS = ("failed_connect", "failed_send", "failed_source", "failed_encode", "failed_other")
NAME = "NHTEST"
SUB_ID = DIALOUT_ID_PREFIX + NAME
# What the entries subscribe to: an entry every image has, switch or BMC.
TARGET = "CONFIG_DB"
PATH = "DEVICE_METADATA/localhost"
# A second entry, configured only by the restart test.
GONE_NAME = "NHTEST_GONE"
GONE_ID = DIALOUT_ID_PREFIX + GONE_NAME
SUB_KEY = "%s|%s" % (SUBSCRIPTION_TABLE, SUB_ID)
REPORT_INTERVAL_MS = 2000
RETRY_INTERVAL_S = 2
ROW_TIMEOUT = 30
TICK_TIMEOUT = 10
CONNECT_TIMEOUT = 60


@pytest.fixture(scope="module")
def dialout_substate(request, dialout_setup):
    """The dial-out server row must exist, and the module runs at a 1 s publish
    interval, put back as it was afterwards (the field is shared with the
    gNMI server). Skips on an image whose client does not publish its state."""
    duthost = dialout_setup["duthost"]
    if not publishes_server_row(duthost, SERVER_DIALOUT):
        pytest.skip("dial-out client on %s does not publish %s" % (duthost.hostname, SERVER_KEY))
    interval = PublishInterval(duthost)
    request.addfinalizer(interval.restore)  # registered first, so a failed check below still restores
    interval.set(MIN_PUBLISH_INTERVAL)
    pytest_assert(wait_for_server_interval(duthost, SERVER_DIALOUT, MIN_PUBLISH_INTERVAL),
                  "dialout row did not pick up state_publish_interval=%d" % MIN_PUBLISH_INTERVAL)
    return dialout_setup


def own_row(duthost):
    """This module's row, dialout_NHTEST, or None."""
    return subscription_row(duthost, SUB_ID) or None


def dialout_rows(duthost):
    """Every dial-out subscription row, for failure messages."""
    return subscription_rows(duthost, subscription_ids(duthost, dialout=True))


@pytest.fixture(scope="function", autouse=True)
def no_rows_left(dialout_substate, telemetry_client_config):
    """Every test ends with its entry removed, so its row must be gone."""
    yield
    duthost = dialout_substate["duthost"]
    delete_config_keys(duthost, telemetry_client_config, telemetry_client_config.owned_keys)
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: own_row(duthost) is None and not path_rows(duthost, SUB_ID)),
           lambda: "%s left after the config was removed: %s" % (SUB_ID, own_row(duthost)))


def configure(config, dst_addrs, paths=PATH, report_type="stream", with_group=True):
    """The Global, DestinationGroup and Subscription rows for the NHTEST entry."""
    logger.info("configuring %s: dst %s, paths %s, report %s, dst_group %s", NAME, dst_addrs, paths, report_type,
                with_group)
    config.hset("Global", retry_interval=RETRY_INTERVAL_S)
    config.hset("DestinationGroup_%s" % NAME, dst_addr=",".join(dst_addrs))
    fields = {"path_target": TARGET, "paths": paths, "report_type": report_type,
              "report_interval": REPORT_INTERVAL_MS}
    if with_group:
        fields["dst_group"] = NAME
    config.hset("Subscription_%s" % NAME, **fields)


def dialout_pid(sub):
    out = sub["duthost"].shell("docker exec %s supervisorctl pid dialout" % sub["container"],
                               module_ignore_errors=True)["stdout"].strip()
    return int(out) if out.isdigit() and int(out) > 0 else None


def wait_for_own_row(duthost):
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: own_row(duthost) is not None),
           lambda: "no %s row: %s" % (SUB_ID, dialout_rows(duthost)))
    return own_row(duthost)


def wait_for_state(duthost, state, timeout=CONNECT_TIMEOUT):
    return wait_for_field(duthost, SUB_KEY, "state", state, timeout=timeout)


def test_dialout_server_row(dialout_substate):
    """The dialout row names the client process and counts its entries; with
    no entry configured both counts are zero, and the client, freshly
    restarted by the module setup, has seen no failure."""
    duthost = dialout_substate["duthost"]
    row = server_row(duthost, SERVER_DIALOUT)
    for field in ("pid", "start_time", "last_updated", "publish_interval", "configured_subscriptions",
                  "connected_subscriptions", "failed_connect", "failed_send", "failed_source", "failed_encode",
                  "failed_other"):
        pytest_assert(field in row, "dialout row has no %s: %s" % (field, row))
    pid = dialout_pid(dialout_substate)
    pytest_assert(pid and int_field(row, "pid") == pid,
                  "dialout row pid %s is not the dialout program %s" % (row["pid"], pid))
    # The DUT's own clock: the test host's is not kept in sync with it here.
    now = int(duthost.shell("date +%s")["stdout"].strip())
    pytest_assert(0 < int_field(row, "start_time") <= now, "start_time %r, DUT clock %d" % (row.get("start_time"), now))
    pytest_assert(int_field(row, "configured_subscriptions") == 0 and int_field(row, "connected_subscriptions") == 0,
                  "counts with no entry: %s" % row)
    pytest_assert(row.get("listen_address") == "" and row.get("vrf") == "", "a client has no listener: %s" % row)
    for field in FAILED_FIELDS:
        pytest_assert(int_field(row, field) == 0, "%s = %s on a fresh client" % (field, row[field]))
    before = int_field(row, "last_updated")
    pytest_assert(wait_until(TICK_TIMEOUT, 1, 0,
                             lambda: int_field(server_row(duthost, SERVER_DIALOUT), "last_updated") > before),
                  "dialout row last_updated did not advance")


def test_dialout_connected_row(dialout_substate, telemetry_client_config, start_collector):
    """A periodic entry pointed at a reachable collector is a row that reaches
    connected, names the collector as its peer, carries its path as a SAMPLE
    at the report interval, and whose tx counters move on the tick; the
    dialout row counts it."""
    duthost = dialout_substate["duthost"]
    collector = start_collector()
    configure(telemetry_client_config, [collector.address], report_type="periodic")

    row = wait_for_own_row(duthost)
    for field, want in (("direction", "dial-out"), ("server", SERVER_DIALOUT), ("destination_group", NAME),
                        ("target", TARGET), ("list_mode", "STREAM"), ("prefix", ""), ("origin", "")):
        pytest_assert(row.get(field) == want, "%s = %r, want %r: %s" % (field, row.get(field), want, row))
    pytest_assert(row.get("state") in ("connecting", "connected"), "state at accept: %s" % row)
    pytest_assert("encoding" in row and "connect_attempts" in row, "fields missing: %s" % row)
    expect(wait_for_field(duthost, SERVER_KEY, "configured_subscriptions", 1, timeout=TICK_TIMEOUT),
           lambda: "configured_subscriptions: %s" % server_row(duthost, SERVER_DIALOUT))

    expect(wait_for_state(duthost, "connected"),
           lambda: "entry never reached connected: %s" % dialout_rows(duthost))
    row = own_row(duthost)
    pytest_assert(row.get("peer") == collector.address,
                  "peer %r, want the collector %s" % (row.get("peer"), collector.address))
    pytest_assert(row.get("last_error", "") == "", "last_error set on a healthy entry: %s" % row)
    pytest_assert(int_field(row, "connect_attempts") >= 1, "connect_attempts: %s" % row)
    paths = path_rows(duthost, SUB_ID)
    # The target renders first in the path; a periodic report is a SAMPLE at the report interval.
    pytest_assert(paths and paths[0].get("path") == "%s/%s" % (TARGET, PATH)
                  and paths[0].get("mode") == "SAMPLE"
                  and paths[0].get("sample_interval_ns") == str(REPORT_INTERVAL_MS * 1000000),
                  "path rows: %s" % paths)
    expect(wait_for_field(duthost, SERVER_KEY, "connected_subscriptions", 1, timeout=TICK_TIMEOUT),
           lambda: "connected_subscriptions: %s" % server_row(duthost, SERVER_DIALOUT))
    pytest_assert(wait_for_growth(duthost, SUB_KEY, "tx_msgs", TICK_TIMEOUT * 2), "tx_msgs did not grow")
    pytest_assert(wait_for_growth(duthost, SUB_KEY, "tx_bytes", TICK_TIMEOUT * 2), "tx_bytes did not grow")
    row = own_row(duthost)
    pytest_assert("rx_msgs" not in row and "rx_bytes" not in row,
                  "a dial-out row carries rx counters, but the stream is one way: %s" % row)
    pytest_assert(collector.count(kind="update") >= 1, "the collector saw no updates while the row said connected")


def test_dialout_unreachable_collector(dialout_substate, telemetry_client_config, start_collector):
    """An entry whose collector refuses the connection stays connecting with
    the error and a climbing attempt count, and the dialout row counts the
    failed dials."""
    duthost = dialout_substate["duthost"]
    gone = start_collector()
    gone.stop()  # the port is never reused, so nothing answers at this address
    failed = int_field(server_row(duthost, SERVER_DIALOUT), "failed_connect")
    configure(telemetry_client_config, [gone.address])

    wait_for_own_row(duthost)
    expect(wait_for_field(duthost, SUB_KEY, "last_error", lambda e: e != "", timeout=CONNECT_TIMEOUT),
           lambda: "no last_error on an unreachable collector: %s" % dialout_rows(duthost))
    row = own_row(duthost)
    pytest_assert(row.get("state") == "connecting", "state %r with the collector down: %s" % (row.get("state"), row))
    pytest_assert(row.get("peer") == gone.address, "peer %r, want %s" % (row.get("peer"), gone.address))
    pytest_assert(wait_for_growth(duthost, SUB_KEY, "connect_attempts", RETRY_INTERVAL_S * 4 + TICK_TIMEOUT),
                  "connect_attempts did not climb across retries")
    expect(wait_for_field(duthost, SUB_KEY, "error_count", lambda v: int(v) >= 1, timeout=TICK_TIMEOUT),
           lambda: "error_count does not count the failed dials: %s" % own_row(duthost))
    expect(wait_for_field(duthost, SERVER_KEY, "failed_connect", lambda v: int(v) > failed,
                          timeout=TICK_TIMEOUT),
           lambda: "failed_connect did not move: %s" % server_row(duthost, SERVER_DIALOUT))
    row = server_row(duthost, SERVER_DIALOUT)
    pytest_assert(int_field(row, "connected_subscriptions") == 0,
                  "connected_subscriptions with the collector down: %s" % row)


def test_dialout_failover_moves_peer(dialout_substate, telemetry_client_config, start_collector):
    """With two destinations, losing the active collector moves the stream,
    and the row's peer, to the other one; the break is counted as a send failure."""
    duthost = dialout_substate["duthost"]
    first, second = start_collector(), start_collector()
    configure(telemetry_client_config, [first.address, second.address])
    pytest_assert(wait_for_state(duthost, "connected"), "entry never connected")
    paths = path_rows(duthost, SUB_ID)  # a streaming report sends on change
    pytest_assert(paths and paths[0].get("path") == "%s/%s" % (TARGET, PATH)
                  and paths[0].get("mode") == "ON_CHANGE", "stream path rows: %s" % paths)
    active = own_row(duthost)["peer"]
    pytest_assert(active in (first.address, second.address), "peer %r is neither collector" % active)
    other = second.address if active == first.address else first.address
    failed = int_field(server_row(duthost, SERVER_DIALOUT), "failed_send")

    logger.info("stopping the active collector %s", active)
    (first if active == first.address else second).stop()
    expect(wait_for_field(duthost, SUB_KEY, "peer", other, timeout=CONNECT_TIMEOUT),
           lambda: "peer did not move to %s: %s" % (other, dialout_rows(duthost)))
    pytest_assert(wait_for_state(duthost, "connected"), "entry did not reconnect to %s" % other)
    expect(wait_for_field(duthost, SERVER_KEY, "failed_send", lambda v: int(v) > failed, timeout=TICK_TIMEOUT),
           lambda: "failed_send did not count the broken stream: %s" % server_row(duthost, SERVER_DIALOUT))


def test_dialout_misconfigured_entry(dialout_substate, telemetry_client_config, start_collector):
    """An entry without a destination group is visible as misconfigured with
    the reason; completing it makes it a normal, connected row."""
    duthost = dialout_substate["duthost"]
    collector = start_collector()
    configure(telemetry_client_config, [collector.address], with_group=False)

    expect(wait_for_state(duthost, "misconfigured", timeout=ROW_TIMEOUT),
           lambda: "no misconfigured row: %s" % dialout_rows(duthost))
    row = own_row(duthost)
    pytest_assert("destination group" in row.get("last_error", "").lower(), "last_error %r" % row.get("last_error"))
    expect(not path_rows(duthost, SUB_ID),
           lambda: "path rows for an entry the client does not serve: %s" % path_rows(duthost, SUB_ID))
    pytest_assert(int_field(server_row(duthost, SERVER_DIALOUT), "connected_subscriptions") == 0,
                  "a misconfigured entry counted as connected")

    logger.info("completing Subscription_%s with dst_group %s", NAME, NAME)
    telemetry_client_config.hset("Subscription_%s" % NAME, dst_group=NAME)
    expect(wait_for_state(duthost, "connected"), lambda: "entry did not connect once completed: %s"
           % dialout_rows(duthost))
    row = own_row(duthost)
    pytest_assert(row.get("destination_group") == NAME and row.get("last_error", "") == "",
                  "row after the fix: %s" % row)
    paths = path_rows(duthost, SUB_ID)
    pytest_assert(paths and paths[0].get("path") == "%s/%s" % (TARGET, PATH),
                  "path rows once the entry is served: %s" % paths)


def test_dialout_unconfigure_removes_rows(dialout_substate, telemetry_client_config, start_collector):
    """Deleting the entry removes its row and path rows at once and the dialout
    row's counts go back to zero."""
    duthost = dialout_substate["duthost"]
    collector = start_collector()
    configure(telemetry_client_config, [collector.address])
    pytest_assert(wait_for_state(duthost, "connected"), "entry never connected")
    pytest_assert(path_rows(duthost, SUB_ID), "no path rows while connected")

    logger.info("deleting Subscription_%s", NAME)
    delete_config_keys(duthost, telemetry_client_config, ["Subscription_%s" % NAME])
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: own_row(duthost) is None),
           lambda: "row survived the delete: %s" % own_row(duthost))
    pytest_assert(not path_rows(duthost, SUB_ID), "path rows survived the delete")
    expect(wait_for_field(duthost, SERVER_KEY, "configured_subscriptions", 0, timeout=TICK_TIMEOUT)
           and wait_for_field(duthost, SERVER_KEY, "connected_subscriptions", 0, timeout=TICK_TIMEOUT),
           lambda: "dialout row counts after the delete: %s" % server_row(duthost, SERVER_DIALOUT))


def test_dialout_rows_follow_a_restart(dialout_substate, telemetry_client_config, start_collector):
    """A client killed outright leaves its rows; the next start removes them,
    writes a dialout row for the new process and writes the row again for
    the entry that is still configured. A second entry deleted while the
    client is down shows the removal: no process writes its rows again."""
    duthost = dialout_substate["duthost"]
    collector = start_collector()
    configure(telemetry_client_config, [collector.address])
    gone = TelemetryClientConfig(duthost, OWNED_KEYS + ("Subscription_%s" % GONE_NAME,))
    logger.info("configuring Subscription_%s", GONE_NAME)
    gone.hset("Subscription_%s" % GONE_NAME, path_target=TARGET, paths=PATH,
              report_type="stream", report_interval=REPORT_INTERVAL_MS, dst_group=NAME)
    try:
        _restart_removes_rows(dialout_substate, gone)
    finally:
        delete_config_keys(duthost, gone, ["Subscription_%s" % GONE_NAME])


def _restart_removes_rows(dialout_substate, gone):
    duthost, container = dialout_substate["duthost"], dialout_substate["container"]
    pytest_assert(wait_for_state(duthost, "connected"), "entry never connected")
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: subscription_row(duthost, GONE_ID)
                      and path_rows(duthost, GONE_ID)),
           lambda: "no rows for %s: %s" % (GONE_ID, dialout_rows(duthost)))
    old_pid = dialout_pid(dialout_substate)
    pytest_assert(old_pid, "could not read the dialout pid")
    old_start = int_field(server_row(duthost, SERVER_DIALOUT), "start_time")
    old_row_start = int_field(own_row(duthost), "start_time")

    logger.info("killing dialout (pid %d) with SIGKILL", old_pid)
    duthost.shell("docker exec %s kill -9 %d" % (container, old_pid), module_ignore_errors=True)
    try:
        pytest_assert(wait_until(PROCESS_EXIT_TIMEOUT, 1, 0, lambda: not dialout_running(duthost, container)),
                      "dialout did not die")
        pytest_assert(own_row(duthost) is not None, "rows of the killed client are already gone")
        logger.info("deleting Subscription_%s while dialout is down", GONE_NAME)
        delete_config_keys(duthost, gone, ["Subscription_%s" % GONE_NAME])
        time.sleep(1)  # so the new process's row carries a later start_time than the dead one's
    finally:
        # autorestart is off: if a check above failed, dial-out must not stay down
        logger.info("starting dialout again")
        duthost.shell("docker exec %s supervisorctl start dialout" % container, module_ignore_errors=True)
    pytest_assert(wait_until(PROCESS_EXIT_TIMEOUT, 2, 0, lambda: dialout_running(duthost, container)),
                  "dialout did not start again")
    pytest_assert(wait_until(SERVER_ROW_TIMEOUT, 2, 0,
                             lambda: int_field(server_row(duthost, SERVER_DIALOUT), "pid") not in (0, old_pid)),
                  "dialout row still shows pid %s" % old_pid)
    row = server_row(duthost, SERVER_DIALOUT)
    pytest_assert(int_field(row, "pid") == dialout_pid(dialout_substate) and int_field(row, "start_time") >= old_start,
                  "dialout row after the restart: %s" % row)
    # The entry's row is the new process's: written again, with a later start_time.
    expect(wait_until(SERVER_ROW_TIMEOUT, 1, 0,
                      lambda: int_field(own_row(duthost) or {}, "start_time") > old_row_start),
           lambda: "the configured entry's row was not written again by the new process: %s" % own_row(duthost))
    expect(wait_for_state(duthost, "connected"),
           lambda: "the configured entry did not reconnect: %s" % own_row(duthost))
    expect(wait_until(ROW_TIMEOUT, 1, 0, lambda: not subscription_row(duthost, GONE_ID)
                      and not path_rows(duthost, GONE_ID)),
           lambda: "the start did not remove the rows of %s, deleted while the client was down: %s"
           % (GONE_ID, subscription_row(duthost, GONE_ID)))
    expect(wait_for_field(duthost, SERVER_KEY, "configured_subscriptions", 1, timeout=TICK_TIMEOUT),
           lambda: "configured_subscriptions after the restart: %s" % server_row(duthost, SERVER_DIALOUT))
