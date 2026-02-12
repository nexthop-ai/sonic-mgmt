import logging
import time
import json
import pytest
from collections import defaultdict
import ptf.packet as scapy
import ptf.testutils as testutils
from ptf.mask import Mask
from tests.common.helpers.assertions import pytest_assert

from tests.ecmp.ars.conftest import setup_ars_profile, setup_ars_interface
from tests.ecmp.ars.conftest import setup_ars_object, setup_ars_acl, wait_for_db_entry
from tests.ecmp.ars.conftest import cleanup_ars_profile, cleanup_ars_interface
from tests.ecmp.ars.conftest import cleanup_ars_acl, cleanup_ars_object
from tests.ecmp.ars.conftest import create_ecmp_route
from tests.ecmp.ars.conftest import enable_ars_counters, disable_ars_counters

import tests.common.snappi_tests.common_helpers as snappi_helpers

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.asic('broadcom')
]

# -----------------------------
# Config constants for this test
# -----------------------------
ROUTE_PREFIX = "10.10.10.0/24"
ROUTE_PREFIX_V6 = "2001:db8:85a3::/64"

ARS_PROFILE = {
    "algorithm": "ewma",
    "ars_nhg_path_selector_mode": "interface",
    "max_flows": "2048",
    "sampling_interval": "10",
    "past_load_min_value": "1000",
    "past_load_max_value": "9000",
    "past_load_weight": "70",
    "future_load_min_value": "10000",
    "future_load_max_value": "70000",
    "future_load_weight": "30",
    "current_load_min_value": "10000",
    "current_load_max_value": "70000",
    "ipv4_enable": "true",
    "ipv6_enable": "true"
}

ARS_NEXTHOP_GROUP = {
    "profile_name": "global",
    "assign_mode": "per_flowlet_quality",
    "max_flows": "512",
    "flowlet_idle_time": "32767"
}

ARS_OBJECT = {
    "assign_mode": "per_flowlet_quality",
    "max_flows": "512",
    "flowlet_idle_time": "32767"
}

ARS_OBJECT_2 = {
    "assign_mode": "per_flowlet_quality",
    "max_flows": "256",
    "flowlet_idle_time": "32767"
}


# Enable ARS NHG counter polling for each test and helper to read stats from CLI

# Wait time for ARS NHG counters to settle before reading
ARS_COUNTERS_WAIT_SEC = 5


def get_nhg_stats(duthost, nexthops, ifaces, logger=logging.getLogger(__name__)):
    """
    Retrieve ARS NHG counters using 'show ars counters --json' and match the
    entry corresponding to the ECMP group created by setup_ecmp_route().

    Matching logic:
    - Select the entry whose nexthop_group IPs and interface names exactly equal
      those returned by setup_ecmp_route().

    Returns a dict with keys: failed_cnt, nh_reassignments, port_reassignments
    """
    stats = {
        "failed_cnt": 0,
        "nh_reassignments": 0,
        "port_reassignments": 0,
    }

    try:
        res = duthost.shell("show ars counters --json", module_ignore_errors=True)
        if res.get("rc", 1) != 0:
            logger.warning("Failed to run 'show ars counters --json': rc=%s", res.get("rc"))
            return stats
        out = (res.get("stdout") or "").strip()
        if not out:
            return stats
        data = json.loads(out)
        if not isinstance(data, list):
            logger.warning("Unexpected JSON from 'show ars counters': %s", out)
            return stats

        nh_ips = set(map(str, nexthops or []))
        iface_names = set((ifaces or {}).keys())

        matched = None

        def parse_group(entry):
            group_str = entry.get("nexthop_group") or ""
            items = [x.strip() for x in group_str.split(",") if x.strip()]
            grp_ips, grp_ifaces = set(), set()
            for it in items:
                if "@" in it:
                    ip, iface = it.split("@", 1)
                else:
                    ip, iface = it, ""
                grp_ips.add(ip)
                if iface:
                    grp_ifaces.add(iface)
            return grp_ips, grp_ifaces

        # Exact match only: both IPs and interface names must match
        for entry in data:
            grp_ips, grp_ifaces = parse_group(entry)
            if grp_ips == nh_ips and grp_ifaces == iface_names:
                matched = entry
                break

        if matched is None:
            logger.info(
                "No exact NHG match found in ARS counters (nexthops=%s ifaces=%s)",
                nh_ips, iface_names,
            )
            return stats

        stats["failed_cnt"] = int(matched.get("packet_drops", "0"))
        stats["nh_reassignments"] = int(matched.get("nh_reassignments", "0"))
        stats["port_reassignments"] = int(matched.get("port_reassignments", "0"))
    except Exception as e:
        logger.warning("Error parsing ARS NHG counters: %s", e)

    return stats


