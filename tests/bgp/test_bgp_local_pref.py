"""
Test BGP route-map functionality for community matching and local preference modification.
"""
import json
import logging
import pytest
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.config_reload import config_reload
from tests.common.devices.eos import EosHost
from tests.common.gu_utils import (
    generate_tmpfile,
    delete_tmpfile,
    apply_patch,
    expect_op_success,
    format_json_patch_for_multiasic
)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t1'),
]


def configure_community_route_map(host, route_map_name="COMM_LOCAL_PREF", community="1234:5678", is_dut=False):
    """
    Configure route-map to match on community and set local preference
    """
    logger.info(f"Configuring route-map {route_map_name} to match community {community}")
    community_name = "LOCAL_PREF_TEST"

    if is_dut and host.get_frr_mgmt_framework_config():
        # Use JSON patch for DUT when FRR management framework is enabled
        json_patch = [
            {
                "op": "add",
                "path": f"/COMMUNITY_LIST/{community_name}",
                "value": {
                    "type": "standard",
                    "members": [
                        {
                            "action": "permit",
                            "community": community,
                            "seq": "5"
                        }
                    ]
                }
            },
            {
                "op": "add",
                "path": f"/ROUTE_MAP/{route_map_name}|10",
                "value": {
                    "route_operation": "permit",
                    "match_community_list": community_name,
                    "set_local_pref": "0"
                }
            }
        ]

        json_patch = format_json_patch_for_multiasic(duthost=host, json_data=json_patch, is_asic_specific=True)
        tmpfile = generate_tmpfile(host)

        try:
            output = apply_patch(host, json_data=json_patch, dest_file=tmpfile)
            expect_op_success(host, output)
        finally:
            delete_tmpfile(host, tmpfile)
    else:
        # Use vtysh commands
        commands = [
            "configure terminal",
            f"bgp community-list standard LOCAL_PREF_TEST seq 5 permit {community}",
            f"route-map {route_map_name} permit 10",
            f"match community {community_name}",
            "set local-preference 0",
            "end"
        ]
        host.shell("vtysh -c '" + "' -c '".join(commands) + "'")


def configure_peer_community_route_map(host, route_map_name="SET_COMMUNITY", community="1234:5678",
                                       dut_addr=None, peer_asn=None):
    """
    Configure route-map on peer to set community on outbound routes to DUT
    """
    logger.info(f"Configuring peer route-map {route_map_name} to set community {community} with peer ASN {peer_asn}")

    # Get the actual host object if it's wrapped in NeighborDevice
    actual_host = host
    if isinstance(host, dict) and 'host' in host:
        # NeighborDevice is a dict with 'host' key containing the actual host object
        actual_host = host['host']
    elif hasattr(host, 'host'):
        actual_host = host.host

    # Check if this is an EOS host
    if isinstance(actual_host, EosHost):
        # EOS configuration - split into two parts to ensure route-map exists before applying
        # First, create the route-map using parents parameter
        logger.info(f"Creating route-map {route_map_name} on EOS peer")
        actual_host.eos_config(
            lines=[f"set community {community}"],
            parents=[f"route-map {route_map_name} permit 10"]
        )

        # Verify route-map was created
        try:
            result = actual_host.eos_command(commands=[f"show route-map {route_map_name}"])
            logger.info(f"Route-map verification: {result}")
        except Exception as e:
            logger.warning(f"Could not verify route-map: {e}")

        # Then, apply it to the BGP neighbor using parents parameter
        actual_host.eos_config(
            lines=[
                f"neighbor {dut_addr} route-map {route_map_name} out",
                f"neighbor {dut_addr} send-community",  # EOS syntax: just "send-community"
            ],
            parents=[f"router bgp {peer_asn}"]
        )
    else:
        # Sonic/FRR configuration
        commands = [
            "configure terminal",
            f"route-map {route_map_name} permit 10",
            f"set community {community}",
            "exit",
            f"router bgp {peer_asn}",
            f"neighbor {dut_addr} route-map {route_map_name} out",
            f"neighbor {dut_addr} send-community both",  # FRR syntax: "send-community both"
            "end"
        ]
        logger.info(f"Configuring Sonic peer with ASN {peer_asn}, commands: {commands}")
        actual_host.shell("vtysh -c '" + "' -c '".join(commands) + "'")


