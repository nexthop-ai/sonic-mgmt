"""
Test BGP peer scaling by adding multiple BGP peers on SONiC DUTs.
"""
import logging
import pytest
import ipaddress
import time
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t0'),
]

# Constants for BGP peer scaling
BASE_VLAN_ID = 2000  # Starting VLAN ID for new peers
BASE_BGP_ASN = 65100  # Starting ASN for new peers
SVI_NETWORK_TEMPLATE = "192.168.{}.0/24"  # Template for SVI networks
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
                    config = duthost.shell(f"show running-config interface {vlan_intf}")["stdout"]
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
        # Check port status
        port_status = duthost.shell(f"show interfaces status {port}")["stdout"]
        if "connected" not in port_status.lower():
            logger.info(f"Port {port} is not connected on {duthost.hostname}, attempting to bring it up")
            duthost.shell(f"config interface startup {port}")
            time.sleep(10)  # Wait for port to initialize
            
            # Check again
            port_status = duthost.shell(f"show interfaces status {port}")["stdout"]
            if "connected" not in port_status.lower():
                return False
    except Exception as e:
        logger.error(f"Error checking port status for {port} on {duthost.hostname}: {str(e)}")
        return False
    return True

def configure_trunk_port(duthost, trunk_port):
    """Configure a port as trunk and ensure it's up."""
    try:
        # Configure as trunk
        duthost.shell(f"config interface trunk {trunk_port}")
        
        # Ensure port is up
        if not ensure_port_is_up(duthost, trunk_port):
            return False
            
        # Add port to each VLAN as tagged member
        return True
    except Exception as e:
        logger.error(f"Error configuring trunk port {trunk_port} on {duthost.hostname}: {str(e)}")
        return False

@pytest.fixture(scope="module")
def setup_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts):
    """Setup additional BGP peers (both IPv4 and IPv6) on each DUT and corresponding neighbor hosts."""
    configs = []
    
    # Get an available port for trunk
    dut_config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
    available_ports = dut_config_facts.get('PORT', {}).keys()
    trunk_port = next(iter(available_ports))  # Get first available port
    
    # Configure trunk port first
    if not configure_trunk_port(duthost, trunk_port):
        pytest.fail(f"Failed to configure trunk port {trunk_port} on {duthost.hostname}")
    
    for dut_index, duthost in enumerate(duthosts):
        nbrhost = nbrhosts[dut_index] if dut_index < len(nbrhosts) else nbrhosts[-1]
        
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        local_asn = config_facts.get('DEVICE_METADATA', {}).get('localhost', {}).get('bgp_asn')
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        existing_peers = bgp_facts.get('bgp_neighbors', {})
        remote_asn = None
        for peer_ip, peer_data in existing_peers.items():
            if 'remote AS' in peer_data and ':' not in peer_ip:
                remote_asn = peer_data['remote AS']
                break
        
        logger.info(f"Using remote ASN {remote_asn} for new peers on {duthost.hostname}")
        vlan_id = BASE_VLAN_ID + dut_index
        local_ip, neighbor_ip = get_free_ip_pair(vlan_id)
        local_ipv6, neighbor_ipv6 = get_free_ipv6_pair(vlan_id)
        
        # Configure DUT side
        logger.info(f"Configuring additional peers for {duthost.hostname}")
        logger.info(f"IPv4 - Local: {local_ip}, Neighbor: {neighbor_ip}")
        logger.info(f"IPv6 - Local: {local_ipv6}, Neighbor: {neighbor_ipv6}")
        
        vlan_intf = f"Vlan{vlan_id}"
        duthost.shell(f"config vlan add {vlan_id}")
        duthost.shell(f"config vlan member add {vlan_id} {trunk_port} --tagged")
        duthost.add_ip_addr_to_vlan(vlan_intf, f"{local_ip}/24")
        duthost.add_ip_addr_to_vlan(vlan_intf, f"{local_ipv6}/64")
        
        if not configure_bgp_peer(duthost, neighbor_ip, local_asn, remote_asn):
            pytest.fail(f"Failed to configure IPv4 BGP peer on {duthost.hostname}")
        if not configure_bgp_peer(duthost, neighbor_ipv6, local_asn, remote_asn, addr_family="ipv6"):
            pytest.fail(f"Failed to configure IPv6 BGP peer on {duthost.hostname}")

        # Configure neighbor host side
        logger.info(f"Configuring BGP on neighbor host {nbrhost.hostname}")
        
        # Create VLAN interface on neighbor
        nbrhost.shell(f"ip link add link eth0 name {vlan_intf} type vlan id {vlan_id}")
        nbrhost.shell(f"ip link set {vlan_intf} up")
        nbrhost.shell(f"ip addr add {neighbor_ip}/24 dev {vlan_intf}")
        nbrhost.shell(f"ip -6 addr add {neighbor_ipv6}/64 dev {vlan_intf}")

        # Configure BGP on neighbor using the same configure_bgp_peer function
        if not configure_bgp_peer(nbrhost, local_ip, remote_asn, local_asn):
            pytest.fail(f"Failed to configure IPv4 BGP peer on {nbrhost.hostname}")
        if not configure_bgp_peer(nbrhost, local_ipv6, remote_asn, local_asn, addr_family="ipv6"):
            pytest.fail(f"Failed to configure IPv6 BGP peer on {nbrhost.hostname}")
        
        configs.append({
            'duthost': duthost,
            'nbrhost': nbrhost,
            'vlan_id': vlan_id,
            'local_ip': local_ip,
            'neighbor_ip': neighbor_ip,
            'local_ipv6': local_ipv6,
            'neighbor_ipv6': neighbor_ipv6,
            'local_asn': local_asn,
            'remote_asn': remote_asn
        })
    
    yield configs
    
    # Cleanup both DUT and neighbor configurations
    for config in configs:
        cleanup_host_config(config['duthost'], [config], is_dut=True)
        cleanup_host_config(config['nbrhost'], [config], is_dut=False)
        # Remove trunk configuration
        config['duthost'].shell(f"config interface no trunk {trunk_port}")


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


