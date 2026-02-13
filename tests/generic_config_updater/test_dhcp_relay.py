import logging
import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.fixtures.duthost_utils import utils_vlan_intfs_dict_orig,\
    utils_vlan_intfs_dict_add, utils_create_test_vlans      # noqa: F401
from tests.common.gu_utils import (
        apply_patch,
        expect_op_success, expect_op_failure,
        generate_tmpfile, delete_tmpfile,
        format_json_patch_for_multiasic,
        create_checkpoint, delete_checkpoint, rollback
)
from tests.dhcp_relay.conftest import \
        setup_routed_dhcp_servers, one_interface_per_type     # noqa: F401
from tests.dhcp_relay.dhcp_relay_utils import get_dhcrelay_process_cmdline


pytestmark = [
    pytest.mark.topology('t0', 'm0'),
]

logger = logging.getLogger(__name__)

DHCP_RELAY_TIMEOUT = 120
DHCP_RELAY_INTERVAL = 10
SETUP_ENV_CP = "test_setup_checkpoint"


def get_all_dhcp_relay_interfaces(dut_dhcp_relay_data):
    """Get all DHCP relay interfaces from both vlan and routed types.

    Args:
        dut_dhcp_relay_data: Fixture data containing vlan and routed interfaces

    Returns:
        List of dicts with 'type' and 'data' keys for each interface
    """
    all_interfaces = []
    for interface_type in ['vlan', 'routed']:
        interfaces = dut_dhcp_relay_data.get(interface_type, [])
        for iface in interfaces:
            all_interfaces.append({
                'type': interface_type,
                'data': iface
            })
    return all_interfaces


@pytest.fixture
def with_checkpoint(rand_selected_dut):
    """Create checkpoint before test, rollback after test.

    This fixture provides per-test checkpoint/rollback for GCU tests that modify config.
    """
    duthost = rand_selected_dut
    create_checkpoint(duthost, SETUP_ENV_CP)

    yield duthost

    try:
        output = rollback(duthost, SETUP_ENV_CP)
        pytest_assert(
            not output['rc'] and "Config rolled back successfull" in output['stdout'],
            "Rollback failed"
        )
    finally:
        delete_checkpoint(duthost, SETUP_ENV_CP)


def ensure_dhcp_server_up(duthost):
    """Wait till dhcp-relay server is setup

    Sample output
    admin@vlab-01:~$ docker exec dhcp_relay supervisorctl status | grep ^dhcp-relay
    dhcp-relay:isc-dhcpv4-relay-Vlan100    RUNNING   pid 72, uptime 0:00:09
    dhcp-relay:isc-dhcpv4-relay-Vlan1000   RUNNING   pid 73, uptime 0:00:09

    """
    def _dhcp_server_up():
        cmds = 'docker exec dhcp_relay supervisorctl status | grep ^dhcp-relay'
        output = duthost.shell(cmds)
        pytest_assert(
            not output['rc'],
            "'{}' is not running successfully".format(cmds)
        )

        return 'RUNNING' in output['stdout']

    pytest_assert(
        wait_until(DHCP_RELAY_TIMEOUT, DHCP_RELAY_INTERVAL, 0, _dhcp_server_up),
        "The dhcp relay server is not running"
    )


def validate_dhcrelay_process(duthost, interface_name, expected_content_list, unexpected_content_list):
    """Wait for dhcrelay process to update, then verify expected/unexpected content.

    Args:
        duthost: DUT host object
        interface_name: Name of the interface (e.g., 'Vlan1000' or 'Ethernet232')
        expected_content_list: List of strings that should be in the command line
        unexpected_content_list: List of strings that should NOT be in the command line
    """
    ensure_dhcp_server_up(duthost)

    def _dhcp_relay_updated():
        cmd_line = get_dhcrelay_process_cmdline(duthost, interface_name)
        if not cmd_line:
            return False

        has_expected = all(content in cmd_line for content in expected_content_list)
        has_no_unexpected = all(content not in cmd_line for content in unexpected_content_list)
        return has_expected and has_no_unexpected

    pytest_assert(
        wait_until(DHCP_RELAY_TIMEOUT, DHCP_RELAY_INTERVAL, 0, _dhcp_relay_updated),
        "dhcrelay process for {} did not update correctly. Expected: {}, Unexpected: {}".format(
            interface_name, expected_content_list, unexpected_content_list)
    )


