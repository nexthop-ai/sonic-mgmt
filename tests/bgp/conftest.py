import os
import contextlib
import ipaddress
import json
import logging
import netaddr
import pytest
import random
import re
import six
import socket

from jinja2 import Template
from tests.common.helpers.assertions import pytest_assert as pt_assert
from tests.common.helpers.generators import generate_ips
from tests.common.helpers.parallel import parallel_run
from tests.common.helpers.parallel import reset_ansible_local_tmp
from tests.common.utilities import wait_until, get_plt_reboot_ctrl
from tests.common.utilities import wait_tcp_connection
from tests.common import config_reload
from bgp_helpers import define_config, apply_default_bgp_config, DUT_TMP_DIR, TEMPLATE_DIR, BGP_PLAIN_TEMPLATE,\
    BGP_NO_EXPORT_TEMPLATE, DUMP_FILE, CUSTOM_DUMP_SCRIPT, CUSTOM_DUMP_SCRIPT_DEST,\
    BGPMON_TEMPLATE_FILE, BGPMON_CONFIG_FILE, BGP_MONITOR_NAME, BGP_MONITOR_PORT, is_chassis
from tests.common.helpers.constants import DEFAULT_NAMESPACE
from tests.common.dualtor.dual_tor_utils import mux_cable_server_ip
from tests.common import constants
from tests.common.devices.eos import EosHost
from tests.common.devices.sonic import SonicHost
from tests.common.helpers.bgp import get_bgp_neighbors_from_config_facts

logger = logging.getLogger(__name__)
_original_dut_frr_mgmt_modes = None


def check_results(results):
    """Helper function for checking results of parallel run.

    Args:
        results (Proxy to shared dict): Results of parallel run, indexed by node name.
    """
    failed_results = {}
    for node_name, node_results in list(results.items()):
        failed_node_results = [res for res in node_results if res['failed']]
        if len(failed_node_results) > 0:
            failed_results[node_name] = failed_node_results
    if failed_results:
        logger.error('failed_results => {}'.format(json.dumps(failed_results, indent=2)))
        pt_assert(False, 'Some processes for updating nbr hosts configuration returned failed results')


def _check_if_module_skipped_for_mode(request, duthost, frr_mgmt_config, zmq_enabled, tbinfo):
    try:
        module_name = request.module.__name__
        # Extract module path from the file path (relative to tests directory)
        fspath_str = str(request.fspath)
        if '/tests/' in fspath_str:
            module_path = fspath_str.split('/tests/')[-1]
        else:
            module_path = module_name.replace('.', '/') + '.py'

        if zmq_enabled == 'true' and not is_chassis(duthost) and tbinfo["topo"]["type"] == 't2':
            logger.info(f"Module {module_path} skipped for zmq_enabled on t2 non chassis")
            return True

        # Check if this module has skip conditions for frr_mgmt_config
        session = request.session
        config = session.config
        # Get cached conditions from conditional_mark plugin
        conditions = config.cache.get('TESTS_MARK_CONDITIONS', None)
        if not conditions:
            return False

        matched_entries = []
        # Search through the list of condition dictionaries and collect ALL matches
        for condition_dict in conditions:
            condition_entry = list(condition_dict.keys())[0]
            if condition_entry == module_path or module_path.startswith(condition_entry):
                matched_entries.append(condition_dict[condition_entry])
        if not matched_entries:
            return False

        # Evaluate all matched 'skip' blocks to see if any explicitly skip for frr_mgmt_config
        for entry in matched_entries:
            if 'skip' not in entry:
                continue

            skip_block = entry['skip']
            skip_conditions = skip_block.get('conditions', [])
            # Check if the specific frr_mgmt_config condition matches current value
            frr_condition_str = f"frr_mgmt_config == '{frr_mgmt_config}'"
            if frr_condition_str not in str(skip_conditions):
                continue

            # For OR logic, skip immediately. For AND logic, evaluate all conditions.
            conditions_logical_operator = skip_block.get('conditions_logical_operator', 'AND').upper()
            if conditions_logical_operator == 'OR':
                logger.info(f"Module {module_path} marked to skip for "
                            f"frr_mgmt_config='{frr_mgmt_config}' (OR)")
                return True

            try:
                basic_facts = config.cache.get(f'BASIC_FACTS_{duthost.hostname}', {})
                if not basic_facts:
                    raise KeyError("Basic facts not available")

                eval_namespace = basic_facts.copy()
                eval_namespace['frr_mgmt_config'] = frr_mgmt_config
                if all(eval(cond, eval_namespace) for cond in skip_conditions if cond):
                    logger.info(f"Module {module_path} marked to skip for "
                                f"frr_mgmt_config='{frr_mgmt_config}' (AND)")
                    return True
            except Exception:
                pass

        return False

    except Exception:
        return False


