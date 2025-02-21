"""
Test BGP peer scaling by adding multiple BGP peers on SONiC DUTs.
"""
import logging
import pytest
import ipaddress
import time
from tests.common.helpers.assertions import pytest_assert, pytest_require
from tests.common.utilities import wait_until
from tests.common.devices.sonic import SonicHost

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t0'),
    pytest.mark.device_type('vs')
]

# Constants for BGP peer scaling
BASE_VLAN_ID = 2000  # Starting VLAN ID for new peers
BASE_BGP_ASN = 65100  # Starting ASN for new peers
SVI_NETWORK_TEMPLATE = "192.168.{}.0/24"  # Template for SVI networks
PEERS_PER_DUT = 2  # Number of additional peers to configure per DUT

def setup_svi_interface(duthost, vlan_id, ip_addr, prefix_len):
    """Configure a new SVI interface on the DUT."""
    try:
        vlan_intf = "Vlan{}".format(vlan_id)
        ip_with_prefix = "{}/{}".format(ip_addr, prefix_len)
        
        logger.info("Creating VLAN %d and interface %s with IP %s", 
                   vlan_id, vlan_intf, ip_with_prefix)
        
        # Get physical interfaces for VLAN members
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        phys_interfaces = [intf for intf in config_facts.get('PORT', {}).keys() 
                          if 'Ethernet' in intf]
        
        if not phys_interfaces:
            logger.error("No physical interfaces found for VLAN members")
            return False
            
        # Use first available interface as VLAN member
        vlan_member = phys_interfaces[0]
        
        # Step 1: Create VLAN
        cmds = [
            "sudo config vlan add {}".format(vlan_id),
            # Add physical interface as VLAN member in untagged mode
            "sudo config vlan member add {} {}".format(vlan_id, vlan_member),
            # Configure IP address on VLAN interface
            "sudo config interface ip add {} {}".format(vlan_intf, ip_with_prefix)
        ]
        
        for cmd in cmds:
            result = duthost.shell(cmd)
            if result['rc'] != 0:
                error_msg = "Command '{}' failed with return code {}\nError: {}\nOutput: {}".format(
                    cmd, result['rc'], result['stderr'], result['stdout'])
                logger.error(error_msg)
                # Get current VLAN and interface status for debugging
                try:
                    vlan_status = duthost.shell("show vlan")['stdout']
                    intf_status = duthost.shell("show interfaces status")['stdout']
                    logger.error("Current VLAN status:\n%s", vlan_status)
                    logger.error("Current interface status:\n%s", intf_status)
                except Exception as e:
                    logger.error("Failed to get debug information: %s", str(e))
                return False
            logger.info("Successfully executed: %s", cmd)
        
        # Step 2: Wait for interface to come up with increased timeout
        def check_interface_up():
            try:
                result = duthost.shell("show interfaces status {}".format(vlan_intf))
                if result['rc'] != 0:
                    logger.error("Failed to check interface status: %s", result['stderr'])
                    return False
                return "up" in result['stdout'].lower()
            except Exception as e:
                logger.error("Error checking interface status: %s", str(e))
                return False
        
        # Wait up to 60 seconds for interface to come up
        for attempt in range(20):
            if check_interface_up():
                logger.info("Interface %s is up", vlan_intf)
                return True
            
            logger.info("Waiting for interface %s to come up (attempt %d/20)...", 
                       vlan_intf, attempt + 1)
            time.sleep(3)
        
        # Get detailed interface status for debugging if interface didn't come up
        try:
            intf_status = duthost.shell("show interfaces status")['stdout']
            vlan_status = duthost.shell("show vlan")['stdout']
            bgp_status = duthost.shell("show ip bgp summary")['stdout']
            logger.error("Interface failed to come up. Debug information:")
            logger.error("Interface status:\n%s", intf_status)
            logger.error("VLAN status:\n%s", vlan_status)
            logger.error("BGP status:\n%s", bgp_status)
        except Exception as e:
            logger.error("Failed to get debug information: %s", str(e))
        
        logger.error("Interface %s failed to come up after 60 seconds", vlan_intf)
        return False
        
    except Exception as e:
        logger.error("Failed to setup SVI interface: %s", str(e))
        import traceback
        logger.error("Traceback: %s", traceback.format_exc())
        # Get system state for debugging
        try:
            intf_status = duthost.shell("show interfaces status")['stdout']
            vlan_status = duthost.shell("show vlan")['stdout']
            bgp_status = duthost.shell("show ip bgp summary")['stdout']
            logger.error("System state at failure:")
            logger.error("Interface status:\n%s", intf_status)
            logger.error("VLAN status:\n%s", vlan_status)
            logger.error("BGP status:\n%s", bgp_status)
        except Exception as debug_e:
            logger.error("Failed to get debug information: %s", str(debug_e))
        return False

