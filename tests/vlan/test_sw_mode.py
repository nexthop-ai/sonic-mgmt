import logging
import pytest
from tests.common.helpers.assertions import pytest_assert

pytestmark = [
    pytest.mark.topology('t0')
]

logger = logging.getLogger(__name__)


def skip_unless_mode_derived(duthost):
    """Skip unless the image derives the switchport mode from VLAN membership."""
    result = duthost.shell("show interfaces switchport status", module_ignore_errors=True)
    if result['rc'] != 0:
        pytest.skip("'show interfaces switchport status' is not supported on this image")
    result = duthost.shell("config switchport mode --help", module_ignore_errors=True)
    if result['rc'] == 0:
        pytest.skip("switchport mode is configured, not derived, on this image")


def get_switchport_mode(dut, interface):
    """
    Get switchport mode from 'show interfaces switchport status' output
    Returns the mode or None if interface not found
    """
    for row in dut.show_and_parse("show interfaces switchport status"):
        if row.get('interface') == interface:
            return row.get('mode')
    return None


def setup_portchannel(dut, portchannel_name, member_ports):
    """
    Create PortChannel and add member ports
    Returns: True if successful, False otherwise
    """
    try:
        # Create PortChannel
        dut.shell(f"config portchannel add {portchannel_name}")

        # Add member ports
        for port in member_ports:
            dut.shell(f"config portchannel member add {portchannel_name} {port}")

        return True
    except Exception as e:
        logger.error(f"Failed to setup PortChannel {portchannel_name}: {str(e)}")
        return False


def setup_vlan_and_members(dut, vlan_id, members, tagged):
    """
    Create VLAN and add members, tagged or untagged.
    Returns: True if successful, False otherwise
    """
    try:
        dut.shell(f"config vlan add {vlan_id}")

        untagged = "" if tagged else "-u "
        for member in members:
            dut.shell(f"config vlan member add {untagged}{vlan_id} {member}")

        return True
    except Exception as e:
        logger.error(f"Failed to setup VLAN {vlan_id}: {str(e)}")
        return False


def cleanup_portchannel(dut, portchannel_name, member_ports):
    """Cleanup PortChannel configuration.

    Member removals are isolated so that a single failure does not abort the
    cleanup and leak an orphan PortChannel for subsequent runs.
    """
    # Remove member ports, swallowing per-member errors so we always attempt
    # to delete the PortChannel itself afterwards.
    for port in member_ports:
        try:
            dut.shell(f"config portchannel member del {portchannel_name} {port}")
        except Exception as e:
            logger.error(f"Failed to remove {port} from {portchannel_name}: {str(e)}")

    try:
        dut.shell(f"config portchannel del {portchannel_name}")
    except Exception as e:
        logger.error(f"Failed to delete PortChannel {portchannel_name}: {str(e)}")


def cleanup_vlan(dut, vlan_id, members):
    """Cleanup VLAN configuration"""
    for member in members:
        try:
            dut.shell(f"config vlan member del {vlan_id} {member}")
        except Exception as e:
            logger.error(f"Failed to remove {member} from Vlan{vlan_id}: {str(e)}")
    try:
        dut.shell(f"config vlan del {vlan_id}")
    except Exception as e:
        logger.error(f"Failed to delete Vlan{vlan_id}: {str(e)}")


