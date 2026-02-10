import pytest
import time
import ipaddress
import logging

from tests.common.utilities import wait_until
from tests.common.helpers.assertions import pytest_assert as py_assert
from tests.dhcp_relay.dhcp_relay_utils import check_routes_to_dhcp_server

logger = logging.getLogger(__name__)

SINGLE_TOR_MODE = 'single'
DUAL_TOR_MODE = 'dual'


def find_vlan_member_for_extraction(duthost, tbinfo, require_ptf_index=True):
    """Find a VLAN member that can be temporarily extracted for testing.

    Args:
        duthost: DUT host object
        tbinfo: Testbed info
        require_ptf_index: If True, only return interfaces with PTF index (default: True)

    Returns:
        dict with keys:
            'interface': Interface name (e.g., 'Ethernet40')
            'vlan': VLAN name (e.g., 'Vlan1000')
        or None if not found
    """
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    config_facts = duthost.get_running_config_facts()

    # Get uplink interfaces to avoid
    uplink_interfaces = set()
    for iface_name, neighbor_info in mg_facts.get('minigraph_neighbors', {}).items():
        if neighbor_info['name'] in mg_facts.get('minigraph_devices', {}):
            neighbor_device = mg_facts['minigraph_devices'][neighbor_info['name']]
            if neighbor_device.get('type') in ['LeafRouter', 'MgmtLeafRouter', 'BackEndLeafRouter']:
                uplink_interfaces.add(iface_name)

    vlan_member_table = config_facts.get('VLAN_MEMBER', {})

    for vlan_name, members_dict in vlan_member_table.items():
        member_list = list(members_dict.keys())

        ethernet_members = [m for m in member_list if m.startswith('Ethernet')]

        if len(ethernet_members) < 2:
            logger.info("Skipping {} - only has {} Ethernet member(s)".format(vlan_name, len(ethernet_members)))
            continue

        for member_name in ethernet_members:
            if member_name in uplink_interfaces:
                continue

            if require_ptf_index and member_name not in mg_facts.get('minigraph_ptf_indices', {}):
                continue

            tagging_mode = members_dict[member_name].get('tagging_mode', 'untagged')

            logger.info("Found VLAN member for extraction: {} from {} ({} members, tagging_mode: {})".format(
                member_name, vlan_name, len(member_list), tagging_mode))
            return {
                'interface': member_name,
                'vlan': vlan_name,
                'tagging_mode': tagging_mode
            }

    logger.warning("No suitable VLAN member found for extraction")
    return None


def configure_dhcp_servers_on_interface(duthost, interface_name, dhcp_servers):
    """Configure dhcp_servers on an interface (VLAN or routed).

    Args:
        duthost: DUT host object
        interface_name: Name of the interface (e.g., 'Ethernet0' or 'Vlan1000')
        dhcp_servers: List of DHCP server IP addresses

    Returns:
        True if configuration succeeded, False otherwise
    """
    if len(dhcp_servers) == 0:
        logger.warn("dhcp_servers is empty, cannot configure dhcp_relay")
        return False

    dhcp_servers_str = ' '.join(dhcp_servers)

    if interface_name.startswith('Vlan'):
        cmd = "config vlan dhcp_relay add {} {}".format(interface_name, dhcp_servers_str)
    else:
        cmd = "config interface dhcp_relay add {} {}".format(interface_name, dhcp_servers_str)

    try:
        duthost.shell(cmd)
        logger.info("Configured dhcp_servers {} on {}".format(dhcp_servers, interface_name))
        return True
    except Exception as e:
        logger.error("Failed to configure dhcp_servers on {}: {}".format(interface_name, str(e)))
        return False


