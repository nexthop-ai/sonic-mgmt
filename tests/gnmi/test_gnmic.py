"""Integration tests for gnmic operations via gnmi_tls fixture."""
import logging
import pytest

from tests.common.fixtures.grpc_fixtures import gnmi_tls  # noqa: F401
from tests.common.ptf_gnmic import (
    GnmicCallError,
    StreamMode,
    SubscribeMode,
)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any'),
]


def test_gnmic_capabilities(gnmi_tls):  # noqa: F811
    """Test gnmic capabilities() returns expected encodings and models."""
    result = gnmi_tls.gnmic.capabilities()
    logger.info("Capabilities response: %s", result)

    assert "version" in result, \
        f"Missing version in response: {list(result.keys())}"
    assert "supported-models" in result, \
        f"Missing supported-models in response: {list(result.keys())}"
    assert len(result["supported-models"]) > 0, \
        "supported-models should not be empty"

    encodings = result.get("encodings", [])
    assert "JSON_IETF" in encodings, \
        f"JSON_IETF not in encodings: {encodings}"

    logger.info("version: %s", result["version"])
    logger.info("encodings: %s", encodings)
    logger.info("supported-models count: %d", len(result["supported-models"]))


def _first_update(result):
    """Unwrap gnmic's [{source, updates:[{Path, values}]}] envelope.

    Returns the first source entry and its first update dict.
    """
    assert isinstance(result, list), f"Expected list response, got: {type(result)}"
    assert len(result) > 0, "Expected at least one response entry"

    entry = result[0]
    assert "source" in entry, f"Missing source in response: {entry}"
    assert "updates" in entry, f"Missing updates in response: {entry}"
    assert isinstance(entry["updates"], list), f"updates should be a list: {entry}"

    updates = entry["updates"]
    assert len(updates) > 0, f"Expected at least one update: {entry}"
    return entry, updates[0]


def test_gnmic_get_interface_mtu(duthost, gnmi_tls):  # noqa: F811
    """Test gnmic get() returns the configured MTU for Ethernet0."""
    path = "/openconfig-interfaces:interfaces/interface[name=Ethernet0]/config/mtu"

    expected_mtu = int(
        duthost.shell("sonic-db-cli CONFIG_DB hget 'PORT|Ethernet0' mtu")["stdout"].strip()
    )

    result = gnmi_tls.gnmic.get(path)
    logger.info("GET mtu response: %s (expected mtu=%d)", result, expected_mtu)

    entry, update = _first_update(result)

    assert entry["source"] == gnmi_tls.gnmic.target, \
        f"Unexpected source: {entry['source']} != {gnmi_tls.gnmic.target}"
    assert update["Path"] == \
        "openconfig-interfaces:interfaces/interface[name=Ethernet0]/config/mtu"

    values = update["values"]
    assert "openconfig-interfaces:interfaces/interface/config/mtu" in values, \
        f"Missing mtu values payload: {values}"

    mtu_payload = values["openconfig-interfaces:interfaces/interface/config/mtu"]
    assert "openconfig-interfaces:mtu" in mtu_payload, \
        f"Missing mtu leaf: {mtu_payload}"
    assert mtu_payload["openconfig-interfaces:mtu"] == expected_mtu, \
        f"gNMI mtu {mtu_payload} != CONFIG_DB mtu {expected_mtu}"


def test_gnmic_get_interface_counters(gnmi_tls):  # noqa: F811
    """Test gnmic get() returns interface counters for Ethernet0."""
    path = "/openconfig-interfaces:interfaces/interface[name=Ethernet0]/state/counters"

    result = gnmi_tls.gnmic.get(path)
    logger.info("GET counters response: %s", result)

    entry, update = _first_update(result)

    assert entry["source"] == gnmi_tls.gnmic.target, \
        f"Unexpected source: {entry['source']} != {gnmi_tls.gnmic.target}"
    assert update["Path"] == \
        "openconfig-interfaces:interfaces/interface[name=Ethernet0]/state/counters"

    values = update["values"]
    assert "openconfig-interfaces:interfaces/interface/state/counters" in values, \
        f"Missing counters values payload: {values}"

    counters_payload = values["openconfig-interfaces:interfaces/interface/state/counters"]
    assert "openconfig-interfaces:counters" in counters_payload, \
        f"Missing counters leaf set: {counters_payload}"

    counters = counters_payload["openconfig-interfaces:counters"]

    for key in [
        "in-pkts", "out-pkts",
        "in-octets", "out-octets",
        "in-errors", "out-errors",
    ]:
        assert key in counters, f"Missing counter {key}: {counters}"
        int(counters[key])  # validate numeric-string format


def test_gnmic_get_empty_paths_raises(gnmi_tls):  # noqa: F811
    """Test gnmic get() raises GnmicCallError when no paths are provided."""
    with pytest.raises(GnmicCallError, match="at least one path"):
        gnmi_tls.gnmic.get([])