def get_available_ports(duthost, tbinfo, num_of_ports=1):
    """Find num_of_ports available i.e not part of any vlan or portchannel.
    When num_of_ports > 1, returns ports with the same speed so they can be
    added to a PortChannel together.
    """
    available_ports = []
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    intfList = mg_facts['minigraph_port_name_to_alias_map'].keys()

    vlanDict = mg_facts['minigraph_vlans']

    poDict = mg_facts['minigraph_portchannels']

    for intf in intfList:
        in_vlan = any(intf in vlanData['members'] for vlanData in vlanDict.values())
        in_portchannel = any(intf in poData['members'] for poData in poDict.values())
        if not in_vlan and not in_portchannel:
            available_ports.append(intf)

    if num_of_ports <= 1:
        return available_ports[:num_of_ports]

    intf_status = {x.get('interface'): x for x in duthost.show_and_parse('show interfaces status')}

    speed_groups = {}
    for port in available_ports:
        if port in intf_status:
            speed_groups.setdefault(intf_status[port].get('speed'), []).append(port)
    if not speed_groups:
        return []

    ports = max(speed_groups.values(), key=len)
    if len(ports) < num_of_ports:
        logger.warning(f"Could not find {num_of_ports} available ports with the same speed")
    return ports[:num_of_ports]


def get_free_lag_intf(duthost):
    """Create a portchannel interface from available idx"""
    portchannels = list(duthost.config_facts(
        host=duthost.hostname, source="running")['ansible_facts'].get('PORTCHANNEL', {}).keys())

    for portchannel_idx in range(1, 10000):  # Max len of portchannel index can be '9999'
        lag_port = 'PortChannel{}'.format(portchannel_idx)

        if lag_port not in portchannels:
            return lag_port

    return None


def verify_switchport_mode(duthost, intf, expected_mode):
    """Verify expected_mode is reported by both 'show interfaces switchport
    status' and the VLAN column of 'show interfaces status'.
    """
    intf_mode = get_switchport_mode(duthost, intf)
    pytest_assert(intf_mode == expected_mode,
                  f"'show interfaces switchport status' reports {intf} as {intf_mode}, not {expected_mode}")

    out = duthost.show_and_parse("show interfaces status {}".format(intf))
    vlan_column = out[0].get('vlan') if out else None
    pytest_assert(vlan_column == expected_mode,
                  f"'show interfaces status' VLAN column reports {intf} as {vlan_column}, not {expected_mode}")


def get_running_config(duthost):
    return duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']


def pick_free_vlan_ids(duthost, count):
    """Return count VLAN ids that are not configured on the DUT."""
    existing = set(get_running_config(duthost).get('VLAN', {}))
    free = [vlan_id for vlan_id in TEST_VLAN_ID_RANGE if f"Vlan{vlan_id}" not in existing]
    pytest_assert(len(free) >= count, f"Fewer than {count} free VLAN ids in {TEST_VLAN_ID_RANGE}")
    return free[:count]


def configure_and_verify_switchport_mode(duthost, intf, memberships, expected_mode):
    """Give intf the VLAN memberships it is parametrized with, then verify the
    mode derived from them.
    """
    for vlan_id, tagged in memberships:
        pytest_assert(setup_vlan_and_members(duthost, vlan_id, [intf], tagged),
                      f"Failed to setup VLAN {vlan_id}")
    verify_switchport_mode(duthost, intf, expected_mode)


def derived_mode(tagged):
    """The mode of a port whose VLAN memberships are tagged as listed."""
    if not tagged:
        return "routed"
    return "access" if tagged == [False] else "trunk"


def remove_and_verify_switchport_mode(duthost, intf, memberships):
    """Remove intf's memberships last first, verifying the mode derived from
    those left after each. Removed memberships are dropped from the list.
    """
    while memberships:
        vlan_id, _ = memberships.pop()
        duthost.shell(f"config vlan member del {vlan_id} {intf}")
        verify_switchport_mode(duthost, intf, derived_mode([tagged for _, tagged in memberships]))


def get_running_l2_l3_state(duthost, intf):
    """Return (VLANs intf is a member of, whether intf is a router interface)."""
    cfg = get_running_config(duthost)
    vlans = [vlan for vlan, members in cfg.get('VLAN_MEMBER', {}).items() if intf in members]
    table = 'PORTCHANNEL_INTERFACE' if intf.startswith('PortChannel') else 'INTERFACE'
    # Any row makes a router interface, including one with no address.
    return vlans, intf in cfg.get(table, {})