@pytest.fixture(
    scope='module', autouse=True,
    params=[{'frr_mgmt_config': 'false', 'routing_config_mode': 'separated',
             'zmq_enabled': 'false', 'mode_name': 'legacy-mode-zmq-disabled'},
            {'frr_mgmt_config': 'false', 'routing_config_mode': 'separated',
             'zmq_enabled': 'true', 'mode_name': 'legacy-mode-zmq-enabled'},
            {'frr_mgmt_config': 'true', 'routing_config_mode': 'unified',
             'zmq_enabled': 'false', 'mode_name': 'unified-mode-zmq-disabled'},
            {'frr_mgmt_config': 'true', 'routing_config_mode': 'unified',
             'zmq_enabled': 'true', 'mode_name': 'unified-mode-zmq-enabled'}
            ],
    ids=['legacy-mode-zmq-disabled', 'legacy-mode-zmq-enabled',
         'unified-mode-zmq-disabled', 'unified-mode-zmq-enabled']
)
def bgp_switch_frr_mgmt_mode(request, duthosts, rand_one_dut_hostname, tbinfo):
    global _original_dut_frr_mgmt_modes
    duthost = duthosts[rand_one_dut_hostname]
    config_params = request.param
    test_routing_cfg_mode = config_params['routing_config_mode']
    test_frr_mgmt_mode = config_params['frr_mgmt_config']
    test_zmq_enabled = config_params['zmq_enabled']

    logger.info(f"{'=' * 30} Testing in {config_params['mode_name'].upper()} mode {'=' * 30}")

    # Check if module is marked to be skipped for this mode
    if _check_if_module_skipped_for_mode(request, duthost, test_frr_mgmt_mode, test_zmq_enabled, tbinfo):
        logger.info(f"Skipping {config_params['mode_name']} - "
                    f"module marked to skip for {test_frr_mgmt_mode}")
        pytest.skip(f"Module is marked to skip for {config_params['mode_name']} mode")
        return

    def get_current_frr_mgmt_modes():
        def redis_get(key):
            return duthost.shell(
                f'redis-cli -n 4 HGET "DEVICE_METADATA|localhost" "{key}"'
            )['stdout'].strip()
        routing_mode = redis_get('docker_routing_config_mode')
        frr_mgmt_mode = 'true' if redis_get('frr_mgmt_framework_config') == 'true' else 'false'
        zmq_mode = 'true' if redis_get('orch_northbond_route_zmq_enabled') == 'true' else 'false'
        logger.info(f"Current modes - routing: {routing_mode}, "
                    f"frr_mgmt: {frr_mgmt_mode}, zmq: {zmq_mode}")
        return routing_mode, frr_mgmt_mode, zmq_mode

    def reload_and_wait_bgp(operation_name):
        logger.info(f"Config reload for {operation_name}")
        config_reload(duthost, safe_reload=True, check_intf_up_ports=True)
        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        bgp_neighbors = get_bgp_neighbors_from_config_facts(duthost, config_facts)
        if not wait_until(600, 10, 10, duthost.check_bgp_session_state, list(bgp_neighbors.keys())):
            raise Exception(f"BGP sessions not up after {operation_name}")

    def set_zmq_in_redis(zmq_value):
        redis_key = '"DEVICE_METADATA|localhost" "orch_northbond_route_zmq_enabled"'
        if zmq_value == 'true':
            cmd = f'redis-cli -n 4 HSET {redis_key} "{zmq_value}"'
        else:
            cmd = f'redis-cli -n 4 HDEL {redis_key}'

        logger.info(f"{'Enabling' if zmq_value == 'true' else 'Disabling'} ZMQ in Redis")
        output = duthost.shell(cmd, module_ignore_errors=True)
        if output.get('rc', 0) != 0:
            logger.error(f"Failed to set ZMQ: {output.get('stderr', '')}")
            return False

        save_output = duthost.shell("sudo config save -y", module_ignore_errors=True)
        if save_output.get('rc', 0) != 0:
            raise False
        return True

    def configure_zmq(zmq_enabled, restart_containers=False, operation_name="ZMQ Configuration"):
        get_cmd = "redis-cli -n 4 HGET \"DEVICE_METADATA|localhost\" \"orch_northbond_route_zmq_enabled\""
        zmq_output = duthost.shell(get_cmd, module_ignore_errors=True)['stdout'].strip()
        current_zmq = 'true' if zmq_output == 'true' else 'false'

        if current_zmq == zmq_enabled:
            logger.info(f"ZMQ already configured to {zmq_enabled} - no change needed")
            return True

        try:
            if not set_zmq_in_redis(zmq_enabled):
                raise Exception("Failed to set ZMQ in Redis")

            if restart_containers:
                reload_and_wait_bgp(operation_name)

            return True
        except Exception as e:
            logger.info(f"ZMQ configuration failed {e} - Reverting ZMQ configuration to {current_zmq}")
            set_zmq_in_redis(current_zmq)
            return False

    # Helper function to execute migration command
    def execute_migration(routing_cfg_mode, frr_mgmt_mode, zmq_enabled, operation_name="Migration"):
        # Configure ZMQ setting before migration so it gets applied during the config reload
        zmq_op_name = f"{operation_name} ZMQ setup"
        if not configure_zmq(zmq_enabled, restart_containers=False, operation_name=zmq_op_name):
            logger.error(f"Failed to configure ZMQ setting for {operation_name}")
            return False

        cmd = (
            f"sudo /usr/local/bin/frr_unified_cfg_mgmt_migrator.py "
            f"--routing_config_mode='{routing_cfg_mode}' "
            f"--frr_mgmt_config='{frr_mgmt_mode}' --config_reload='true'"
        )

        logger.info(f"Executing {operation_name.lower()} command: {cmd}")
        output = duthost.shell(cmd, module_ignore_errors=True)

        # Log output appropriately
        if output.get('stdout'):
            logger.info(f"{operation_name} output: {output['stdout']}")
        if output.get('stderr'):
            logger.warning(f"{operation_name} stderr: {output['stderr']}")

        # Check for failure
        if output.get('rc', 0) != 0:
            logger.error(f"{operation_name} failed with return code {output.get('rc', 'unknown')}")
            if output.get('stderr'):
                logger.error(f"{operation_name} error: {output['stderr']}")
            return False

        config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
        bgp_neighbors = get_bgp_neighbors_from_config_facts(duthost, config_facts)

        # Wait for BGP sessions to come up giving an initial delay for bgp to start. Without the
        # delay, the wait_until loop can start checking before bgp has even started and we would see
        # annoying "bgpd is not running" errors in the logs.
        initial_delay = 10
        if not wait_until(600, 10, initial_delay, duthost.check_bgp_session_state, list(bgp_neighbors.keys())):
            logger.error(f"Not all BGP sessions are up after {operation_name.lower()}")
            return False

        logger.info(f"All BGP sessions are up after {operation_name.lower()}")
        return True

    # Get current configuration
    dut_routing_cfg_mode, dut_frr_mgmt_mode, dut_zmq_mode = get_current_frr_mgmt_modes()
    # Store original configuration only once
    if _original_dut_frr_mgmt_modes is None:
        _original_dut_frr_mgmt_modes = {
            'routing_config_mode': dut_routing_cfg_mode,
            'frr_mgmt_config': dut_frr_mgmt_mode,
            'zmq_enabled': dut_zmq_mode
        }

    if (dut_routing_cfg_mode == test_routing_cfg_mode and
            dut_frr_mgmt_mode == test_frr_mgmt_mode and
            dut_zmq_mode == test_zmq_enabled):
        logger.info(f"DUT already in {config_params['mode_name']} mode - no changes needed")
    else:
        # Check if only ZMQ setting needs to change (no migration needed)
        if (dut_routing_cfg_mode == test_routing_cfg_mode and
                dut_frr_mgmt_mode == test_frr_mgmt_mode and
                dut_zmq_mode != test_zmq_enabled):
            logger.info(f"Only ZMQ setting needs to change from {dut_zmq_mode} to {test_zmq_enabled}")
            if not configure_zmq(test_zmq_enabled, restart_containers=True,
                                 operation_name=f"{config_params['mode_name']} ZMQ-only change"):
                pytest.fail(f"Failed to configure ZMQ for {config_params['mode_name']} mode")
        else:
            # Execute migrator script (which will also apply ZMQ setting)
            if not execute_migration(test_routing_cfg_mode, test_frr_mgmt_mode, test_zmq_enabled,
                                     f"{config_params['mode_name']} switch"):
                pytest.fail(f"Failed to switch DUT to {config_params['mode_name']} mode")

    yield

    # Only restore after the last mode has been tested
    if config_params['mode_name'] == 'unified-mode-zmq-enabled':
        logger.info(f"{'=' * 30} Restoring original frr mgmt mode {'=' * 30}")
        # Get current configuration
        current_routing_mode, current_frr_mgmt_mode, current_zmq_mode = get_current_frr_mgmt_modes()

        # Check if restoration is needed
        if (current_routing_mode == _original_dut_frr_mgmt_modes['routing_config_mode'] and
                current_frr_mgmt_mode == _original_dut_frr_mgmt_modes['frr_mgmt_config'] and
                current_zmq_mode == _original_dut_frr_mgmt_modes['zmq_enabled']):
            logger.info("DUT already in original frr mgmt config mode - no restoration needed")
        else:
            logger.info(f"Restoring DUT to original frr mgmt config mode: {_original_dut_frr_mgmt_modes}")
            # Execute restoration
            if not execute_migration(_original_dut_frr_mgmt_modes['routing_config_mode'],
                                     _original_dut_frr_mgmt_modes['frr_mgmt_config'],
                                     _original_dut_frr_mgmt_modes['zmq_enabled'], "Restoration"):
                pytest.fail("Failed to restore original frr mgmt config mode")

        logger.info(f"{'=' * 30} Completed restoring frr mgmt config mode {'=' * 30}")