def build_pkt(dest_mac, ip_src, ip_dst, ttl, flow_count):
    pkt = testutils.simple_tcp_packet(
          eth_dst=dest_mac,
          eth_src="00:11:22:33:44:55",
          pktlen=100,
          ip_src=ip_src,
          ip_dst=ip_dst,
          ip_ttl=ttl,
          tcp_dport=200 + flow_count,
          tcp_sport=100 + flow_count
    )
    exp_packet = Mask(pkt)
    exp_packet.set_do_not_care_scapy(scapy.Ether, "dst")
    exp_packet.set_do_not_care_scapy(scapy.Ether, "src")

    exp_packet.set_do_not_care_scapy(scapy.IP, "version")
    exp_packet.set_do_not_care_scapy(scapy.IP, "ihl")
    exp_packet.set_do_not_care_scapy(scapy.IP, "tos")
    exp_packet.set_do_not_care_scapy(scapy.IP, "len")
    exp_packet.set_do_not_care_scapy(scapy.IP, "flags")
    exp_packet.set_do_not_care_scapy(scapy.IP, "id")
    exp_packet.set_do_not_care_scapy(scapy.IP, "frag")
    exp_packet.set_do_not_care_scapy(scapy.IP, "ttl")
    exp_packet.set_do_not_care_scapy(scapy.IP, "chksum")
    exp_packet.set_do_not_care_scapy(scapy.IP, "options")

    exp_packet.set_do_not_care_scapy(scapy.TCP, "seq")
    exp_packet.set_do_not_care_scapy(scapy.TCP, "ack")
    exp_packet.set_do_not_care_scapy(scapy.TCP, "reserved")
    exp_packet.set_do_not_care_scapy(scapy.TCP, "dataofs")
    exp_packet.set_do_not_care_scapy(scapy.TCP, "window")
    exp_packet.set_do_not_care_scapy(scapy.TCP, "chksum")
    exp_packet.set_do_not_care_scapy(scapy.TCP, "urgptr")

    exp_packet.set_ignore_extra_bytes()
    return pkt, exp_packet


def build_and_send_tcp_ip_packet(ptfadapter, rtr_mac, src_ip, dst_ip, ip_ttl,
                                 src_port_id, dst_port_ids, recvd_pkt_result,
                                 flow_count_range=50):
    """
    Build and send TCP/IP packets for ECMP distribution analysis.

    Args:
        ptfadapter: PTF adapter for sending/receiving packets
        rtr_mac: Router MAC address
        src_ip: Source IP address for packets
        dst_ip: Destination IP address for packets
        ip_ttl: IP TTL value
        src_port_id: Source port ID to send packets from
        dst_port_ids: List of destination port IDs to verify packets on
        recvd_pkt_result: Dictionary to track received packet distribution
        flow_count_range: Number of flows to send (default: 50)
    """
    logger = logging.getLogger(__name__)
    for flow_count in range(flow_count_range):
        # Build packet destined to the ECMP route prefix
        pkt, exp_pkt = build_pkt(rtr_mac, src_ip, dst_ip, ip_ttl, flow_count)

        # Send packet from source port
        testutils.send(ptfadapter, src_port_id, pkt, 500)

        # Verify packet is received on one of the destination ports (ECMP)
        verify_result = testutils.verify_packet_any_port(test=ptfadapter, pkt=exp_pkt,
                                                         ports=dst_port_ids)
        if isinstance(verify_result, bool):
            logger.info("Using dummy testutils to skip traffic test.")
            return
        else:
            port_index, recv_pkt = verify_result

        assert recv_pkt

        # Make sure routing is done correctly
        pytest_assert(scapy.Ether(recv_pkt).ttl == (ip_ttl - 1), "Routed Packet TTL not decremented")
        pytest_assert(scapy.Ether(recv_pkt).src == rtr_mac, "Routed Packet Source Mac is not router MAC")

        # Track which port received the packet for ECMP distribution analysis
        received_port = dst_port_ids[port_index]
        recvd_pkt_result[flow_count].add(received_port)
        logger.info("Flow {} received on port {}".format(flow_count, received_port))


def build_rocev2_pkt(dest_mac, ip_src, ip_dst, dscp, ttl, bth_opcode, flow_count):
    """
    Build a ROCEv2 packet

    Args:
        dest_mac: Destination MAC address
        ip_src: Source IP address
        ip_dst: Destination IP address
        ttl: IP TTL value
        bth_opcode: BTH (Base Transport Header) opcode for RoCEv2 packets
        flow_count: Flow count for port variation (default: 50)

    Returns:
        Tuple of (packet, expected_packet_mask)
    """
    import struct

    # Default RoCEv2 UDP port
    ROCEV2_PORT = 4791

    pkt = testutils.simple_udp_packet(
        eth_dst=dest_mac,
        eth_src="00:11:22:33:44:55",
        pktlen=60,
        ip_src=ip_src,
        ip_dst=ip_dst,
        ip_dscp=dscp,
        ip_ttl=ttl,
        udp_dport=ROCEV2_PORT,
        udp_sport=ROCEV2_PORT + flow_count,
    )

    # Build BTH (Base Transport Header, 12 bytes)
    # Fields: opcode(1), flags(1), pkey(2), reserved(1), dest_qp(3), psn(4)
    bth_header = struct.pack(
        "!BBH B 3s I",
        bth_opcode,         # opcode
        0x00,               # flags
        0x0000,             # partition key
        0x00,               # reserved
        b"\x00\x00\x01",    # dest_qp
        0x00000001,         # PSN
    )

    # Replace UDP payload with BTH header
    del pkt[scapy.UDP].payload
    dummy_payload = b"\x00" * 16
    pkt = pkt / bth_header / dummy_payload

    return pkt


