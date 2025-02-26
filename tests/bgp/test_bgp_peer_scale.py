"""
Test BGP peer scaling by adding multiple BGP peers on SONiC DUTs.
"""
import logging
import pytest
import ipaddress
import time
import re
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.config_reload import config_reload

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("t1"),
]

# Constants for BGP peer scaling
BASE_VLAN_ID = 2000  # Starting VLAN ID for new peers
BASE_BGP_ASN = 65100  # Starting ASN for new peers
PEERS_PER_DUT = 2  # Number of additional peers to configure per DUT


def verify_vlan_interface_status(duthost, vlan_id):
    """Verify VLAN interface status using show ip interfaces command."""
    vlan_intf = "Vlan{}".format(vlan_id)

    # Use show ip interfaces to check status
    output = duthost.shell("show ip interfaces")["stdout"]
    logger.debug("show ip interfaces output:\n%s", output)

    # Parse the output to check if interface exists and is up
    for line in output.splitlines():
        if vlan_intf in line:
            if "up/up" in line:
                logger.info("Interface %s is up: %s", vlan_intf, line.strip())
                return True
            else:
                logger.error("Interface %s is not up: %s", vlan_intf, line.strip())
                # Get additional interface details for debugging
                try:
                    config = duthost.shell(f"show runningconfiguration interface {vlan_intf}")["stdout"]
                    logger.error("Interface configuration:\n%s", config)
                except Exception as e:
                    logger.error("Failed to get interface configuration: %s", str(e))
                return False

    logger.error("Interface %s not found in show ip interfaces output", vlan_intf)
    # Check if VLAN exists
    vlan_output = duthost.shell("show vlan")["stdout"]
    if str(vlan_id) not in vlan_output:
        logger.error("VLAN %s does not exist in show vlan output", vlan_id)
    return False


def configure_bgp_peer(duthost, neighbor_ip, local_asn, remote_asn, addr_family="ipv4"):
    """Configure a BGP peer with proper peer group and timers."""
    try:
        peer_group = "PEER_V4" if ":" not in neighbor_ip else "PEER_V6"
        commands = [
            "vtysh -c 'configure terminal' "
            f"-c 'router bgp {local_asn}' "
            f"-c 'neighbor {neighbor_ip} remote-as {remote_asn}' "
            f"-c 'neighbor {neighbor_ip} peer-group {peer_group}' "
            f"-c 'neighbor {neighbor_ip} timers 3 10' "
            f"-c 'neighbor {neighbor_ip} timers connect 10' "
            f"-c 'address-family {addr_family} unicast' "
            f"-c 'neighbor {neighbor_ip} activate'"
        ]

        result = duthost.shell("\n".join(commands))
        if result['rc'] != 0:
            logger.error("Failed to configure BGP peer. Error: %s", result['stderr'])
            return False
        return True

    except Exception as e:
        logger.error("Failed to configure BGP peer: %s", str(e))
        return False


def get_free_ip_pair(vlan_id):
    """Get a pair of non-overlapping IP addresses for local and neighbor use based on VLAN ID."""
    # Use VLAN ID to create unique subnet for each VLAN
    # For example:
    # VLAN 2000 -> 192.168.200.0/24
    # VLAN 2001 -> 192.168.201.0/24
    third_octet = vlan_id % 256  # Ensure we stay within valid range
    network = f"192.168.{third_octet}.0/24"

    net = ipaddress.ip_network(network)
    # Use .1 for local IP and .2 for neighbor IP
    return str(net[1]), str(net[2])


def get_free_ipv6_pair(vlan_id):
    """Get a pair of non-overlapping IPv6 addresses for local and neighbor use based on VLAN ID."""
    # Use VLAN ID to create unique IPv6 subnet for each VLAN
    # For example:
    # VLAN 2000 -> 2001:db8:2000::/64
    # VLAN 2001 -> 2001:db8:2001::/64
    network = f"2001:db8:{vlan_id:x}::/64"

    net = ipaddress.ip_network(network)
    # Use ::1 for local IP and ::2 for neighbor IP
    return str(net[1]), str(net[2])