TEST_VLAN_ID_RANGE = range(2601, 2700)
L2_L3_TEST_PREFIX = "192.0.2.0/31"


def verify_router_interface_is_not_vlan_member(duthost, intf, vlan_id, first):
    """Configure intf as a router interface and as a member of vlan_id, in the
    order given by first, and verify the second is refused and the first stays.
    """
    ip_prefix = L2_L3_TEST_PREFIX
    logger.info(f"Testing {intf} as a router interface and a Vlan{vlan_id} member, {first} first")
    duthost.shell(f"config vlan add {vlan_id}")

    if first == "router_interface":
        duthost.shell(f"config interface ip add {intf} {ip_prefix}")
        result = duthost.shell(f"config vlan member add -u {vlan_id} {intf}", module_ignore_errors=True)
        pytest_assert(result['rc'] != 0 and "is a router interface" in result['stdout'] + result['stderr'],
                      f"Adding router interface {intf} to Vlan{vlan_id} was not refused: {result}")
        expected_mode = "routed"
    else:
        duthost.shell(f"config vlan member add -u {vlan_id} {intf}")
        result = duthost.shell(f"config interface ip add {intf} {ip_prefix}", module_ignore_errors=True)
        # This refusal exits 0, so only the output shows it.
        pytest_assert("is a member of vlan" in result['stdout'] + result['stderr'],
                      f"Adding an address to Vlan{vlan_id} member {intf} was not refused: {result}")
        expected_mode = "access"

    vlans, is_router_interface = get_running_l2_l3_state(duthost, intf)
    pytest_assert(is_router_interface == (first == "router_interface"),
                  f"{intf} router interface state is {is_router_interface} after configuring {first} first")
    pytest_assert(bool(vlans) == (first == "vlan_member"),
                  f"{intf} is a member of {vlans} after configuring {first} first")
    verify_switchport_mode(duthost, intf, expected_mode)


def cleanup_router_interface_and_vlan(duthost, intf, vlan_id):
    try:
        vlans, is_router_interface = get_running_l2_l3_state(duthost, intf)
    except Exception as e:
        logger.error(f"Failed to read the running config for {intf}: {str(e)}")
        vlans, is_router_interface = [f"Vlan{vlan_id}"], True
    if is_router_interface:
        try:
            duthost.shell(f"config interface ip remove {intf} {L2_L3_TEST_PREFIX}")
        except Exception as e:
            logger.error(f"Failed to remove the address from {intf}: {str(e)}")
    cleanup_vlan(duthost, vlan_id, [intf] if f"Vlan{vlan_id}" in vlans else [])


# (expected mode, whether each VLAN membership is tagged)
SWITCHPORT_MODE_CASES = [
    pytest.param("access", [False], id="access"),
    pytest.param("trunk", [True], id="trunk-tagged"),
    pytest.param("trunk", [False, True], id="trunk-native-and-tagged"),
    pytest.param("routed", [], id="routed"),
]


@pytest.mark.parametrize("mode, tagged", SWITCHPORT_MODE_CASES)
def test_ethernet_switchport_mode(duthosts, rand_one_dut_hostname, tbinfo, mode, tagged):
    """
    Test the switchport mode derived for Ethernet Interfaces
    """
    duthost = duthosts[rand_one_dut_hostname]
    skip_unless_mode_derived(duthost)

    # Fetch ports which are not part of vlan or portchannel
    intfList = get_available_ports(duthost, tbinfo)
    pytest_assert(len(intfList) != 0, "There are no available ports")

    intf = intfList[0]
    vlan_ids = pick_free_vlan_ids(duthost, len(tagged))
    memberships = list(zip(vlan_ids, tagged))

    logger.info(f"Testing {intf} with mode {mode}")

    try:
        configure_and_verify_switchport_mode(duthost, intf, memberships, mode)
        remove_and_verify_switchport_mode(duthost, intf, memberships)
    finally:
        left = {vlan_id for vlan_id, _ in memberships}
        for vlan_id in vlan_ids:
            cleanup_vlan(duthost, vlan_id, [intf] if vlan_id in left else [])