def cleanup_peer_community_route_map(host, route_map_name="SET_COMMUNITY", dut_addr=None, peer_asn=None):
    """
    Remove route-map configuration from peer
    """
    logger.info(f"Cleaning up peer route-map {route_map_name} with peer ASN {peer_asn}")

    # Get the actual host object if it's wrapped in NeighborDevice
    actual_host = host
    if isinstance(host, dict) and 'host' in host:
        # NeighborDevice is a dict with 'host' key containing the actual host object
        actual_host = host['host']
    elif hasattr(host, 'host'):
        actual_host = host.host

    # Check if this is an EOS host
    if isinstance(actual_host, EosHost):
        # EOS configuration - split cleanup into two parts
        # First, remove BGP neighbor configuration using parents parameter
        logger.info("Removing BGP neighbor config on EOS peer")
        try:
            actual_host.eos_config(
                lines=[
                    f"no neighbor {dut_addr} route-map {route_map_name} out",
                    f"no neighbor {dut_addr} send-community",  # EOS syntax: just "send-community"
                ],
                parents=[f"router bgp {peer_asn}"]
            )
        except Exception as e:
            logger.warning(f"Failed to remove BGP neighbor config: {e}")

        # Then, remove the route-map
        logger.info("Removing route-map on EOS peer")
        try:
            actual_host.eos_config(lines=[f"no route-map {route_map_name}"])
        except Exception as e:
            logger.warning(f"Failed to remove route-map: {e}")
    else:
        # Sonic/FRR configuration
        commands = [
            "configure terminal",
            f"router bgp {peer_asn}",
            f"no neighbor {dut_addr} route-map {route_map_name} out",
            f"no neighbor {dut_addr} send-community both",  # FRR syntax: "send-community both"
            "exit",
            f"no route-map {route_map_name}",
            "end"
        ]
        logger.info(f"Cleaning up Sonic peer with ASN {peer_asn}, commands: {commands}")
        actual_host.shell("vtysh -c '" + "' -c '".join(commands) + "'")


def get_multi_path_routes(host, peer_addr):
    """
    Get routes that are learned from multiple peers, including the specified peer
    """
    cmd = "show ip bgp json"
    logger.info(f"Executing command: {cmd}")
    output = json.loads(host.shell(f"vtysh -c '{cmd}'")['stdout'])
    multi_path_routes = []

    routes = output.get('routes', {})
    for prefix, paths in routes.items():
        # paths is a list of path entries
        if len(paths) > 1:  # Multiple paths exist for this prefix
            # Check if our target peer is one of the path sources
            peer_path = None
            other_paths = []

            for path in paths:
                # Extract peer ID from nexthops
                nexthops = path.get('nexthops', [])
                if nexthops:
                    path_peer_ip = nexthops[0].get('ip')
                    if path_peer_ip == peer_addr:
                        peer_path = path
                    else:
                        other_paths.append(path)

            if peer_path and other_paths:
                multi_path_routes.append({
                    'prefix': prefix,
                    'peer_path': peer_path,
                    'other_paths': other_paths
                })

    logger.info(f"Found {len(multi_path_routes)} multi-path routes including peer {peer_addr}")
    return multi_path_routes