@pytest.fixture(scope='module')
def setup_bgp_graceful_restart(duthosts, rand_one_dut_hostname, nbrhosts, tbinfo, cct=8):
    duthost = duthosts[rand_one_dut_hostname]

    config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
    bgp_neighbors = get_bgp_neighbors_from_config_facts(duthost, config_facts)

    @reset_ansible_local_tmp
    def configure_nbr_gr(node=None, results=None):
        """Target function will be used by multiprocessing for configuring VM hosts.

        Args:
            node (object, optional): A value item of the dict type fixture 'nbrhosts'. Defaults to None.
            results (Proxy to shared dict, optional): An instance of multiprocessing.Manager().dict(). Proxy to a dict
                shared by all processes for returning execution results. Defaults to None.
        """
        if node is None or results is None:
            logger.error('Missing kwarg "node" or "results"')
            return

        node_results = []
        asn = node['conf']['bgp']['asn']
        logger.info('enable graceful restart on neighbor host {}'.format(node['host'].hostname))
        logger.info('bgp asn {}'.format(asn))
        if isinstance(node['host'], EosHost):
            node_results.append(node['host'].config(
                    lines=['graceful-restart restart-time 300'],
                    parents=['router bgp {}'.format(asn)],
                    module_ignore_errors=True)
                )
            node_results.append(node['host'].config(
                    lines=['graceful-restart'],
                    parents=['router bgp {}'.format(asn), 'address-family ipv4'],
                    module_ignore_errors=True)
                )
            node_results.append(node['host'].config(
                    lines=['graceful-restart'],
                    parents=['router bgp {}'.format(asn), 'address-family ipv6'],
                    module_ignore_errors=True)
                )
        elif isinstance(node['host'], SonicHost):
            node_results.append(node['host'].config(
                lines=['bgp graceful-restart', 'bgp graceful-restart restart-time 300'],
                parents=['router bgp {}'.format(asn)],
                module_ignore_errors=True))
            # enable graceful-restart for peers connected to DUT
            bgp_peers = node['conf']['bgp']['peers']
            dut_asn = int(next(iter(bgp_peers.keys())))
            peers = bgp_peers[dut_asn]
            if not peers:
                results[node['host'].hostname] = [{'failed': True, 'msg': "DUT ASN not found in BGP peers"}]
                return

            for neighbor_ip in peers:
                node_results.append(node['host'].config(
                    lines=['neighbor {} graceful-restart'.format(neighbor_ip)],
                    parents=['router bgp {}'.format(asn)],
                    module_ignore_errors=True))

            node['host'].command("sudo vtysh -c 'clear bgp ipv4 *'", module_ignore_errors=True)
            node['host'].command("sudo vtysh -c 'clear bgp ipv6 *'", module_ignore_errors=True)
        else:
            logger.error(f"Unsupported host type: {type(node['host'])}")
            node_results.append({
                'failed': True,
                'msg': f"Unsupported host type: {type(node['host'])}"
            })

        results[node['host'].hostname] = node_results

    @reset_ansible_local_tmp
    def restore_nbr_gr(node=None, results=None):
        """Target function will be used by multiprocessing for restoring configuration for the VM hosts.

        Args:
            node (object, optional): A value item of the dict type fixture 'nbrhosts'. Defaults to None.
            results (Proxy to shared dict, optional): An instance of multiprocessing.Manager().dict(). Proxy to a dict
                shared by all processes for returning execution results. Defaults to None.
        """
        if node is None or results is None:
            logger.error('Missing kwarg "node" or "results"')
            return

        # start bgpd if not started
        node_results = []
        node['host'].start_bgpd()
        logger.info('disable graceful restart on neighbor {}'.format(node))
        asn = (node['conf']['bgp']['asn'])
        if isinstance(node['host'], EosHost):
            node_results.append(node['host'].config(
                    lines=['no graceful-restart'],
                    parents=['router bgp {}'.format(asn), 'address-family ipv4'],
                    module_ignore_errors=True)
                )
            node_results.append(node['host'].config(
                    lines=['no graceful-restart'],
                    parents=['router bgp {}'.format(asn), 'address-family ipv6'],
                    module_ignore_errors=True)
                )
        elif isinstance(node['host'], SonicHost):
            node_results.append(node['host'].config(
                lines=['no bgp graceful-restart', 'no bgp graceful-restart restart-time 300'],
                parents=['router bgp {}'.format(asn)],
                module_ignore_errors=True))
            # restore graceful-restart for peers connected to DUT
            bgp_peers = node['conf']['bgp']['peers']
            dut_asn = int(next(iter(bgp_peers.keys())))
            peers = bgp_peers[dut_asn]
            if not peers:
                results[node['host'].hostname] = [{'failed': True, 'msg': "DUT ASN not found in BGP peers"}]
                return
            for neighbor_ip in peers:
                node_results.append(node['host'].config(
                    lines=['no neighbor {} graceful-restart'.format(neighbor_ip)],
                    parents=['router bgp {}'.format(asn)],
                    module_ignore_errors=True))
            node['host'].command("sudo vtysh -c 'clear bgp ipv4 *'", module_ignore_errors=True)
            node['host'].command("sudo vtysh -c 'clear bgp ipv6 *'", module_ignore_errors=True)
        else:
            logger.error(f'Unsupported host type: {type(node["host"])}')
            node_results.append({
                'failed': True,
                'msg': f'Unsupported host type: {type(node["host"])}'
            })
        results[node['host'].hostname] = node_results

    # enable graceful restart on neighbors
    results = parallel_run(configure_nbr_gr, (), {}, list(nbrhosts.values()), timeout=120, concurrent_tasks=cct)

    check_results(results)

    logger.info("bgp neighbors: {}".format(list(bgp_neighbors.keys())))
    res = True
    err_msg = ""
    if not wait_until(300, 10, 0, duthost.check_bgp_session_state, list(bgp_neighbors.keys())):
        res = False
        err_msg = "not all bgp sessions are up after enable graceful restart"

    is_backend_topo = "backend" in tbinfo["topo"]["name"]
    if not is_backend_topo and res and not wait_until(100, 5, 0, duthost.check_bgp_default_route):
        res = False
        err_msg = "ipv4 or ipv6 bgp default route not available"

    if not res:
        # Disable graceful restart in case of failure
        parallel_run(restore_nbr_gr, (), {}, list(nbrhosts.values()), timeout=120, concurrent_tasks=cct)
        pytest.fail(err_msg)
    yield

    results = parallel_run(restore_nbr_gr, (), {}, list(nbrhosts.values()), timeout=120, concurrent_tasks=cct)

    check_results(results)

    if not wait_until(300, 10, 0, duthost.check_bgp_session_state, list(bgp_neighbors.keys())):
        pytest.fail("not all bgp sessions are up after disable graceful restart")


