# Copyright 2026 Nexthop Systems Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Readers and drivers for the gNMI subscription state tables in STATE_DB.

The gNMI server and the dial-out client each publish one server row and
one subscription row (with path rows) per subscription they serve:

    GNMI_SERVER_STATE|<server>            gnmi, or dialout
    GNMI_SUBSCRIPTION|<id>                the server's numeric client id, or dialout_<name>
    GNMI_SUBSCRIPTION_PATH|<id>|<n>       one per path of the subscription

The tests read the rows through sonic-db-cli on the DUT, as an operator or
a collector reading STATE_DB would, with no CLI in between. The publish
cadence is CONFIG_DB GNMI|gnmi state_publish_interval.
"""
import ast
import logging
import threading

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.sonic_db import _run_sonic_db_cli, redis_hdel, redis_hset
from tests.common.pygnmi_client import PygnmiClient
from tests.common.utilities import wait_until
from tests.gnmi.helper import DUT_COMMAND_TIMEOUT, dut_command_result

logger = logging.getLogger(__name__)

SERVER_TABLE = "GNMI_SERVER_STATE"
SUBSCRIPTION_TABLE = "GNMI_SUBSCRIPTION"
PATH_TABLE = "GNMI_SUBSCRIPTION_PATH"
SERVER_GNMI = "gnmi"
SERVER_DIALOUT = "dialout"
DIALOUT_ID_PREFIX = "dialout_"

CONFIG_KEY = "GNMI|gnmi"
INTERVAL_FIELD = "state_publish_interval"
DEFAULT_PUBLISH_INTERVAL = 10
MIN_PUBLISH_INTERVAL = 1
# The producer re-reads the interval every 10 s and ticks at once on a
# change, so a change shows within about 10 s, plus slack for the reads.
INTERVAL_APPLY_TIMEOUT = 30
# How long a server or client is given to come up and write its row.
SERVER_ROW_TIMEOUT = 60
# How long a killed process is given to go away.
PROCESS_EXIT_TIMEOUT = 30


def state_keys(duthost, pattern):
    """The STATE_DB keys matching pattern, sorted. Raises the test when
    sonic-db-cli fails, so an empty list always means no such key."""
    res = _run_sonic_db_cli(duthost, "STATE_DB KEYS '{}'".format(pattern), verbose=False)
    pytest_assert(res.get("rc", 1) == 0,
                  "sonic-db-cli STATE_DB KEYS '{}' failed: {}".format(pattern, res.get("stderr", "")))
    return sorted(line.strip() for line in (res.get("stdout") or "").splitlines() if line.strip())


def hgetall(duthost, key):
    """One STATE_DB hash as a dict of strings; {} when the key does not exist.
    Raises the test when sonic-db-cli fails, so {} never stands for a failed
    read."""
    res = _run_sonic_db_cli(duthost, "STATE_DB HGETALL '{}'".format(key), verbose=False)
    pytest_assert(res.get("rc", 1) == 0,
                  "sonic-db-cli STATE_DB HGETALL '{}' failed: {}".format(key, res.get("stderr", "")))
    out = (res.get("stdout") or "").strip()
    if not out:
        return {}
    # The dict sonic-db-cli prints, else field and value on alternate lines.
    if out.startswith("{") and out.endswith("}"):
        try:
            parsed = ast.literal_eval(out)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except (ValueError, SyntaxError):
            pass
    lines = out.split("\n")
    return {lines[i]: lines[i + 1] for i in range(0, len(lines) - 1, 2)}


def server_row(duthost, server):
    return hgetall(duthost, "{}|{}".format(SERVER_TABLE, server))


def subscription_id(key):
    """The id part of a GNMI_SUBSCRIPTION key; ids never contain the separator."""
    return key.split("|", 1)[1]


def subscription_ids(duthost, dialout=False):
    """The ids of one producer's subscription rows, without reading the rows."""
    ids = set()
    for key in state_keys(duthost, "{}|*".format(SUBSCRIPTION_TABLE)):
        sid = subscription_id(key)
        if sid.startswith(DIALOUT_ID_PREFIX) == dialout:
            ids.add(sid)
    return ids


def subscription_row(duthost, sid):
    """The subscription row of id sid, {} when there is none."""
    return hgetall(duthost, "{}|{}".format(SUBSCRIPTION_TABLE, sid))


def subscription_rows(duthost, ids):
    """{id: row} for the subscription rows of the given ids that exist."""
    rows = {sid: subscription_row(duthost, sid) for sid in ids}
    return {sid: row for sid, row in rows.items() if row}