def remove_dhcp_servers_from_interface(duthost, interface_name, dhcp_servers):
    """Remove dhcp_servers from an interface (VLAN or routed).

    Args:
        duthost: DUT host object
        interface_name: Name of the interface (e.g., 'Ethernet0' or 'Vlan1000')
        dhcp_servers: List of DHCP server IP addresses to remove

    Returns:
        True if removal succeeded, False otherwise
    """
    dhcp_servers_str = ' '.join(dhcp_servers)

    if interface_name.startswith('Vlan'):
        cmd = "config vlan dhcp_relay del {} {}".format(interface_name, dhcp_servers_str)
    else:
        cmd = "config interface dhcp_relay del {} {}".format(interface_name, dhcp_servers_str)

    try:
        duthost.shell(cmd)
        logger.info("Removed dhcp_servers {} from {}".format(dhcp_servers, interface_name))
        return True
    except Exception as e:
        logger.warning("Failed to remove dhcp_servers from {}: {}".format(interface_name, str(e)))
        return False


def calculate_uplink_interfaces_and_port_indices(mg_facts):
    """
    Calculate uplink interfaces and their PTF port indices from minigraph facts.

    Uplink interfaces are those connected to LeafRouter, MgmtLeafRouter, or BackEndLeafRouter.
    If an uplink's physical interface is a member of a PortChannel, the PortChannel name is used.

    Args:
        mg_facts: Minigraph facts dictionary containing neighbor and portchannel information

    Returns:
        tuple: (uplink_interfaces, uplink_port_indices)
            - uplink_interfaces: List of interface names (PortChannel or physical interface)
            - uplink_port_indices: List of PTF port indices for the physical uplink interfaces
    """
    uplink_interfaces = []
    uplink_port_indices = []

    for iface_name, neighbor_info_dict in list(mg_facts['minigraph_neighbors'].items()):
        if neighbor_info_dict['name'] in mg_facts['minigraph_devices']:
            neighbor_device_info_dict = mg_facts['minigraph_devices'][neighbor_info_dict['name']]
            if 'type' in neighbor_device_info_dict and neighbor_device_info_dict['type'] in \
                    ['LeafRouter', 'MgmtLeafRouter', 'BackEndLeafRouter']:
                # If this uplink's physical interface is a member of a portchannel interface,
                # we record the name of the portchannel interface here, as this is the actual
                # interface the DHCP relay will listen on.
                iface_is_portchannel_member = False
                for portchannel_name, portchannel_info_dict in list(mg_facts['minigraph_portchannels'].items()):
                    if 'members' in portchannel_info_dict and iface_name in portchannel_info_dict['members']:
                        iface_is_portchannel_member = True
                        if portchannel_name not in uplink_interfaces:
                            uplink_interfaces.append(portchannel_name)
                        break
                if not iface_is_portchannel_member:
                    uplink_interfaces.append(iface_name)
                uplink_port_indices.append(mg_facts['minigraph_ptf_indices'][iface_name])

    return uplink_interfaces, uplink_port_indices


