"""
Test BGP peer scaling by adding multiple BGP peers using loopback interfaces on SONiC DUTs.
"""
import logging
import pytest
import ipaddress
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("t0", "t1"),
]

# Constants for BGP peer scaling
BASE_LOOPBACK_ID = 1  # Starting Loopback ID
BASE_BGP_ASN = 65100  # Starting ASN for new peers
PEERS_PER_DUT = 2  # Number of additional peers to configure per DUT


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


def configure_bgp_peer(duthost, neighbor_ip, local_asn, remote_asn, addr_family="ipv4"):
    """Configure a BGP peer with proper timers."""
    try:
        commands = [
            "vtysh -c 'configure terminal' "
            f"-c 'router bgp {local_asn}' "
            f"-c 'neighbor {neighbor_ip} remote-as {remote_asn}' "
            f"-c 'neighbor {neighbor_ip} ebgp-multihop 10' "
            f"-c 'neighbor {neighbor_ip} timers 3 10' "
            f"-c 'neighbor {neighbor_ip} timers connect 10' "
            f"-c 'neighbor {neighbor_ip} update-source lo{BASE_LOOPBACK_ID}' "
            f"-c 'address-family {addr_family} unicast' "
            f"-c 'neighbor {neighbor_ip} activate' "
            f"-c 'exit-address-family'"
        ]

        result = duthost.shell("\n".join(commands))
        if result['rc'] != 0:
            logger.error("Failed to configure BGP peer. Error: %s", result['stderr'])
            return False
        return True

    except Exception as e:
        logger.error("Failed to configure BGP peer: %s", str(e))
        return False


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


def configure_loopback(duthost, loopback_id, ip_addr):
    """Configure a loopback interface with the given IP.

    Args:
        duthost: DUT host object
        loopback_id: Loopback interface ID
        ip_addr: IP address (IPv4 or IPv6) to configure
    """
    try:
        loopback_name = f"Loopback{loopback_id}"
        is_ipv6 = ':' in ip_addr
        ipcmd = 'ipv6' if is_ipv6 else 'ip'
        prefix_len = '128' if is_ipv6 else '32'

        # Configure loopback interface
        try:
            # Check if loopback exists
            check_cmd = duthost.shell(f"show {ipcmd} interfaces | grep {loopback_name}", module_ignore_errors=True)
            loopback_exists = check_cmd['rc'] == 0
        except Exception:
            loopback_exists = False

        if not loopback_exists:
            result = duthost.shell(f"config loopback add {loopback_name}")
            if result['rc'] != 0 and "already exists" not in result['stderr']:
                logger.error(f"Failed to add loopback: {result['stderr']}")
                return False

        # Configure IP address
        cmd = f"config interface ip add {loopback_name} {ip_addr}/{prefix_len}"
        result = duthost.shell(cmd)
        if result['rc'] != 0:
            logger.error(f"Failed to configure IP on {loopback_name}. Error: {result['stderr']}")
            return False

        return True

    except Exception as e:
        logger.error(f"Error configuring loopback: {str(e)}")
        return False


def configure_peer_route(duthost, peer_ip, next_hop_ip):
    """Configure route to reach peer's loopback IP.

    Args:
        duthost: DUT host object
        peer_ip: Peer's loopback IP to reach
        next_hop_ip: Next hop IP for reaching the peer
    """
    try:
        is_ipv6 = ':' in peer_ip
        ip_route_cmd = 'ip -6 route' if is_ipv6 else 'ip route'
        prefix_len = '128' if is_ipv6 else '32'

        route_cmd = f"{ip_route_cmd} add {peer_ip}/{prefix_len} via {next_hop_ip}"
        result = duthost.shell(route_cmd, module_ignore_errors=True)
        if result['rc'] != 0 and "File exists" not in result.get('stderr', ''):
            logger.error(f"Failed to configure route to peer. Error: {result['stderr']}")
            return False

        return True

    except Exception as e:
        logger.error(f"Error configuring peer route: {str(e)}")
        return False


def unconfigure_loopback(duthost, loopback_id, ip_addr):
    """Unconfigure a loopback interface and its route.

    Args:
        duthost: DUT host object
        loopback_id: Loopback interface ID
        ip_addr: IP address (IPv4 or IPv6) configured on the loopback
    """
    try:
        loopback_name = f"Loopback{loopback_id}"
        is_ipv6 = ':' in ip_addr
        prefix_len = '128' if is_ipv6 else '32'

        # Remove IP address from loopback
        cmd = f"config interface ip remove {loopback_name} {ip_addr}/{prefix_len}"
        result = duthost.shell(cmd, module_ignore_errors=True)
        if result['rc'] != 0 and "does not exist" not in result.get('stderr', ''):
            logger.error(f"Failed to remove IP from {loopback_name}. Error: {result['stderr']}")
            return False

        # Remove loopback interface
        result = duthost.shell(f"config loopback del {loopback_name}", module_ignore_errors=True)
        if result['rc'] != 0 and "does not exist" not in result.get('stderr', ''):
            logger.error(f"Failed to remove loopback: {result['stderr']}")
            return False

        return True

    except Exception as e:
        logger.error(f"Error unconfiguring loopback: {str(e)}")
        return False


