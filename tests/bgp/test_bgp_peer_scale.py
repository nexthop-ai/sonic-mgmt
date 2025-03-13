"""
Test BGP peer scaling by adding multiple BGP peers using loopback interfaces on SONiC DUTs.
"""
import ipaddress
import logging
import pytest
from tests.bgp.bgp_helpers import configure_bgp_peer
from tests.ip.ip_helpers import (
    configure_loopback,
    unconfigure_loopback,
    configure_static_route,
    unconfigure_static_route,
)
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("t0", "t1"),
]

# Constants for BGP peer scaling
BASE_LOOPBACK_ID = 1  # Starting Loopback ID
PEERS_PER_DUT = 10  # Number of additional peers to configure per DUT

# Default ASN values when they cannot be determined from configuration
DEFAULT_LOCAL_ASN = 65100  # Default local ASN, commonly used in t0 topologies
DEFAULT_REMOTE_ASN = 64600  # Default remote ASN for BGP peers


def get_neighbor_ip_pairs(duthost, nbrhost, tbinfo, addr_family="ipv4"):
    """Get the IP address pairs between DUT and neighbor host.

    Args:
        duthost: DUT host object
        nbrhost: Neighbor host object
        tbinfo: Testbed info fixture
        addr_family: Address family ("ipv4" or "ipv6")

    Returns:
        tuple: (dut_nbr_ip, nbr_dut_ip) - IP addresses on DUT and neighbor sides
    """
    try:
        mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

        # Get the VM base number (e.g., 100 from VM0100)
        current_vm = int(nbrhost.hostname[2:])  # Extract number from VMxxxx

        # Find the topology name for this neighbor
        topo_name = None
        for _, neigh in mg_facts['minigraph_neighbors'].items():
            vm_offset = tbinfo['topo']['properties']['topology']['VMs'][neigh['name']]['vm_offset']
            base_vm = current_vm - vm_offset  # Calculate what the base VM should be
            # Check if this neighbor's base VM matches
            if base_vm == int(tbinfo['vm_base'][2:]):  # Compare with actual base VM number
                topo_name = neigh['name']
                break

        if not topo_name:
            logger.error(f"Could not find topology name for VM {nbrhost.hostname}")
            return None, None

        # Find BGP neighbor information
        for bgp_peer in mg_facts['minigraph_bgp']:
            if bgp_peer['name'] == topo_name:
                # Check if it's the right address family
                if addr_family == "ipv4" and '.' in bgp_peer['addr']:
                    return bgp_peer['addr'], bgp_peer['peer_addr']
                elif addr_family == "ipv6" and ':' in bgp_peer['addr']:
                    return bgp_peer['addr'], bgp_peer['peer_addr']

        logger.error(f"No {addr_family} BGP connection found between {duthost.hostname} and {topo_name}")
        return None, None

    except Exception as e:
        logger.warning(f"Failed to get neighbor IP pairs: {str(e)}")
        return None, None


def get_loopback_ip_pair(loopback_id):
    """Get a pair of non-overlapping IP addresses for local and neighbor loopback use."""
    # Use loopback ID to create unique addresses
    # For example:
    # Loopback 1 -> 172.16.1.1/32 and 172.16.1.2/32
    network = f"172.16.{loopback_id}.0/24"
    net = ipaddress.ip_network(network)
    return str(net[1]), str(net[2])


def get_loopback_ipv6_pair(loopback_id):
    """Get a pair of non-overlapping IPv6 addresses for local and neighbor loopback use."""
    # Use loopback ID to create unique IPv6 addresses
    # For example:
    # Loopback 1 -> fc00:1::1/128 and fc00:1::2/128
    network = f"fc00:{loopback_id:x}::/64"
    net = ipaddress.ip_network(network)
    return str(net[1]), str(net[2])