def test_dhcp_relay_remove_nonexistent_server(with_checkpoint, dut_dhcp_relay_data):    # noqa: F811
    """Test removing a non-existent dhcp_server from all interfaces (should fail).

    Verifies that GCU rejects attempts to remove a DHCP server at an invalid index.
    """
    duthost = with_checkpoint

    all_interfaces = get_all_dhcp_relay_interfaces(dut_dhcp_relay_data)
    if not all_interfaces:
        pytest.skip("No DHCP relay interfaces available")

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        # Test on each interface
        for iface_info in all_interfaces:
            interface_type = iface_info['type']
            iface = iface_info['data']

            interface_name = iface['downlink_iface']['name']  # e.g., "Vlan1000" or "Ethernet104"
            table_name = "VLAN" if interface_type == "vlan" else "PORT"

            num_servers = len(iface['downlink_iface']['dhcp_server_addrs'])

            logger.info("Testing invalid remove on interface {}".format(interface_name))

            # Try to remove server at invalid index (way beyond the array)
            dhcp_rm_nonexist_json = [
                {
                    "op": "remove",
                    "path": "/{}/{}/dhcp_servers/{}".format(table_name, interface_name, num_servers + 10)
                }]
            dhcp_rm_nonexist_json = format_json_patch_for_multiasic(
                duthost=duthost, json_data=dhcp_rm_nonexist_json)

            output = apply_patch(duthost, json_data=dhcp_rm_nonexist_json, dest_file=tmpfile)
            expect_op_failure(output)

    finally:
        delete_tmpfile(duthost, tmpfile)


def test_dhcp_relay_add_duplicate_server(with_checkpoint, dut_dhcp_relay_data):    # noqa: F811
    """Test adding a duplicate dhcp_server to all interfaces (should fail).

    Verifies that GCU rejects attempts to add a DHCP server that already exists.
    """
    duthost = with_checkpoint

    all_interfaces = get_all_dhcp_relay_interfaces(dut_dhcp_relay_data)
    if not all_interfaces:
        pytest.skip("No DHCP relay interfaces available")

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        # Test on each interface
        for iface_info in all_interfaces:
            interface_type = iface_info['type']
            iface = iface_info['data']

            interface_name = iface['downlink_iface']['name']  # e.g., "Vlan1000" or "Ethernet104"
            table_name = "VLAN" if interface_type == "vlan" else "PORT"

            existing_servers = iface['downlink_iface']['dhcp_server_addrs']
            if len(existing_servers) == 0:
                logger.info("Skipping {} - no DHCP servers configured".format(interface_name))
                continue

            existing_server = existing_servers[0]  # Get first existing server

            logger.info("Testing duplicate add on interface {}".format(interface_name))

            # Try to add the same server again
            dhcp_add_duplicate_json = [
                {
                    "op": "add",
                    "path": "/{}/{}/dhcp_servers/0".format(table_name, interface_name),
                    "value": existing_server
                }]
            dhcp_add_duplicate_json = format_json_patch_for_multiasic(
                duthost=duthost, json_data=dhcp_add_duplicate_json)

            output = apply_patch(duthost, json_data=dhcp_add_duplicate_json, dest_file=tmpfile)
            expect_op_failure(output)

    finally:
        delete_tmpfile(duthost, tmpfile)