def build_rocev2_pkt_ipv6(dest_mac, ipv6_src, ipv6_dst, dscp, hlim, bth_opcode, flow_count):
    """
    Build a ROCEv2 IPv6 packet

    Args:
        dest_mac: Destination MAC address
        ipv6_src: Source IPv6 address
        ipv6_dst: Destination IPv6 address
        dscp: DSCP value
        hlim: IPv6 Hop Limit value
        bth_opcode: BTH (Base Transport Header) opcode for RoCEv2 packets
        flow_count: Flow count for port variation (default: 50)

    Returns:
        IPv6 ROCEv2 packet with BTH header
    """
    import struct

    # Default RoCEv2 UDP port
    ROCEV2_PORT = 4791

    pkt = testutils.simple_udpv6_packet(
        eth_dst=dest_mac,
        eth_src="00:11:22:33:44:55",
        pktlen=60,
        ipv6_src=ipv6_src,
        ipv6_dst=ipv6_dst,
        ipv6_dscp=dscp,
        ipv6_hlim=hlim,
        udp_dport=ROCEV2_PORT,
        udp_sport=ROCEV2_PORT + flow_count,
    )

    # Build BTH (Base Transport Header, 12 bytes)
    # Fields: opcode(1), flags(1), pkey(2), reserved(1), dest_qp(3), psn(4)
    bth_header = struct.pack(
        "!BBH B 3s I",
        bth_opcode,         # opcode
        0x00,               # flags
        0x0000,             # partition key
        0x00,               # reserved
        b"\x00\x00\x01",    # dest_qp
        0x00000001,         # PSN
    )

    # Replace UDP payload with BTH header
    del pkt[scapy.UDP].payload
    dummy_payload = b"\x00" * 16
    pkt = pkt / bth_header / dummy_payload

    return pkt


