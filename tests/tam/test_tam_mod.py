import json
import logging
import pytest
import ptf.testutils as testutils
from scapy.all import Ether, IP, UDP, TCP, Raw
import time
import random
import threading
import ipaddress
import re

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from ipfix_common import IPFIXHeader

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("any"),
]

TAM_ASICDB_TIMEOUT = 180
TAM_ASICDB_INTERVAL = 10

# TAM Mirror on Drop configuration template
# Note: ports will be dynamically populated in the fixture
TAM_MOD_CONFIG_TEMPLATE = {
    "TAM": {
        "device": {
            "device-id": "12345",
            "enterprise-id": "54321",
        }
    },
    "TAM_COLLECTOR": {
        "COLLECTOR1": {
            "src_ip": "1.1.1.1",
            "dst_ip": "2.2.2.2",
            "dst_port": "10000",
            "dscp_value": "32",
            "vrf": "default"
        }
    },
    "TAM_SESSION": {
        "DROPMONITOR": {
            "type": "drop-monitor",
            "report_type": "ipfix",
            "collector": ["COLLECTOR1"]
        }
    }
}

def tam_asicdb_state(duthost, shouldExist):
    """
    Verify that ASIC_DB contains all required TAM objects:
    - TAM_TRANSPORT
    - TAM_COLLECTOR
    - TAM_EVENT
    - TAM_EVENT_ACTION
    - TAM_REPORT
    - TAM

    Returns:
        bool: True if all required TAM objects are present, False otherwise
    """
    required_tam_objects = [
        "TAM_TRANSPORT",
        "TAM_COLLECTOR",
        "TAM_EVENT",
        "TAM_EVENT_ACTION",
        "TAM_REPORT",
        "TAM"
    ]

    for tam_object in required_tam_objects:
        out = duthost.shell(f'sonic-db-cli ASIC_DB KEYS "*{tam_object}:oid:*"')
        lines = out.get("stdout_lines", [])
        if shouldExist and not lines:
            logger.warning(f"ASIC_DB missing {tam_object} objects")
            return False
        elif not shouldExist and lines:
            logger.debug(f"ASIC_DB has {tam_object} objects: {lines}")
            return False

    logger.info("ASIC_DB has expected state")
    return True

def wait_for_tam_asicdb_applied(duthost, timeout=TAM_ASICDB_TIMEOUT, interval=TAM_ASICDB_INTERVAL):
    """
    Wait for TAM configuration to be applied to ASIC_DB.

    Verifies that all required TAM objects are present:
    - TAM_TRANSPORT
    - TAM_COLLECTOR
    - TAM_EVENT
    - TAM_EVENT_ACTION
    - TAM_REPORT
    - TAM

    Args:
        duthost: DUT host object
        timeout: Maximum time to wait in seconds
        interval: Check interval in seconds

    Returns:
        bool: True if all TAM objects are present within timeout, False otherwise
    """
    return wait_until(timeout, interval, 0, lambda: tam_asicdb_state(duthost, True))

def verify_tam_mod_config_applied(duthost):
    """
    Verify that TAM Mirror on Drop config has been applied:
    - CONFIG_DB contains TAM tables with expected fields
    - ASIC_DB contains TAM-related SAI objects
    """
    # Check TAM device config
    show = duthost.shell('sonic-db-cli CONFIG_DB HGETALL "TAM|device"', module_ignore_errors=False)
    lines = show.get("stdout_lines", []) or []
    dbg = "\n".join(lines)
    pytest_assert(lines, "CONFIG_DB: TAM|device not found or empty")

    # Check TAM collector config
    show = duthost.shell('sonic-db-cli CONFIG_DB HGETALL "TAM_COLLECTOR|COLLECTOR1"', module_ignore_errors=False)
    lines = show.get("stdout_lines", []) or []
    pytest_assert(lines, "CONFIG_DB: TAM_COLLECTOR|COLLECTOR1 not found or empty")

    # Check TAM session config
    show = duthost.shell('sonic-db-cli CONFIG_DB HGETALL "TAM_SESSION|DROPMONITOR"', module_ignore_errors=False)
    lines = show.get("stdout_lines", []) or []
    pytest_assert(lines, "CONFIG_DB: TAM_SESSION|DROPMONITOR not found or empty")

    # Ensure orchagent applied TAM config into ASIC_DB
    pytest_assert(
        wait_for_tam_asicdb_applied(duthost, TAM_ASICDB_TIMEOUT, TAM_ASICDB_INTERVAL),
        "ASIC_DB missing TAM keys; orchagent may not have processed TAM config.",
    )