def extract_interface_from_vlan(duthost, interface_name, vlan_name, ip_address):
    """Extract an interface from a VLAN and configure it as a routed interface.

    Args:
        duthost: DUT host object
        interface_name: Interface to extract (e.g., 'Ethernet40')
        vlan_name: VLAN to extract from (e.g., 'Vlan1000')
        ip_address: IP address to assign (e.g., '10.0.0.1/31')

    Returns:
        True if successful, False otherwise
    """
    try:
        vlan_id = vlan_name.replace('Vlan', '')

        logger.info("Removing {} from {}".format(interface_name, vlan_name))
        duthost.shell("config vlan member del {} {}".format(vlan_id, interface_name))

        def _vlan_member_removed():
            try:
                result = duthost.shell('sonic-db-cli CONFIG_DB keys "VLAN_MEMBER|{}|{}"'.format(vlan_name,
                                                                                                interface_name))
                return vlan_name not in result['stdout']
            except Exception as e:
                logger.debug("Exception checking VLAN member removal: {}".format(e))
                return False

        py_assert(
            wait_until(10, 1, 0, _vlan_member_removed),
            "Interface {} was not removed from {} in CONFIG_DB within timeout".format(interface_name, vlan_name)
        )

        logger.info("Adding IP address {} to {}".format(ip_address, interface_name))
        duthost.shell("config interface ip add {} {}".format(interface_name, ip_address))

        def _ip_address_added():
            try:
                result = duthost.shell('sonic-db-cli CONFIG_DB hgetall "INTERFACE|{}|{}"'.format(interface_name,
                                                                                                 ip_address))
                return len(result['stdout'].strip()) > 0
            except Exception as e:
                logger.debug("Exception checking IP address addition: {}".format(e))
                return False

        py_assert(
            wait_until(10, 1, 0, _ip_address_added),
            "IP address {} was not added to {} in CONFIG_DB within timeout".format(ip_address, interface_name)
        )
        logger.info("Verified IP address {} added to {} in CONFIG_DB".format(ip_address, interface_name))

        logger.info("Bringing up interface {}".format(interface_name))
        duthost.shell("config interface startup {}".format(interface_name))

        def _interface_is_up():
            try:
                result = duthost.shell("show ip interfaces | grep -w {}".format(interface_name),
                                       module_ignore_errors=True)
                if result['rc'] != 0:
                    logger.debug("Interface {} not found in 'show ip interfaces'".format(interface_name))
                    return False

                return ip_address.split('/')[0] in result['stdout']
            except Exception as e:
                logger.debug("Exception checking interface operational status: {}".format(e))
                return False

        py_assert(
            wait_until(30, 2, 0, _interface_is_up),
            "Interface {} did not come up with IP {} within timeout".format(interface_name, ip_address)
        )

        logger.info("Successfully extracted {} from {} and configured as routed interface".format(
            interface_name, vlan_name))
        return True
    except Exception as e:
        logger.error("Failed to extract {} from {}: {}".format(interface_name, vlan_name, str(e)))
        return False


def restore_interface_to_vlan(duthost, interface_name, vlan_name, ip_address, tagging_mode='untagged'):
    """Restore an interface back to a VLAN.

    Args:
        duthost: DUT host object
        interface_name: Interface to restore (e.g., 'Ethernet40')
        vlan_name: VLAN to restore to (e.g., 'Vlan1000')
        ip_address: IP address to remove (e.g., '10.0.0.1/31')
        tagging_mode: Tagging mode to restore ('untagged' or 'tagged', default: 'untagged')

    Returns:
        True if successful, False otherwise
    """
    try:
        vlan_id = vlan_name.replace('Vlan', '')

        logger.info("Removing IP address {} from {}".format(ip_address, interface_name))
        duthost.shell("config interface ip remove {} {}".format(interface_name, ip_address))

        tagging_flag = '-u' if tagging_mode == 'untagged' else ''
        logger.info("Adding {} back to {} (tagging_mode: {})".format(interface_name, vlan_name, tagging_mode))
        cmd = "config vlan member add {} {} {}".format(tagging_flag, vlan_id, interface_name).strip()
        duthost.shell(cmd)

        logger.info("Successfully restored {} to {}".format(interface_name, vlan_name))
        return True
    except Exception as e:
        logger.error("Failed to restore {} to {}: {}".format(
            interface_name, vlan_name, str(e)))
        return False


def wait_for_dhcp_relay_ready_on_interface(duthost, interface_name, timeout=60):
    """Wait for dhcp_relay to be ready and listening on the specified interface.

    Args:
        duthost: DUT host object
        interface_name: Name of the interface (e.g., 'Ethernet104' or 'Vlan1000')
        timeout: Maximum time to wait in seconds (default: 60)

    Returns:
        True if dhcp_relay is ready, False otherwise
    """
    def _is_dhcp_relay_listening_on_interface():
        # Check if dhcrelay process is listening on port 67 on the interface
        output = duthost.shell("docker exec dhcp_relay ss -nlp | grep dhcrelay | grep '{}:67'".format(interface_name),
                               module_ignore_errors=True)
        return output['rc'] == 0 and interface_name in output['stdout']

    logger.info("Waiting for dhcp_relay to be ready on interface {}...".format(interface_name))
    result = wait_until(timeout, 2, 0, _is_dhcp_relay_listening_on_interface)

    if result:
        logger.info("dhcp_relay is ready and listening on {}".format(interface_name))
    else:
        logger.error("dhcp_relay failed to start listening on {} after {} seconds".format(interface_name, timeout))

    return result


