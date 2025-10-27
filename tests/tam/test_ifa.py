import json
import logging
import pytest
import ptf.testutils as testutils
from scapy.all import Ether, IP, UDP, TCP, Raw
import time
import random

from tests.common.helpers.assertions import pytest_assert
from tests.tam.ifa_common import IFA2Header, IFA2MetadataHeader, IFAMetadata

from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("any"),
]


TAM_ASICDB_TIMEOUT = 180
TAM_ASICDB_INTERVAL = 10


def tam_asicdb_has_keys(duthost):
    out = duthost.shell('sonic-db-cli ASIC_DB KEYS "*TAM*"')
    lines = out.get("stdout_lines", [])
    return bool(lines)


def wait_for_tam_asicdb_applied(duthost, timeout=TAM_ASICDB_TIMEOUT, interval=TAM_ASICDB_INTERVAL):
    return wait_until(timeout, interval, 0, lambda: tam_asicdb_has_keys(duthost))


TAM_CONFIG = {"TAM": {"device": {"device-id": "12345", "enterprise-id": "54321", "ifa": "true"}}}


def verify_config_applied(duthost):
    """
    Verify that TAM config has been applied:
    - CONFIG_DB contains TAM|device with expected fields
    - ASIC_DB contains TAM-related SAI objects (keys matching *TAM*)
    """
    show = duthost.shell('sonic-db-cli CONFIG_DB HGETALL "TAM|device"', module_ignore_errors=False)
    lines = show.get("stdout_lines", []) or []
    dbg = "\n".join(lines)

    # Ensure TAM|device exists and at least the 'ifa' field is present
    pytest_assert(lines, "CONFIG_DB: TAM|device not found or empty")
    pytest_assert("ifa" in dbg, f"CONFIG_DB: 'ifa' field missing in TAM|device. Got:\n{dbg}")

    # Ensure orchagent applied TAM config into ASIC_DB by checking for TAM-related keys
    pytest_assert(
        wait_for_tam_asicdb_applied(duthost, TAM_ASICDB_TIMEOUT, TAM_ASICDB_INTERVAL),
        "ASIC_DB missing TAM keys; orchagent may not have processed TAM config.",
    )


@pytest.fixture(scope="module")
def tam_transit_mode_config(duthosts, rand_one_dut_hostname):
    """
    Apply TAM device config to enable IFA transit mode, and clean up after.
    """
    duthost = duthosts[rand_one_dut_hostname]

    # Apply config to CONFIG_DB using sonic-cfggen
    tam_cfg_path = "/tmp/tam_config.json"
    duthost.copy(content=json.dumps(TAM_CONFIG, indent=2), dest=tam_cfg_path)
    res = duthost.shell(f"sonic-cfggen -j {tam_cfg_path} --write-to-db")
    pytest_assert(res["rc"] == 0, f"Failed to apply TAM config: {res}")

    # Verify TAM config is applied to both CONFIG_DB and ASIC_DB
    verify_config_applied(duthost)

    yield duthost

    # Cleanup: remove TAM|device
    duthost.shell('sonic-db-cli CONFIG_DB DEL "TAM|device"', module_ignore_errors=True)


def _pick_two_active_ptf_ports(duthost, ptfadapter, tbinfo):
    """
    Pick two front-panel ports and map them to PTF port indices.
    """
    # Prefer cached mg_facts on ptfadapter if present
    mg_facts = getattr(ptfadapter, "mg_facts", None)
    if not mg_facts:
        mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    ports = [p for p in mg_facts["minigraph_ports"].keys()]
    pytest_assert(len(ports) >= 2, "Need at least two front-panel ports")

    p1, p2 = random.sample(ports, 2)
    ptf_p1 = mg_facts["minigraph_ptf_indices"][p1]
    ptf_p2 = mg_facts["minigraph_ptf_indices"][p2]
    return (p1, ptf_p1), (p2, ptf_p2)


def _get_router_mac(duthost):
    out = duthost.shell("sonic-db-cli CONFIG_DB HGET 'DEVICE_METADATA|localhost' mac")
    pytest_assert(out["rc"] == 0 and out["stdout"], "Failed to read DUT router MAC")
    return out["stdout"].strip().lower()


def _get_config_device_id(duthost):
    show = duthost.shell("sonic-db-cli CONFIG_DB HGET 'TAM|device' 'device-id'")
    pytest_assert(show["rc"] == 0 and show["stdout"].strip(), f"Failed to read device-id: {show}")
    return int(show["stdout"].strip())


def build_ifa2_probe(ptfadapter, router_mac, ptf_src_port, next_hdr="UDP", sent_ttl=64):
    """
    Build an IFA2 probe packet with configurable next header (UDP or TCP) and TTL.
    """
    src_mac = ptfadapter.dataplane.get_mac(0, ptf_src_port)
    sport, dport = 1111, 2222
    ip_nxthdr = 17 if str(next_hdr).upper() == "UDP" else 6

    ifah = IFA2Header(version=2, gns=0xF, ip_nxthdr=ip_nxthdr, flags=0x4)
    ifamh = IFA2MetadataHeader(hop_limit=63, request_vector=0xFF, action_vector=0xFF, length=0)
    ifam_orig = IFAMetadata(device_id=0xEEEE)

    l4 = UDP(sport=sport, dport=dport) if ip_nxthdr == 17 else TCP(sport=sport, dport=dport)

    probe = (
        Ether(src=src_mac, dst=router_mac)
        / IP(src="100.0.0.1", dst="192.168.1.200", ttl=sent_ttl, proto=253)
        / ifah
        / l4
        / ifamh
        / ifam_orig
        / Raw(b"Test")
    )
    return probe