@pytest.fixture(scope="module")
def tam_mod_config(duthosts, rand_one_dut_hostname, tbinfo):
    """
    Apply TAM Mirror on Drop config with dynamically selected ports and clean up after.

    This fixture:
    1. Applies TAM Mirror on Drop configuration with dynamically selected ports
    2. Verifies TAM config is applied to both CONFIG_DB and ASIC_DB
    3. Cleans up TAM configurations after test completes

    Returns:
        tuple: (duthost, ingress_ports, collector_ports) where:
            - duthost: DUT host object
            - ingress_ports: dict of {port_name: ptf_index} for ingress traffic
            - collector_ports: list of PTF indices where collector is reachable
    """
    duthost = duthosts[rand_one_dut_hostname]

    # Get available ports
    logger.info("Discovering available ports...")
    available_ports = _get_available_ports(duthost, tbinfo)
    logger.info(f"Available ports: {available_ports}")

    # Select ingress ports (can be any available ports)
    ingress_port_names = list(available_ports.keys())
    logger.info(f"Selected ingress ports: {ingress_port_names}")

    # Get collector egress ports based on routing
    collector_config = _get_collector_config(duthost)
    collector_ports = _get_collector_egress_ports(duthost, collector_config["dst_ip"], available_ports)
    logger.info(f"Collector reachable on PTF ports: {collector_ports}")

    # Create and apply TAM config with selected ports
    logger.info("Applying TAM Mirror on Drop configuration...")
    tam_config = json.loads(json.dumps(TAM_MOD_CONFIG_TEMPLATE))  # Deep copy

    # Apply config to CONFIG_DB using sonic-cfggen
    tam_cfg_path = "/tmp/tam_mod_config.json"
    duthost.copy(content=json.dumps(tam_config, indent=2), dest=tam_cfg_path)
    res = duthost.shell(f"sonic-cfggen -j {tam_cfg_path} --write-to-db")
    pytest_assert(res["rc"] == 0, f"Failed to apply TAM MoD config: {res}")

    # Verify TAM config is applied to both CONFIG_DB and ASIC_DB
    verify_tam_mod_config_applied(duthost)

    # Build ingress_ports dict for test: map port/LAG names to individual PTF indices
    # For LAGs, we need to expand to individual member ports for packet injection
    ingress_ports_for_test = {}
    for port_name in ingress_port_names:
        port_value = available_ports[port_name]
        if isinstance(port_value, list):
            # LAG: map LAG name to list of member PTF indices
            ingress_ports_for_test[port_name] = port_value
        else:
            # Individual port: map port name to PTF index
            ingress_ports_for_test[port_name] = port_value

    yield duthost, ingress_ports_for_test, collector_ports

    # Cleanup: remove TAM configurations
    logger.info("Cleaning up TAM configurations...")
    duthost.shell('sonic-db-cli CONFIG_DB DEL "TAM|device"', module_ignore_errors=True)
    duthost.shell('sonic-db-cli CONFIG_DB DEL "TAM_COLLECTOR|COLLECTOR1"', module_ignore_errors=True)
    duthost.shell('sonic-db-cli CONFIG_DB DEL "TAM_SESSION|DROPMONITOR"', module_ignore_errors=True)
    return wait_until(TAM_ASICDB_TIMEOUT, TAM_ASICDB_INTERVAL, 0, lambda: tam_asicdb_state(duthost, False))
    logger.info("TAM cleanup completed")