def build_dhcp_relay_data_dict(duthost, tbinfo, mg_facts, config_facts, standby_duthost, downlink_iface_name,
                               downlink_addr, downlink_mask, dhcp_server_addrs):
    """
    Build a dhcp_relay_data dictionary for both VLAN and routed interfaces.

    Args:
        duthost: DUT host object
        tbinfo: Testbed information
        mg_facts: Minigraph facts dictionary
        standby_duthost: Standby DUT host object
        config_facts: Running config facts (optional, will fetch if not provided)
        downlink_iface_name: Name of the downlink interface (e.g., 'Vlan1000' or 'Ethernet0')
        downlink_addr: IPv4 address of the downlink interface
        downlink_mask: Netmask of the downlink interface
        dhcp_server_addrs: List of DHCP server addresses

    Returns:
        dict: dhcp_relay_data dictionary with all required fields
    """
    uplink_interfaces, uplink_port_indices = calculate_uplink_interfaces_and_port_indices(mg_facts)

    switch_loopback_ip = mg_facts['minigraph_lo_interfaces'][0]['addr']

    downlink_iface = {}
    downlink_iface['name'] = downlink_iface_name
    downlink_iface['addr'] = downlink_addr
    downlink_iface['mask'] = downlink_mask
    downlink_iface['dhcp_server_addrs'] = dhcp_server_addrs

    # Get MAC address for the interface
    res = duthost.shell('cat /sys/class/net/{}/address'.format(downlink_iface_name), module_ignore_errors=True)
    if res['rc'] == 0 and res['stdout'].strip():
        downlink_iface['mac'] = res['stdout'].strip()
    else:
        res = duthost.shell("ip link show {} | grep 'link/ether' | awk '{{print $2}}'".format(downlink_iface_name),
                            module_ignore_errors=True)
        if res['rc'] == 0 and res['stdout'].strip():
            downlink_iface['mac'] = res['stdout'].strip()
        elif not downlink_iface_name.startswith('Vlan'):
            logger.info("Using router MAC for routed interface {}".format(downlink_iface_name))
            downlink_iface['mac'] = duthost.facts["router_mac"]
        else:
            logger.warning("Cannot get MAC address for VLAN interface {} - skipping".format(downlink_iface_name))
            return None

    vlan_members = []
    if downlink_iface_name.startswith('Vlan'):
        vlan_member_table = config_facts.get('VLAN_MEMBER', {})
        if downlink_iface_name in vlan_member_table:
            # Filter out PortChannels, keep only Ethernet interfaces
            vlan_members = [port for port in vlan_member_table[downlink_iface_name].keys()
                            if 'PortChannel' not in port]

    if vlan_members:
        # VLAN case: find first member with alias
        client_port_name = None
        for port in vlan_members:
            if port in mg_facts['minigraph_port_name_to_alias_map']:
                client_port_name = port
                break
        if not client_port_name:
            raise ValueError("No valid client port found in VLAN members: {}".format(vlan_members))
    else:
        # Routed interface case: client port is the downlink interface itself
        client_port_name = downlink_iface_name

    client_iface = {}
    client_iface['name'] = client_port_name
    client_iface['alias'] = mg_facts['minigraph_port_name_to_alias_map'].get(client_port_name, client_port_name)
    client_iface['port_idx'] = mg_facts['minigraph_ptf_indices'][client_port_name]

    other_client_ports = []
    if vlan_members:
        client_port_idx = client_iface['port_idx']
        for iface_name in vlan_members:
            if mg_facts['minigraph_ptf_indices'][iface_name] != client_port_idx:
                other_client_ports.append(mg_facts['minigraph_ptf_indices'][iface_name])

    dhcp_relay_data = {}
    dhcp_relay_data['downlink_iface'] = downlink_iface
    dhcp_relay_data['client_iface'] = client_iface
    dhcp_relay_data['other_client_ports'] = other_client_ports
    dhcp_relay_data['uplink_interfaces'] = uplink_interfaces
    dhcp_relay_data['uplink_port_indices'] = uplink_port_indices
    dhcp_relay_data['switch_loopback_ip'] = str(switch_loopback_ip)
    dhcp_relay_data['portchannels'] = mg_facts['minigraph_portchannels']
    dhcp_relay_data['vlan_members'] = vlan_members

    # Add loopback interface name (needed for source_interface)
    loopback_iface = mg_facts['minigraph_lo_interfaces'][0]['name']
    dhcp_relay_data['loopback_iface'] = loopback_iface
    portchannels_with_ips = {}
    portchannels_ip_list = []

    for portchannel_name, portchannel_info in mg_facts['minigraph_portchannels'].items():
        for pc_interface in mg_facts['minigraph_portchannel_interfaces']:
            if pc_interface['attachto'] == portchannel_name:
                ip_with_mask = f"{pc_interface['addr']}/{pc_interface['mask']}"

                # Optional: format to standard CIDR
                # formatted_ip = str(ipaddress.ip_interface(ip_with_mask))
                ip_obj = ipaddress.ip_interface(ip_with_mask)
                # Skip IPv6 if needed
                if ip_obj.version != 4:
                    continue
                hosts = list(ip_obj.network.hosts())
                if len(hosts) < 2:
                    logger.warning(f"Not enough hosts for nexthop in {ip_with_mask}")
                    continue

                nexthop = str(hosts[1]) if str(ip_obj.ip) == str(hosts[0]) else str(hosts[0])
                if portchannel_name not in portchannels_with_ips:
                    portchannels_with_ips[portchannel_name] = []
                # Save as flat dictionary
                portchannels_with_ips[portchannel_name] = {
                    "ip": str(ip_obj),
                    "nexthop": nexthop
                }
                # Append the IP to the list
                portchannels_ip_list.append(str(ip_obj))

    dhcp_relay_data['portchannels_with_ips'] = portchannels_with_ips
    dhcp_relay_data['portchannels_ip_list'] = portchannels_ip_list

    # Obtain MAC address of an uplink interface because vlan mac may be different than that of physical interfaces
    res = duthost.shell('cat /sys/class/net/{}/address'.format(uplink_interfaces[0]))
    dhcp_relay_data['uplink_mac'] = res['stdout']
    if standby_duthost:
        res = standby_duthost.shell('cat /sys/class/net/{}/address'.format(uplink_interfaces[0]))
        dhcp_relay_data['standby_uplink_mac'] = res['stdout']
        dhcp_relay_data['standby_dut_lo_addr'] = \
            mg_facts["minigraph_devices"][standby_duthost.sonichost.hostname]['lo_addr']
        standby_mg_facts = standby_duthost.get_extended_minigraph_facts(tbinfo)
        standby_uplink_interfaces, standby_uplink_port_indices = \
            calculate_uplink_interfaces_and_port_indices(standby_mg_facts)
        dhcp_relay_data['standby_uplink_port_indices'] = standby_uplink_port_indices

    dhcp_relay_data['default_gw_ip'] = mg_facts['minigraph_mgmt_interface']['gwaddr']

    return dhcp_relay_data