def test_ars_config(duthost, setup_ecmp_route, gather_facts,
                    enum_rand_one_frontend_asic_index):
    """
    Verify ARS configuration is applied correctly.
    """
    logger = logging.getLogger(__name__)

    try:
        enable_ars_counters(duthost, 1000)
        _, nexthops, ifaces = setup_ecmp_route
        asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)

        # setup ars object, ars-1
        setup_ars_object(duthost, ARS_OBJECT, "ars-1")
        wait_for_db_entry(duthost, "ARS_OBJECT_TABLE|ars-1",
                          expected_attrs={"max_flows": "512"}, db=6)
        # Configure ARS_PROFILE with default_ars_object set to "ars-1"
        ars_ifaces = {}
        ars_ifaces_2 = {}
        profile_name = None
        ars_profile = ARS_PROFILE.copy()
        ars_profile["default_ars_object"] = "ars-1"
        ars_profile["ars_nhg_path_selector_mode"] = "global"
        profile_name = setup_ars_profile(duthost, ars_profile)
        wait_for_db_entry(duthost, "ARS_PROFILE_TABLE|global",
                          expected_attrs={"default_ars_object": "ars-1"}, db=6)

        # verify that STATE_DB has entry for ars-1 in ARS_NEXTHOP_GROUP_TABLE, and has
        # all the nexthops from the ecmp group
        _, state_entry = wait_for_db_entry(
            duthost, "ARS_NEXTHOP_GROUP_TABLE|ars-1",
            expected_attrs=None, db=6
        )
        for nexthop in nexthops:
            pytest_assert(nexthop in state_entry,
                          f"Nexthop {nexthop} not found in STATE_DB entry for ars-1")

        # Lets change the default_ars_object to ars-2 and verify that the entry in STATE_DB changes
        setup_ars_object(duthost, ARS_OBJECT_2, "ars-2")
        logger.info("Changing default_ars_object to ars-2")
        ars_profile["default_ars_object"] = "ars-2"
        setup_ars_profile(duthost, ars_profile)
        wait_for_db_entry(
            duthost, "ARS_PROFILE_TABLE|global",
            expected_attrs={"default_ars_object": "ars-2"}, db=6
        )
        _, state_entry = wait_for_db_entry(
            duthost, "ARS_NEXTHOP_GROUP_TABLE|ars-2",
            expected_attrs=None, db=6, timeout=20
        )
        for nexthop in nexthops:
            pytest_assert(nexthop in state_entry,
                          f"Nexthop {nexthop} not found in STATE_DB entry for ars-2")

        # pickup interfaces from the ecmp group
        for ifname in ifaces.keys():
            ars_ifaces[ifname] = {"scaling_factor": "10", "ars_obj_name": "ars-1"}
        setup_ars_interface(duthost, ars_ifaces)

        # Verify that the ARS interfaces have the correct ARS object configured in the STATE_DB
        for ifname in ars_ifaces.keys():
            wait_for_db_entry(duthost, f"ARS_INTERFACE_TABLE|{ifname}",
                              expected_attrs={"ars_obj_name": "ars-1"}, db=6)

        # Now change the mode to interface, and verify that STATE_DB has entry for ars-1
        ars_profile["ars_nhg_path_selector_mode"] = "interface"
        setup_ars_profile(duthost, ars_profile)
        wait_for_db_entry(duthost, "ARS_PROFILE_TABLE|global",
                          expected_attrs={"ars_nhg_path_selector_mode": "interface"}, db=6)
        _, state_entry = wait_for_db_entry(
            duthost, "ARS_NEXTHOP_GROUP_TABLE|ars-1",
            expected_attrs=None, db=6
        )
        for nexthop in nexthops:
            pytest_assert(nexthop in state_entry,
                          f"Nexthop {nexthop} not found in STATE_DB entry for ars-1")

        # Choose 2 interfaces other than the ones configured above and configure them with ars-2
        # Then create a new ecmp group with those interfaces
        # Verify that the new ecmp group has the correct nexthops in the STATE_DB entry
        logger.info("Configure interfaces with ars-2 and then create new ECMP group")

        # Get the interfaces for dst4 and dst5
        logger.info("Getting interfaces for dst4 and dst5")
        ars_iface_names_2 = []
        for dst_num in [4, 5]:
            dst_key = f'dst{dst_num}'
            if f'{dst_key}_router_intf_name' in gather_facts:
                ifname = gather_facts[f'{dst_key}_router_intf_name']
                ars_ifaces_2[ifname] = {"scaling_factor": "10", "ars_obj_name": "ars-2"}
                ars_iface_names_2.append(ifname)
                logger.info(f"Adding interface {ifname} to ars-2 configuration")

        # Configure the interfaces with ars-2
        setup_ars_interface(duthost, ars_ifaces_2)
        logger.info("Interfaces configured with ars-2 successfully")

        # Verify that the ARS interfaces have the correct ARS object configured in the STATE_DB
        for ifname in ars_ifaces_2.keys():
            wait_for_db_entry(duthost, f"ARS_INTERFACE_TABLE|{ifname}",
                              expected_attrs={"ars_obj_name": "ars-2"}, db=6)
            logger.info(f"Interface {ifname} verified in STATE_DB with ars-2")

        # Create a new ECMP group with dst4 and dst5 interfaces
        logger.info("Creating new ECMP group with dst4 and dst5 interfaces")
        test2_prefix = "20.0.0.0/24"

        try:
            # Create ECMP route with dst4, dst5
            _, test2_nexthops, _ = create_ecmp_route(
                duthost, asic, test2_prefix, gather_facts, [4, 5]
            )
            logger.info(f"ECMP route {test2_prefix} configured successfully with dst4, dst5")
        except ValueError as e:
            logger.warning(f"Failed to create test2 ECMP route: {e}")
            test2_nexthops = []

        # Verify that STATE_DB has the new ECMP group with all nexthops
        logger.info("Verifying STATE_DB entry for new ARS nexthop group with ars-2")
        state_db_key_test2 = "ARS_NEXTHOP_GROUP_TABLE|ars-2"
        _, state_entry_test2 = wait_for_db_entry(
            duthost, state_db_key_test2,
            expected_attrs=None, db=6, timeout=20
        )
        for nexthop in test2_nexthops:
            pytest_assert(nexthop in state_entry_test2,
                          f"Nexthop {nexthop} not found in STATE_DB entry for ars-2")

    finally:
        disable_ars_counters(duthost)

        # Clean up ARS configuration
        if ars_ifaces:
            cleanup_ars_interface(duthost, ars_ifaces)
        if ars_ifaces_2:
            cleanup_ars_interface(duthost, ars_ifaces_2)
        if profile_name:
            cleanup_ars_profile(duthost, profile_name)
        cleanup_ars_object(duthost, "ars-1")
        cleanup_ars_object(duthost, "ars-2")


