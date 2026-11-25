"""
Unit tests for is_port_trim_tx_reported in tests/packet_trimming/packet_trimming_helper.py

Run from the repository root:
    python3 -m pytest --noconftest tests/packet_trimming/unit_tests/ -v
"""
import json
from unittest import mock

import pytest

from tests.packet_trimming import packet_trimming_helper as helper


def dut_reporting(trim_tx, cached=False):
    stdout = json.dumps({"Ethernet0": {"STATE": "U", "TRIM_DRP_PKTS": "0", "TRIM_PKTS": "1,200",
                                       "TRIM_TX_PKTS": trim_tx}})
    if cached:
        stdout = "Last cached time was 2026-11-18 18:00:00\n" + stdout
    duthost = mock.Mock()
    duthost.shell.return_value = {"stdout": stdout}
    return duthost


@pytest.mark.parametrize("trim_tx, reported", [("1,200", True), ("0", True), ("N/A", False)])
def test_trim_tx_reported_unless_na(trim_tx, reported):
    assert helper.is_port_trim_tx_reported(dut_reporting(trim_tx), "Ethernet0") is reported


def test_trim_tx_reported_skips_the_cached_time_line():
    assert helper.is_port_trim_tx_reported(dut_reporting("N/A", cached=True), "Ethernet0") is False


def test_port_trim_counters_still_read_na_as_zero(monkeypatch):
    monkeypatch.setattr(helper, "logger", mock.Mock())
    counters = helper.get_port_trim_counters_json(dut_reporting("N/A"), "Ethernet0")
    assert counters == {"TRIM_DRP_PKTS": 0, "TRIM_PKTS": 1200, "TRIM_TX_PKTS": 0}