@pytest.fixture(scope="module")
def setup_interfaces(duthosts, enum_rand_one_per_hwsku_frontend_hostname, ptfhost, request, tbinfo, topo_scenario):
    """Setup interfaces for the new BGP peers on PTF."""

    def _is_ipv4_address(ip_addr):
        return ipaddress.ip_address(ip_addr).version == 4

    def _duthost_cleanup_ip(asichost, ip):
        """
        Search if "ip" is configured on any DUT interface. If yes, remove it.
        """

        for line in duthost.shell("{} ip addr show | grep 'inet '".format(asichost.ns_arg))['stdout_lines']:
            # Example line: '''    inet 10.0.0.2/31 scope global Ethernet104'''
            fields = line.split()
            intf_ip = fields[1].split("/")[0]
            if intf_ip == ip:
                intf_name = fields[-1]
                asichost.config_ip_intf(intf_name, ip, "remove")

        ip_intfs = duthost.show_and_parse('show ip interface {}'.format(asichost.cli_ns_option))

        # For interface that has two IP configured, the output looks like:
        #       admin@vlab-03:~$ show ip int
        #       Interface        Master    IPv4 address/mask    Admin/Oper    BGP Neighbor    Neighbor IP
        #       ---------------  --------  -------------------  ------------  --------------  -------------
        #       Ethernet100                10.0.0.50/31         up/up         ARISTA10T0      10.0.0.51
        #       Ethernet104                10.0.0.2/31          up/up         N/A             N/A
        #                                  10.0.0.52/31                       ARISTA11T0      10.0.0.53
        #       Ethernet108                10.0.0.54/31         up/up         ARISTA12T0      10.0.0.55
        #       Ethernet112                10.0.0.56/31         up/up         ARISTA13T0      10.0.0.57
        #
        # For interface Ethernet104, it has two entries in the output list:
        #   [{
        #     "ipv4 address/mask": "10.0.0.2/31",
        #     "neighbor ip": "N/A",
        #     "master": "",
        #     "admin/oper": "up/up",
        #     "interface": "Ethernet104",
        #     "bgp neighbor": "N/A"
        #   },
        #   {
        #     "ipv4 address/mask": "10.0.0.52/31",
        #     "neighbor ip": "10.0.0.53",
        #     "master": "",
        #     "admin/oper": "",
        #     "interface": "",
        #     "bgp neighbor": "ARISTA11T0"
        #   },]
        # The second item has empty value for key "interface". Below code is to fill "Ethernet104" for the second item.
        last_interface = ""
        for ip_intf in ip_intfs:
            if ip_intf["interface"] == "":
                ip_intf["interface"] = last_interface
            else:
                last_interface = ip_intf["interface"]

        # Remove the specified IP from interfaces
        for ip_intf in ip_intfs:
            if ip_intf["ipv4 address/mask"].split("/")[0] == ip:
                asichost.config_ip_intf(ip_intf["interface"], ip, "remove")

    def _find_vlan_intferface(mg_facts):
        for vlan_intf in mg_facts["minigraph_vlan_interfaces"]:
            if _is_ipv4_address(vlan_intf["addr"]):
                return vlan_intf
        raise ValueError("No Vlan interface defined in current topo")

    def _find_loopback_interface(mg_facts, loopback_intf_name="Loopback0"):
        for loopback in mg_facts["minigraph_lo_interfaces"]:
            if loopback["name"] == loopback_intf_name:
                return loopback
        raise ValueError("No loopback interface %s defined." % loopback_intf_name)

    @contextlib.contextmanager
    def _setup_interfaces_dualtor(mg_facts, peer_count):
        try:
            connections = []
            vlan_intf = _find_vlan_intferface(mg_facts)
            loopback_intf = _find_loopback_interface(mg_facts, "Loopback3")
            vlan_intf_addr = vlan_intf["addr"]
            vlan_intf_prefixlen = vlan_intf["prefixlen"]
            loopback_intf_addr = loopback_intf["addr"]
            loopback_intf_prefixlen = loopback_intf["prefixlen"]

            mux_configs = mux_cable_server_ip(duthost)
            local_interfaces = random.sample(list(mux_configs.keys()), peer_count)
            for local_interface in local_interfaces:
                connections.append(
                    {
                        "local_intf": loopback_intf["name"],
                        "local_addr": "%s/%s" % (loopback_intf_addr, loopback_intf_prefixlen),
                        # Note: Config same subnets on PTF will generate two connect routes on PTF.
                        # This may lead different IPs has same FDB entry on DUT even they are on different
                        # interface and cause layer3 packet drop on PTF, so here same interface for different
                        # neighbor.
                        "neighbor_intf": "eth%s" % mg_facts["minigraph_port_indices"][local_interfaces[0]],
                        "neighbor_addr": "%s/%s" % (mux_configs[local_interface]["server_ipv4"].split("/")[0],
                                                    vlan_intf_prefixlen)
                    }
                )

            ptfhost.remove_ip_addresses()
            # let's stop arp_responder and garp_service as they could pollute
            # devices' arp tables.
            ptfhost.shell("supervisorctl stop garp_service", module_ignore_errors=True)
            ptfhost.shell("supervisorctl stop arp_responder", module_ignore_errors=True)

            first_neighbor_port = None
            for conn in connections:
                ptfhost.shell("ip address add %s dev %s" % (conn["neighbor_addr"], conn["neighbor_intf"]))
                if not first_neighbor_port:
                    first_neighbor_port = conn["neighbor_intf"]
                # NOTE: this enables the standby ToR to passively learn
                # all the neighbors configured on the ptf interfaces.
                # As the ptf is a multihomed environment, the packets to the
                # vlan gateway will always egress the first ptf port that has
                # vlan subnet address assigned, so let's use the first
                # ptf port to announce the neigbors.
                ptfhost.shell(
                    "arping %s -S %s -i %s -C 5" % (
                        vlan_intf_addr, conn["neighbor_addr"].split("/")[0], first_neighbor_port
                    ),
                    module_ignore_errors=True
                )

            ptfhost.shell("ip route add %s via %s" % (loopback_intf_addr, vlan_intf_addr))
            yield connections

        finally:
            ptfhost.shell("ip route delete %s" % loopback_intf_addr)
            for conn in connections:
                ptfhost.shell("ifconfig %s 0.0.0.0" % conn["neighbor_intf"])

    @contextlib.contextmanager
    def _setup_interfaces_t0_or_mx(mg_facts, peer_count):
        try:
            connections = []
            is_backend_topo = "backend" in tbinfo["topo"]["name"]
            vlan_intf = _find_vlan_intferface(mg_facts)
            vlan_intf_name = vlan_intf["attachto"]
            vlan_intf_addr = "%s/%s" % (vlan_intf["addr"], vlan_intf["prefixlen"])
            vlan_members = mg_facts["minigraph_vlans"][vlan_intf_name]["members"]
            is_vlan_tagged = mg_facts["minigraph_vlans"][vlan_intf_name].get("type", "").lower() == "tagged"
            vlan_id = mg_facts["minigraph_vlans"][vlan_intf_name]["vlanid"]
            local_interfaces = random.sample(vlan_members, peer_count)
            neighbor_addresses = generate_ips(
                peer_count,
                vlan_intf["subnet"],
                [netaddr.IPAddress(vlan_intf["addr"])]
            )

            loopback_ip = None
            for intf in mg_facts["minigraph_lo_interfaces"]:
                if netaddr.IPAddress(intf["addr"]).version == 4:
                    loopback_ip = intf["addr"]
                    break
            if not loopback_ip:
                pytest.fail("ipv4 lo interface not found")

            neighbor_intf = random.choice(local_interfaces)
            for neighbor_addr in neighbor_addresses:
                conn = {}
                conn["local_intf"] = vlan_intf_name
                conn["local_addr"] = vlan_intf_addr
                conn["neighbor_addr"] = neighbor_addr
                conn["neighbor_intf"] = "eth%s" % mg_facts["minigraph_port_indices"][neighbor_intf]
                if is_backend_topo and is_vlan_tagged:
                    conn["neighbor_intf"] += (constants.VLAN_SUB_INTERFACE_SEPARATOR + vlan_id)
                conn["loopback_ip"] = loopback_ip
                connections.append(conn)

            ptfhost.remove_ip_addresses()  # In case other case did not cleanup IP address configured on PTF interface

            for conn in connections:
                ptfhost.shell("ip address add %s/%d dev %s" % (
                    conn["neighbor_addr"], vlan_intf["prefixlen"], conn["neighbor_intf"]
                ))

            yield connections

        finally:
            for conn in connections:
                ptfhost.shell("ip address flush %s scope global" % conn["neighbor_intf"])

    @contextlib.contextmanager
    def _setup_interfaces_t1_or_t2(mg_facts, peer_count):
        try:
            connections = []
            is_backend_topo = "backend" in tbinfo["topo"]["name"]
            ipv4_interfaces = []
            used_subnets = set()
            asic_idx = 0
            if mg_facts["minigraph_interfaces"]:
                for intf in mg_facts["minigraph_interfaces"]:
                    if _is_ipv4_address(intf["addr"]):
                        intf_asic_idx = duthost.get_port_asic_instance(intf["attachto"]).asic_index
                        if not ipv4_interfaces:
                            ipv4_interfaces.append(intf["attachto"])
                            asic_idx = intf_asic_idx
                        else:
                            if intf_asic_idx != asic_idx:
                                continue
                            else:
                                ipv4_interfaces.append(intf["attachto"])
                        used_subnets.add(ipaddress.ip_network(intf["subnet"]))

            ipv4_lag_interfaces = []
            if mg_facts["minigraph_portchannel_interfaces"]:
                for pt in mg_facts["minigraph_portchannel_interfaces"]:
                    if _is_ipv4_address(pt["addr"]):
                        pt_members = mg_facts["minigraph_portchannels"][pt["attachto"]]["members"]
                        pc_asic_idx = duthost.get_asic_index_for_portchannel(pt["attachto"])
                        # Only use LAG with 1 member for bgpmon session between PTF,
                        # It's because exabgp on PTF is bind to single interface
                        if len(pt_members) == 1:
                            # If first time, we record the asic index
                            if not ipv4_interfaces and not ipv4_lag_interfaces:
                                asic_idx = pc_asic_idx
                                ipv4_lag_interfaces.append(pt["attachto"])
                            # Not first time, only append the port-channel that belongs to the same asic in current list
                            else:
                                if pc_asic_idx != asic_idx:
                                    continue
                                else:
                                    ipv4_lag_interfaces.append(pt["attachto"])
                            used_subnets.add(ipaddress.ip_network(pt["subnet"]))

            vlan_sub_interfaces = []
            if is_backend_topo:
                for intf in mg_facts.get("minigraph_vlan_sub_interfaces"):
                    if _is_ipv4_address(intf["addr"]):
                        vlan_sub_interfaces.append(intf["attachto"])
                        used_subnets.add(ipaddress.ip_network(intf["subnet"]))

            subnet_prefixlen = list(used_subnets)[0].prefixlen
            # Use a subnet which doesnt conflict with other subnets used in minigraph
            subnets = ipaddress.ip_network(six.text_type("20.0.0.0/24")).subnets(new_prefix=subnet_prefixlen)

            loopback_ip = None
            for intf in mg_facts["minigraph_lo_interfaces"]:
                if netaddr.IPAddress(intf["addr"]).version == 4:
                    loopback_ip = intf["addr"]
                    break
            if not loopback_ip:
                pytest.fail("ipv4 lo interface not found")

            num_intfs = len(ipv4_interfaces + ipv4_lag_interfaces + vlan_sub_interfaces)
            if num_intfs < peer_count:
                pytest.skip("Found {} IPv4 interfaces or lags with 1 port member,"
                            " but require {} interfaces".format(num_intfs, peer_count))

            for intf, subnet in zip(random.sample(ipv4_interfaces + ipv4_lag_interfaces + vlan_sub_interfaces,
                                                  peer_count), subnets):
                def _get_namespace(minigraph_config, intf):
                    namespace = DEFAULT_NAMESPACE
                    if intf in minigraph_config and 'namespace' in minigraph_config[intf] and \
                            minigraph_config[intf]['namespace']:
                        namespace = minigraph_config[intf]['namespace']
                    return namespace
                conn = {}
                local_addr, neighbor_addr = [_ for _ in subnet][:2]
                conn["local_intf"] = "%s" % intf
                conn["local_addr"] = "%s/%s" % (local_addr, subnet_prefixlen)
                conn["neighbor_addr"] = "%s/%s" % (neighbor_addr, subnet_prefixlen)
                conn["loopback_ip"] = loopback_ip
                conn["namespace"] = _get_namespace(mg_facts['minigraph_neighbors'], intf)

                if intf.startswith("PortChannel"):
                    member_intf = mg_facts["minigraph_portchannels"][intf]["members"][0]
                    conn["neighbor_intf"] = "eth%s" % mg_facts["minigraph_ptf_indices"][member_intf]
                    conn["namespace"] = _get_namespace(mg_facts["minigraph_portchannels"], intf)
                elif constants.VLAN_SUB_INTERFACE_SEPARATOR in intf:
                    orig_intf, vlan_id = intf.split(constants.VLAN_SUB_INTERFACE_SEPARATOR)
                    ptf_port_index = str(mg_facts["minigraph_port_indices"][orig_intf])
                    conn["neighbor_intf"] = "eth" + ptf_port_index + constants.VLAN_SUB_INTERFACE_SEPARATOR + vlan_id
                else:
                    conn["neighbor_intf"] = "eth%s" % mg_facts["minigraph_ptf_indices"][intf]
                connections.append(conn)

            ptfhost.remove_ip_addresses()  # In case other case did not cleanup IP address configured on PTF interface

            for conn in connections:
                asichost = duthost.asic_instance_from_namespace(conn['namespace'])

                # Find out if any other interface has the same IP configured. If yes, remove it
                # Otherwise, there may be conflicts and test would fail.
                _duthost_cleanup_ip(asichost, conn["local_addr"])

                # bind the ip to the interface and notify bgpcfgd
                asichost.config_ip_intf(conn["local_intf"], conn["local_addr"], "add")

                ptfhost.shell("ifconfig %s %s" % (conn["neighbor_intf"], conn["neighbor_addr"]))

                # add route to loopback address on PTF host
                nhop_ip = re.split("/", conn["local_addr"])[0]
                try:
                    socket.inet_aton(nhop_ip)
                    ptfhost.shell(
                        "ip route del {}/32".format(conn["loopback_ip"]),
                        module_ignore_errors=True
                    )
                    ptfhost.shell("ip route add {}/32 via {}".format(
                        conn["loopback_ip"], nhop_ip
                    ))
                except socket.error:
                    raise Exception("Invalid V4 address {}".format(nhop_ip))

            yield connections

        finally:
            for conn in connections:
                asichost = duthost.asic_instance_from_namespace(conn['namespace'])
                asichost.config_ip_intf(conn["local_intf"], conn["local_addr"], "remove")
                ptfhost.shell("ifconfig %s 0.0.0.0" % conn["neighbor_intf"])
                ptfhost.shell(
                    "ip route del {}/32".format(conn["loopback_ip"]),
                    module_ignore_errors=True
                )

    peer_count = getattr(request.module, "PEER_COUNT", 1)
    if "dualtor" in tbinfo["topo"]["name"]:
        setup_func = _setup_interfaces_dualtor
    elif tbinfo["topo"]["type"] in ["t0", "mx"]:
        setup_func = _setup_interfaces_t0_or_mx
    elif tbinfo["topo"]["type"] in set(["t1", "t2", "m1", "lt2", "ft2"]):
        setup_func = _setup_interfaces_t1_or_t2
    elif tbinfo["topo"]["type"] == "m0":
        if topo_scenario == "m0_l3_scenario":
            setup_func = _setup_interfaces_t1_or_t2
        else:
            setup_func = _setup_interfaces_t0_or_mx
    else:
        raise TypeError("Unsupported topology: %s" % tbinfo["topo"]["type"])

    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    with setup_func(mg_facts, peer_count) as connections:
        yield connections

    duthost.shell("sonic-clear arp")
    duthost.shell('sudo config save -y')
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True)