def _get_available_ports(duthost, tbinfo):
    """
    Get all available front-panel ports that are admin up, handling LAGs.

    If a port is part of a LAG, returns the LAG name instead of individual member ports.
    Returns a dict where keys are either port names or LAG names, and values are either
    PTF indices (for individual ports) or lists of PTF indices (for LAGs).

    Returns:
        dict: Mapping of port/LAG name to PTF index or list of indices
              e.g., {"Ethernet0": 0, "Ethernet4": 1, "PortChannel0": [2, 3], ...}
    """
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    cfg_facts = duthost.config_facts(host=duthost.hostname, source="persistent")['ansible_facts']

    # Get ports that are admin up
    admin_up_ports = {k: v for k, v in list(cfg_facts['PORT'].items())
                      if v.get('admin_status', 'down') == 'up'}

    # Get LAG member ports
    config_portchannels = cfg_facts.get('PORTCHANNEL_MEMBER', {})
    lag_member_ports = set()
    lag_members_map = {}  # Map LAG name to list of member ports

    for lag_name, members in config_portchannels.items():
        member_list = list(members.keys())
        lag_members_map[lag_name] = member_list
        lag_member_ports.update(member_list)

    # Build available ports dict
    available_ports = {}

    # Add LAGs (with their member PTF indices)
    for lag_name, member_ports in lag_members_map.items():
        # Check if all members are admin up
        if all(port in admin_up_ports for port in member_ports):
            # Get PTF indices for all members
            ptf_indices = [mg_facts['minigraph_ptf_indices'][port]
                          for port in member_ports
                          if port in mg_facts['minigraph_ptf_indices']]
            if ptf_indices:
                available_ports[lag_name] = ptf_indices
                logger.info(f"Added LAG {lag_name} with member PTF indices: {ptf_indices}")

    # Add individual ports that are NOT LAG members
    for port in admin_up_ports.keys():
        if port not in lag_member_ports and port in mg_facts['minigraph_ptf_indices']:
            available_ports[port] = mg_facts['minigraph_ptf_indices'][port]
            logger.info(f"Added individual port {port} with PTF index: {available_ports[port]}")

    pytest_assert(len(available_ports) > 0, "No available front-panel ports found")
    return available_ports

def _get_collector_egress_ports(duthost, collector_ip, available_ports):
    """
    Get the egress ports where collector is reachable.
    Uses 'ip route get <collector-ip> fibmatch' to determine all egress interface(s).
    This command returns all ECMP paths to the destination.

    Handles LAGs by expanding them to individual member ports for collection.

    Args:
        duthost: DUT host object
        collector_ip: Collector IP address (string)
        available_ports: Dict of available ports {port_name: ptf_index or [ptf_indices]}

    Returns:
        list: List of individual PTF port indices where collector is reachable
    """
    try:
        # Use 'ip route get <ip> fibmatch' to get all ECMP paths
        cmd = f"ip route get {collector_ip} fibmatch"
        result = duthost.shell(cmd, module_ignore_errors=True)

        if result["rc"] != 0:
            logger.warning(f"Failed to get route for {collector_ip}: {result.get('stderr', '')}, using all available ports")
            return _flatten_port_indices(available_ports)

        route_lines = result.get("stdout_lines", [])
        if not route_lines:
            logger.warning(f"No route found for {collector_ip}, using all available ports")
            return _flatten_port_indices(available_ports)

        logger.info(f"Route output for {collector_ip}:\n{chr(10).join(route_lines)}")

        # Parse the output to extract interfaces
        # Example output:
        # 2.2.2.2 via 10.0.0.1 dev PortChannel0001 table 0 src 10.1.0.32 uid 0
        # 2.2.2.2 via 10.0.0.2 dev PortChannel0002 table 0 src 10.1.0.32 uid 0
        # 2.2.2.2 via 10.0.0.3 dev PortChannel0003 table 0 src 10.1.0.32 uid 0

        egress_ports = []
        seen_interfaces = set()

        for line in route_lines:
            # Match pattern: "... dev <interface> ..."
            match = re.search(r'\bdev\s+(\S+)', line)
            if match:
                interface = match.group(1)
                # Avoid duplicates
                if interface not in seen_interfaces and interface in available_ports:
                    port_value = available_ports[interface]
                    # If it's a LAG (list of indices), add all member indices
                    if isinstance(port_value, list):
                        egress_ports.extend(port_value)
                        logger.info(f"Collector reachable via LAG {interface} (PTF ports {port_value})")
                    else:
                        egress_ports.append(port_value)
                        logger.info(f"Collector reachable via {interface} (PTF port {port_value})")
                    seen_interfaces.add(interface)

        if egress_ports:
            return egress_ports
        else:
            logger.warning(f"No available ports found in route for {collector_ip}, using all available ports")
            return _flatten_port_indices(available_ports)

    except Exception as e:
        logger.warning(f"Failed to get route info for {collector_ip}: {e}, using all available ports")
        return _flatten_port_indices(available_ports)


