import ipaddress
import logging
import time
from tests.common.utilities import wait_until
from tests.common.helpers.assertions import pytest_assert

logger = logging.getLogger(__name__)


def get_dhcrelay_process_cmdline(duthost, interface_name):
    """Get the dhcrelay process command line for a specific interface.

    Args:
        duthost: DUT host object
        interface_name: Name of the interface (e.g., 'Ethernet0' or 'Vlan1000')

    Returns:
        Command line string of the dhcrelay process, or None if not found
    """
    # First, check for unified dhcrelay process
    cmd = "docker exec dhcp_relay supervisorctl status | grep 'isc-dhcpv4-relay-unified' | awk '{print $4}'"
    output = duthost.shell(cmd, module_ignore_errors=True)

    if output['rc'] == 0 and output['stdout'].strip():
        # Unified process exists - get its command line
        pid = output['stdout'].strip().rstrip(',')
        cmd = 'docker exec dhcp_relay ps -fp {} | sed "1d"'.format(pid)
        output = duthost.shell(cmd, module_ignore_errors=True)

        if output['rc'] == 0:
            cmdline = output['stdout'].strip()
            # Verify the interface is included in the unified process
            if '-id {}'.format(interface_name) in cmdline:
                return cmdline

    # Fall back to per-interface dhcrelay process (old architecture)
    cmd = "docker exec dhcp_relay supervisorctl status | grep 'dhcpv4-relay-{}' | awk '{{print $4}}'".format(
        interface_name)
    output = duthost.shell(cmd, module_ignore_errors=True)

    if output['rc'] != 0 or not output['stdout'].strip():
        logger.warning("No dhcrelay process found for interface {}".format(interface_name))
        return None

    pid = output['stdout'].strip().rstrip(',')

    # Get command line for this PID
    cmd = 'docker exec dhcp_relay ps -fp {} | sed "1d"'.format(pid)
    output = duthost.shell(cmd, module_ignore_errors=True)

    if output['rc'] != 0:
        logger.warning("Failed to get process info for PID {}".format(pid))
        return None

    return output['stdout'].strip()


def check_routes_to_dhcp_server(duthost, dut_dhcp_relay_data):
    """Validate there is route on DUT to each DHCP server
    """
    output = duthost.shell("show ip bgp sum", module_ignore_errors=True)
    logger.info("bgp state: {}".format(output["stdout"]))
    output = duthost.shell("show int po", module_ignore_errors=True)
    logger.info("portchannel state: {}".format(output["stdout"]))
    default_gw_ip = dut_dhcp_relay_data[0]['default_gw_ip']
    dhcp_servers = set()
    for dhcp_relay in dut_dhcp_relay_data:
        dhcp_servers |= set(dhcp_relay['downlink_iface']['dhcp_server_addrs'])

    for dhcp_server in dhcp_servers:
        rtInfo = duthost.get_ip_route_info(ipaddress.ip_address(dhcp_server))
        nexthops = rtInfo["nexthops"]
        if len(nexthops) == 0:
            logger.info("Failed to find route to DHCP server '{0}'".format(dhcp_server))
            return False
        if len(nexthops) == 1:
            # if only 1 route to dst available - check that it's not default route via MGMT iface
            route_index_in_list = 0
            ip_dst_index = 0
            route_dst_ip = nexthops[route_index_in_list][ip_dst_index]
            if default_gw_ip and route_dst_ip == ipaddress.ip_address(default_gw_ip):
                logger.info("Found route to DHCP server via default GW(MGMT interface)")
                return False
    return True


def check_dhcp_stress_status(duthost, test_duration_seconds):
    # Monitor DHCP status during the test
    start_time = time.time()
    sleep_time = 30
    while time.time() - start_time < test_duration_seconds - sleep_time:
        # Check the status of the DHCP container
        dhcp_container_status = duthost.shell('docker ps | grep dhcp_relay')["stdout"]
        if dhcp_container_status == "":
            assert False, "DHCP container is NOT running."

        # Check CPU usage of the DHCP process
        dhcp_cpu_usage = duthost.shell('show processes cpu --verbose | grep dhc | awk \'{print $9}\'')["stdout"]
        if dhcp_cpu_usage:
            dhcp_cpu_usage_lines = dhcp_cpu_usage.splitlines()
            for cpu_usage in dhcp_cpu_usage_lines:
                cpu_usage_float = float(cpu_usage)
            assert cpu_usage_float < 50.0, "DHCP CPU usage is too high: {}%".format(cpu_usage_float)

        # Check the status of multiple DHCP processes inside the container
        dhcp_process_status = duthost.shell(
             'docker exec dhcp_relay supervisorctl status | grep dhcp | grep -v dhcp6')["stdout"]
        if dhcp_process_status:
            dhcp_process_status_lines = dhcp_process_status.splitlines()
            for dhcp_process_status_line in dhcp_process_status_lines:
                process_name, process_status = dhcp_process_status_line.split()[0], dhcp_process_status_line.split()[1],
                assert process_status == "RUNNING", "{} is not running!".format(process_name)
    time.sleep(sleep_time)


def restart_dhcp_service(duthost):
    duthost.shell('systemctl reset-failed dhcp_relay')
    duthost.shell('systemctl restart dhcp_relay')
    duthost.shell('systemctl reset-failed dhcp_relay')

    def _is_dhcp_relay_ready():
        output = duthost.shell('docker exec dhcp_relay supervisorctl status | grep dhcp | awk \'{print $2}\'',
                               module_ignore_errors=True)
        return (not output['rc'] and output['stderr'] == '' and len(output['stdout_lines']) != 0 and
                all(element == 'RUNNING' for element in output['stdout_lines']))

    pytest_assert(wait_until(60, 1, 10, _is_dhcp_relay_ready), "dhcp_relay is not ready after restarting")
