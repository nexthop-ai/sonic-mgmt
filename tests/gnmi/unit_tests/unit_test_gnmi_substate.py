# Copyright 2026 Nexthop Systems Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the STATE_DB readers in tests/common/helpers/gnmi_substate.py, against a fake duthost: a read
that fails must fail the test, never pass for an empty answer.

Run from the repository root:
    python3 -m pytest --noconftest tests/gnmi/unit_tests/ -v
"""
import pytest

from tests.common.helpers import gnmi_substate
from tests.common.helpers.gnmi_substate import (
    PublishInterval,
    delete_config_keys,
    expect,
    hgetall,
    publishes_server_row,
    state_keys,
)

FAILED = pytest.fail.Exception


class FakeDut:
    """A duthost whose shell answers every command with one result, or with the first result whose key the
    command contains, and records the commands."""
    hostname = "fake"

    def __init__(self, rc=0, stdout="", by_command=None):
        self.default = (rc, stdout)
        self.by_command = by_command or {}
        self.commands = []

    def shell(self, cmd, module_ignore_errors=False, **_):
        self.commands.append(cmd)
        rc, stdout = next((r for key, r in self.by_command.items() if key in cmd), self.default)
        return {"rc": rc, "stdout": stdout, "stdout_lines": stdout.splitlines(), "stderr": "boom" if rc else ""}


@pytest.fixture
def one_try(monkeypatch):
    '''wait_until as one attempt, which swallows a failure in the condition as the real one does.'''
    def once(timeout, interval, delay, condition):
        try:
            return bool(condition())
        except (Exception, FAILED):
            return False
    monkeypatch.setattr(gnmi_substate, "wait_until", once)


def test_failed_read_raises():
    with pytest.raises(FAILED, match="HGETALL 'K' failed: boom"):
        hgetall(FakeDut(rc=1), "K")
    with pytest.raises(FAILED, match=r"KEYS 'K\*' failed: boom"):
        state_keys(FakeDut(rc=1), "K*")


def test_empty_read_is_empty():
    assert hgetall(FakeDut(), "K") == {}
    assert state_keys(FakeDut(), "K*") == []


@pytest.mark.parametrize("stdout,want", [
    ("{'pid': '7', 'vrf': ''}", {"pid": "7", "vrf": ""}),
    ("pid\n7\nstate\nconnected", {"pid": "7", "state": "connected"}),
    # Not a Python literal: read as field and value lines.
    ("{not a dict\nx", {"{not a dict": "x"}),
    ("{broken\nx}", {"{broken": "x}"}),
    ("{'a set'}", {}),
])
def test_hgetall_parse(stdout, want):
    assert hgetall(FakeDut(stdout=stdout), "K") == want


def test_state_keys_sorted():
    assert state_keys(FakeDut(stdout="T|b\n\n T|a \n"), "T|*") == ["T|a", "T|b"]


def test_publishes_server_row(one_try):
    assert publishes_server_row(FakeDut(stdout="{'pid': '7'}"), "gnmi") is True
    assert publishes_server_row(FakeDut(stdout="{'start_time': '1'}"), "gnmi") is False
    assert publishes_server_row(FakeDut(), "gnmi") is False
    with pytest.raises(FAILED, match=r"HGETALL 'GNMI_SERVER_STATE\|gnmi' failed"):
        publishes_server_row(FakeDut(rc=1), "gnmi")


def test_publish_interval_failed_read_raises():
    with pytest.raises(FAILED, match=r"reading GNMI\|gnmi state_publish_interval failed"):
        PublishInterval(FakeDut(rc=1))


def test_publish_interval_restore_removes_an_absent_field():
    dut = FakeDut()
    interval = PublishInterval(dut)
    assert interval.saved == ""
    dut.commands.clear()
    interval.restore()
    assert len(dut.commands) == 1 and "HDEL" in dut.commands[0] and "state_publish_interval" in dut.commands[0]


def test_publish_interval_restore_writes_back_a_value():
    dut = FakeDut(stdout="5\n")
    interval = PublishInterval(dut)
    assert interval.saved == "5"
    dut.commands.clear()
    interval.restore()
    assert len(dut.commands) == 1 and "HSET" in dut.commands[0] and "state_publish_interval 5" in dut.commands[0]


class RecordingConfig:
    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


def test_delete_config_keys_checks_the_key_is_gone():
    config = RecordingConfig()
    delete_config_keys(FakeDut(stdout="0"), config, ["Subscription_A", "Subscription_B"])
    assert config.deleted == ["Subscription_A", "Subscription_B"]
    with pytest.raises(FAILED, match=r"deleting TELEMETRY_CLIENT\|Subscription_A failed"):
        delete_config_keys(FakeDut(stdout="1"), RecordingConfig(), ["Subscription_A"])
    with pytest.raises(FAILED, match=r"deleting TELEMETRY_CLIENT\|Subscription_A failed"):
        delete_config_keys(FakeDut(rc=1), RecordingConfig(), ["Subscription_A"])


def test_expect_builds_its_message_only_on_a_failure():
    calls = []

    def message():
        calls.append(1)
        return "rows: none"
    expect(True, message)
    assert not calls
    with pytest.raises(FAILED, match="rows: none"):
        expect(False, message)
    assert calls == [1]
