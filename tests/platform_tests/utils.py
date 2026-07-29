import logging
import os
import yaml

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.multi_thread_utils import SafeThreadPoolExecutor
from tests.common.platform.device_utils import fanout_switch_port_lookup
from tests.common.utilities import get_plt_reboot_ctrl, wait_until
from tests.platform_tests.test_reboot import check_interfaces_and_services


logger = logging.getLogger(__name__)


def is_pddf_supported_and_enabled(duthost):
    """
    Check if PDDF mode is supported and enabled on this platform.

    Args:
        duthost: The DUT host to check

    Returns:
        bool: True if PDDF is supported and enabled, False otherwise
    """
    result = duthost.shell(
        "test -f /usr/share/sonic/platform/pddf_support", module_ignore_errors=True
    )
    return result["rc"] == 0


def get_max_to_reboot(duthost, test_name):
    """
    For chassis testbeds, we need to specify plt_reboot_ctrl in inventory file,
    to let MAX_TIME_TO_REBOOT to be overwritten by specified timeout value
    """
    max_time_to_reboot = 300
    plt_reboot_ctrl = get_plt_reboot_ctrl(duthost, test_name, 'cold')
    if plt_reboot_ctrl:
        max_time_to_reboot = plt_reboot_ctrl.get('timeout', 120)

    return max_time_to_reboot


