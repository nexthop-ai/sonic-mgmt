"""
Test BGP peer scaling by adding multiple BGP peers on SONiC DUTs.
"""
import logging
import pytest
import ipaddress
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


def setup_svi_interface(duthost, vlan_id, ip_addr, mask_length, port=None):
    """Configure VLAN interface with IP address and optionally add member ports."""
    try:
        # Create VLAN
        duthost.command(f"config vlan add {vlan_id}")
        
        # Create VLAN interface and add IP
        vlan_intf = f"Vlan{vlan_id}"
        ip_with_mask = f"{ip_addr}/{mask_length}"
        
        # Add IP to VLAN interface
        duthost.add_ip_addr_to_vlan(vlan_intf, ip_with_mask)
        
        # Wait for interface to come up
        if not wait_until(30, 2, 0, verify_vlan_interface_status, duthost, vlan_id):
            logger.error(f"VLAN interface {vlan_intf} is not up")
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"Failed to setup VLAN interface: {str(e)}")
        return False


def cleanup_svi_interface(duthost, vlan_id, ip_addr, mask_length, port=None):
    """Remove VLAN interface configuration."""
    try:
        vlan_intf = f"Vlan{vlan_id}"
        ip_with_mask = f"{ip_addr}/{mask_length}"
        
        # If port is specified, remove it from VLAN using DUT API
        if port:
            duthost.del_member_from_vlan(vlan_id, port)
        
        # Remove IP from VLAN interface using DUT API
        duthost.remove_ip_addr_from_vlan(vlan_intf, ip_with_mask)
        
        # Remove VLAN using DUT API
        duthost.remove_vlan(vlan_id)
        
    except Exception as e:
        logger.error("Failed to cleanup VLAN interface: %s", str(e))


def check_interface_status(duthost, interface_name):
    """Check if interface is up."""
    try:
        interface_status = duthost.show_interface(command="status")["ansible_facts"]["int_status"]
        return (interface_name in interface_status and 
                interface_status[interface_name]["oper_state"] == "up")
    except Exception as e:
        logger.error("Failed to check interface status: %s", str(e))
        return False


def configure_bgp_peer(duthost, neighbor_ip, local_asn, remote_asn):
    """Configure a BGP peer on the DUT."""
    try:
        # Configure BGP neighbor using vtysh commands
        commands = [
            "vtysh -c 'configure terminal' "
            "-c 'router bgp {}' "
            "-c 'neighbor {} remote-as {}' "
            "-c 'neighbor {} timers 3 10'".format(
                local_asn, neighbor_ip, remote_asn, neighbor_ip
            )
        ]

        result = duthost.shell("\n".join(commands))
        if result['rc'] != 0:
            logger.error("Failed to configure BGP peer. Command output: %s", result['stdout'])
            logger.error("Error message: %s", result['stderr'])
            return False

        # Verify BGP configuration
        try:
            bgp_info = duthost.get_bgp_neighbor_info(neighbor_ip)
            if bgp_info and str(bgp_info.get('remoteAs', '')) == str(remote_asn):
                logger.info("BGP peer %s configured successfully with AS %s", neighbor_ip, remote_asn)
                return True
            else:
                logger.error("BGP peer configuration verification failed. Expected remote AS %s, got %s", 
                             remote_asn, bgp_info.get('remoteAs', 'None'))
                logger.error("Full BGP neighbor info: %s", bgp_info)
                return False
        except Exception as e:
            logger.error("Failed to verify BGP configuration: %s", str(e))
            return False

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


@pytest.fixture(scope="module")
def setup_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname):
    """Setup additional BGP peers on each DUT for scaling test."""
    configs = []
    
    for dut_index, duthost in enumerate(duthosts):
        # Get existing BGP configuration and facts
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        local_asn = config_facts.get('DEVICE_METADATA', {}).get('localhost', {}).get('bgp_asn')
        
        # Get BGP facts to check existing peers
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        existing_peers = bgp_facts.get('bgp_neighbors', {})
        
        # Get remote ASN from first existing peer
        remote_asn = None
        for peer_ip, peer_data in existing_peers.items():
            if 'remote AS' in peer_data:  # BGP facts uses 'remote AS' not 'asn'
                remote_asn = peer_data['remote AS']
                logger.info(f"Found remote ASN {remote_asn} from neighbor {peer_ip}")
                break
        
        if not remote_asn:
            pytest.fail(f"Could not determine remote ASN from BGP neighbors on {duthost.hostname}")
        
        logger.info(f"Using existing remote ASN {remote_asn} for new peers on {duthost.hostname}")
        
        # Configure multiple peers per DUT
        for peer_index in range(PEERS_PER_DUT):
            # Calculate unique VLAN ID for this peer
            vlan_id = BASE_VLAN_ID + (dut_index * PEERS_PER_DUT) + peer_index
            
            # Get non-overlapping IP addresses for this VLAN
            local_ip, neighbor_ip = get_free_ip_pair(vlan_id)
            
            logger.info("Configuring peer %d: VLAN %d, Local IP %s, Neighbor IP %s, Local ASN %s, Remote ASN %s",
                        peer_index + 1, vlan_id, local_ip, neighbor_ip, local_asn, remote_asn)
            
            # Setup SVI interface with /24 mask
            if not setup_svi_interface(duthost, vlan_id, local_ip, 24):
                pytest.fail("Failed to setup SVI interface for peer {} on {}".format(
                    peer_index + 1, duthost.hostname))
            
            # Configure BGP peer using same remote ASN as existing peers
            if not configure_bgp_peer(duthost, neighbor_ip, local_asn, remote_asn):
                pytest.fail("Failed to configure BGP peer {} on {}".format(
                    peer_index + 1, duthost.hostname))
            
            configs.append({
                'duthost': duthost,
                'vlan_id': vlan_id,
                'local_ip': local_ip,
                'neighbor_ip': neighbor_ip,
                'remote_asn': remote_asn
            })
    
    yield configs
    
    # Cleanup
    for config in configs:
        duthost = config['duthost']
        try:
            # Remove BGP neighbor using vtysh
            commands = [
                "vtysh -c 'configure terminal' "
                "-c 'router bgp {}' "
                "-c 'no neighbor {}'".format(
                    local_asn, config['neighbor_ip']
                )
            ]
            duthost.shell("\n".join(commands))
            
            # Clean up VLAN interface using existing helper function
            cleanup_svi_interface(duthost, config['vlan_id'], config['local_ip'], 24)
        except Exception as e:
            logger.error("Cleanup failed for DUT {}: {}".format(duthost.hostname, str(e)))


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
            if len(fields) >= 4 and fields[0] == vlan_name:
                interface_found = True
                configured_ip = fields[1].split('/')[0]  # Extract IP without mask
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