def pytest_addoption(parser):
    """
    Adds options to pytest that are used by the COPP tests.
    """
    parser.addoption(
        "--stress_restart_round",
        action="store",
        type=int,
        default=10,
        help="Set custom restart rounds",
    )
    parser.addoption(
        "--stress_restart_duration",
        action="store",
        type=int,
        default=90,
        help="Set custom restart rounds",
    )
    parser.addoption(
        "--stress_restart_pps",
        action="store",
        type=int,
        default=100,
        help="Set custom restart rounds",
    )
    parser.addoption(
        "--max_packets_per_sec",
        action="store",
        type=int,
        help="Set maximum packets per second for stress test",
    )
    parser.addoption(
        "--enable_dhcp_relay_feature",
        action="store_true",
        default=False,
        help="Enable dhcp_relay feature on DUT"
    )


@pytest.fixture(scope="module", autouse=True)
def check_dhcp_feature_status(request, duthost):
    feature_status_output = duthost.show_and_parse("show feature status")
    for feature in feature_status_output:
        if feature["feature"] == "dhcp_relay" and feature["state"] != "enabled":
            # Enable dhcp_relay feature on DUT if enable_dhcp_relay_feature argument was passed
            if request.config.getoption("--enable_dhcp_relay_feature"):
                duthost.shell("sudo config feature state dhcp_relay enabled")
                time.sleep(2)
                yield
                duthost.shell("sudo config feature state dhcp_relay disabled")
                return
            else:
                pytest.skip("dhcp_relay is not enabled")
    yield