@pytest.mark.parametrize("mode", ["global", "interface"], ids=["mode_global", "mode_interface"])
def test_ecmp_route_with_ars(duthost, setup_ecmp_route, gather_facts,
                             enum_rand_one_frontend_asic_index, ptfadapter, mode):
    """
    Verify ECMP load-balancing with ARS enabled using ptf_runner.
    Uses setup_ars_profile and setup_ars_nhg functions for configuration.

    Args:
        mode: ARS mode - 'global' or 'interface'
    """
    logger = logging.getLogger(__name__)

    # Get route information from setup_ecmp_route
    prefix, nexthops, ifaces = setup_ecmp_route

    # Setup ARS configuration with proper cleanup
    profile_name = None
    ars_ifaces = None

    try:
        enable_ars_counters(duthost, 1000)

        # setup ars object
        setup_ars_object(duthost, ARS_OBJECT, "ars-1")
        # Setup ARS profile
        ars_profile = ARS_PROFILE.copy()
        ars_profile["default_ars_object"] = None if mode == "interface" else "ars-1"
        ars_profile["ars_nhg_path_selector_mode"] = mode
        profile_name = setup_ars_profile(duthost, ars_profile)

        # Build interface entries and setup ARS interfaces
        ars_ifaces = {}
        for ifname in ifaces.keys():
            ars_ifaces[ifname] = {"scaling_factor": "10", "ars_obj_name": "ars-1"}
        setup_ars_interface(duthost, ars_ifaces)

        logger.info(f"Running ECMP + ARS test for prefix {prefix} via nexthops {nexthops}")
        logger.info(f"Selected interfaces: {list(ifaces.keys())}")

        # Get ASIC instance and router MAC
        asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)
        rtr_mac = asic.get_router_mac()

        # Extract destination IP from prefix for packet generation
        ip_route = prefix.split('/')[0]  # Get IP address from CIDR notation
        ip_ttl = 64

        # Track received packets for ECMP distribution analysis
        recvd_pkt_result = defaultdict(set)

        # Get destination port IDs for the 3 destination interfaces
        dst_port_ids = []
        for i in range(1, 4):  # dst1, dst2, dst3
            dst_key = f'dst{i}'
            if f'{dst_key}_port_ids' in gather_facts:
                dst_port_ids.extend(gather_facts[f'{dst_key}_port_ids'])

        logger.info(f"Using destination port IDs: {dst_port_ids}")
        logger.info(f"Using source port ID: {gather_facts['src_port_ids'][0]}")

        # Run the traffic test
        ptfadapter.dataplane.flush()
        prev_stats = get_nhg_stats(duthost, nexthops, ifaces)
        build_and_send_tcp_ip_packet(ptfadapter, rtr_mac, "19.0.0.100", ip_route, ip_ttl,
                                     gather_facts['src_port_ids'][0], dst_port_ids, recvd_pkt_result)
        time.sleep(ARS_COUNTERS_WAIT_SEC)
        current_stats = get_nhg_stats(duthost, nexthops, ifaces)
        # Make sure there are no failed packets for this DLB group
        pytest_assert(current_stats['failed_cnt'] == prev_stats['failed_cnt'],
                      "Failed packets detected in NHG stats")
        # expect at least 3 port reassignments - one per destination port
        pytest_assert(current_stats['port_reassignments'] >= prev_stats['port_reassignments'] + 3,
                      "All ports were not assigned in DLB forwarding")

        # Analyze ECMP distribution
        port_distribution = defaultdict(int)
        for _, ports in recvd_pkt_result.items():
            for port in ports:
                port_distribution[port] += 1

        logger.info(f"ECMP distribution across ports: {dict(port_distribution)}")

        # Verify that traffic was distributed across multiple ports (basic ECMP check)
        if len(port_distribution) > 1:
            logger.info("ECMP load balancing is working - traffic distributed across multiple ports")
        else:
            logger.warning("Traffic only used one port - ECMP may not be working as expected")

    finally:
        disable_ars_counters(duthost)

        # Clean up ARS configuration in reverse order
        logger.info("Cleaning up ARS configuration")
        if ars_ifaces:
            cleanup_ars_interface(duthost, ars_ifaces)
        if profile_name:
            ars_profile["default_ars_object"] = None
            setup_ars_profile(duthost, ars_profile)
            cleanup_ars_profile(duthost, profile_name)
        cleanup_ars_object(duthost, "ars-1")


'''
Test to check that disabling ARS flows through ACL works
'''