@pytest.fixture(scope="module")
def deploy_plain_bgp_config(duthost):
    """
    Deploy bgp plain config on the DUT

    Args:
        duthost: DUT host object

    Returns:
        Pathname of the bgp plain config on the DUT
    """
    bgp_plain_template_src_path = os.path.join(TEMPLATE_DIR, BGP_PLAIN_TEMPLATE)
    bgp_plain_template_path = os.path.join(DUT_TMP_DIR, BGP_PLAIN_TEMPLATE)

    define_config(duthost, bgp_plain_template_src_path, bgp_plain_template_path)

    return bgp_plain_template_path


@pytest.fixture(scope="module")
def deploy_no_export_bgp_config(duthost):
    """
    Deploy bgp no export config on the DUT

    Args:
        duthost: DUT host object

    Returns:
        Pathname of the bgp no export config on the DUT
    """
    bgp_no_export_template_src_path = os.path.join(TEMPLATE_DIR, BGP_NO_EXPORT_TEMPLATE)
    bgp_no_export_template_path = os.path.join(DUT_TMP_DIR, BGP_NO_EXPORT_TEMPLATE)

    define_config(duthost, bgp_no_export_template_src_path, bgp_no_export_template_path)

    return bgp_no_export_template_path


@pytest.fixture(scope="module")
def backup_bgp_config(duthost):
    """
    Copy default bgp configuration to the DUT and apply default configuration on the bgp
    docker after test

    Args:
        duthost: DUT host object
    """
    apply_default_bgp_config(duthost, copy=True)
    yield
    try:
        apply_default_bgp_config(duthost)
    except Exception:
        config_reload(duthost, safe_reload=True, check_intf_up_ports=True)
        apply_default_bgp_config(duthost)


