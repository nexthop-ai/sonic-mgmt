# Copyright 2026 Nexthop Systems Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the PTF server restart and SSH retry helpers in ``tests/tacacs/utils.py``.

Follows the repo unit-test convention (unit_test_*.py, run with --noconftest).
"""

import os
import sys
from unittest import mock

import paramiko
import pytest

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_TEST_DIR)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.tacacs import utils as tacacs_utils  # noqa: E402

RUNNING = {"stdout": "tacacs+ running"}
STOPPED = {"stdout": "tacacs+ apparently not running"}


def _ptfhost(status_outputs):
    ptfhost = mock.MagicMock()
    ptfhost.command.side_effect = lambda cmd, **kwargs: status_outputs.pop(0) if "status" in cmd else {}
    return ptfhost


def _commands(ptfhost):
    return [c.args[0] for c in ptfhost.command.call_args_list]


def test_running_server_is_left_alone():
    ptfhost = _ptfhost([RUNNING])
    tacacs_utils.restart_tacacs_server_if_down(ptfhost)
    assert "service tacacs_plus restart" not in _commands(ptfhost)


def test_stopped_server_is_restarted():
    ptfhost = _ptfhost([STOPPED, RUNNING])
    tacacs_utils.restart_tacacs_server_if_down(ptfhost)
    assert "service tacacs_plus restart" in _commands(ptfhost)


@mock.patch.object(tacacs_utils.time, "sleep")
@mock.patch.object(tacacs_utils, "paramiko_ssh", side_effect=paramiko.ssh_exception.AuthenticationException())
def test_wrong_password_backs_off_then_raises(mock_ssh, mock_sleep):
    duthost = mock.MagicMock()
    duthost.shell.side_effect = Exception("DUT unreachable")
    with pytest.raises(paramiko.ssh_exception.AuthenticationException):
        tacacs_utils.ssh_connect_remote_retry("10.0.0.1", "user", "wrong", duthost)
    assert mock_ssh.call_count == 5
    # A sleep between every pair of attempts, each longer than the last.
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2, 3, 4, 5]


@pytest.mark.parametrize("message", ["Not allowed at this time", "Error reading SSH protocol banner"])
@mock.patch.object(tacacs_utils.time, "sleep")
@mock.patch.object(tacacs_utils, "paramiko_ssh")
def test_sshd_not_ready_backs_off_then_raises(mock_ssh, mock_sleep, message):
    mock_ssh.side_effect = paramiko.ssh_exception.SSHException(message)
    with pytest.raises(paramiko.ssh_exception.SSHException):
        tacacs_utils.ssh_connect_remote_retry("10.0.0.1", "user", "pass", mock.MagicMock())
    assert mock_ssh.call_count == 5
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2, 3, 4, 5]


@mock.patch.object(tacacs_utils.time, "sleep")
@mock.patch.object(tacacs_utils, "paramiko_ssh",
                   side_effect=paramiko.ssh_exception.SSHException("No existing session"))
def test_other_ssh_error_raises_at_once(mock_ssh, mock_sleep):
    with pytest.raises(paramiko.ssh_exception.SSHException):
        tacacs_utils.ssh_connect_remote_retry("10.0.0.1", "user", "pass", mock.MagicMock())
    assert mock_ssh.call_count == 1
    mock_sleep.assert_not_called()


def test_fixture_restarts_stopped_server_at_teardown():
    ptfhost = _ptfhost([STOPPED, RUNNING])
    fixture = getattr(tacacs_utils.ensure_tacacs_server_running_after_ut, "__wrapped__",
                      tacacs_utils.ensure_tacacs_server_running_after_ut)
    gen = fixture(ptfhost)
    next(gen)
    assert "service tacacs_plus restart" not in _commands(ptfhost)
    with pytest.raises(StopIteration):
        next(gen)
    assert "service tacacs_plus restart" in _commands(ptfhost)