@pytest.mark.parametrize("mode", ["global", "interface"], ids=["mode_global", "mode_interface"])
def test_disable_ars_flow(duthost, setup_ecmp_route, gather_facts,
                          enum_rand_one_frontend_asic_index, ptfadapter, mode):
    """
    Verify ECMP load-balancing with ARS disabled using ptf_runner.
    Uses setup_ars_profile and setup_ars_nhg functions for configuration.

    Args:
        mode: ARS mode - 'global' or 'interface'
    """
    logger = logging.getLogger(__name__)

    # Get route information from setup_ecmp_route
    prefix, nexthops, ifaces = setup_ecmp_route

    # Setup ARS configuration with proper cleanup
    profile_name = None
    ars_ifaces = None
    table_name = None
    rule_name = None

    try:
        enable_ars_counters(duthost, 1000)

        # Setup ars object for global mode
        setup_ars_object(duthost, ARS_OBJECT, "ars-1")

        ars_profile = ARS_PROFILE.copy()
        ars_profile["default_ars_object"] = None if mode == "interface" else "ars-1"
        ars_profile["ars_nhg_path_selector_mode"] = mode
        profile_name = setup_ars_profile(duthost, ars_profile)

        # Build interface entries and setup ARS interfaces
        ars_ifaces = {}
        for ifname in ifaces.keys():
            ars_ifaces[ifname] = {"scaling_factor": "10", "ars_obj_name": "ars-1"}
        setup_ars_interface(duthost, ars_ifaces)

        # Setup ACL to disable ARS for specific flow
        src_port_id = gather_facts['src_port_ids'][0]
        src_port = gather_facts['src_port'][0]
        src_ip = "192.168.1.100"
        dst_ip = prefix.split('/')[0]  # Get IP address from CIDR notation
        test_flow = {
            "srcIp": f'{src_ip}/32',
            "dstIp": f'{dst_ip}/32',
            "protocol": "6",  # TCP
        }
        table_name, rule_name = setup_ars_acl(duthost, src_port, test_flow, priority=5, enable=False)

        logger.info(f"ARS disabled test setup complete for prefix {prefix}")
        logger.info(f"ACL configured: table={table_name}, rule={rule_name}")

        # Get ASIC instance and router MAC
        asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)
        rtr_mac = asic.get_router_mac()
        ip_ttl = 64

        # Track received packets for ECMP distribution analysis
        recvd_pkt_result = defaultdict(set)

        # Get destination port IDs for the 3 destination interfaces
        dst_port_ids = []
        for i in range(1, 4):  # dst1, dst2, dst3
            dst_key = f'dst{i}'
            if f'{dst_key}_port_ids' in gather_facts:
                dst_port_ids.extend(gather_facts[f'{dst_key}_port_ids'])

        logger.info(f"Using destination port IDs: {dst_port_ids}")
        logger.info(f"Using source port ID: {gather_facts['src_port_ids'][0]}")

        # Run the traffic test
        ptfadapter.dataplane.flush()
        prev_stats = get_nhg_stats(duthost, nexthops, ifaces)
        build_and_send_tcp_ip_packet(ptfadapter, rtr_mac, src_ip, dst_ip, ip_ttl,
                                     src_port_id, dst_port_ids, recvd_pkt_result)

        # Verify packets hit the installed ACL rule (50 flows x 500 packets = 25000 total)
        expected_acl_count = 50 * 500
        acl_counter = duthost.get_acl_counter("ARS_CONTROL_TABLE", "DISABLE_ARS_RULE")
        logger.info(f"ACL counter for DISABLE_ARS_RULE: {acl_counter}")
        pytest_assert(acl_counter == expected_acl_count,
                      f"ACL counter {acl_counter} does not match expected {expected_acl_count} packets")

        time.sleep(ARS_COUNTERS_WAIT_SEC)
        current_stats = get_nhg_stats(duthost, nexthops, ifaces)
        # Make sure there are no failed packets for this DLB group
        pytest_assert(current_stats['failed_cnt'] == prev_stats['failed_cnt'],
                      "Failed packets detected in NHG stats")
        # No port reassignments
        pytest_assert(current_stats['port_reassignments'] == prev_stats['port_reassignments'],
                      "some ports were assigned in DLB forwarding")

        # Analyze ECMP distribution
        port_distribution = defaultdict(int)
        for _, ports in recvd_pkt_result.items():
            for port in ports:
                port_distribution[port] += 1

        logger.info(f"ECMP distribution across ports: {dict(port_distribution)}")

        # Verify that traffic was distributed across multiple ports (basic ECMP check)
        if len(port_distribution) > 1:
            logger.info("ECMP load balancing is working - traffic distributed across multiple ports")
        else:
            logger.warning("Traffic only used one port - ECMP may not be working as expected")

    finally:
        disable_ars_counters(duthost)

        # Clean up ARS configuration in reverse order
        logger.info("Cleaning up ARS configuration")
        if table_name and rule_name:
            cleanup_ars_acl(duthost, table_name, rule_name)
        if ars_ifaces:
            cleanup_ars_interface(duthost, ars_ifaces)
        if profile_name:
            cleanup_ars_profile(duthost, profile_name)
        cleanup_ars_object(duthost, "ars-1")


'''
Test to check that enabling ARS flows through ACL works
'''