@pytest.mark.parametrize("mode, tagged", SWITCHPORT_MODE_CASES)
def test_portchannel_switchport_mode(duthosts, rand_one_dut_hostname, tbinfo, mode, tagged):
    """
    Test the switchport mode derived for PortChannels
    """
    duthost = duthosts[rand_one_dut_hostname]
    skip_unless_mode_derived(duthost)

    # Fetch free portchannel index
    portchannel = get_free_lag_intf(duthost)
    pytest_assert(portchannel is not None, "Free portchannel idx is NOT found!!")

    # Fetch ports which are not part of vlan or portchannel
    num_of_members = 2
    members = get_available_ports(duthost, tbinfo, num_of_members)
    pytest_assert(len(members) == num_of_members,
                  f"There are no available ports, requested:{num_of_members}, available:{len(members)}")
    vlan_ids = pick_free_vlan_ids(duthost, len(tagged))
    memberships = list(zip(vlan_ids, tagged))

    logger.info(f"Testing {portchannel} with mode {mode}")

    try:
        # Setup PortChannel
        pytest_assert(setup_portchannel(duthost, portchannel, members),
                      f"Failed to setup {portchannel}")

        configure_and_verify_switchport_mode(duthost, portchannel, memberships, mode)
        remove_and_verify_switchport_mode(duthost, portchannel, memberships)
    finally:
        left = {vlan_id for vlan_id, _ in memberships}
        for vlan_id in vlan_ids:
            cleanup_vlan(duthost, vlan_id, [portchannel] if vlan_id in left else [])
        cleanup_portchannel(duthost, portchannel, members)


@pytest.mark.parametrize("first", ["router_interface", "vlan_member"])
def test_ethernet_router_interface_is_not_vlan_member(duthosts, rand_one_dut_hostname, tbinfo, first):
    """
    Test that an Ethernet Interface cannot be both a router interface and a VLAN member
    """
    duthost = duthosts[rand_one_dut_hostname]
    skip_unless_mode_derived(duthost)

    intfList = get_available_ports(duthost, tbinfo)
    pytest_assert(len(intfList) != 0, "There are no available ports")
    intf = intfList[0]
    vlan_id = pick_free_vlan_ids(duthost, 1)[0]

    try:
        verify_router_interface_is_not_vlan_member(duthost, intf, vlan_id, first)
    finally:
        cleanup_router_interface_and_vlan(duthost, intf, vlan_id)


@pytest.mark.parametrize("first", ["router_interface", "vlan_member"])
def test_portchannel_router_interface_is_not_vlan_member(duthosts, rand_one_dut_hostname, tbinfo, first):
    """
    Test that a PortChannel cannot be both a router interface and a VLAN member
    """
    duthost = duthosts[rand_one_dut_hostname]
    skip_unless_mode_derived(duthost)

    portchannel = get_free_lag_intf(duthost)
    pytest_assert(portchannel is not None, "Free portchannel idx is NOT found!!")

    num_of_members = 2
    members = get_available_ports(duthost, tbinfo, num_of_members)
    pytest_assert(len(members) == num_of_members,
                  f"There are no available ports, requested:{num_of_members}, available:{len(members)}")
    vlan_id = pick_free_vlan_ids(duthost, 1)[0]

    try:
        pytest_assert(setup_portchannel(duthost, portchannel, members),
                      f"Failed to setup {portchannel}")
        verify_router_interface_is_not_vlan_member(duthost, portchannel, vlan_id, first)
    finally:
        cleanup_router_interface_and_vlan(duthost, portchannel, vlan_id)
        cleanup_portchannel(duthost, portchannel, members)
