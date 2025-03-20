'''

This script is to test prefix-list filering for BGP on SONiC.
Note: This script deletes any route-map applied in outbound direction.

'''

import logging
import pytest
import json
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until

from natsort import natsorted

logger = logging.getLogger(__name__)
skip_hosts = []

pytestmark = [
    pytest.mark.topology('t0', 't1', 't2')
]


@pytest.fixture(scope='module')
def setup(tbinfo, nbrhosts, duthosts, enum_frontend_dut_hostname, enum_rand_one_frontend_asic_index):
    ''' 
    Sets up the test environment and gathers BGP neighbor information.
    Args:
        tbinfo: Testbed information fixture from pytest.
        nbrhosts: Dictionary of BGP neighbor hostnames and their corresponding information.
        duthosts: Dictionary of DUT (Device Under Test) objects.
        enum_frontend_dut_hostname: Pytest fixture for DUT hostname.
        enum_rand_one_frontend_asic_index: Pytest fixture for a random ASIC index on the DUT.
    Returns:
        A dictionary containing DUT, neighbor information, namespace, and ASN dictionary.
    Raises:
        Assert: If no BGP neighbors are found on in established state on the DUT.
    ''' 
    duthost = duthosts[enum_frontend_dut_hostname]
    asic_index = enum_rand_one_frontend_asic_index
    namespace = duthost.get_namespace_from_asic_id(asic_index)

    bgp_facts = duthost.bgp_facts(instance_id=asic_index)['ansible_facts']

    neigh_keys = []
    tor_neighbors = dict()
    neigh_asn = dict()
    for k, v in bgp_facts['bgp_neighbors'].items():
        if 'asic' not in v['description'].lower():
            neigh_keys.append(v['description'])
            neigh_asn[v['description']] = v['remote AS']
            logger.info(nbrhosts)
            tor_neighbors[v['description']] = nbrhosts[v['description']]["host"]
            logger.info(k)
            assert v['state'] == 'established'

    if not neigh_keys:
        pytest.skip("No BGP neighbors found on ASIC {} of DUT {}".format(asic_index, duthost.hostname))

    tor1 = natsorted(neigh_keys)[0]
    logger.info(tor1)

    # verify sessions are established
    logger.info(duthost.shell('show ip bgp summary'))
    logger.info(duthost.shell('show ipv6 bgp summary'))

    setup_info = {
        'duthost': duthost,
        'neighhost': tor_neighbors[tor1],
        'neigh_asn': neigh_asn[tor1],
        'asn_dict':  neigh_asn,
        'neighbors': tor_neighbors,
        'namespace': namespace
    }

    logger.info("DUT BGP Config: {}".format(duthost.shell("vtysh -n {} -c \"show run bgp\"".format(namespace),
          module_ignore_errors=True)))
    logger.info('Setup_info: {}'.format(setup_info))

    yield setup_info

    bgp_facts = duthost.bgp_facts(instance_id=asic_index)['ansible_facts']
    for k, v in bgp_facts['bgp_neighbors'].items():
        if v['description'].lower() not in skip_hosts:
            assert v['state'] == 'established'


def delete_existing_outbound(duthost, asn):
    ''' 
    Check and delete existing outbound route-maps to avoid conflicts.
    Args:
         duthost: DUT (Device Under Test).
         asn : DUT AS Number
     Returns:
         None
    Raises:
         Failure if config commands fail
    ''' 
    bgp_config = duthost.shell("vtysh -c 'show running-config bgp'")
    conf_bgp = f"vtysh -c 'configure terminal' -c 'router bgp {asn}'"
    for line in bgp_config['stdout_lines']:
        if "route-map" in line and "out" in line:
            config_command = f"{conf_bgp} -c 'address-family ipv4 unicast' -c 'no {line}'"
            duthost.shell(config_command)


def configure_prefix_list(duthost, prefix_list_name, action, prefix, seq):
    '''  
    Configures test prefix-list as per the parameters. 
    Args:
         duthost: DUT (Device Under Test).
         prefix_list_name: Name of the test prefix-list to be configured
         action: Action either permit or deny
         prefix: Prefix to be filtered
         seq: Sequence number in prefix-list
     Returns:
         None
    Raises:
         Failure if config commands fail
    '''  
    conft = "vtysh -c 'configure terminal'"
    config_command = f"{conft} -c 'ip prefix-list {prefix_list_name} seq {seq} {action} {prefix}' -c 'exit'"
    duthost.shell(config_command)


def apply_prefix_list_to_bgp(duthost, prefix_list_name, neighbor_ip, asn, allowed_prefix, denied_prefix):
    '''  
    Advertises prefixes in the ipv4 address-family. 
    Applies test prefix-list to the BGP neighbor.
    Args:
         duthost: DUT (Device Under Test).
         prefix_list_name: Name of the test prefix-list to be configured
         neighbor_ip: BGP neighbor IP to apply the prefix-list
         asn: BGP ASN for the configuration
         action: Action either permit or deny
         allowed_prefix: Prefix to be advertised
         denied_prefix: Prefix to be denied 
     Returns:
         None
    Raises:
         Failure if config commands fail
    ''' 

    conf_bgp = f"vtysh -c 'configure terminal' -c 'router bgp {asn}'"
    config_command = f"{conf_bgp} -c 'address-family ipv4 unicast' -c 'network {allowed_prefix}'"
    duthost.shell(config_command)
    config_command = f"{conf_bgp} -c 'address-family ipv4 unicast' -c 'network {denied_prefix}'"
    duthost.shell(config_command)
    config_command = f"{conf_bgp} -c 'neighbor {neighbor_ip} prefix-list {prefix_list_name} out'"
    duthost.shell(config_command)