@pytest.fixture(scope="module")
def bgpmon_setup_teardown(ptfhost, duthosts, enum_rand_one_per_hwsku_frontend_hostname, localhost, setup_interfaces):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    connection = setup_interfaces[0]
    dut_lo_addr = connection["loopback_ip"].split("/")[0]
    peer_addr = connection['neighbor_addr'].split("/")[0]
    mg_facts = duthost.minigraph_facts(host=duthost.hostname)['ansible_facts']
    asn = mg_facts['minigraph_bgp_asn']
    # TODO: Add a common method to load BGPMON config for test_bgpmon and test_traffic_shift
    logger.info("Configuring bgp monitor session on DUT")

    bgpmon_args = {
        'db_table_name': 'BGP_MONITORS',
        'peer_addr': peer_addr,
        'asn': asn,
        'local_addr': dut_lo_addr,
        'peer_name': BGP_MONITOR_NAME
    }
    bgpmon_template = Template(open(BGPMON_TEMPLATE_FILE).read())
    duthost.copy(content=bgpmon_template.render(**bgpmon_args),
                 dest=BGPMON_CONFIG_FILE)
    # Start bgpmon on DUT
    logger.info("Starting bgpmon on DUT")
    asichost = duthost.asic_instance_from_namespace(connection['namespace'])
    asichost.write_to_config_db(BGPMON_CONFIG_FILE)

    logger.info("Starting bgp monitor session on PTF")

    # Clean up in case previous run failed to clean up.
    ptfhost.exabgp(name=BGP_MONITOR_NAME, state="absent")
    ptfhost.file(path=CUSTOM_DUMP_SCRIPT_DEST, state="absent")

    # Start bgp monitor session on PTF
    ptfhost.file(path=DUMP_FILE, state="absent")
    ptfhost.copy(src=CUSTOM_DUMP_SCRIPT, dest=CUSTOM_DUMP_SCRIPT_DEST)
    ptfhost.exabgp(name=BGP_MONITOR_NAME,
                   state="started",
                   local_ip=peer_addr,
                   router_id=peer_addr,
                   peer_ip=dut_lo_addr,
                   local_asn=asn,
                   peer_asn=asn,
                   port=BGP_MONITOR_PORT,
                   dump_script=CUSTOM_DUMP_SCRIPT_DEST)

    # Flush neighbor and route in advance to avoid possible "RTNETLINK answers: File exists"
    ptfhost.shell("ip neigh flush to %s nud permanent" % dut_lo_addr)
    ptfhost.shell("ip route del %s" % dut_lo_addr + "/32", module_ignore_errors=True)

    # Add the route to DUT loopback IP  and the interface router mac
    ptfhost.shell("ip neigh add %s lladdr %s dev %s" % (dut_lo_addr,
                                                        duthost.facts["router_mac"],
                                                        connection["neighbor_intf"]))
    ptfhost.shell("ip route add %s dev %s" % (dut_lo_addr + "/32", connection["neighbor_intf"]))

    pt_assert(wait_tcp_connection(localhost, ptfhost.mgmt_ip, BGP_MONITOR_PORT, timeout_s=60),
              "Failed to start bgp monitor session on PTF")
    if is_chassis(duthost):
        # the BGPMON session comes up when its a chassis. currently for
        # t2-single-node-min topo it doesn't comeup as the update-source
        # loopback4096 isn't present
        pt_assert(wait_until(20, 5, 0, duthost.check_bgp_session_state, [peer_addr]),
                  'BGP session {} on duthost is not established'.format(BGP_MONITOR_NAME))

    yield connection
    # Cleanup bgp monitor
    asichost.run_sonic_db_cli_cmd("CONFIG_DB DEL 'BGP_MONITORS|{}'".format(peer_addr))

    ptfhost.exabgp(name=BGP_MONITOR_NAME, state="absent")
    ptfhost.file(path=CUSTOM_DUMP_SCRIPT_DEST, state="absent")
    ptfhost.file(path=DUMP_FILE, state="absent")
    # Remove the route to DUT loopback IP  and the interface router mac
    ptfhost.shell("ip route del %s" % dut_lo_addr + "/32")
    ptfhost.shell("ip neigh flush to %s nud permanent" % dut_lo_addr)