def test_dhcp_relay_add_new_server(with_checkpoint, dut_dhcp_relay_data):    # noqa: F811
    """Test adding a new dhcp_server to all interfaces (should succeed).

    Verifies that GCU can successfully add a new DHCP server to both VLAN and routed interfaces.
    """
    duthost = with_checkpoint

    all_interfaces = get_all_dhcp_relay_interfaces(dut_dhcp_relay_data)
    if not all_interfaces:
        pytest.skip("No DHCP relay interfaces available")

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        num_interfaces = len(all_interfaces)
        # Process each interface
        for idx, iface_info in enumerate(all_interfaces):
            interface_type = iface_info['type']
            iface = iface_info['data']

            interface_name = iface['downlink_iface']['name']  # e.g., "Vlan1000" or "Ethernet104"
            table_name = "VLAN" if interface_type == "vlan" else "PORT"

            new_server = "192.0.0.99"
            num_servers = len(iface['downlink_iface']['dhcp_server_addrs'])

            logger.info("Adding server {} to interface {}".format(new_server, interface_name))

            # Add a new DHCP server
            dhcp_add_json = [
                {
                    "op": "add",
                    "path": "/{}/{}/dhcp_servers/{}".format(table_name, interface_name, num_servers),
                    "value": new_server
                }]
            dhcp_add_json = format_json_patch_for_multiasic(duthost=duthost, json_data=dhcp_add_json)

            output = apply_patch(duthost, json_data=dhcp_add_json, dest_file=tmpfile)
            expect_op_success(duthost, output)
            pytest_assert(
                duthost.is_service_fully_started('dhcp_relay'),
                "dhcp_relay service is not running"
            )
            if idx < num_interfaces - 1:
                # For all but the last interface, just validate immediately
                validate_dhcrelay_process(duthost, interface_name, [new_server], [])

        validate_dhcrelay_process(duthost, interface_name, [new_server], [])

    finally:
        delete_tmpfile(duthost, tmpfile)


def test_dhcp_relay_remove_existing_server(with_checkpoint, dut_dhcp_relay_data):    # noqa: F811
    """Test removing an existing dhcp_server from all interfaces (should succeed).

    Verifies that GCU can successfully remove a DHCP server from both VLAN and routed interfaces.
    """
    duthost = with_checkpoint

    all_interfaces = get_all_dhcp_relay_interfaces(dut_dhcp_relay_data)
    if not all_interfaces:
        pytest.skip("No DHCP relay interfaces available")

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        num_interfaces = len(all_interfaces)
        # Process each interface
        for idx, iface_info in enumerate(all_interfaces):
            interface_type = iface_info['type']
            iface = iface_info['data']

            interface_name = iface['downlink_iface']['name']  # e.g., "Vlan1000" or "Ethernet104"
            table_name = "VLAN" if interface_type == "vlan" else "PORT"

            existing_servers = iface['downlink_iface']['dhcp_server_addrs']
            num_servers = len(existing_servers)

            if num_servers == 0:
                logger.info("Skipping {} - no DHCP servers configured".format(interface_name))
                continue

            # Remove the last DHCP server
            server_to_remove = existing_servers[-1]
            logger.info("Removing server {} from interface {}".format(server_to_remove, interface_name))

            dhcp_rm_json = [
                {
                    "op": "remove",
                    "path": "/{}/{}/dhcp_servers/{}".format(table_name, interface_name, num_servers - 1)
                }]
            dhcp_rm_json = format_json_patch_for_multiasic(duthost=duthost, json_data=dhcp_rm_json)

            output = apply_patch(duthost, json_data=dhcp_rm_json, dest_file=tmpfile)
            expect_op_success(duthost, output)
            pytest_assert(
                duthost.is_service_fully_started('dhcp_relay'),
                "dhcp_relay service is not running"
            )
            if idx < num_interfaces - 1:
                # For all but the last interface, just validate immediately
                validate_dhcrelay_process(duthost, interface_name, [server_to_remove], [])

        validate_dhcrelay_process(duthost, interface_name, [], [server_to_remove])

    finally:
        delete_tmpfile(duthost, tmpfile)


