"""
Test BGP functionality using IPv6 link-local addresses for peering.
"""
import ipaddress
import json
import logging
import pytest
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
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

peer_group_name = "ipv6_link_local"


def configure_bgp_link_local(host, local_asn, peer_asn, interface, is_dut=True):
    """
    Configure BGP link-local peering
    """
    if is_dut and host.get_frr_mgmt_framework_config():
        # Use JSON patch for DUT when FRR management framework is enabled
        json_patch = [
            {
                "op": "add",
                "path": f"/BGP_NEIGHBOR/default|{interface}",
                "value": {
                    "asn": str(peer_asn),
                    "name": interface,
                    "peer_type": "external",
                    "admin_status": "up",
                    "peer_group_name": "PEER_V6"
                }
            },
            {
                "op": "add",
                "path": f"/BGP_NEIGHBOR_AF/default|{interface}|ipv6_unicast",
                "value": {
                    "admin_status": "up",
                    "afi_safi": "ipv6_unicast",
                    "neighbor": interface,
                    "vrf_name": "default"
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

        # 'v6only' is not supported only through vtysh
        commands = [
            "configure terminal",
            f"router bgp {local_asn}",
            f"neighbor {interface} interface v6only",
            "end"
        ]
        host.shell("vtysh -c '" + "' -c '".join(commands) + "'")
    else:
        # Base commands that are common to all platforms
        base_commands = [
            "configure terminal",
            f"router bgp {local_asn}",
        ]

        if isinstance(host, EosHost):
            # EOS requires peer-group for interface neighbors
            neighbor_commands = [
                f"neighbor {peer_group_name} peer group",
                f"neighbor {peer_group_name} remote-as {peer_asn}",
                f"neighbor interface {interface} peer-group {peer_group_name}",
            ]
            activate_command = f"neighbor {peer_group_name} activate"

            af_commands = [
                "address-family ipv6",
                activate_command,
            ]

            intf_commands = [
                f"interface {interface}",
                "ipv6 enable",
                "no ipv6 nd ra disabled"
            ]

            # Execute on EOS
            commands = base_commands + neighbor_commands + af_commands + intf_commands + ["end"]

            if hasattr(host, 'run_command_list'):
                host.run_command_list(commands)
            else:
                host.eos_config(lines=commands[1:-1], parents=[])
        else:
            pg_config = "peer-group PEER_V6" if is_dut else ""
            neighbor_commands = [
                f"neighbor {interface} interface v6only {pg_config}",
                f"neighbor {interface} remote-as {peer_asn}",
            ]
            activate_command = f"neighbor {interface} activate"

            # Build complete command list
            commands = base_commands + neighbor_commands + [
                "address-family ipv6 unicast",
                activate_command,
                "end"
            ]

            host.shell("vtysh -c '" + "' -c '".join(commands) + "'")


def unconfigure_bgp_link_local(host, local_asn, interface, is_dut=False):
    """
    Unconfigure BGP link-local peering
    """
    if is_dut and host.get_frr_mgmt_framework_config():
        # Use JSON patch for DUT when FRR management framework is enabled
        json_patch = [
            {
                "op": "remove",
                "path": f"/BGP_NEIGHBOR/default|{interface}"
            },
            {
                "op": "remove",
                "path": f"/BGP_NEIGHBOR_AF/default|{interface}|ipv6_unicast"
            }
        ]

        json_patch = format_json_patch_for_multiasic(duthost=host, json_data=json_patch, is_asic_specific=True)
        tmpfile = generate_tmpfile(host)

        try:
            # Ignore the output, if the test failed, it might not have the config for removal
            apply_patch(host, json_data=json_patch, dest_file=tmpfile)
        finally:
            delete_tmpfile(host, tmpfile)
    else:
        # Use existing vtysh commands for non-DUT or when FRR management is disabled
        pg_config = "peer-group PEER_V6" if is_dut else ""
        commands = [
            "configure terminal",
            f"router bgp {local_asn}",
            f"neighbor {interface} interface v6only {pg_config}",
            "end"
        ]
        eos_commands = [
            "configure terminal",
            f"router bgp {local_asn}",
            f"no neighbor interface {interface} peer-group {peer_group_name}",
            f"no neighbor {peer_group_name} peer group",
            "end"
        ]
        if isinstance(host, EosHost):
            host.run_command_list(eos_commands)
        else:
            host.shell("vtysh -c '" + "' -c '".join(commands) + "'")


def check_bgp_session_state(host, neighbor_addr, interface):
    """
    Check if BGP session is established
    """
    logger.info("Checking BGP session state...")
    if isinstance(host, EosHost):
        # For EOS neighbors - use EOS JSON format
        cmd = "show bgp ipv6 unicast summary | json"
        result = host.run_command(cmd)['stdout']
        # EOS structure: result[0]['vrfs']['default']['peers']
        if result and 'vrfs' in result[0] and 'default' in result[0]['vrfs']:
            peers = result[0]['vrfs']['default'].get('peers', {})
            logger.info(f"EOS peers found: {list(peers.keys())}")

            # Convert interface name: "Ethernet1" -> "Et1"
            eos_interface = interface.replace("Ethernet", "Et")
            # For EOS, check if any peer contains the interface name (e.g., "fe80::...%Et1")
            for peer_addr, peer_data in peers.items():
                if f"%{eos_interface}" in peer_addr:  # Check if %Et1 is part of the peer address
                    logger.info(f"Found EOS peer {peer_addr} for interface {interface} "
                                f"(EOS format: {eos_interface}): {peer_data}")
                    return peer_data.get('peerState', '') == 'Established'
            return False
        return False

    cmd = "show bgp ipv6 unicast summary json"
    result = json.loads(host.shell(f"vtysh -c '{cmd}'")['stdout'])

    logger.info(f"Checking BGP session state for interface {interface}")
    logger.info(f"BGP summary result: {result}")

    # Get peers directly from the result
    peers = result.get('peers', {})
    logger.info(f"Found peers: {peers}")

    # Check if the interface is directly in peers
    if interface in peers:
        peer_data = peers[interface]
        logger.info(f"Found peer data for {interface}: {peer_data}")
        return peer_data['state'] == 'Established'

    logger.info(f"No matching peer found for interface {interface}")
    return False


def get_existing_ipv6_bgp_neighbor(mg_facts):
    """
    Get the first IPv6 BGP neighbor and returns a tuple of
    (interface_name, bgp_neighbor) information from topology facts.
    """
    try:
        for bgp_neighbor in mg_facts.get('minigraph_bgp', []):
            if ipaddress.ip_address(bgp_neighbor['addr']).version != 6:
                continue
            for dut_interface, neighbor in mg_facts.get('minigraph_neighbors', {}).items():
                if neighbor['name'] == bgp_neighbor['name']:
                    return dut_interface, bgp_neighbor
        return None, {}
    except Exception as e:
        logger.error(f"Failed to get IPv6 interface information: {str(e)}")
        return None, {}


def bgp_link_local_setup(duthosts, rand_one_dut_hostname, nbrhosts, tbinfo):
    """
    Setup BGP link-local configuration before test
    """
    duthost = duthosts[rand_one_dut_hostname]
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    # Find (any) existing IPv6 BGP neighbor to transform as
    # interface neighbor for link-local test
    dut_interface, bgp_neighbor = get_existing_ipv6_bgp_neighbor(mg_facts)
    pytest_assert(bgp_neighbor and dut_interface,
                  "Failed to find an Ethernet interface with IPv6 peer configured")

    # Get the corresponding peer interface
    peer_interfaces = mg_facts['minigraph_neighbors']
    peer_interface = None
    if dut_interface in peer_interfaces:
        peer_data = peer_interfaces[dut_interface]
        if peer_data['name'] in nbrhosts:
            peer_interface = peer_data['port']

    pytest_assert(peer_interface,
                  f"Failed to find peer interface corresponding to DUT interface {dut_interface}")

    return (dut_interface, peer_interface, bgp_neighbor)


def bgp_link_local_teardown(duthost, nbrhosts, dut_interface, peer_interface, bgp_neighbor):
    '''
    Cleanup and restore the original configuration
    '''
    config_facts = duthost.get_running_config_facts()
    dut_asn = config_facts['DEVICE_METADATA']['localhost']['bgp_asn']
    peer_name = bgp_neighbor['name']

    logger.info(f"Cleanup BGP link-local neighbor configurations on {duthost} {peer_name}")
    unconfigure_bgp_link_local(duthost, dut_asn, dut_interface, is_dut=True)
    unconfigure_bgp_link_local(nbrhosts[peer_name]['host'], bgp_neighbor['asn'],
                               peer_interface, is_dut=False)
    update_global_bgp_neighbor(duthost, bgp_neighbor['addr'], activate=True, check_output=False)
    update_global_bgp_neighbor(nbrhosts[peer_name]['host'], bgp_neighbor['peer_addr'], asn=bgp_neighbor['asn'],
                               activate=True, check_output=False)


@pytest.fixture
def bgp_link_local_setup_teardown(duthosts, rand_one_dut_hostname, nbrhosts, tbinfo):
    '''
    Fixture to setup and do the cleanup for the link-local test
    '''
    # Setup
    dut_interface, peer_interface, bgp_neighbor = bgp_link_local_setup(duthosts, rand_one_dut_hostname,
                                                                       nbrhosts, tbinfo)
    # Run the test
    yield (dut_interface, peer_interface, bgp_neighbor)

    # Cleanup
    duthost = duthosts[rand_one_dut_hostname]
    bgp_link_local_teardown(duthost, nbrhosts, dut_interface, peer_interface, bgp_neighbor)


def update_global_bgp_neighbor(host, neighbor_addr, asn=None, activate=True, check_output=True):
    """
    Activate / Deactivate a global BGP neighbor configuration
    """
    if isinstance(host, EosHost):
        # For cEOS, use the provided ASN
        if asn is None:
            raise ValueError("ASN must be provided for EOS hosts")

        action_cmd = ("no " if activate else "") + f"neighbor {neighbor_addr} shutdown"
        host.eos_config(
            lines=[action_cmd],
            parents=[f'router bgp {asn}']
        )
        return True

    # SONiC logic (keep exact existing logic)
    neighbor_addr = neighbor_addr.lower()
    logger.info(f"{'' if activate else 'de'}activating global BGP neighbor {neighbor_addr}")

    if host.get_frr_mgmt_framework_config():
        neighbor_addr = "default|" + neighbor_addr
    # Use JSON patch for DUT when FRR management framework is enabled
    json_patch = [
        {
            "op": "replace",
            "path": f"/BGP_NEIGHBOR/{neighbor_addr}/admin_status",
            "value": "up" if activate else "down"
        }
    ]
    tmpfile = generate_tmpfile(host)
    try:
        output = apply_patch(host, json_data=json_patch, dest_file=tmpfile)
        if check_output:
            expect_op_success(host, output)
    finally:
        delete_tmpfile(host, tmpfile)


def get_received_prefixes(duthost, neighbor):
    '''
    Get the received prefix-count from the specified neighbor
    '''
    dut_cmd = f"show bgp ipv6 unicast neighbor {neighbor} prefix-count json"
    logger.info(f"Checking DUT received prefixes with command: {dut_cmd}")

    dut_neighbor_info = json.loads(duthost.shell(f"vtysh -c '{dut_cmd}'")['stdout'])
    logger.info(f"DUT neighbor info: {json.dumps(dut_neighbor_info, indent=2)}")

    dut_received_prefixes = int(dut_neighbor_info.get('pfxCounter', 0))
    logger.info(f"DUT received {dut_received_prefixes} prefixes from peer")

    return dut_received_prefixes


def test_bgp_link_local_peer(duthosts, rand_one_dut_hostname, nbrhosts, tbinfo, bgp_link_local_setup_teardown):
    """
    Test BGP peering over IPv6 link-local address.
    """
    duthost = duthosts[rand_one_dut_hostname]

    # Get first available IPv6 Ethernet interface
    dut_interface, peer_interface, bgp_neighbor = bgp_link_local_setup_teardown
    peer_name = bgp_neighbor['name']

    # Log testbed information for debugging
    logger.info(f"Testing with DUT: {duthost.hostname}")
    logger.info(f"Selected DUT interface: {dut_interface}")
    logger.info(f"Selected neighbor: {bgp_neighbor}, interface: {peer_interface}")

    config_facts = duthost.get_running_config_facts()
    dut_asn = config_facts['DEVICE_METADATA']['localhost']['bgp_asn']

    logger.info(f"Selected peer interface: {peer_interface}")

    received_prefixes = get_received_prefixes(duthost, bgp_neighbor['addr'])
    logger.info(f"Deactivating global BGP neighbor for test peer {peer_name}")
    update_global_bgp_neighbor(duthost, bgp_neighbor['addr'], activate=False)
    update_global_bgp_neighbor(nbrhosts[peer_name]['host'], bgp_neighbor['peer_addr'],
                               asn=bgp_neighbor['asn'], activate=False)

    # Configure BGP on DUT
    logger.info(f"Configuring BGP on DUT (interface: {dut_interface})")
    configure_bgp_link_local(duthost, dut_asn, bgp_neighbor['asn'], dut_interface, is_dut=True)

    # Configure BGP on peer
    logger.info(f"Configuring BGP on peer (interface: {peer_interface})")
    configure_bgp_link_local(nbrhosts[peer_name]['host'], bgp_neighbor['asn'], dut_asn, peer_interface, is_dut=False)

    # Wait for BGP session to establish on DUT
    logger.info("Waiting for BGP session to establish on DUT...")
    dut_established = wait_until(90, 1, 0, lambda: check_bgp_session_state(duthost, None, dut_interface))
    logger.info(f"DUT BGP session established: {dut_established}")

    # Wait for BGP session to establish on peer
    logger.info("Waiting for BGP session to establish on peer...")
    peer_established = wait_until(
        30, 1, 0,
        lambda: check_bgp_session_state(
            nbrhosts[peer_name]['host'],
            None,
            peer_interface
        )
    )
    logger.info(f"Peer BGP session established: {peer_established}")

    pytest_assert(dut_established, f"BGP session failed to establish on DUT (interface {dut_interface})")
    pytest_assert(peer_established, f"BGP session failed to establish on peer (interface {peer_interface})")

    # Verify route exchange
    logger.info("Waiting for DUT to receive prefixes from peer...")
    pytest_assert(wait_until(60, 5, 0,
                             lambda: get_received_prefixes(duthost, dut_interface) == received_prefixes),
                  f"Expected {received_prefixes} prefixes received on DUT from peer after 60 seconds")
    # Verify the bash command for received routes from the peer
    if duthost.get_frr_mgmt_framework_config():
        bash_cmd = f"show ipv6 bgp neighbors {dut_interface} received-routes"
        vtysh_cmd = f"vtysh -c 'show bgp ipv6 neighbors {dut_interface} received-routes'"
        pytest_assert(duthost.shell(bash_cmd)['stdout'] == duthost.shell(vtysh_cmd)['stdout'],
                      "received-routes command output mismatch")