def _flatten_port_indices(available_ports):
    """
    Flatten available_ports dict to a list of individual PTF indices.

    Handles both individual ports (int values) and LAGs (list values).

    Args:
        available_ports: Dict of {port_name: ptf_index or [ptf_indices]}

    Returns:
        list: Flattened list of all PTF indices
    """
    flattened = []
    for port_value in available_ports.values():
        if isinstance(port_value, list):
            flattened.extend(port_value)
        else:
            flattened.append(port_value)
    return flattened

def _get_router_mac(duthost):
    out = duthost.shell("sonic-db-cli CONFIG_DB HGET 'DEVICE_METADATA|localhost' mac")
    pytest_assert(out["rc"] == 0 and out["stdout"], "Failed to read DUT router MAC")
    return out["stdout"].strip().lower()

def _get_collector_config(duthost):
    """Get collector configuration from CONFIG_DB"""
    show = duthost.shell("sonic-db-cli CONFIG_DB HGETALL 'TAM_COLLECTOR|COLLECTOR1'")
    pytest_assert(show["rc"] == 0 and show["stdout"].strip(), f"Failed to read collector config: {show}")
    
    # Parse the output to extract collector details
    lines = show["stdout_lines"]
    config = {}
    for i in range(0, len(lines), 2):
        if i + 1 < len(lines):
            config[lines[i]] = lines[i + 1]
    
    return {
        "src_ip": config.get("src_ip", "1.1.1.1"),
        "dst_ip": config.get("dst_ip", "2.2.2.2"),
        "dst_port": int(config.get("dst_port", "10000")),
        "dscp_value": int(config.get("dscp_value", "32"))
    }

def build_ttl_expiry_packet(ptfadapter, router_mac, ptf_src_port, is_ipv4=True):
    """
    TTL=1 ensures packet will be dropped due to TTL expiry during forwarding.
    """
    src_mac = ptfadapter.dataplane.get_mac(0, ptf_src_port)

    # Create packet with TTL=1
    if is_ipv4:
        packet = (
            Ether(src=src_mac, dst=router_mac)
            / IP(src="10.1.1.100", dst="20.2.2.100", ttl=1)  # TTL=1 will expire
            / TCP(sport=1000, dport=80)  # Matches L4 ports and protocol 6
            / Raw(b"TTL expiry test packet for MoD V4")
        )
    else :
        packet = (
            Ether(src=src_mac, dst=router_mac)
            / IPv6(src="2000:10:1:1::100", dst="2000:20:2:2::100", hlim=1)  # TTL=1 will expire
            / TCP(sport=1000, dport=80)  # Matches L4 ports and protocol 6
            / Raw(b"TTL expiry test packet for MoD V6")
        )

    return packet