@pytest.fixture(scope="module")
def setup_routed_dhcp_servers(duthosts, rand_one_dut_hostname, tbinfo):
    """
    Setup fixture that ensures at least some routed interfaces have dhcp_servers configured.
    This enables testing of DHCP relay on routed interfaces.

    This fixture will:
    1. Take a config checkpoint (for guaranteed rollback)
    2. Extract a VLAN member to create a routed interface
    3. Configure dhcp_servers on the routed interface
    4. Save config (so tests can do 'config reload' and get our changes)
    5. Yield for tests to run
    6. Restore original configuration via checkpoint rollback
    """
    duthost = duthosts[rand_one_dut_hostname]
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    dhcp_servers = mg_facts.get('dhcp_servers', [])
    if not dhcp_servers:
        pytest.skip("No dhcp_servers found in minigraph - DHCP relay tests require dhcp_servers configuration")

    checkpoint_name = "dhcp_relay_test_checkpoint"
    logger.info("Creating config checkpoint: {}".format(checkpoint_name))
    duthost.shell("sudo config checkpoint {}".format(checkpoint_name))

    try:
        logger.info("Attempting to extract VLAN member for routed interface testing")
        vlan_member_info = find_vlan_member_for_extraction(duthost, tbinfo, require_ptf_index=True)

        if not vlan_member_info:
            logger.info("No suitable VLAN member found for extraction - routed interface tests will be skipped")
            yield
            return

        candidate_interface = vlan_member_info['interface']
        extracted_from_vlan = vlan_member_info['vlan']
        original_tagging_mode = vlan_member_info['tagging_mode']
        test_ip_address = "100.255.255.0/31"

        logger.info("Extracting {} from {} for DHCP relay testing".format(
            candidate_interface, extracted_from_vlan))
        if not extract_interface_from_vlan(duthost, candidate_interface,
                                           extracted_from_vlan, test_ip_address):
            logger.error("Failed to extract {} from {}".format(
                candidate_interface, extracted_from_vlan))
            yield
            return

        logger.info("Configuring dhcp_servers on routed interface: {}".format(candidate_interface))
        if not configure_dhcp_servers_on_interface(duthost, candidate_interface, dhcp_servers):
            logger.error("Failed to configure dhcp_servers on {} - skipping routed interface tests".format(
                candidate_interface))
            yield
            return

        if not wait_for_dhcp_relay_ready_on_interface(duthost, candidate_interface, timeout=60):
            logger.error("dhcp_relay failed to start on {} - skipping routed interface tests".format(
                candidate_interface))
            yield
            return

        # Save config so that 'config reload' during tests will pick up our changes
        logger.info("Saving config with routed interface configuration")
        duthost.shell("sudo config save -y")

        yield

        logger.info("Attempting manual cleanup before rollback")
        cleanup_success = remove_dhcp_servers_from_interface(duthost, candidate_interface, dhcp_servers)
        if not cleanup_success:
            logger.warning("Failed to remove dhcp_servers from {} - will rely on rollback".format(candidate_interface))

        if extracted_from_vlan is not None:
            logger.info("Restoring {} to {}".format(candidate_interface, extracted_from_vlan))
            restore_success = restore_interface_to_vlan(duthost, candidate_interface,
                                                        extracted_from_vlan, test_ip_address,
                                                        original_tagging_mode)
            if not restore_success:
                logger.warning("Failed to restore {} to {} - will rely on rollback".format(
                    candidate_interface, extracted_from_vlan))

    finally:
        logger.info("Rolling back to checkpoint: {}".format(checkpoint_name))
        result = duthost.shell("sudo config rollback {}".format(checkpoint_name), module_ignore_errors=True)
        logger.info("Rollback result: rc={}, stdout={}, stderr={}".format(
            result.get('rc'), result.get('stdout'), result.get('stderr')))

        # Save the restored config so pytest's config_reload gets the original config
        logger.info("Saving restored config to file")
        result = duthost.shell("sudo config save -y", module_ignore_errors=True)
        logger.info("Config save result: rc={}, stdout={}, stderr={}".format(
            result.get('rc'), result.get('stdout'), result.get('stderr')))

        logger.info("Deleting checkpoint: {}".format(checkpoint_name))
        duthost.shell("sudo config checkpoint delete {}".format(checkpoint_name), module_ignore_errors=True)