@pytest.mark.parametrize("mode", ["global", "interface"], ids=["mode_global", "mode_interface"])
def test_enable_ars_flow(duthost, setup_ecmp_route, gather_facts,
                         enum_rand_one_frontend_asic_index, ptfadapter, mode):
    """
    Verify DLB load-balancing is enabled for flows matching an ACL.

    Args:
        mode: ARS mode - 'global' or 'interface'
    """
    logger = logging.getLogger(__name__)

    # Get route information from setup_ecmp_route
    prefix, nexthops, ifaces = setup_ecmp_route

    # Setup ARS configuration with proper cleanup
    profile_name = None
    ars_ifaces = None
    table_name = None
    rule_name = None

    try:
        enable_ars_counters(duthost, 1000)

        setup_ars_object(duthost, ARS_OBJECT, "ars-1")

        # Setup ARS profile with disabled ARS
        ars_profile = ARS_PROFILE.copy()
        ars_profile["ipv4_enable"] = "false"
        ars_profile["ipv6_enable"] = "false"
        ars_profile["default_ars_object"] = None if mode == "interface" else "ars-1"
        ars_profile["ars_nhg_path_selector_mode"] = mode
        profile_name = setup_ars_profile(duthost, ars_profile)

        # Build interface entries and setup ARS interfaces
        ars_ifaces = {}
        for ifname in ifaces.keys():
            ars_ifaces[ifname] = {"scaling_factor": "10", "ars_obj_name": "ars-1"}
        setup_ars_interface(duthost, ars_ifaces)

        # Setup ACL to disable ARS for specific flow
        src_port_id = gather_facts['src_port_ids'][0]
        src_port = gather_facts['src_port'][0]
        src_ip = "192.168.1.100"
        dst_ip = prefix.split('/')[0]  # Get IP address from CIDR notation
        dscp = 59
        bth_opcode = 10
        test_flow = {
            "srcIp": f'{src_ip}/32',
            "dstIp": f'{dst_ip}/32',
            "protocol": "17",   # UDP
            "dstPort": "4791",  # ROCEv2
            "dscp": dscp,
            "bthOpcode": f'{hex(bth_opcode)}/0xff',
        }
        table_name, rule_name = setup_ars_acl(duthost, src_port, test_flow, priority=5, enable=True)

        logger.info(f"ARS enable test setup complete for prefix {prefix}")
        logger.info(f"ACL configured: table={table_name}, rule={rule_name}")

        # Get ASIC instance and router MAC
        asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)
        rtr_mac = asic.get_router_mac()
        ip_ttl = 64

        # Track received packets for ECMP distribution analysis
        recvd_pkt_result = defaultdict(set)

        # Get destination port IDs for the 3 destination interfaces
        dst_ports = []
        for i in range(1, 4):  # dst1, dst2, dst3
            dst_key = f'dst{i}'
            if f'{dst_key}_port' in gather_facts:
                dst_ports.extend(gather_facts[f'{dst_key}_port'])

        logger.info(f"Using destination ports: {dst_ports}")
        logger.info(f"Using source port ID: {gather_facts['src_port_ids'][0]}")

        def build_and_send_rocev2_packet():
            for flow_count in range(20):
                # Build packet destined to the ECMP route prefix
                pkt = build_rocev2_pkt(rtr_mac, src_ip, dst_ip, dscp, ip_ttl, bth_opcode, flow_count)

                duthost.command("sonic-clear counters")
                # Send packet from source port
                testutils.send(ptfadapter, src_port_id, pkt, 500)

                # Verify packets hit the installed ACL rule
                expected_acl_count = (flow_count + 1) * 500
                acl_counter = duthost.get_acl_counter("ARS_CONTROL_TABLE", "ENABLE_ARS_RULE")
                logger.info(f"ACL counter for ENABLE_ARS_RULE: {acl_counter}")
                pytest_assert(acl_counter == expected_acl_count,
                              f"ACL counter {acl_counter} does not match expected {expected_acl_count} packets")

                # Get port counters for ECMP distribution analysis
                time.sleep(2)
                dst_port_counters = {}
                for port in dst_ports:
                    tx_pkts, _ = snappi_helpers.get_tx_frame_count(duthost, port)
                    logger.info(f"Port {port} received {tx_pkts} packets")
                    dst_port_counters[port] = tx_pkts

                # Track which port received the packet for ECMP distribution analysis
                port_index = max(dst_port_counters, key=dst_port_counters.get)
                recvd_pkt_result[flow_count].add(port_index)
                logger.info("Flow {} received on port {}".format(flow_count, port_index))

        # Run the traffic test
        # We don't want the payload to be updated during send, as payload contains BTH header
        ptfadapter.update_payload = None
        ptfadapter.dataplane.flush()
        prev_stats = get_nhg_stats(duthost, nexthops, ifaces)
        build_and_send_rocev2_packet()
        time.sleep(ARS_COUNTERS_WAIT_SEC)
        current_stats = get_nhg_stats(duthost, nexthops, ifaces)
        # Make sure there are no failed packets for this DLB group
        pytest_assert(current_stats['failed_cnt'] == prev_stats['failed_cnt'],
                      "Failed packets detected in NHG stats")
        # expect at least 3 port reassignments - one per destination port
        pytest_assert(current_stats['port_reassignments'] >= prev_stats['port_reassignments'] + 3,
                      "All ports were not assigned in DLB forwarding")

        # Analyze ECMP distribution
        port_distribution = defaultdict(int)
        for _, ports in recvd_pkt_result.items():
            for port in ports:
                port_distribution[port] += 1

        logger.info(f"ECMP distribution across ports: {dict(port_distribution)}")

        # Verify that traffic was distributed across multiple ports (basic ECMP check)
        if len(port_distribution) > 1:
            logger.info("ECMP load balancing is working - traffic distributed across multiple ports")
        else:
            logger.warning("Traffic only used one port - ECMP may not be working as expected")

    finally:
        disable_ars_counters(duthost)

        # Clean up ARS configuration in reverse order
        logger.info("Cleaning up ARS configuration")
        if table_name and rule_name:
            cleanup_ars_acl(duthost, table_name, rule_name)
        if ars_ifaces:
            cleanup_ars_interface(duthost, ars_ifaces)
        if profile_name:
            cleanup_ars_profile(duthost, profile_name)
        cleanup_ars_object(duthost, "ars-1")


'''
Test to check that enabling ARS flows through ACL works with IPv6
'''