def delete_peer_route(duthost, peer_ip):
    """Delete route to peer's loopback IP.

    Args:
        duthost: DUT host object
        peer_ip: Peer's loopback IP whose route needs to be deleted
    """
    try:
        print(f"\nDeleting route on {duthost.hostname}:")
        print(f"  To peer IP: {peer_ip}")

        is_ipv6 = ':' in peer_ip
        ip_route_cmd = 'ip -6 route' if is_ipv6 else 'ip route'
        prefix_len = '128' if is_ipv6 else '32'

        route_cmd = f"{ip_route_cmd} del {peer_ip}/{prefix_len}"
        result = duthost.shell(route_cmd, module_ignore_errors=True)
        if result['rc'] != 0 and "No such process" not in result.get('stderr', ''):
            logger.error(f"Failed to delete route to peer. Error: {result['stderr']}")
            return False
        return True

    except Exception as e:
        logger.error(f"Error deleting peer route: {str(e)}")
        return False


def get_remote_asn(duthost):
    """Get the remote ASN from the existing BGP neighbors or config."""
    try:
        # First try to get from BGP facts
        bgp_facts = duthost.bgp_facts(module_ignore_errors=True)['ansible_facts']
        existing_peers = bgp_facts.get('bgp_neighbors', {})
        for peer_ip, peer_data in existing_peers.items():
            if 'remote AS' in peer_data and ':' not in peer_ip:
                return peer_data['remote AS']
    except Exception as e:
        logger.warning(f"Failed to get BGP facts: {str(e)}")

    try:
        # Fallback to config facts
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        bgp_config = config_facts.get('BGP_NEIGHBOR', {})
        for peer_ip, peer_data in bgp_config.items():
            if 'asn' in peer_data and ':' not in peer_ip:
                return peer_data['asn']
    except Exception as e:
        logger.warning(f"Failed to get config facts: {str(e)}")

    # If no ASN found, use a default value for testing
    logger.warning("No existing BGP configuration found, using default ASN 65502")
    return 65502


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
            # Get DUT configuration
            config_facts = duthost.config_facts(host=duthost.hostname, source="running",
                                                module_ignore_errors=True)['ansible_facts']
            local_asn = config_facts.get('DEVICE_METADATA',
                                         {}).get('localhost', {}).get('bgp_asn', 65100)  # Default ASN if not found
            remote_asn = get_remote_asn(duthost)

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
                    if not configure_peer_route(duthost, neighbor_ip, dut_nbr_ip):
                        pytest.fail(f"Failed to configure route to peer loopback on {duthost.hostname}")
                    if not configure_peer_route(nbrhost, local_ip, nbr_dut_ip):
                        pytest.fail(f"Failed to configure route to peer loopback on {nbrhost.hostname}")

                    # Configure BGP peers
                    if not configure_bgp_peer(duthost, neighbor_ip, local_asn, remote_asn, addr_family=addr_family):
                        pytest.fail(f"Failed to configure {addr_family} BGP peer on {duthost.hostname}")
                    if not configure_bgp_peer(nbrhost, local_ip, remote_asn, local_asn, addr_family=addr_family):
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
        verify_bgp_peer_scale(configs)
    finally:
        # Clean up configurations
        for config in configs:
            duthost = config['duthost']
            nbrhost = config['nbrhost']
            loopback_id = config['loopback_id']
            local_ip = config['local_ip']
            neighbor_ip = config['neighbor_ip']

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
            if not delete_peer_route(duthost, neighbor_ip):
                logger.error(f"Failed to delete route to peer loopback on {duthost.hostname}")
            if not delete_peer_route(nbrhost, local_ip):
                logger.error(f"Failed to delete route to peer loopback on {nbrhost.hostname}")

            # Remove loopback interfaces
            if not unconfigure_loopback(duthost, loopback_id, local_ip):
                logger.error(f"Failed to unconfigure loopback {loopback_id} on {duthost.hostname}")
            if not unconfigure_loopback(nbrhost, loopback_id, neighbor_ip):
                logger.error(f"Failed to unconfigure loopback {loopback_id} on {nbrhost.hostname}")


def test_bgp_peer_scale_v4(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo):
    """
    Verify BGP IPv4 peer scaling by checking:
    1. All VLAN interfaces are properly configured and up
    2. All BGP peers are configured
    3. All BGP sessions are established
    """
    run_bgp_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo, addr_family="ipv4")


# def test_bgp_peer_scale_v6(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo):
#     """
#     Verify BGP IPv6 peer scaling by checking:
#     1. All VLAN interfaces are properly configured and up
#     2. All BGP peers are configured
#     3. All BGP sessions are established
#     """
#     run_bgp_peer_scale(duthosts, enum_rand_one_per_hwsku_hostname, nbrhosts, tbinfo, addr_family="ipv6")


def wait_bgp_sessions(duthost, timeout=60):
    """
    Wait for all BGP sessions to establish across all ASICs.

    Args:
        duthost: DUT host object
        timeout: Maximum time to wait in seconds (default: 60)

    Returns:
        None. Raises assertion error if sessions don't establish.
    """
    bgp_neighbors = duthost.get_bgp_neighbors()
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
        loopback_name = f"Loopback{config['loopback_id']}"

        # Verify loopback interface configuration
        output = duthost.shell("show ip interfaces")["stdout"]
        interface_found = False
        ip_configured = False
        status_up = False

        for line in output.split('\n'):
            if loopback_name in line:
                interface_found = True
                ip_configured = config['local_ip'] in line
                status_up = 'up' in line.lower()
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

        # Verify BGP peer configuration and status
        bgp_facts = duthost.bgp_facts()['ansible_facts']
        pytest_assert(
            config['neighbor_ip'] in bgp_facts['bgp_neighbors'],
            f"BGP peer {config['neighbor_ip']} not found in BGP neighbors on {duthost.hostname}"
        )

        wait_bgp_sessions(duthost)