def ensure_port_is_up(duthost, port):
    """Ensure the port is up and operational."""
    try:
        def _check_port_state():
            output = duthost.shell("show interface status")["stdout"]
            for line in output.splitlines():
                if port in line:
                    fields = line.split()
                    # Ensure we have enough fields (Oper is 8th field, index 7)
                    if len(fields) >= 9:
                        oper_status = fields[7]
                        return oper_status.lower() == "up"
            return False

        if not wait_until(30, 2, 0, _check_port_state):
            logger.error(f"Port {port} failed to come up after 30 seconds")
            return False
        return True
    except Exception as e:
        logger.error(f"Error checking port status for {port} on {duthost.hostname}: {str(e)}")
        return False


def is_trunk_port(duthost, port):
    """Check if port is already configured as trunk."""
    try:
        output = duthost.shell("show interfaces switchport status")["stdout"]
        for line in output.splitlines():
            if port in line:
                # Port is trunk if mode is not 'routed'
                return 'routed' not in line
        logger.error(f"Port {port} not found in switchport status output")
        return False
    except Exception as e:
        logger.error(f"Error checking port mode for {port}: {str(e)}")
        return False


def convert_to_trunk_port(duthost, port, vlan_id=None):
    """
    Convert a routed port to trunk port while preserving its IP configuration.

    Args:
        duthost: DUT host object
        port: Port name to convert
        vlan_id: VLAN ID to move the IP address to

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Check if port is already a trunk
        if is_trunk_port(duthost, port):
            logger.info(f"Port {port} is already a trunk port")
            return True

        # Get current IP configurations
        existing_ip = get_interface_ip(duthost, port)
        existing_ipv6 = get_interface_ip(duthost, port, ip_version=6)
        logger.info(f"Detected IPs on {port} - IPv4: {existing_ip}, IPv6: {existing_ipv6}")

        # Remove IPv4 if it exists
        if existing_ip:
            try:
                duthost.shell(f"sudo config interface ip remove {port} {existing_ip}")
                def check_ipv4_removed():
                    return not get_interface_ip(duthost, port)
                if not wait_until(30, 2, 0, check_ipv4_removed):
                    logger.error(f"Timeout waiting for IPv4 {existing_ip} to be removed from {port}")
                    return False
            except Exception as e:
                logger.warning(f"Error while removing IPv4 {existing_ip} from {port}: {str(e)}")

        # Remove IPv6 if it exists
        if existing_ipv6:
            try:
                duthost.shell(f"sudo config interface ip remove {port} {existing_ipv6}")
                def check_ipv6_removed():
                    return not get_interface_ip(duthost, port, ip_version=6)
                if not wait_until(30, 2, 0, check_ipv6_removed):
                    logger.error(f"Timeout waiting for IPv6 {existing_ipv6} to be removed from {port}")
                    return False
            except Exception as e:
                logger.warning(f"Error while removing IPv6 {existing_ipv6} from {port}: {str(e)}")

        # Disable IPv6 link-local address
        try:
            duthost.shell(f"sudo config interface ipv6 disable use-link-local-only {port}")
            time.sleep(2)  # Wait for IPv6 link-local to be disabled
        except Exception as e:
            logger.warning(f"Error while disabling IPv6 link-local on {port}: {str(e)}")

        # Now configure interface as trunk
        result = duthost.shell(f"sudo config switchport mode trunk {port}")
        if result['rc'] != 0:
            logger.error(f"Failed to convert port to trunk: {result['stderr']}")
            return False

        # If we need to preserve IP configurations on a VLAN
        if vlan_id:
            # Configure VLAN and add port as member
            duthost.shell(f"sudo config vlan add {vlan_id}")
            duthost.shell(f"sudo config vlan member add {vlan_id} {port}")

            # Add IPs to VLAN interface if they existed
            vlan_intf = f"Vlan{vlan_id}"
            if existing_ip:
                duthost.shell(f"sudo config interface ip add {vlan_intf} {existing_ip}")
            if existing_ipv6:
                duthost.shell(f"sudo config interface ip add {vlan_intf} {existing_ipv6}")

        return ensure_port_is_up(duthost, port)

    except Exception as e:
        logger.error(f"Error converting port {port} to trunk on {duthost.hostname}: {str(e)}")
        return False


def get_interface_ip(duthost, interface, ip_version=4):
    """Get IP address configured on an interface.

    Args:
        duthost: DUT host object
        interface: Interface name to check
        ip_version: IP version (4 or 6)

    Returns:
        str: IP address with prefix or None if not found
    """
    try:
        if ip_version == 4:
            ip_intf_facts = duthost.show_ip_interface()['ansible_facts']['ip_interfaces']
            if interface in ip_intf_facts:
                intf_info = ip_intf_facts[interface]
                if 'ipv4' in intf_info and 'prefix_len' in intf_info:
                    return f"{intf_info['ipv4']}/{intf_info['prefix_len']}"
        else:
            ipv6_interfaces = duthost.show_ipv6_interfaces()
            if interface in ipv6_interfaces:
                ipv6_addr = ipv6_interfaces[interface].get('ipv6 address/mask')
                # Skip link-local addresses
                if ipv6_addr and not ipv6_addr.startswith('fe80:'):
                    return ipv6_addr
    except Exception as e:
        logger.error(f"Error getting IPv{ip_version} for interface {interface}: {str(e)}")
    return None


def get_remote_asn(duthost):
    """Get the remote ASN from the existing BGP neighbors."""
    bgp_facts = duthost.bgp_facts()['ansible_facts']
    existing_peers = bgp_facts.get('bgp_neighbors', {})
    for peer_ip, peer_data in existing_peers.items():
        if 'remote AS' in peer_data and ':' not in peer_ip:
            return peer_data['remote AS']
    pytest.fail("No valid remote ASN found in existing BGP neighbors")


def run_bgp_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo, addr_family="ipv4"):
    """
    Common helper function to run BGP peer scale tests for IPv4 or IPv6.

    Args:
        duthosts: DUT host objects
        enum_rand_one_per_hwsku_hostname: Test fixture
        nbrhosts: Neighbor host objects
        tbinfo: Testbed information dictionary containing topology details
        addr_family: Address family ("ipv4" or "ipv6")
    """
    configs = []

    for dut_index, duthost in enumerate(duthosts):
        # Get DUT configuration
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        local_asn = config_facts.get('DEVICE_METADATA', {}).get('localhost', {}).get('bgp_asn')
        remote_asn = get_remote_asn(duthost)

        # Get all current BGP neighbors for this DUT
        current_neighbors = [nbr["host"] for nbr in nbrhosts.values()]

        if not current_neighbors:
            logger.error(f"No existing BGP neighbors found for DUT {duthost.hostname}")
            continue

        # Create mapping between VM hostnames and DUT neighbor names
        vm_to_dut = {v["host"].hostname: k for k, v in nbrhosts.items()}

        # Get port connections between DUT and neighbors
        for neighbor_index, nbrhost in enumerate(current_neighbors):
            # Get the port connecting DUT to this neighbor
            neighbor_facts = duthost.get_extended_minigraph_facts(tbinfo)['minigraph_neighbors']
            dut_nbr_ports = {k: v['name'] for k, v in neighbor_facts.items()
                             if v['name'] == vm_to_dut[nbrhost.hostname]}

            if not dut_nbr_ports:
                pytest.fail(f"No connection found between {duthost.hostname} and {nbrhost.hostname}")

            # Get the first connected port pair
            dut_port = next(iter(dut_nbr_ports.keys()))
            nbr_port = neighbor_facts[dut_port]['port']

            # Get existing IP addresses before converting to trunk
            ip_version = 6 if addr_family == "ipv6" else 4
            dut_ip = get_interface_ip(duthost, dut_port, ip_version=ip_version)
            nbr_ip = get_interface_ip(nbrhost, nbr_port, ip_version=ip_version)

            if not dut_ip or not nbr_ip:
                pytest.fail(f"Could not get existing IPs for ports {dut_port}/{nbr_port}")

            # Use BASE_VLAN_ID for the first VLAN to preserve existing BGP session
            first_vlan_id = BASE_VLAN_ID + (dut_index * 100) + (neighbor_index * 10)

            # Convert ports to trunk while preserving IPs on first VLAN
            if not convert_to_trunk_port(duthost, dut_port, first_vlan_id):
                pytest.fail(f"Failed to convert {dut_port} to trunk on {duthost.hostname}")

            if not convert_to_trunk_port(nbrhost, nbr_port, first_vlan_id):
                pytest.fail(f"Failed to convert {nbr_port} to trunk on {nbrhost.hostname}")

            # Wait for original BGP session to recover
            neighbor_ip = nbr_ip.split('/')[0]  # Get IP without subnet mask
            if not wait_until(60, 5, 0, duthost.check_bgp_session_state, [neighbor_ip]):
                pytest.fail(f"Original BGP session failed to recover on {duthost.hostname}")

            # Configure additional peers for this neighbor
            for peer_index in range(PEERS_PER_DUT):
                vlan_id = BASE_VLAN_ID + (dut_index * 100) + (neighbor_index * 10) + peer_index
                vlan_intf = f"Vlan{vlan_id}"

                if addr_family == "ipv4":
                    local_ip, neighbor_ip = get_free_ip_pair(vlan_id)
                    ip_config = [(local_ip, neighbor_ip, "24")]
                else:
                    local_ip, neighbor_ip = get_free_ipv6_pair(vlan_id)
                    ip_config = [(local_ip, neighbor_ip, "64")]

                # Configure VLANs and add trunk ports as members
                duthost.shell(f"config vlan add {vlan_id}")
                duthost.shell(f"config vlan member add {vlan_id} {dut_port} --tagged")
                nbrhost.shell(f"config vlan add {vlan_id}")
                nbrhost.shell(f"config vlan member add {vlan_id} {nbr_port} --tagged")

                for lip, nip, prefix in ip_config:
                    duthost.add_ip_addr_to_vlan(vlan_intf, f"{lip}/{prefix}")
                    if not configure_bgp_peer(duthost, nip, local_asn, remote_asn, addr_family=addr_family):
                        pytest.fail(f"Failed to configure {addr_family} BGP peer on {duthost.hostname}")

                # Configure neighbor host side
                logger.info(f"Configuring BGP on neighbor host {nbrhost.hostname}")

                # Create VLAN and VLAN interface on neighbor
                nbrhost.shell(f"config vlan add {vlan_id}")
                nbrhost.shell(f"config vlan member add {vlan_id} \"Ethernet2\" --tagged")

                # Add IP address to VLAN interface on neighbor
                for lip, nip, prefix in ip_config:
                    nbrhost.add_ip_addr_to_vlan(vlan_intf, f"{nip}/{prefix}")
                    if not configure_bgp_peer(nbrhost, lip, remote_asn, local_asn, addr_family=addr_family):
                        pytest.fail(f"Failed to configure {addr_family} BGP peer on {nbrhost.hostname}")

                configs.append({
                    'duthost': duthost,
                    'nbrhost': nbrhost,
                    'vlan_id': vlan_id,
                    'local_ip': local_ip,
                    'neighbor_ip': neighbor_ip,
                    'local_asn': local_asn,
                    'remote_asn': remote_asn,
                    'addr_family': addr_family,
                    'trunk_port': dut_port,
                    'nbr_trunk_port': nbr_port
                })

    # Verify BGP peer configuration and status
    verify_bgp_peer_scale(configs)

    # Cleanup
    for config in configs:
        cleanup_host_config(config['duthost'], [config], is_dut=True)
        cleanup_host_config(config['nbrhost'], [config], is_dut=False)
        config['duthost'].shell(f"config interface no trunk {config['trunk_port']}")
        config['nbrhost'].shell(f"config interface no trunk {config['nbr_trunk_port']}")


def test_bgp_peer_scale_v4(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo):
    """
    Verify BGP IPv4 peer scaling by checking:
    1. All VLAN interfaces are properly configured and up
    2. All BGP peers are configured
    3. All BGP sessions are established
    """
    run_bgp_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo, addr_family="ipv4")


def test_bgp_peer_scale_v6(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo):
    """
    Verify BGP IPv6 peer scaling by checking:
    1. All VLAN interfaces are properly configured and up
    2. All BGP peers are configured
    3. All BGP sessions are established
    """
    run_bgp_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo, addr_family="ipv6")


def wait_bgp_sessions(duthost, timeout=60):
    """
    Wait for all BGP sessions to establish across all ASICs.

    Args:
        duthost: DUT host object
        timeout: Maximum time to wait in seconds (default: 60)

    Returns:
        None. Raises assertion error if sessions don't establish.
    """
    bgp_neighbors = duthost.get_bgp_neighbors_per_asic(state="all")
    neighbor_ips = [ip for ip in bgp_neighbors.keys() if ip is not None]
    if not neighbor_ips:
        pytest.fail(f"No valid BGP neighbor IPs found on {duthost.hostname}")

    pytest_assert(
        wait_until(timeout, 5, 0, duthost.check_bgp_session_state, neighbor_ips),
        f"Not all BGP sessions are established after {timeout} seconds on {duthost.hostname}"
    )


def verify_bgp_peer_scale(configs):
    """
    Verify BGP peer scale configuration and status
    """
    for config in configs:
        duthost = config['duthost']
        vlan_name = f"Vlan{config['vlan_id']}"

        # Verify VLAN interface configuration
        output = duthost.shell("show ip interfaces")["stdout"]
        interface_found = False
        ip_configured = False
        status_up = False

        for line in output.split('\n'):
            if vlan_name in line:
                interface_found = True
                ip_configured = config['local_ip'] in line
                status_up = 'up' in line.lower()
                break

        pytest_assert(
            interface_found,
            f"VLAN interface {vlan_name} not found in show ip interfaces output on {duthost.hostname}"
        )

        pytest_assert(
            ip_configured,
            f"Incorrect IP address configured on {vlan_name}. Expected {config['local_ip']}"
        )

        pytest_assert(
            status_up,
            f"Interface {vlan_name} is not up on {duthost.hostname}"
        )

        # Verify BGP peer configuration and status
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        pytest_assert(
            config['neighbor_ip'] in bgp_facts['bgp_neighbors'],
            f"BGP peer {config['neighbor_ip']} not found in BGP neighbors on {duthost.hostname}"
        )

        wait_bgp_sessions(duthost)


def cleanup_host_config(host, host_configs, is_dut=True):
    """
    Clean up BGP and VLAN configurations from a host.

    Args:
        host: The host object (duthost or nbrhost)
        host_configs: List of configurations for this specific host
        is_dut: Boolean indicating if the host is a DUT (True) or neighbor (False)
    """
    try:
        # Remove BGP neighbors
        for config in host_configs:
            if is_dut:
                peer_ip = config['neighbor_ip']
                peer_ipv6 = config['neighbor_ipv6']
                asn = config['local_asn']
            else:
                peer_ip = config['local_ip']
                peer_ipv6 = config['local_ipv6']
                asn = config['remote_asn']

            commands = [
                "vtysh -c 'configure terminal'",
                f"-c 'router bgp {asn}'",
                f"-c 'no neighbor {peer_ip}'",
                f"-c 'no neighbor {peer_ipv6}'"
            ]
            host.shell(" ".join(commands))

        # Clean up VLAN interfaces
        for config in host_configs:
            vlan_intf = f"Vlan{config['vlan_id']}"
            if is_dut:
                ip = config['local_ip']
                ipv6 = config['local_ipv6']
            else:
                ip = config['neighbor_ip']
                ipv6 = config['neighbor_ipv6']

            # Remove IPv4 and IPv6 addresses
            host.remove_ip_addr_from_vlan(vlan_intf, f"{ip}/24")
            host.remove_ip_addr_from_vlan(vlan_intf, f"{ipv6}/64")
            # Remove VLAN only after both addresses are removed
            host.remove_vlan(config['vlan_id'])

    except Exception as e:
        host_type = "DUT" if is_dut else "Neighbor"
        logger.error(f"{host_type} cleanup failed: {str(e)}")


@pytest.fixture(scope="module", autouse=True)
def restore_topology(duthosts, nbrhosts, tbinfo):
    """
    Fixture to restore original topology configuration after tests complete.
    Automatically applied to all tests in the module.
    """
    yield  # Run the test

    logger.info("Restoring original topology configuration...")

    # Restore DUT hosts
    for duthost in duthosts:
        try:
            # Reload original config
            config_reload(duthost, config_source='config_db', safe_reload=True)

            # Wait for critical services to be fully started
            if not wait_until(300, 10, 0, duthost.critical_services_fully_started):
                logger.error(f"Not all critical services are fully started on {duthost.hostname}")

        except Exception as e:
            logger.error(f"Error restoring configuration on {duthost.hostname}: {str(e)}")

    # Restore neighbor hosts
    for nbrhost in nbrhosts.values():
        try:
            host = nbrhost["host"]
            logger.info(f"Restoring configuration on neighbor {host.hostname}")
            host.shell("config reload -y")
        except Exception as e:
            logger.error(f"Error restoring configuration on neighbor {host.hostname}: {str(e)}")

    # Wait for BGP sessions to establish on all DUTs
    for duthost in duthosts:
        try:
            wait_bgp_sessions(duthost)
        except Exception as e:
            logger.error(f"Error waiting for BGP sessions on {duthost.hostname}: {str(e)}")

    logger.info("Topology restoration completed")
