"""
Test BGP peer scaling by adding multiple BGP peers on SONiC DUTs.
"""
import logging
import pytest
import ipaddress
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
        # Create VLAN using VLAN API
        duthost.create_vlan(vlan_id)
        
        # Add IP to VLAN interface
        duthost.config_ip_intf("Vlan{}".format(vlan_id), 
                             "{}/{}".format(ip_addr, prefix_len))
        
        # Wait for interface to be up
        pytest_assert(
            wait_until(30, 3, 0, check_interface_status, duthost, "Vlan{}".format(vlan_id)),
            "SVI interface Vlan{} did not come up".format(vlan_id)
        )
        return True
    except Exception as e:
        logger.error("Failed to setup SVI interface: {}".format(str(e)))
        return False

def check_interface_status(duthost, interface_name):
    """Check if interface is up."""
    interface_status = duthost.show_interface(command="status")["ansible_facts"]["int_status"]
    return interface_name in interface_status and interface_status[interface_name]["oper_state"] == "up"

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