def get_asn_values(duthost):
    """Get the local and remote ASN values from existing BGP neighbors or config.
    Returns tuple of (local_asn, remote_asn)
    """
    try:
        # First try to get from config facts
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        local_asn = config_facts.get('DEVICE_METADATA', {}).get('localhost', {}).get('bgp_asn', DEFAULT_LOCAL_ASN)

        # Try to get remote ASN from existing BGP neighbors
        bgp_config = config_facts.get('BGP_NEIGHBOR', {})
        for peer_data in bgp_config.values():
            if 'asn' in peer_data and peer_data['asn'] != local_asn:
                return local_asn, peer_data['asn']

    except Exception as e:
        logger.warning(f"Failed to get ASN values from config: {str(e)}")

    logger.warning(f"Using default ASN values: Local ASN {DEFAULT_LOCAL_ASN}, Remote ASN {DEFAULT_REMOTE_ASN}")
    return DEFAULT_LOCAL_ASN, DEFAULT_REMOTE_ASN


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
    try:
        for dut_index, duthost in enumerate(duthosts):
            # Get local and remote asn values
            local_asn, remote_asn = get_asn_values(duthost)

            # Get all current BGP neighbors for this DUT
            current_neighbors = [nbr["host"] for nbr in nbrhosts.values()]

            if not current_neighbors:
                logger.error(f"No existing BGP neighbors found for DUT {duthost.hostname}")
                continue

            # Get port connections between DUT and neighbors
            for neighbor_index, nbrhost in enumerate(current_neighbors):
                # Get neighbor IPs for connectivity
                dut_nbr_ip, nbr_dut_ip = get_neighbor_ip_pairs(duthost, nbrhost, tbinfo, addr_family=addr_family)

                if not dut_nbr_ip or not nbr_dut_ip:
                    pytest.fail(f"Failed to get neighbor IP addresses for {duthost.hostname} and {nbrhost.hostname}")

                # Configure additional peers for this neighbor
                for peer_index in range(PEERS_PER_DUT):
                    loopback_id = BASE_LOOPBACK_ID + (dut_index * 100) + (neighbor_index * 10) + peer_index

                    if addr_family == "ipv4":
                        local_ip, neighbor_ip = get_loopback_ip_pair(loopback_id)
                    else:
                        local_ip, neighbor_ip = get_loopback_ipv6_pair(loopback_id)

                    # Configure loopback interfaces on DUT and neighbor
                    if not configure_loopback(duthost, loopback_id, local_ip):
                        pytest.fail(f"Failed to configure loopback {loopback_id} on {duthost.hostname}")
                    if not configure_loopback(nbrhost, loopback_id, neighbor_ip):
                        pytest.fail(f"Failed to configure loopback {loopback_id} on {nbrhost.hostname}")

                    # Configure routes to reach each other's loopbacks
                    prefix_len = '128' if addr_family == "ipv6" else '32'
                    if not configure_static_route(duthost, f"{neighbor_ip}/{prefix_len}", dut_nbr_ip):
                        pytest.fail(f"Failed to configure route to peer loopback on {duthost.hostname}")
                    if not configure_static_route(nbrhost, f"{local_ip}/{prefix_len}", nbr_dut_ip):
                        pytest.fail(f"Failed to configure route to peer loopback on {nbrhost.hostname}")

                    # Configure eBGP peers
                    loopback_name = f"Loopback{loopback_id}"
                    if not configure_bgp_peer(duthost, neighbor_ip, local_asn,
                                              remote_asn, afi=addr_family,
                                              update_source_intf=loopback_name):
                        pytest.fail(f"Failed to configure {addr_family} BGP peer on {duthost.hostname}")
                    if not configure_bgp_peer(nbrhost, local_ip, remote_asn,
                                              local_asn, afi=addr_family,
                                              update_source_intf=loopback_name):
                        pytest.fail(f"Failed to configure {addr_family} BGP peer on {nbrhost.hostname}")

                    configs.append({
                        'duthost': duthost,
                        'nbrhost': nbrhost,
                        'loopback_id': loopback_id,
                        'local_ip': local_ip,
                        'neighbor_ip': neighbor_ip,
                        'local_asn': local_asn,
                        'remote_asn': remote_asn,
                        'addr_family': addr_family
                    })

        # Verify BGP peer configuration and status
        verify_bgp_peer_scale(duthosts, configs, addr_family=addr_family)
    finally:
        # Clean up configurations
        for config in configs:
            duthost = config['duthost']
            nbrhost = config['nbrhost']
            loopback_id = config['loopback_id']
            local_ip = config['local_ip']
            neighbor_ip = config['neighbor_ip']
            addr_family = config['addr_family']

            # Remove BGP neighbors added by this test
            duthost.shell(
                f"vtysh -c 'configure terminal' "
                f"-c 'router bgp {config['local_asn']}' "
                f"-c 'no neighbor {neighbor_ip}'",
                module_ignore_errors=True
            )
            nbrhost.shell(
                f"vtysh -c 'configure terminal' "
                f"-c 'router bgp {config['remote_asn']}' "
                f"-c 'no neighbor {local_ip}'",
                module_ignore_errors=True
            )

            # Delete routes to peer loopbacks
            prefix_len = '128' if addr_family == "ipv6" else '32'
            if not unconfigure_static_route(duthost, f"{neighbor_ip}/{prefix_len}"):
                logger.error(f"Failed to delete route to peer loopback on {duthost.hostname}")
            if not unconfigure_static_route(nbrhost, f"{local_ip}/{prefix_len}"):
                logger.error(f"Failed to delete route to peer loopback on {nbrhost.hostname}")

            # Remove loopback interfaces
            if not unconfigure_loopback(duthost, loopback_id):
                logger.error(f"Failed to unconfigure loopback {loopback_id} on {duthost.hostname}")
            if not unconfigure_loopback(nbrhost, loopback_id):
                logger.error(f"Failed to unconfigure loopback {loopback_id} on {nbrhost.hostname}")


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