def get_config_from_yaml(file_path):
    """
    Load configuration from a YAML file.

    Python 3.7+ maintains dictionary insertion order by default,
    so no need for OrderedDict.

    Args:
        file_path (str): Path to the YAML file to load

    Returns:
        dict: The loaded configuration
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Config file not found: {file_path}")
    with open(file_path) as stream:
        try:
            config = yaml.safe_load(stream)
        except yaml.YAMLError as e:
            raise ValueError(f"Malformed YAML in config file '{file_path}': {e}") from e
    if config is None:
        raise ValueError(f"Config file is empty: {file_path}")
    return config


<<<<<<< HEAD
=======
def _wait_for_status(duthost, status_cmd, expected_status, exact=False):
    """
    Poll status_cmd until its stdout matches expected_status.

    exact=True requires stdout.strip() == expected_status (for terse outputs
    like `systemctl is-active`, where substring matching would let "active"
    match a stopped service's "inactive" status). exact=False substring-matches
    (for verbose outputs like `supervisorctl status`, which include extra
    columns such as PID/uptime alongside the state).
    """
    max_wait_sec = 10
    retry_interval_sec = 0.5
    max_retries = int(max_wait_sec / retry_interval_sec)
    last_stdout = None
    for _ in range(max_retries):
        status_result = duthost.shell(status_cmd, module_ignore_errors=True)
        last_stdout = status_result.get("stdout", "")
        matched = last_stdout.strip() == expected_status if exact else expected_status in last_stdout
        if matched:
            return True
        time.sleep(retry_interval_sec)
    logger.warning(f"Timed out waiting for '{expected_status}' from '{status_cmd}', last observed: {last_stdout!r}")
    return False


def _run_control_cmd(duthost, action, name, cmd, verify_cmd, expected_status, exact=False):
    logger.info(f"{action.capitalize()}ing {name}")
    try:
        result = duthost.shell(cmd, module_ignore_errors=True)
        logger.info(
            f"{action.capitalize()} {name} result: rc={result.get('rc', 'N/A')}, \
                stdout='{result.get('stdout', '')}', stderr='{result.get('stderr', '')}'"
        )

        return _wait_for_status(duthost, verify_cmd, expected_status, exact=exact)

    except Exception as e:
        logger.error(f"Exception while {action}ing {name}: {e}")
        return False


def daemon_stop(duthost, daemon_name):
    """Stop a daemon managed by supervisorctl inside the pmon container."""
    return _run_control_cmd(
        duthost, "stop", daemon_name,
        cmd=f"docker exec pmon supervisorctl stop {daemon_name}",
        verify_cmd=f"docker exec pmon supervisorctl status {daemon_name}",
        expected_status="STOPPED",
    )


def daemon_start(duthost, daemon_name):
    """Start a daemon managed by supervisorctl inside the pmon container."""
    return _run_control_cmd(
        duthost, "start", daemon_name,
        cmd=f"docker exec pmon supervisorctl start {daemon_name}",
        verify_cmd=f"docker exec pmon supervisorctl status {daemon_name}",
        expected_status="RUNNING",
    )


def service_stop(duthost, service_name):
    """Stop a host systemd service."""
    return _run_control_cmd(
        duthost, "stop", service_name,
        cmd=f"sudo systemctl stop {service_name}",
        verify_cmd=f"systemctl is-active {service_name}",
        expected_status="inactive",
        exact=True,
    )


def service_start(duthost, service_name):
    """Start a host systemd service."""
    return _run_control_cmd(
        duthost, "start", service_name,
        cmd=f"sudo systemctl start {service_name}",
        verify_cmd=f"systemctl is-active {service_name}",
        expected_status="active",
        exact=True,
    )


>>>>>>> 68fc4f9a0 (NOS-4208: Reuse shared daemon/service utils in test_pddf_ledutil (#2177))
def fanout_hosts_and_ports(fanouthosts, duts_and_ports):
    """
    Use cases:
        1 duthost -> 1 fanout host
        1 duthost -> no fanout host
        1 duthost -> multiple fanout hosts
        multiple duthosts -> 1 fanout hosts

    Returns:
            dict of [fanout, {set of its ports}]
    """
    fanout_and_ports = {}
    for duthost in list(duts_and_ports.keys()):
        for port in duts_and_ports[duthost]:
            fanout, fanout_port = fanout_switch_port_lookup(fanouthosts, duthost.hostname, port)
            # some ports on dut may not have link to fanout
            if fanout is None and fanout_port is None:
                logger.info("Interface {} on duthost {} doesn't link to any fanout switch"
                            .format(port, duthost.hostname))
                continue
            logger.info("Interface {} on fanout {} (os type {}) map to interface {} on duthost {}"
                        .format(fanout_port, fanout.hostname, fanout.get_fanout_os(), port, duthost.hostname))
            if fanout in list(fanout_and_ports.keys()):
                fanout_and_ports[fanout].add(fanout_port)
            else:
                fanout_and_ports[fanout] = {fanout_port}
    return fanout_and_ports


def links_down(fanout, ports):
    """
    Input:
        ports: set of ports on this fanout
    Returns:
        True: if all ports are down
        False: if any port is up
    """
    return fanout.links_status_down(ports)


def links_up(fanout, ports):
    """
    Returns:
        True: if all ports are up
        False: if any port is down
    """
    return fanout.links_status_up(ports)


def link_status_on_host(fanouts_and_ports, max_time_to_reboot, up=True):
    for fanout, ports in list(fanouts_and_ports.items()):
        hostname = fanout.hostname
        # Assumption here is all fanouts are healthy.
        # If fanout is not healthy, or links not in expected state, following errors will be thrown
        if up:
            # Make sure interfaces are up on fanout hosts
            pytest_assert(wait_until(max_time_to_reboot, 5, 0, links_up, fanout, ports),
                          "Interface(s) on {} is still down after {}sec".format(hostname, max_time_to_reboot))
        else:
            # Check every interface is down on this host every 5 sec until device boots up
            pytest_assert(wait_until(max_time_to_reboot, 5, 0, links_down, fanout, ports),
                          "Interface(s) on {} is still up after {}sec".format(hostname, max_time_to_reboot))
    return True


def check_interfaces_and_services_all_lcs(duthosts, conn_graph_facts, xcvr_skip_list):
    with SafeThreadPoolExecutor(max_workers=8) as executor:
        for linecard in duthosts.frontend_nodes:
            executor.submit(
                check_interfaces_and_services,
                linecard, conn_graph_facts["device_conn"][linecard.hostname], xcvr_skip_list,
            )