def test_bgp_community_local_pref(duthosts, rand_one_dut_hostname, nbrhosts, tbinfo):
    """
    Test BGP route-map matching on community and setting local preference

    Test steps:
    1. Find routes that are learned from multiple peers
    2. Configure route-map on remote peer to set a specific community on outbound routes
    3. Configure route-map on DUT to match that community and set local-pref to 0
    4. Apply route-map to target peer
    5. Verify routes are still being learned from other peers with original local preference

    Expected results:
    - Routes from the target peer should have local preference 0
    - Routes should still be learned from other peers with original local preference
    - Best path selection should prefer paths from other peers
    """
    duthost = duthosts[rand_one_dut_hostname]

    # Get configuration facts
    config_facts = duthost.get_running_config_facts()
    dut_asn = config_facts['DEVICE_METADATA']['localhost']['bgp_asn']
    # Get peer information
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    bgp_neighbors = mg_facts.get('minigraph_bgp', [])

    # Find a suitable peer
    peer_name = None
    peer_addr = None
    peer_host = None
    peer_asn = None
    for neighbor in bgp_neighbors:
        if neighbor['name'] in nbrhosts:
            peer_name = neighbor['name']
            peer_addr = neighbor['addr']
            peer_host = nbrhosts[peer_name]
            peer_asn = neighbor['asn']  # Get the peer's ASN from minigraph
            break

    pytest_assert(peer_name is not None, "Could not find suitable peer")
    logger.info(f"Selected peer: {peer_name} (ASN: {peer_asn}, Address: {peer_addr})")

    # Get multi-path routes that include our target peer
    multi_path_routes = get_multi_path_routes(duthost, peer_addr)
    pytest_assert(multi_path_routes, "No multi-path routes found")

    test_route = multi_path_routes[0]

    # Use a specific community that we will configure on the peer
    test_community = "1234:5678"

    # Get DUT's interface IP address that connects to this specific peer
    dut_addr = None
    for neighbor in bgp_neighbors:
        if neighbor['name'] == peer_name:
            dut_addr = neighbor.get('local_addr')
            if dut_addr:
                logger.info(f"Found DUT local address {dut_addr} for peer {peer_name}")
            break

    if not dut_addr:
        # Fallback: try to find the interface IP from minigraph interfaces
        # Look for the interface that connects to this peer
        import ipaddress
        interfaces = mg_facts.get('minigraph_interfaces', []) + mg_facts.get('minigraph_portchannel_interfaces', [])
        for interface in interfaces:
            # Check if this interface subnet contains the peer address
            try:
                interface_network = ipaddress.ip_network(f"{interface['addr']}/{interface['prefixlen']}", strict=False)
                peer_ip = ipaddress.ip_address(peer_addr)
                if peer_ip in interface_network:
                    dut_addr = interface['addr']
                    logger.info(f"Found DUT interface address {dut_addr} for peer {peer_addr} via subnet matching")
                    break
            except Exception as e:
                logger.debug(f"Failed to check interface {interface.get('addr', 'unknown')}: {e}")
                continue

        if not dut_addr:
            logger.error("Could not determine DUT interface address for peer configuration")
            pytest_assert(False, f"Could not determine DUT interface address for peer {peer_name} ({peer_addr})")

    peer_route_map_name = "SET_COMMUNITY"
    dut_route_map_name = "COMM_LOCAL_PREF"

    logger.info("Configuration summary: ")
    logger.info(f"  Peer: {peer_name} (ASN: {peer_asn}, Address: {peer_addr})")
    logger.info(f"  DUT interface address for peer: {dut_addr}")
    logger.info(f"  Test community: {test_community}")

    try:
        # Step 1: Configure route-map on peer to set community on outbound routes
        configure_peer_community_route_map(peer_host, peer_route_map_name, test_community, dut_addr, peer_asn)

        # Step 2: Configure route-map on DUT to match community and set local preference
        configure_community_route_map(duthost, dut_route_map_name, test_community, is_dut=True)

        # Step 3: Apply route-map to peer on DUT
        if duthost.get_frr_mgmt_framework_config():
            json_patch = [
                {
                    "op": "add",
                    "path": f"/BGP_NEIGHBOR/default|{peer_addr}",
                    "value": {
                        "route_map_in": dut_route_map_name
                    }
                }
            ]

            json_patch = format_json_patch_for_multiasic(duthost=duthost, json_data=json_patch, is_asic_specific=True)
            tmpfile = generate_tmpfile(duthost)
            try:
                output = apply_patch(duthost, json_data=json_patch, dest_file=tmpfile)
                expect_op_success(duthost, output)
            finally:
                delete_tmpfile(duthost, tmpfile)
        else:
            commands = [
                "configure terminal",
                f"router bgp {dut_asn}",
                "address-family ipv4 unicast",
                f"neighbor {peer_addr} route-map {dut_route_map_name} in",
                "end"
            ]
            duthost.shell("vtysh -c '" + "' -c '".join(commands) + "'")

        # Wait for route changes and verify
        def check_route_paths():
            # Use the get_route method from SonicHost to get route information
            output = duthost.get_route(test_route['prefix'])

            paths = output.get('paths', [])
            if not paths:
                logger.warning("No paths found")
                return False

            target_peer_path = None
            other_paths = []

            for path in paths:
                peer_info = path.get('peer', {})
                peer_id = peer_info.get('peerId')

                if peer_id == peer_addr:
                    target_peer_path = path
                else:
                    other_paths.append(path)

            if target_peer_path is None:
                logger.warning(f"No path found from target peer {peer_addr}")
                return False

            # Verify target peer path has local_pref 0
            local_pref = target_peer_path.get('locPrf')
            if local_pref != 0:
                logger.warning(f"Target peer path local_pref is {local_pref}, expected 0")
                return False

            # Verify other paths still have original local_pref
            for path in other_paths:
                if path.get('locPrf') == 0:
                    logger.warning(f"Non-target path has local_pref 0: {path}")
                    return False

            # Verify best path is not from target peer
            best_path = next((p for p in paths if p.get('bestpath', {}).get('overall')), None)
            if not best_path:
                logger.warning("No best path found")
                return False

            best_path_peer = best_path.get('peer', {}).get('peerId')
            is_valid = (best_path_peer != peer_addr)
            logger.info(f"Best path check {'passed' if is_valid else 'failed'} "
                        f"(best path peer: {best_path_peer}, target peer: {peer_addr})")

            return is_valid

        result = wait_until(30, 5, 0, check_route_paths)
        if not result:
            cmd = f"show ip bgp {test_route['prefix']} json"
            current_paths = json.loads(duthost.shell(f"vtysh -c '{cmd}'")['stdout'])
            logger.error(f"Final BGP path state: {json.dumps(current_paths, indent=2)}")
            pytest.fail("Route path verification failed")

    finally:
        # Clean up peer configuration first
        try:
            cleanup_peer_community_route_map(peer_host, peer_route_map_name, dut_addr, peer_asn)
        except Exception as e:
            logger.warning(f"Failed to cleanup peer configuration: {e}")

        # Config reload will restore original configuration on DUT
        config_reload(duthost, config_source='config_db', wait=60)