@pytest.fixture(scope="module")
def dut_dhcp_relay_data(duthosts, rand_one_dut_hostname, ptfhost, tbinfo, setup_routed_dhcp_servers):
    """ Fixture which returns a dictionary keyed by interface type ('vlan', 'routed')
        where each value is a list of dictionaries containing data necessary to test
        DHCP relay agents running on the DuT for that interface type.
        This fixture is scoped to the module, as the data it gathers can be used by
        all tests in this module. It does not need to be run before each test.

        Returns:
            Dict with keys 'vlan' and 'routed', each containing a list of interface dicts:
            {
                'vlan': [<dhcp_relay_data>, <dhcp_relay_data>, ...],
                'routed': [<dhcp_relay_data>, <dhcp_relay_data>, ...]
            }
    """
    duthost = duthosts[rand_one_dut_hostname]
    dhcp_relay_data_dict = {'vlan': [], 'routed': []}

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    config_facts = duthost.get_running_config_facts()
    standby_duthost = None
    if 'dualtor' in tbinfo['topo']['name']:
        standby_duthost = [duthost for duthost in duthosts if duthost != duthosts[rand_one_dut_hostname]][0]

    # SONiC spawns one DHCP relay agent per VLAN interface configured on the DUT
    vlan_dict = mg_facts['minigraph_vlans']
    for vlan_iface_name, vlan_info_dict in list(vlan_dict.items()):
        vlan_addr = None
        vlan_mask = None
        for vlan_interface_info_dict in mg_facts['minigraph_vlan_interfaces']:
            if vlan_interface_info_dict['attachto'] == vlan_iface_name:
                vlan_addr = vlan_interface_info_dict['addr']
                vlan_mask = vlan_interface_info_dict['mask']
                break

        if not vlan_addr:
            continue  # Skip VLANs without IP configuration

        dhcp_relay_data = build_dhcp_relay_data_dict(
            duthost=duthost,
            tbinfo=tbinfo,
            mg_facts=mg_facts,
            standby_duthost=standby_duthost,
            config_facts=config_facts,
            downlink_iface_name=vlan_iface_name,
            downlink_addr=vlan_addr,
            downlink_mask=vlan_mask,
            dhcp_server_addrs=mg_facts['dhcp_servers']
        )

        if dhcp_relay_data:
            dhcp_relay_data_dict['vlan'].append(dhcp_relay_data)

    port_table = config_facts.get('PORT', {})
    interface_table = config_facts.get('INTERFACE', {})

    for port_name, port_config in port_table.items():
        dhcp_servers = port_config.get('dhcp_servers', [])
        if not dhcp_servers:
            continue

        port_ipv4_addr = None
        port_ipv4_mask = None
        if port_name in interface_table:
            for ip_prefix in interface_table[port_name].keys():
                try:
                    ip_obj = ipaddress.ip_interface(ip_prefix)
                    if ip_obj.version == 4:
                        port_ipv4_addr = str(ip_obj.ip)
                        port_ipv4_mask = str(ip_obj.netmask)
                        break
                except ValueError:
                    continue

        if not port_ipv4_addr:
            continue  # Skip ports without IPv4 addresses

        if port_name not in mg_facts.get('minigraph_ptf_indices', {}):
            continue

        dhcp_relay_data = build_dhcp_relay_data_dict(
            duthost=duthost,
            tbinfo=tbinfo,
            mg_facts=mg_facts,
            standby_duthost=standby_duthost,
            config_facts=config_facts,
            downlink_iface_name=port_name,
            downlink_addr=port_ipv4_addr,
            downlink_mask=port_ipv4_mask,
            dhcp_server_addrs=dhcp_servers
        )

        if dhcp_relay_data:
            dhcp_relay_data_dict['routed'].append(dhcp_relay_data)

    return dhcp_relay_data_dict