def path_row_keys(duthost):
    """Every GNMI_SUBSCRIPTION_PATH key on the DUT."""
    return set(state_keys(duthost, "{}|*".format(PATH_TABLE)))


def path_rows(duthost, sid):
    """{n: row} for the path rows of subscription sid, keyed by their index."""
    rows = {}
    for key in state_keys(duthost, "{}|{}|*".format(PATH_TABLE, sid)):
        rows[int(key.rsplit("|", 1)[1])] = hgetall(duthost, key)
    return rows


def expect(ok, message):
    """pytest_assert for a message that reads the DUT: message is a callable,
    called only when ok is false, so a passing check costs no read and a
    read that fails cannot fail it."""
    __tracebackhide__ = True
    if not ok:
        pytest_assert(False, message())


def int_field(row, name, default=0):
    try:
        return int(row.get(name, default))
    except (TypeError, ValueError):
        return default


class PublishInterval(object):
    """state_publish_interval on GNMI|gnmi, saved when made and put back as it
    was by restore(): the value an operator set, or no field at all."""

    def __init__(self, duthost):
        self.duthost = duthost
        # A failed read must not pass for an absent field, or restore()
        # would delete a value an operator had set.
        res = duthost.shell("sonic-db-cli CONFIG_DB HGET '{}' {}".format(CONFIG_KEY, INTERVAL_FIELD),
                            module_ignore_errors=True)
        pytest_assert(res.get("rc", 1) == 0, "reading {} {} failed: {}".format(CONFIG_KEY, INTERVAL_FIELD, res))
        self.saved = res["stdout"].strip()
        logger.info("state_publish_interval before the test: %r", self.saved or None)

    def set(self, seconds):
        logger.info("setting state_publish_interval=%s", seconds)
        res = redis_hset(self.duthost, "CONFIG_DB", CONFIG_KEY, **{INTERVAL_FIELD: seconds})
        pytest_assert(res.get("rc", 1) == 0, "HSET {} {} failed: {}".format(CONFIG_KEY, INTERVAL_FIELD, res))

    def clear(self):
        logger.info("removing state_publish_interval")
        res = redis_hdel(self.duthost, "CONFIG_DB", CONFIG_KEY, INTERVAL_FIELD)
        pytest_assert(res.get("rc", 1) == 0, "HDEL {} {} failed: {}".format(CONFIG_KEY, INTERVAL_FIELD, res))

    def restore(self):
        if self.saved:
            self.set(self.saved)
        else:
            self.clear()


def wait_for_server_interval(duthost, server, seconds, timeout=INTERVAL_APPLY_TIMEOUT):
    """True once the server row reports publish_interval == seconds."""
    return wait_until(timeout, 2, 0,
                      lambda: server_row(duthost, server).get("publish_interval") == str(seconds))


def wait_for_stamp_advance(duthost, server, timeout, after=None):
    """The server row's last_updated once it moves past after, or past its
    current value when after is None; 0 on a timeout. Passing the stamp a
    previous call returned keeps a tick that lands between two calls from
    being skipped."""
    before = int_field(server_row(duthost, server), "last_updated") if after is None else after
    seen = []

    def advanced():
        stamp = int_field(server_row(duthost, server), "last_updated")
        if stamp > before:
            seen.append(stamp)
            return True
        return False
    return seen[0] if wait_until(timeout, 1, 0, advanced) else 0


def wait_for_field(duthost, key, field, want, timeout):
    """True once hash key has field == want (a string, or a predicate on the string)."""
    def _ok():
        got = hgetall(duthost, key).get(field)
        if got is None:
            return False
        return want(got) if callable(want) else got == str(want)
    return wait_until(timeout, 1, 0, _ok)


def wait_for_growth(duthost, key, field, timeout):
    """True once integer field on hash key exceeds the value it has now."""
    before = int_field(hgetall(duthost, key), field)
    return wait_until(timeout, 1, 0, lambda: int_field(hgetall(duthost, key), field) > before)


def delete_config_keys(duthost, config, keys):
    """Deletes each TELEMETRY_CLIENT key through config and fails unless it
    is gone, so a DEL that did not happen is not read as the client leaving
    a row behind."""
    for key in keys:
        config.delete(key)
        res = duthost.shell("sonic-db-cli CONFIG_DB EXISTS 'TELEMETRY_CLIENT|%s'" % key, module_ignore_errors=True)
        pytest_assert(res.get("rc", 1) == 0 and res["stdout"].strip() == "0",
                      "deleting TELEMETRY_CLIENT|%s failed: %s" % (key, res))