def test_gnmic_get_invalid_datatype_raises(gnmi_tls):  # noqa: F811
    """Test gnmic get() raises GnmicCallError on an invalid datatype value."""
    path = "/openconfig-interfaces:interfaces/interface[name=Ethernet0]/state/counters"
    with pytest.raises(GnmicCallError, match="invalid datatype"):
        gnmi_tls.gnmic.get(path, datatype="BOGUS")


def test_gnmic_subscribe_sample_queue_counters(gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() supports STREAM+SAMPLE on COUNTERS_DB queue stats."""
    result = gnmi_tls.gnmic.subscribe(
        "COUNTERS/Ethernet0/Queues",
        target="COUNTERS_DB",
        sample_interval="1s",
        collect_seconds=6,
    )
    logger.info("SUBSCRIBE queue counters response: %s", result)

    entry, update = _first_update(result)
    assert entry["source"] == gnmi_tls.gnmic.target, \
        f"Unexpected source: {entry['source']} != {gnmi_tls.gnmic.target}"
    assert entry["target"] == "COUNTERS_DB", \
        f"Unexpected target: {entry.get('target')}"
    assert update["Path"] == "COUNTERS/Ethernet0/Queues"

    values = update["values"]
    assert "COUNTERS/Ethernet0/Queues" in values, \
        f"Missing queue values payload: {values}"

    queue_payload = values["COUNTERS/Ethernet0/Queues"]
    assert "Ethernet0:0" in queue_payload, \
        f"Missing queue Ethernet0:0: {queue_payload.keys()}"

    q0 = queue_payload["Ethernet0:0"]
    for key in ["SAI_QUEUE_STAT_PACKETS", "SAI_QUEUE_STAT_BYTES"]:
        assert key in q0, f"Missing queue stat {key}: {q0}"
        int(q0[key])  # validate numeric-string format


def test_gnmic_subscribe_empty_paths_raises(gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() raises when no paths are provided."""
    with pytest.raises(GnmicCallError, match="at least one path"):
        gnmi_tls.gnmic.subscribe([], sample_interval="1s")


def test_gnmic_subscribe_invalid_mode_raises(gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() rejects unsupported top-level modes in PR1."""
    with pytest.raises(GnmicCallError, match="only .*stream.* is supported"):
        gnmi_tls.gnmic.subscribe("proc/uptime", mode=SubscribeMode.POLL, sample_interval="1s")


def test_gnmic_subscribe_invalid_stream_mode_raises(gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() rejects unsupported stream sub-modes in PR1."""
    with pytest.raises(GnmicCallError, match="only .*sample.* stream_mode is supported"):
        gnmi_tls.gnmic.subscribe(
            "proc/uptime",
            stream_mode=StreamMode.ON_CHANGE,
            sample_interval="1s",
        )


def test_gnmic_subscribe_heartbeat_interval_forwarded(monkeypatch, gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() forwards explicit heartbeat_interval to gnmic."""
    captured = {}

    def fake_run_stream_for(cmd, collect_seconds, op_name):
        captured["cmd"] = cmd
        return """
{
  "source": "dummy",
  "target": "OTHERS",
  "updates": [
    {
      "Path": "proc/uptime",
      "values": {
        "proc/uptime": {
          "idle": 100.0,
          "total": 10.0
        }
      }
    }
  ]
}
"""

    monkeypatch.setattr(gnmi_tls.gnmic, "_run_stream_for", fake_run_stream_for)

    result = gnmi_tls.gnmic.subscribe(
        "proc/uptime",
        target="OTHERS",
        sample_interval="1s",
        collect_seconds=3,
        heartbeat_interval="30s",
    )

    assert result
    assert "--heartbeat-interval" in captured["cmd"]
    assert "30s" in captured["cmd"]


def test_gnmic_subscribe_extra_args_forwarded(monkeypatch, gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() forwards extra_args tokens into the gnmic command."""
    captured = {}

    def fake_run_stream_for(cmd, collect_seconds, op_name):
        captured["cmd"] = cmd
        return """
{
  "source": "dummy",
  "target": "OTHERS",
  "updates": [
    {
      "Path": "proc/uptime",
      "values": {
        "proc/uptime": {
          "idle": 100.0,
          "total": 10.0
        }
      }
    }
  ]
}
"""

    monkeypatch.setattr(gnmi_tls.gnmic, "_run_stream_for", fake_run_stream_for)

    result = gnmi_tls.gnmic.subscribe(
        "proc/uptime",
        target="OTHERS",
        sample_interval="1s",
        collect_seconds=3,
        extra_args=["--qos", "32"],
    )

    assert result
    assert "--qos" in captured["cmd"]
    assert "32" in captured["cmd"]


def test_gnmic_subscribe_extra_args_string_rejected(gnmi_tls):  # noqa: F811
    """Test gnmic subscribe() rejects a plain string for extra_args."""
    with pytest.raises(GnmicCallError, match="must be a sequence"):
        gnmi_tls.gnmic.subscribe(
            "proc/uptime",
            target="OTHERS",
            sample_interval="1s",
            extra_args="--qos 32",
        )