def pytest_addoption(parser):
    """
    Adds options to pytest that are used by bgp suppress fib pending test
    """

    parser.addoption(
        "--bgp_suppress_fib_pending",
        action="store_true",
        dest="bgp_suppress_fib_pending",
        default=False,
        help="enable bgp suppress fib pending function, by default it will not enable bgp suppress fib pending function"
    )
    parser.addoption(
        "--bgp_suppress_fib_reboot_type",
        action="store",
        dest="bgp_suppress_fib_reboot_type",
        type=str,
        choices=["reload", "fast", "warm", "cold", "random"],
        default="reload",
        help="reboot type such as reload, fast, warm, cold, random"
    )
    parser.addoption(
        "--continuous_boot_times",
        action="store",
        dest="continuous_boot_times",
        type=int,
        default=3,
        help="continuous reboot time number. default is 3"
    )
    parser.addoption(
        "--max_flap_neighbor_number",
        action="store",
        dest="max_flap_neighbor_number",
        type=int,
        default=None,
        help="Max flap neighbor number, default is None"
    )
    parser.addoption(
        "--peers-per-dut",
        action="store",
        type=int,
        default=10,
        help="Number of additional BGP peers to configure per DUT for BGP peer scale test (default: 10)"
    )


@pytest.fixture(scope="module", autouse=True)
def config_bgp_suppress_fib(duthosts, rand_one_dut_hostname, request):
    """
    Enable or disable bgp suppress-fib-pending function
    """
    duthost = duthosts[rand_one_dut_hostname]
    config = request.config.getoption("--bgp_suppress_fib_pending")
    logger.info("--bgp_suppress_fib_pending:{}".format(config))

    if config:
        logger.info("Check if bgp suppress fib pending is supported")
        res = duthost.command("show suppress-fib-pending", module_ignore_errors=True)
        if res['rc'] != 0:
            pytest.skip('BGP suppress fib pending function is not supported')
        logger.info('Enable BGP suppress fib pending function')
        duthost.shell('sudo config suppress-fib-pending enabled')
        duthost.shell('sudo config save -y')

    yield

    if config:
        logger.info('Disable BGP suppress fib pending function')
        duthost.shell('sudo config suppress-fib-pending disabled')
        duthost.shell('sudo config save -y')