def send_probe(ptfadapter, ptf_src_port, probe, count=5):
    ptfadapter.dataplane.flush()
    testutils.send(ptfadapter, ptf_src_port, probe, count=count)


def verify_ifa2_egress(ptfadapter, ptf_src_port, sent_ttl, device_id=12345, timeout=10):
    """
    Poll dataplane and verify an egress packet with IFA2 header and IFAMetadata carrying the
    specified device_id. Also checks that TTL was decremented by 1.
    """
    deadline = time.time() + timeout
    last_pkt_debug = None
    while time.time() < deadline:
        res = testutils.dp_poll(ptfadapter, device_number=0, timeout=5)
        if not isinstance(res, ptfadapter.dataplane.PollSuccess):
            continue

        try:
            rx = Ether(res.packet)
        except Exception:
            continue
        last_pkt_debug = rx.summary()

        # Quick filter: must be IP proto 253 and contain our IFA2 header
        if IP not in rx or rx[IP].proto != 253 or IFA2Header not in rx:
            continue

        # Verify L3 forwarding decremented TTL by 1
        pytest_assert(
            int(rx[IP].ttl) == sent_ttl - 1, f"TTL not decremented: got {int(rx[IP].ttl)}, expected {sent_ttl - 1}"
        )

        # Collect IFAMetadata layers and check for device_id
        metas = []
        i = 1
        while True:
            m = rx.getlayer(IFAMetadata, i)
            if not m:
                break
            metas.append(m)
            i += 1
        if not metas:
            continue

        dut_meta = next((m for m in metas if int(m.device_id) == int(device_id)), None)
        if not dut_meta:
            continue

        return True

    pytest_assert(False, f"Did not observe IFA metadata insertion (device_id={device_id}). Last pkt: {last_pkt_debug}")
    return False


@pytest.mark.disable_loganalyzer
@pytest.mark.parametrize("next_hdr", ["UDP", "TCP"])
def test_ifa_transit_mode_metadata_insertion(tam_transit_mode_config, ptfadapter, tbinfo, next_hdr):
    """
    Configure TAM IFA transit mode and verify that upon receiving an IP packet with
    protocol 253 (IFA2), the DUT inserts IFA metadata on egress.

    Note: Packet crafting and verification will be completed after IFA packet
    format is provided by the user.
    """
    duthost = tam_transit_mode_config

    # Verify TAM config applied (CONFIG_DB and ASIC_DB)
    verify_config_applied(duthost)

    # Choose two ports to send/receive
    (ingr_port_name, ptf_src_port), (egr_port_name, ptf_dst_port) = _pick_two_active_ptf_ports(
        duthost, ptfadapter, tbinfo
    )
    router_mac = _get_router_mac(duthost)

    logger.info(f"Using ingress {ingr_port_name}/PTF{ptf_src_port} -> egress {egr_port_name}/PTF{ptf_dst_port}")

    # Build, send, and verify IFA metadata insertion
    sent_ttl = 64
    expected_device_id = _get_config_device_id(duthost)
    probe = build_ifa2_probe(
        ptfadapter=ptfadapter, router_mac=router_mac, ptf_src_port=ptf_src_port, next_hdr=next_hdr, sent_ttl=sent_ttl
    )
    send_probe(ptfadapter, ptf_src_port, probe, count=5)
    verify_ifa2_egress(ptfadapter, ptf_src_port, sent_ttl, device_id=expected_device_id, timeout=10)


@pytest.mark.disable_loganalyzer
@pytest.mark.parametrize("next_hdr", ["UDP", "TCP"])
def test_ifa_device_id_update(tam_transit_mode_config, ptfadapter, tbinfo, next_hdr):
    """
    After enabling IFA transit mode, change the TAM device-id and verify that
    subsequent IFA metadata carries the updated device id.
    """
    duthost = tam_transit_mode_config

    # Choose ingress PTF port and resolve DUT router MAC
    (_, ptf_src_port), _ = _pick_two_active_ptf_ports(duthost, ptfadapter, tbinfo)
    router_mac = _get_router_mac(duthost)

    # First, verify baseline behavior with currently configured device-id
    sent_ttl = 64
    baseline_device_id = _get_config_device_id(duthost)
    probe = build_ifa2_probe(
        ptfadapter=ptfadapter, router_mac=router_mac, ptf_src_port=ptf_src_port, next_hdr=next_hdr, sent_ttl=sent_ttl
    )
    send_probe(ptfadapter, ptf_src_port, probe, count=5)
    verify_ifa2_egress(ptfadapter, ptf_src_port, sent_ttl, device_id=baseline_device_id, timeout=10)

    # Update device-id and verify it takes effect
    new_device_id = 67890
    res = duthost.shell(
        f"sonic-db-cli CONFIG_DB HSET 'TAM|device' 'device-id' '{new_device_id}'",
        module_ignore_errors=False,
    )
    pytest_assert(res["rc"] == 0, f"Failed to update device-id: {res}")

    # Verify config applied and ASIC_DB populated after device-id update
    verify_config_applied(duthost)

    # Send another probe and verify the updated device-id is present in metadata
    probe2 = build_ifa2_probe(
        ptfadapter=ptfadapter,
        router_mac=router_mac,
        ptf_src_port=ptf_src_port,
        next_hdr=next_hdr,
        sent_ttl=sent_ttl,
    )
    send_probe(ptfadapter, ptf_src_port, probe2, count=5)
    verify_ifa2_egress(ptfadapter, ptf_src_port, sent_ttl, device_id=new_device_id, timeout=10)