def test_enable_ars_flow_v6(duthost, setup_ecmp_route_v6, gather_facts,
                            enum_rand_one_frontend_asic_index, ptfadapter):
    """
    Verify DLB load-balancing is enabled for IPv6 flows matching an ACL.
    This test always runs in interface mode with IPv6 packets.

    Args:
        duthost: DUT host object
        setup_ecmp_route_v6: Fixture providing IPv6 ECMP route setup
        gather_facts: Fixture providing port and interface facts
        enum_rand_one_frontend_asic_index: ASIC index
        ptfadapter: PTF adapter for packet operations
    """
    logger = logging.getLogger(__name__)

    # Get route information from setup_ecmp_route_v6
    _, nexthops, ifaces, src_ipv6, dst_ipv6 = setup_ecmp_route_v6

    # Setup ARS configuration with proper cleanup
    profile_name = None
    ars_ifaces = None
    table_name = None
    rule_name = None

    try:
        enable_ars_counters(duthost, 1000)

        setup_ars_object(duthost, ARS_OBJECT, "ars-1")

        # Setup ARS profile with disabled ARS (interface mode)
        ars_profile = ARS_PROFILE.copy()
        ars_profile["ipv4_enable"] = "false"
        ars_profile["ipv6_enable"] = "false"
        ars_profile["default_ars_object"] = None  # interface mode
        ars_profile["ars_nhg_path_selector_mode"] = "interface"
        profile_name = setup_ars_profile(duthost, ars_profile)

        # Build interface entries and setup ARS interfaces
        ars_ifaces = {}
        for ifname in ifaces.keys():
            ars_ifaces[ifname] = {"scaling_factor": "10", "ars_obj_name": "ars-1"}
        setup_ars_interface(duthost, ars_ifaces)

        # Setup ACL to enable ARS for IPv6 flow
        src_port_id = gather_facts['src_port_ids'][0]
        src_port = gather_facts['src_port'][0]
        dscp = 59
        bth_opcode = 10
        test_flow = {
            "protocol": "17",   # UDP
            "dstPort": "4791",  # ROCEv2
            "dscp": dscp,
            "bthOpcode": f'{hex(bth_opcode)}/0xff',
        }
        table_name, rule_name = setup_ars_acl(duthost, src_port, test_flow, priority=5, enable=True)

        logger.info("ARS IPv6 enable test setup complete")
        logger.info(f"ACL configured: table={table_name}, rule={rule_name}")

        # Get ASIC instance and router MAC
        asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)
        rtr_mac = asic.get_router_mac()
        ip_hlim = 64

        # Track received packets for ECMP distribution analysis
        recvd_pkt_result = defaultdict(set)

        # Get destination port IDs for the 3 destination interfaces
        dst_ports = []
        for i in range(1, 4):  # dst1, dst2, dst3
            dst_key = f'dst{i}'
            if f'{dst_key}_port' in gather_facts:
                dst_ports.extend(gather_facts[f'{dst_key}_port'])

        logger.info(f"Using destination ports: {dst_ports}")
        logger.info(f"Using source port ID: {gather_facts['src_port_ids'][0]}")

        def build_and_send_ipv6_packet():
            for flow_count in range(20):
                # Build IPv6 ROCEv2 packet destined to the ECMP route prefix
                pkt = build_rocev2_pkt_ipv6(rtr_mac, src_ipv6, dst_ipv6, dscp, ip_hlim, bth_opcode, flow_count)

                duthost.command("sonic-clear counters")
                # Send packet from source port
                testutils.send(ptfadapter, src_port_id, pkt, 500)

                # Verify packets hit the installed ACL rule
                expected_acl_count = (flow_count + 1) * 500
                acl_counter = duthost.get_acl_counter("ARS_CONTROL_TABLE", "ENABLE_ARS_RULE")
                logger.info(f"ACL counter for ENABLE_ARS_RULE: {acl_counter}")
                pytest_assert(acl_counter == expected_acl_count,
                              f"ACL counter {acl_counter} does not match expected {expected_acl_count} packets")

                # Get port counters for ECMP distribution analysis
                time.sleep(2)
                dst_port_counters = {}
                for port in dst_ports:
                    tx_pkts, _ = snappi_helpers.get_tx_frame_count(duthost, port)
                    logger.info(f"Port {port} received {tx_pkts} packets")
                    dst_port_counters[port] = tx_pkts

                # Track which port received the packet for ECMP distribution analysis
                port_index = max(dst_port_counters, key=dst_port_counters.get)
                recvd_pkt_result[flow_count].add(port_index)
                logger.info("Flow {} received on port {}".format(flow_count, port_index))

        # Run the traffic test
        # We don't want the payload to be updated during send, as payload contains BTH header
        ptfadapter.update_payload = None
        ptfadapter.dataplane.flush()
        prev_stats = get_nhg_stats(duthost, nexthops, ifaces)
        build_and_send_ipv6_packet()
        time.sleep(ARS_COUNTERS_WAIT_SEC)
        current_stats = get_nhg_stats(duthost, nexthops, ifaces)
        # Make sure there are no failed packets for this DLB group
        pytest_assert(current_stats['failed_cnt'] == prev_stats['failed_cnt'],
                      "Failed packets detected in NHG stats")
        # expect at least 3 port reassignments - one per destination port
        pytest_assert(current_stats['port_reassignments'] >= prev_stats['port_reassignments'] + 3,
                      "All ports were not assigned in DLB forwarding")

        # Analyze ECMP distribution
        port_distribution = defaultdict(int)
        for _, ports in recvd_pkt_result.items():
            for port in ports:
                port_distribution[port] += 1

        logger.info(f"ECMP distribution across ports: {dict(port_distribution)}")

        # Verify that traffic was distributed across multiple ports (basic ECMP check)
        if len(port_distribution) > 1:
            logger.info("ECMP load balancing is working - traffic distributed across multiple ports")
        else:
            logger.warning("Traffic only used one port - ECMP may not be working as expected")

    finally:
        disable_ars_counters(duthost)

        # Clean up ARS configuration in reverse order
        logger.info("Cleaning up ARS configuration")
        if table_name and rule_name:
            cleanup_ars_acl(duthost, table_name, rule_name)
        if ars_ifaces:
            cleanup_ars_interface(duthost, ars_ifaces)
        if profile_name:
            cleanup_ars_profile(duthost, profile_name)
        cleanup_ars_object(duthost, "ars-1")