@pytest.fixture(scope="module")
def dut_with_default_route(duthosts, enum_rand_one_per_hwsku_frontend_hostname, tbinfo):
    if tbinfo['topo']['type'] == 't2':
        # For T2 setup, default route via eBGP is only advertised from T3 VM's which are connected to one of the
        # linecards and not the other. So, can't use enum_rand_one_per_hwsku_frontend_hostname for T2.
        dut_to_T3 = None
        for a_dut in duthosts.frontend_nodes:
            minigraph_facts = a_dut.get_extended_minigraph_facts(tbinfo)
            minigraph_neighbors = minigraph_facts['minigraph_neighbors']
            for key, value in list(minigraph_neighbors.items()):
                if 'T3' in value['name']:
                    dut_to_T3 = a_dut
                    break
            if dut_to_T3:
                break
        if dut_to_T3 is None:
            pytest.fail("Did not find any DUT in the DUTs (linecards) that are connected to T3 VM's")
        return dut_to_T3
    else:
        return duthosts[enum_rand_one_per_hwsku_frontend_hostname]


@pytest.fixture(scope="module")
def set_timeout_for_bgpmon(duthost):
    """
    For chassis testbeds, we need to specify plt_reboot_ctrl in inventory file,
    to let MAX_TIME_TO_REBOOT to be overwritten by specified timeout value
    """
    global MAX_TIME_FOR_BGPMON
    plt_reboot_ctrl = get_plt_reboot_ctrl(duthost, 'test_bgpmon.py', 'cold')
    if plt_reboot_ctrl:
        MAX_TIME_FOR_BGPMON = plt_reboot_ctrl.get('timeout', 180)


@pytest.fixture(scope="module")
def is_quagga(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Return True if current bgp is using Quagga."""
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    show_res = duthost.asic_instance().run_vtysh("-c 'show version'")
    return "Quagga" in show_res["stdout"]


@pytest.fixture(scope="module")
def is_dualtor(tbinfo):
    return "dualtor" in tbinfo["topo"]["name"]


@pytest.fixture(scope="module")
def traffic_shift_community(duthost):
    community = duthost.shell('sonic-cfggen -y /etc/sonic/constants.yml -v constants.bgp.traffic_shift_community')[
        'stdout']
    return community


@pytest.fixture(scope='module')
def get_function_completeness_level(pytestconfig):
    return pytestconfig.getoption("--completeness_level")