def publishes_server_row(duthost, server, timeout=None):
    """True once server's GNMI_SERVER_STATE row has a pid. A failed read
    fails the test rather than reading as a producer that does not publish:
    the wait gives the row time to appear, then one last read outside it
    decides."""
    if wait_until(timeout or SERVER_ROW_TIMEOUT, 3, 0, lambda: server_row(duthost, server).get("pid")):
        return True
    return bool(server_row(duthost, server).get("pid"))


# --- the DUT over one ssh session ---------------------------------------------

class DutShell(object):
    """duthost.shell over one paramiko session, for the duthost readers in
    this file while a gRPC channel is open in this process: Ansible forks the
    process for every call, which gRPC does not survive. Other attributes come
    from the duthost it wraps."""

    def __init__(self, duthost, ssh, timeout=DUT_COMMAND_TIMEOUT):
        self._duthost = duthost
        self._ssh = ssh
        self._timeout = timeout

    def __getattr__(self, name):
        return getattr(self._duthost, name)

    def shell(self, cmd, module_ignore_errors=False, **_):
        # The readers poll every second; at INFO they would flood the log.
        rc, out, err = dut_command_result(self._ssh, cmd, self._timeout, quiet=True)
        res = {"rc": rc, "stdout": out, "stdout_lines": out.splitlines(), "stderr": err, "failed": rc != 0}
        if rc != 0 and not module_ignore_errors:
            pytest.fail("{!r} exited {}: {}".format(cmd, rc, err.strip()))
        return res


# --- dial-in subscriptions from the test host, through PygnmiClient ---------

# How long a subscription is given for its first response.
FIRST_RESPONSE_SECONDS = 30


class Subscriber(object):
    """One dial-in subscription held open from the test host by
    PygnmiClient, in this process. A thread waits for the first response;
    the generator is then left suspended, and pygnmi's receiver thread keeps
    draining the stream, so the server keeps sending until stop() closes it.
    While one is open, nothing may call Ansible: read the DUT through a
    DutShell."""

    def __init__(self, conn, first_response_seconds=FIRST_RESPONSE_SECONDS, **sub_kwargs):
        client = PygnmiClient(conn["host"], conn["port"], ca_cert=conn["ca_cert"],
                              client_cert=conn["client_cert"], client_key=conn["client_key"])
        # Bounds only the wait for the first response: the generator is not
        # iterated after that.
        sub_kwargs["collect_seconds"] = first_response_seconds
        self.events = []
        self._result = None
        self._ready = threading.Event()
        self._note("subscribing: {}".format(sub_kwargs))
        self._gen = client.subscribe(**sub_kwargs)
        self._thread = threading.Thread(target=self._first_response, daemon=True)
        self._thread.start()

    def _note(self, msg):
        self.events.append(msg)
        logger.info("subscriber %x: %s", id(self), msg)

    def _first_response(self):
        try:
            next(self._gen)
            self._note("first response")
        except StopIteration:
            self._result = ("ok", "ended with no response")
            self._note(self._result[1])
        except Exception as exc:
            self._result = ("error", "{}: {}".format(type(exc).__name__, exc))
            self._note(self._result[1])
        finally:
            self._ready.set()

    def log(self):
        """What happened to the subscription, for failure messages."""
        return "\n".join(self.events)

    def wait_ready(self, timeout):
        """True once the first response has arrived or the subscription ended."""
        if not self._ready.wait(timeout):
            logger.warning("subscriber %x did not answer in %s s:\n%s", id(self), timeout, self.log())
            return False
        return True

    def outcome(self, timeout=0):
        """('ok' | 'error', detail) once the subscription has ended, else None."""
        if timeout:
            self._ready.wait(timeout)
        return self._result

    def stop(self):
        """Close the subscription. One with no response yet is left to its
        first-response window, which then ends it."""
        self._thread.join()
        if self._result is None:
            self._gen.close()
            self._result = ("ok", "stopped")
            self._note("stopped")


class Subscribers(object):
    """The subscriptions one test opens; stop_all stops only those."""

    def __init__(self, conn):
        self.conn = conn
        self.started = []

    def start(self, **sub_kwargs):
        s = Subscriber(self.conn, **sub_kwargs)
        self.started.append(s)
        return s

    def stop_all(self):
        for s in self.started:
            s.stop()
        self.started = []


def pygnmi_conn(client, host=None):
    """The connection settings of a PygnmiClient, as a plain dict each
    Subscriber builds its own client from; host, if given, replaces the
    client's."""
    return {
        "host": host or client.host,
        "port": client.port,
        "ca_cert": client.ca_cert,
        "client_cert": client.client_cert,
        "client_key": client.client_key,
    }