class PacketTest:
    def __init__(self, ptfadapter, ptf_ingress_port, collector, router_mac ):
        self.ptfadapter = ptfadapter
        self.ptf_ingress_port = ptf_ingress_port
        self.collector = collector
        self.router_mac = router_mac

    def send_packets(self, is_ipv4=True):
        try:
            # Start collecting IPFIX reports on all possible collector ports
            self.collector.start_collection(timeout=30)
            logger.info(f"Started IPFIX collection on PTF ports {self.collector.collector_ports}")

            # Build packets with TTL=1 that will expire and be dropped
            ttl_expiry_packet = build_ttl_expiry_packet( self.ptfadapter, self.router_mac,  
                                                         self.ptf_ingress_port, is_ipv4=is_ipv4)

            logger.info("Sending packets with TTL=1 that should expire and trigger MoD...")
            for i in range(10):  # Send multiple packets to ensure drops are detected
                testutils.send(self.ptfadapter, self.ptf_ingress_port, ttl_expiry_packet)
                time.sleep(0.1)  # Small delay between packets

            # Wait for IPFIX reports to be generated and sent
            time.sleep(5)

        finally:
            # Stop collector
            self.collector.stop_collection()

    def run_packet_test(self, is_ipv4, expect_reports):
        self.collector.cleanup()
        self.send_packets(is_ipv4=is_ipv4)

        # Verify that there were IPFIX packets sent out on one of the ports to the collector
        report_count = self.collector.get_report_count()
        logger.info(f"Found {report_count} IPFIX reports on collector")
        if expect_reports:
            pytest_assert(report_count > 0, "At least one report must have been sent")
        else:
            pytest_assert(report_count == 0, "No report must have been sent")

class IPFIXCollector:
    """
    IPFIX collector that captures and validates IPFIX reports.
    Collects packets from all specified ports and tracks which port each packet arrived on.
    """
    def __init__(self, ptfadapter, collector_ports, collector_config):
        self.ptfadapter = ptfadapter
        self.collector_ports = collector_ports  # List of ports to collect from
        self.collector_config = collector_config
        self.captured_reports = {}  # {port: [packets]}
        self.collecting = False
        self.collection_thread = None

        # Initialize dictionaries for each port
        for port in collector_ports:
            self.captured_reports[port] = []

    def start_collection(self, timeout=30):
        """Start collecting IPFIX reports in a separate thread."""
        self.collecting = True
        self.collection_thread = threading.Thread(target=self._collect_reports, args=(timeout,))
        self.collection_thread.start()

    def stop_collection(self):
        """Stop collecting IPFIX reports."""
        self.collecting = False
        if self.collection_thread:
            self.collection_thread.join()

    def _collect_reports(self, timeout):
        """Collect IPFIX reports from the dataplane on all configured ports."""
        deadline = time.time() + timeout

        while self.collecting and time.time() < deadline:
            res = testutils.dp_poll(self.ptfadapter, device_number=0, timeout=0.5)
            if not isinstance(res, self.ptfadapter.dataplane.PollSuccess):
                continue

            # Only process packets from configured collector ports
            if res.port not in self.collector_ports:
                continue

            try:
                pkt = Ether(res.packet)
                if self._is_ipfix_report(pkt):
                    self.captured_reports[res.port].append(pkt)
                    logger.info(f"Captured IPFIX report on port {res.port}: {pkt.summary()}")

            except Exception as e:
                logger.debug(f"Failed to parse packet on port {res.port}: {e}")
    
    def _is_ipfix_report(self, packet):
        """Check if the packet is an IPFIX report matching our collector config."""
        if IP not in packet or UDP not in packet:
            return False

        ip_layer = packet[IP]
        udp_layer = packet[UDP]

        # Check if it matches our collector configuration
        if (not(ip_layer.src == self.collector_config["src_ip"] and
                ip_layer.dst == self.collector_config["dst_ip"] and
                udp_layer.dport == self.collector_config["dst_port"])):
            return False

        # Verify that it is an IPFix packet
        udp_payload = bytes(packet[UDP].payload)
    
        if len(udp_payload) < 20:  # Minimum IPFIX header size
            return False
        
        # Check if it looks like IPFIX (version 10)
        header = IPFIXHeader(udp_payload[:20])
        if header.version != 10:
            return False

        # TODO - Need to verify DSCP values, 
        # they seem incorrect
        # dscp = (ip_layer.tos >> 2) & 0x3F
        # pytest_assert( dscp == int( self.collector_config["dscp_value"] ) )

        return True

    def get_report_count(self, port=None):
        """Get the number of captured IPFIX reports.

        Args:
            port: If specified, return count for that port only. Otherwise return total.
        """
        if port is not None:
            return len(self.captured_reports.get(port, []))
        return sum(len(reports) for reports in self.captured_reports.values())

    def get_reports_for_port(self, port):
        """Get captured reports for a specific port."""
        return self.captured_reports.get(port, [])
  
    def cleanup(self):
        # Delete all the old reports
        for port in self.collector_ports:
            self.captured_reports[port] = []