def get_advertised_prefix(duthost, first_bgp_neighbor, prefix, action):
    ''' 
    Retrieve advertised prefixes to neighbor and, check BGP is filtering the right prefixes.
    Args:
         duthost: DUT (Device Under Test).
         first_bgp_neighbor: BGP neighbor IP to which the prefix-list is applied
         prefix: Prefix to be advertised
         action: permit/deny
    Returns:
         True/False
    Raises:
         Failure if show commands fail
    ''' 
    show_bgp_nei = f"vtysh -c 'show ip bgp neighbor {first_bgp_neighbor} advertised-routes json'"
    route_info = json.loads(duthost.shell(f"{show_bgp_nei}")['stdout'])

    logger.info(route_info)
    adv_routes = route_info.get('advertisedRoutes')
    adv_routes_list = list(adv_routes.keys())
    logger.info(adv_routes)
    logger.info(adv_routes_list)

    if action == "permit" and prefix not in adv_routes_list:
        return False
    elif action == "deny" and prefix in adv_routes_list:
        return False

    return True


def cleanup(duthost, prefix_list_name, allowed_prefix, denied_prefix, asn, neighbor_ip):
    ''' 
    Deletes prefix-list applied to the BGP neighbor and the loopbacks.
    Args:
         duthost: DUT (Device Under Test).
         prefix_list_name: Name of the test prefix-list to be configured
         allowed_prefix: Prefix to be advertised
         denied_prefix: Prefix to be denied
         neighbor_ip: BGP neighbor IP
     Returns:
         None
    Raises:
         Failure if config commands fail
    ''' 
    config_command = f"sudo config interface ip remove Loopback100 {allowed_prefix}"
    duthost.shell(config_command)
    config_command = f"sudo config interface ip remove Loopback101 {denied_prefix}"
    duthost.shell(config_command)
    conf_bgp = f"vtysh -c 'configure terminal' -c 'router bgp {asn}'"
    config_command = f"{conf_bgp} -c 'no neighbor {neighbor_ip} prefix-list {prefix_list_name} out'"
    duthost.shell(config_command)


def test_prefix_list_application(setup):
    ''' 
    Testcase: Test the application of prefix lists in BGP.
    ''' 
    duthost = setup['duthost']

    # Step 1: Define prefix list to permit a specific range and deny others
    prefix_list_name = "test-prefix-list"
    allowed_prefix = "100.1.1.1/32"
    denied_prefix = "100.2.2.1/32"

    # Step 1.1: Configure loopback interfaces with the prefixes to get them in routing table
    config_command = f"sudo config interface ip add Loopback100 {allowed_prefix}"
    duthost.shell(config_command)
    config_command = f"sudo config interface ip add Loopback101 {denied_prefix}"
    duthost.shell(config_command)

    # Step 2: Configure the prefix list to permit the allowed prefix and deny the other
    logger.info(f"Configuring prefix list {prefix_list_name} to allow {allowed_prefix} and deny {denied_prefix}")
    configure_prefix_list(duthost, prefix_list_name, 'permit', allowed_prefix, '10')
    configure_prefix_list(duthost, prefix_list_name, 'deny', denied_prefix, '20') 
     
    # Step 2.1 : Fetch BGP facts
    bgp_facts = duthost.bgp_facts()['ansible_facts']
    
    # Step 2.1: Accessing the list of BGP neighbors
    bgp_neighbors = bgp_facts['bgp_neighbors']
    
    # Step 2.3: Get the first BGP neighbor's description (or neighbor's IP)
    first_bgp_neighbor = list(bgp_neighbors.keys())[0]  # Get the first key
    logger.info(first_bgp_neighbor)

    # Step 2.4: Extract the local AS number of the first BGP neighbor
    first_bgp_asn = bgp_neighbors[first_bgp_neighbor]['local AS']

    # Step 2.5 Delete outbound route-map if any configured
    delete_existing_outbound(duthost, first_bgp_asn)

    # Step 3: Apply the prefix-list to bgp neighbor
    apply_prefix_list_to_bgp(duthost, prefix_list_name, first_bgp_neighbor, 
           first_bgp_asn, allowed_prefix,denied_prefix)

    # Step 4: Verify the prefix-list filters the prefixes correctly
    pytest_assert(wait_until(60, 10, 0, lambda: 
        get_advertised_prefix(duthost, first_bgp_neighbor, allowed_prefix, 'permit')), 
        "Allowed prefix is not advertised out")
    pytest_assert(wait_until(60, 10, 0, lambda: 
        get_advertised_prefix(duthost, first_bgp_neighbor, denied_prefix, 'deny')), 
        "Denied prefix is advertised out")

    # Step 5: Cleanup the prefix-list configuration
    cleanup(duthost, prefix_list_name, allowed_prefix, denied_prefix, first_bgp_asn, first_bgp_neighbor)