def test_bgp_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, setup_peer_scale):
    """
    Verify BGP peer scaling by checking:
    1. All VLAN interfaces are properly configured and up
    2. All BGP peers are configured
    3. All BGP sessions are established
    """
    def check_bgp_peer_status(duthost, neighbor_ip):
        """Helper function to check BGP peer status."""
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        return (neighbor_ip in bgp_facts['bgp_neighbors'] and
                bgp_facts['bgp_neighbors'][neighbor_ip]['state'] == 'established')

    for config in setup_peer_scale:
        duthost = config['duthost']

        # Verify VLAN interface status
        vlan_name = f"Vlan{config['vlan_id']}"

        # Get show ip interfaces output
        output = duthost.shell("show ip interfaces")["stdout"]
        logger.info(f"Show IP interfaces output:\n{output}")

        # Parse the output to verify if VLAN interface is up
        interface_found = False
        ip_configured = False
        status_up = False
        configured_ip = None

        lines = output.strip().split('\n')[2:]  # Skip header and separator lines
        for line in lines:
            fields = line.split()
            if len(fields) >= 3 and fields[0] == vlan_name:
                interface_found = True
                configured_ip = fields[1].split('/')[0].strip()
                if configured_ip == config['local_ip']:
                    ip_configured = True
                if "up/up" in fields[2]:
                    status_up = True
                break

        pytest_assert(
            interface_found,
            f"VLAN interface {vlan_name} not found in show ip interfaces output on {duthost.hostname}"
        )

        pytest_assert(
            ip_configured,
            f"Incorrect IP address configured on {vlan_name}. Expected {config['local_ip']}, got {configured_ip}"
        )

        pytest_assert(
            status_up,
            f"Interface {vlan_name} is not up on {duthost.hostname}"
        )

        # Verify BGP peer configuration
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        pytest_assert(
            config['neighbor_ip'] in bgp_facts['bgp_neighbors'],
            "BGP peer {} not found in BGP neighbors on {}".format(
                config['neighbor_ip'], duthost.hostname)
        )

        # Verify BGP session establishment
        pytest_assert(
            wait_until(60, 5, 0, check_bgp_peer_status, duthost, config['neighbor_ip']),
            "BGP peer {} failed to establish on {}".format(
                config['neighbor_ip'], duthost.hostname)
        )
