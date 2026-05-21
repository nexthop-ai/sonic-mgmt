"""Integration tests for gnmic operations via gnmi_tls fixture."""
import logging
import pytest

from tests.common.fixtures.grpc_fixtures import gnmi_tls  # noqa: F401
from tests.common.ptf_gnmic import GnmicCallError

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