@pytest.fixture(scope="module")
def one_interface_per_type(dut_dhcp_relay_data):
    """Fixture that returns one VLAN and one routed interface for testing.

    This fixture is useful for tests that are heavyweight and should only run once
    per interface type instead of for every interface. For example, tests that
    restart services, perform stress testing, or have long execution times.

    Args:
        dut_dhcp_relay_data: List of DHCP relay data dictionaries

    Returns:
        Dict with keys 'vlan' and 'routed', each containing an interface dict or None:
        {
            'vlan': {
                'downlink_iface': {'name': 'Vlan1000', ...},
                'client_iface': {...},
                'uplink_interfaces': [...],
                ...
            } or None,
            'routed': {
                'downlink_iface': {'name': 'Ethernet232', ...},
                'client_iface': {...},
                'uplink_interfaces': [...],
                ...
            } or None
        }
    """
    result = {'vlan': None, 'routed': None}

    for intf_type in result.keys():
        if len(dut_dhcp_relay_data[intf_type]) > 0:
            result[intf_type] = dut_dhcp_relay_data[intf_type][0]

    return result


@pytest.fixture(scope="module")
def validate_dut_routes_exist(duthosts, rand_one_dut_hostname, dut_dhcp_relay_data):
    """Fixture to valid a route to each DHCP server exist
    """
    # Flatten the dict to a list for the check_routes_to_dhcp_server function
    all_interfaces = dut_dhcp_relay_data.get('vlan', []) + dut_dhcp_relay_data.get('routed', [])
    py_assert(wait_until(360, 5, 0, check_routes_to_dhcp_server, duthosts[rand_one_dut_hostname],
                         all_interfaces),
              "Packets relayed to DHCP server should go through default route via upstream neighbor, but now it's" +
              " going through mgmt interface, which means device is in an unhealthy status")


@pytest.fixture(scope="module")
def testing_config(duthosts, rand_one_dut_hostname, tbinfo):
    duthost = duthosts[rand_one_dut_hostname]

    if 'dualtor' in tbinfo['topo']['name']:
        yield DUAL_TOR_MODE, duthost
    else:
        yield SINGLE_TOR_MODE, duthost


@pytest.fixture(scope="function")
def clean_processes_after_stress_test(ptfhost):
    """Clean up stress test processes after each test.

    This prevents packet capture contamination between sequential test runs.
    """

    yield

    ptfhost.shell("kill -9 $(ps aux | grep  dhcp_relay_stress_test | grep -v 'grep' | awk '{print $2}')",
                  module_ignore_errors=True)

    def _no_stress_test_processes():
        result = ptfhost.shell("ps aux | grep dhcp_relay_stress_test | grep -v grep | wc -l",
                               module_ignore_errors=True)
        return result['stdout'].strip() == '0'

    wait_until(10, 1, 0, _no_stress_test_processes)