def cleanup_svi_interface(duthost, vlan_id, ip_addr, prefix_len):
    """Clean up SVI interface configuration."""
    try:
        vlan_intf = "Vlan{}".format(vlan_id)
        ip_with_prefix = "{}/{}".format(ip_addr, prefix_len)
        
        # Get VLAN members
        result = duthost.shell("show vlan {}".format(vlan_id))
        if result['rc'] == 0:
            # Extract member interfaces from output
            for line in result['stdout'].splitlines():
                if 'Ethernet' in line:
                    member = line.split()[0]
                    duthost.shell("config vlan member del {} {}".format(vlan_id, member))
        
        # Remove IP address and VLAN
        cmds = [
            "config interface ip remove {} {}".format(vlan_intf, ip_with_prefix),
            "config vlan del {}".format(vlan_id)
        ]
        
        for cmd in cmds:
            result = duthost.shell(cmd)
            if result['rc'] != 0:
                logger.error("Cleanup command '%s' failed: %s", cmd, result['stderr'])
        
    except Exception as e:
        logger.error("Failed to cleanup SVI interface: %s", str(e))

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
        # Using BGP API methods
        duthost.config_bgp_neighbor(neighbor_ip, remote_asn, local_asn)
        duthost.config_bgp_neighbor_timers(neighbor_ip, 3, 9)
        return True
    except Exception as e:
        logger.error("Failed to configure BGP peer: {}".format(str(e)))
        return False

def get_free_ip_pair(network, index):
    """Get a pair of IP addresses from the network for local and neighbor use."""
    net = ipaddress.ip_network(network)
    # Skip .0 (network) and .255 (broadcast)
    base_index = (index * 2) + 1
    return str(net[base_index]), str(net[base_index + 1])

@pytest.fixture(scope="module")
def setup_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname):
    """Setup additional BGP peers on each DUT for scaling test."""
    
    # Store configuration details for cleanup
    configs = []
    
    for dut_index, duthost in enumerate(duthosts):
        # Get existing BGP configuration
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        existing_asn = config_facts.get('DEVICE_METADATA', {}).get('localhost', {}).get('bgp_asn')
        
        # Configure multiple peers per DUT
        for peer_index in range(PEERS_PER_DUT):
            # Calculate unique VLAN ID and network for this peer
            vlan_id = BASE_VLAN_ID + (dut_index * PEERS_PER_DUT) + peer_index
            network = SVI_NETWORK_TEMPLATE.format(vlan_id - BASE_VLAN_ID)
            remote_asn = BASE_BGP_ASN + peer_index
            
            local_ip, neighbor_ip = get_free_ip_pair(network, 0)
            
            # Setup SVI interface
            if not setup_svi_interface(duthost, vlan_id, local_ip, 24):
                pytest.fail("Failed to setup SVI interface for peer {} on {}".format(
                    peer_index + 1, duthost.hostname))
            
            # Configure BGP peer
            if not configure_bgp_peer(duthost, neighbor_ip, existing_asn, remote_asn):
                pytest.fail("Failed to configure BGP peer {} on {}".format(
                    peer_index + 1, duthost.hostname))
            
            # Store config for cleanup
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
            # Remove BGP neighbor using API
            duthost.remove_bgp_neighbor(config['neighbor_ip'])
            
            # Remove IP from VLAN interface
            duthost.remove_ip_intf("Vlan{}".format(config['vlan_id']), 
                                 "{}/24".format(config['local_ip']))
            
            # Remove VLAN using API
            duthost.remove_vlan(config['vlan_id'])
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
        
        # Step 1: Verify VLAN interface configuration and status
        interface_facts = duthost.show_interface(command="status")["ansible_facts"]["int_status"]
        vlan_name = "Vlan{}".format(config['vlan_id'])
        
        pytest_assert(
            vlan_name in interface_facts,
            "VLAN interface {} not found on {}".format(vlan_name, duthost.hostname)
        )
        
        pytest_assert(
            interface_facts[vlan_name]["oper_state"] == "up",
            "VLAN interface {} is not up on {}".format(vlan_name, duthost.hostname)
        )
        
        # Step 2: Verify BGP peer configuration
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        pytest_assert(
            config['neighbor_ip'] in bgp_facts['bgp_neighbors'],
            "BGP peer {} not found in BGP neighbors on {}".format(
                config['neighbor_ip'], duthost.hostname)
        )
        
        # Step 3: Verify BGP session establishment
        pytest_assert(
            wait_until(60, 5, 0, check_bgp_peer_status, duthost, config['neighbor_ip']),
            "BGP peer {} failed to establish on {}".format(
                config['neighbor_ip'], duthost.hostname)
        )