def test_dhcp_relay_replace_server(with_checkpoint, dut_dhcp_relay_data):    # noqa: F811
    """Test replacing a dhcp_server on all interfaces (should succeed).

    Verifies that GCU can successfully replace a DHCP server on both VLAN and routed interfaces.
    """
    duthost = with_checkpoint

    all_interfaces = get_all_dhcp_relay_interfaces(dut_dhcp_relay_data)
    if not all_interfaces:
        pytest.skip("No DHCP relay interfaces available")

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        num_interfaces = len(all_interfaces)
        # Process each interface
        for idx, iface_info in enumerate(all_interfaces):
            interface_type = iface_info['type']
            iface = iface_info['data']

            interface_name = iface['downlink_iface']['name']  # e.g., "Vlan1000" or "Ethernet104"
            table_name = "VLAN" if interface_type == "vlan" else "PORT"

            existing_servers = iface['downlink_iface']['dhcp_server_addrs']
            if len(existing_servers) == 0:
                logger.info("Skipping {} - no DHCP servers configured".format(interface_name))
                continue

            new_server = "192.0.0.88"
            old_server = existing_servers[0]

            logger.info("Replacing server {} with {} on interface {}".format(old_server, new_server, interface_name))

            # Replace the first DHCP server
            dhcp_replace_json = [
                {
                    "op": "replace",
                    "path": "/{}/{}/dhcp_servers/0".format(table_name, interface_name),
                    "value": new_server
                }]
            dhcp_replace_json = format_json_patch_for_multiasic(duthost=duthost, json_data=dhcp_replace_json)

            output = apply_patch(duthost, json_data=dhcp_replace_json, dest_file=tmpfile)
            expect_op_success(duthost, output)
            pytest_assert(
                duthost.is_service_fully_started('dhcp_relay'),
                "dhcp_relay service is not running"
            )
            if idx < num_interfaces - 1:
                # For all but the last interface, just validate immediately
                validate_dhcrelay_process(duthost, interface_name, [old_server, new_server], [])

        validate_dhcrelay_process(duthost, interface_name, [new_server], [old_server])

    finally:
        delete_tmpfile(duthost, tmpfile)


def test_dhcp_relay_cross_table_operations(with_checkpoint, one_interface_per_type):    # noqa: F811
    """Test mixed add and rm ops for dhcp server on VLAN and routed interface.

    Tests both adding a new server to VLAN and removing an existing server from routed
    interface in one operation. This verifies GCU can handle operations on different
    interface types in a single JSON patch.
    """
    duthost = with_checkpoint

    vlan_iface = one_interface_per_type.get('vlan')
    routed_iface = one_interface_per_type.get('routed')

    if not vlan_iface:
        pytest.skip("No VLAN interface available")
    if not routed_iface:
        pytest.skip("No routed interface available")

    vlan_name = vlan_iface['downlink_iface']['name']  # e.g., "Vlan1000"
    vlan_num_servers = len(vlan_iface['downlink_iface']['dhcp_server_addrs'])

    routed_name = routed_iface['downlink_iface']['name']  # e.g., "Ethernet232"
    routed_num_servers = len(routed_iface['downlink_iface']['dhcp_server_addrs'])

    new_vlan_server = "192.0.0.77"

    dhcp_add_rm_json = [
        {
            "op": "remove",
            "path": "/PORT/{}/dhcp_servers/{}".format(routed_name, routed_num_servers - 1)
        },
        {
            "op": "add",
            "path": "/VLAN/{}/dhcp_servers/{}".format(vlan_name, vlan_num_servers),
            "value": new_vlan_server
        }]
    dhcp_add_rm_json = format_json_patch_for_multiasic(duthost=duthost, json_data=dhcp_add_rm_json)

    tmpfile = generate_tmpfile(duthost)
    logger.info("tmpfile {}".format(tmpfile))

    try:
        output = apply_patch(duthost, json_data=dhcp_add_rm_json, dest_file=tmpfile)
        expect_op_success(duthost, output)
        pytest_assert(
            duthost.is_service_fully_started('dhcp_relay'),
            "dhcp_relay service is not running"
        )

        validate_dhcrelay_process(duthost, vlan_name, [new_vlan_server], [])
        validate_dhcrelay_process(duthost, routed_name, [new_vlan_server], [])
    finally:
        delete_tmpfile(duthost, tmpfile)