def verify_bgp_peer_scale(duthosts, configs, addr_family="ipv4"):
    """
    Verify BGP peer scale configuration and status for all DUTs

    Args:
        duthosts: List of DUT host objects
        configs: List of configuration dictionaries containing neighbor information
        addr_family: Address family ("ipv4" or "ipv6")
    """
    if not configs:
        return

    ipcmd = 'ipv6' if addr_family == "ipv6" else 'ip'

    # Verify configuration for each DUT
    for duthost in duthosts:
        # Get DUT-specific configs
        dut_configs = [config for config in configs if config['duthost'] == duthost]
        if not dut_configs:
            continue

        # Get interface info once per DUT
        output = duthost.shell(f"show {ipcmd} interfaces")["stdout"]

        # Get BGP facts once per DUT
        bgp_facts = duthost.bgp_facts()['ansible_facts']

        # Verify all loopback interfaces for this DUT
        neighbor_ips = []
        for config in dut_configs:
            loopback_name = f"Loopback{config['loopback_id']}"
            interface_found = False
            ip_configured = False
            status_up = False

            for line in output.split('\n'):
                if loopback_name in line:
                    interface_found = True
                    ip_configured = config['local_ip'] in line
                    status_up = 'up/up' in line.lower()
                    break

            pytest_assert(
                interface_found,
                f"Loopback interface {loopback_name} not found in show ip interfaces output on {duthost.hostname}"
            )

            pytest_assert(
                ip_configured,
                f"Incorrect IP address configured on {loopback_name}. Expected {config['local_ip']}"
            )

            pytest_assert(
                status_up,
                f"Interface {loopback_name} is not up on {duthost.hostname}"
            )

            # Verify BGP peer configuration
            pytest_assert(
                config['neighbor_ip'] in bgp_facts['bgp_neighbors'],
                f"BGP peer {config['neighbor_ip']} not found in BGP neighbors on {duthost.hostname}"
            )

            neighbor_ips.append(config['neighbor_ip'])

        # Check all BGP sessions
        timeout = 120
        pytest_assert(
            wait_until(timeout, 5, 0, duthost.check_bgp_session_state, neighbor_ips),
            f"Not all BGP sessions are established after {timeout} seconds on {duthost.hostname}"
        )