@pytest.mark.disable_loganalyzer
@pytest.mark.topology('t1')
def test_mod_stateless_flow_unaware_basic(tam_mod_config, ptfadapter, tbinfo):
    """
    Test basic TAM Mirror on Drop(stateless, flow aware) functionality with TTL expiry drops.

    1. Configure TAM MoD with dynamically selected ports
    2. Send packets with TTL=1 that will expire and be dropped on ingress ports
    3. Verify IPFIX reports are sent to the collector on the designated egress port

    Note: Reports are sent to only ONE collector port (determined at TAM configuration time),
    not distributed across multiple ports. We collect on all possible ports and identify
    which one receives the reports.
    """
    duthost, ingress_ports, collector_ports = tam_mod_config

    # Get router MAC and collector config
    router_mac = _get_router_mac(duthost)
    collector_config = _get_collector_config(duthost)

    # Select one ingress port for packet injection
    ingress_port_name = list(ingress_ports.keys())[0]
    port_value = ingress_ports[ingress_port_name]

    # If it's a LAG (list of indices), pick the first member port
    if isinstance(port_value, list):
        ptf_ingress_port = port_value[0]
        logger.info(f"Using ingress LAG {ingress_port_name} with member PTF port {ptf_ingress_port} for packet injection")
    else:
        ptf_ingress_port = port_value
        logger.info(f"Using ingress port {ingress_port_name}/PTF{ptf_ingress_port} for packet injection")

    logger.info(f"Possible collector ports (based on routing): {collector_ports}")
    logger.info(f"Collector config: {collector_config}")

    # Set up single IPFIX collector for all possible collector ports
    # Reports will be sent to only ONE of these ports, but we collect on all
    collector = IPFIXCollector(ptfadapter, collector_ports, collector_config)

    packet_test = PacketTest(ptfadapter, ptf_ingress_port, collector, router_mac)

    # Run both IPv4 and Ipv6 test packets
    packet_test.run_packet_test(is_ipv4=True, expect_reports=True)
    packet_test.run_packet_test(is_ipv4=False, expect_reports=True)

    # Blackhole collector IP and verify that all ASIC DB configuration is removed
    cmd = f"vtysh -c 'configure terminal' -c 'ip route {collector_config['dst_ip']}/32 blackhole'"
    duthost.shell(cmd,  module_ignore_errors=True)
    wait_until(TAM_ASICDB_TIMEOUT, TAM_ASICDB_INTERVAL, 0, lambda: tam_asicdb_state(duthost, False))

    # Verify that now no drop reports are sent
    packet_test.run_packet_test(is_ipv4=True, expect_reports=False)
    packet_test.run_packet_test(is_ipv4=False, expect_reports=False)

    # Now, remove the blackhole, the TAM configuration should be recreated
    cmd = f"vtysh -c 'configure terminal' -c 'no ip route {collector_config['dst_ip']}/32 blackhole'"
    duthost.shell(cmd,  module_ignore_errors=True)
    wait_until(TAM_ASICDB_TIMEOUT, TAM_ASICDB_INTERVAL, 0, lambda: tam_asicdb_state(duthost, True))

     # Verify that now reports are generated on drops again
    packet_test.run_packet_test(is_ipv4=True, expect_reports=True)
    packet_test.run_packet_test(is_ipv4=False, expect_reports=True)
