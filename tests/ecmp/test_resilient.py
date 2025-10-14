import pytest

import time
import logging
import ipaddress
import json
import six
from collections import defaultdict
from tests.ptf_runner import ptf_runner
from tests.common import config_reload
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.constants import DEFAULT_NAMESPACE

from tests.common.fixtures.ptfhost_utils import copy_ptftests_directory   # noqa F401
from tests.common.fixtures.ptfhost_utils import change_mac_addresses      # noqa F401
from tests.common.fixtures.ptfhost_utils import remove_ip_addresses       # noqa F401
from tests.common.fixtures.ptfhost_utils import copy_arp_responder_py     # noqa F401

# Constants
NUM_NHs = 8
DEFAULT_VLAN_ID = 1000
DEFAULT_VLAN_IPv4 = ipaddress.ip_network(u'200.200.200.0/28')
DEFAULT_VLAN_IPv6 = ipaddress.ip_network(u'200:200:200:200::/124')
PREFIX_IPV4_LIST = [u'100.50.25.12/32', u'100.50.25.13/32', u'100.50.25.14/32']
PREFIX_IPV6_LIST = [u'fc:05::/128', u'fc:06::/128', u'fc:07::/128']
RESILIENT_ECMP_CFG = '/tmp/resilient_ecmp.json'
NUM_FLOWS = 200
ptf_to_dut_port_map = {}
ptf_to_dut_mac_map = {}

pytestmark = [
    pytest.mark.topology('t0'),
    pytest.mark.asic('broadcom'),
    pytest.mark.disable_loganalyzer
]

logger = logging.getLogger(__name__)


def configure_interfaces(cfg_facts, duthost, ptfhost, vlan_ip):
    config_port_indices = cfg_facts['port_index_map']
    port_list = []
    eth_port_list = []
    ip_to_port = {}
    global ptf_to_dut_port_map

    vlan_members = cfg_facts.get('VLAN_MEMBER', {})
    index = 0
    for vlan in list(cfg_facts['VLAN_MEMBER'].keys()):
        vlan_id = vlan[4:]
        DEFAULT_VLAN_ID = int(vlan_id)
        if len(port_list) == NUM_NHs:
            break
        for port in vlan_members[vlan]:
            if len(port_list) == NUM_NHs:
                break
            ptf_port_id = config_port_indices[port]
            port_list.append(ptf_port_id)
            eth_port_list.append(port)
            index = index + 1
            ptf_to_dut_port_map[ptf_port_id] = port

    port_list.sort()

    # Create vlan if
    duthost.command('config interface ip add Vlan' + str(DEFAULT_VLAN_ID) + ' ' + str(vlan_ip))

    for index, ip in enumerate(vlan_ip.hosts()):
        if len(ip_to_port) == NUM_NHs:
            break
        ip_to_port[str(ip)] = port_list[index]

    return port_list, ip_to_port


def generate_fgnhg_config(duthost, ip_to_port, prefixes):
    if '.' in list(ip_to_port.keys())[0]:
        fgnhg_name = 'fgnhg_v4'
    else:
        fgnhg_name = 'fgnhg_v6'

    fgnhg_data = {}

    fgnhg_data['FG_NHG'] = {}
    fgnhg_data['FG_NHG'][fgnhg_name] = {
        "bucket_size": 64,
        "match_mode": "prefix-based",
        "max_next_hops": 16
    }

    fgnhg_data['FG_NHG_PREFIX'] = {}
    for prefix in prefixes:
        fgnhg_data['FG_NHG_PREFIX'][prefix] = {
            "FG_NHG": fgnhg_name
        }

    logger.info("fgnhg entries programmed to DUT " + str(fgnhg_data))
    duthost.copy(content=json.dumps(fgnhg_data, indent=2), dest="/tmp/fgnhg.json")
    duthost.shell("sonic-cfggen -j /tmp/fgnhg.json --write-to-db")


def setup_neighbors(duthost, ptfhost, ip_to_port):
    vlan_name = "Vlan" + str(DEFAULT_VLAN_ID)
    neigh_entries = {}
    neigh_entries['NEIGH'] = {}

    for ip, port in list(ip_to_port.items()):

        neigh_mac = ptfhost.shell("cat /sys/class/net/eth" + str(port) + "/address")["stdout_lines"][0]
        ptf_to_dut_mac_map[port] = neigh_mac
        if isinstance(ipaddress.ip_address(six.text_type(ip)), ipaddress.IPv4Address):
            neigh_entries['NEIGH'][vlan_name + "|" + ip] = {
                "neigh": neigh_mac,
                "family": "IPv4"
            }
        else:
            neigh_entries['NEIGH'][vlan_name + "|" + ip] = {
                "neigh": neigh_mac,
                "family": "IPv6"
            }

    logger.info("neigh entries programmed to DUT " + str(neigh_entries))
    duthost.copy(content=json.dumps(neigh_entries, indent=2), dest="/tmp/neigh.json")
    duthost.shell("sonic-cfggen -j /tmp/neigh.json --write-to-db")


def setup_arpresponder(ptfhost, ip_to_port):
    logger.info("Copy arp_responder to ptfhost")
    # Stop existing arp responder if running
    ptfhost.command('supervisorctl stop arp_responder', module_ignore_errors=True)

    d = defaultdict(list)

    for ip, port in list(ip_to_port.items()):
        iface = "eth{}".format(port)
        d[iface].append(ip)

    with open('/tmp/from_t1.json', 'w') as file:
        json.dump(d, file)

    ptfhost.copy(src='/tmp/from_t1.json', dest='/tmp/from_t1.json')

    extra_vars = {
            'arp_responder_args': ''
    }

    ptfhost.host.options['variable_manager'].extra_vars.update(extra_vars)
    ptfhost.template(src='templates/arp_responder.conf.j2', dest='/tmp')
    ptfhost.command("cp /tmp/arp_responder.conf.j2 /etc/supervisor/conf.d/arp_responder.conf")

    ptfhost.command('supervisorctl reread')
    ptfhost.command('supervisorctl update')

    logger.info("Start arp_responder")
    ptfhost.command('supervisorctl start arp_responder')


def create_rh_ptf_config(ptfhost, ip_to_port, port_list, router_mac, net_ports):
    rh_ecmp = {
            "serv_ports": port_list,
            "port_list": port_list,
            "dut_mac": router_mac,
            "net_ports": net_ports,
            "num_flows": NUM_FLOWS,
    }

    logger.info("rh_ecmp config sent to PTF: " + str(rh_ecmp))
    ptfhost.copy(content=json.dumps(rh_ecmp, indent=2), dest=RESILIENT_ECMP_CFG)


def setup_test_config(duthost, ptfhost, cfg_facts, router_mac, net_ports, vlan_ip, prefixes):
    port_list, ip_to_port = configure_interfaces(cfg_facts, duthost, ptfhost, vlan_ip)
    generate_fgnhg_config(duthost, ip_to_port, prefixes)
    setup_arpresponder(ptfhost, ip_to_port)

    time.sleep(60)
    create_rh_ptf_config(ptfhost, ip_to_port, port_list, router_mac, net_ports)
    return port_list, ip_to_port


def configure_dut(duthost, cmd):
    logger.info("Configuring dut with " + cmd)
    duthost.shell(cmd, executable="/bin/bash")


def partial_ptf_runner(ptfhost, test_case, dst_ip, exp_flow_count, **kwargs):
    log_file = "/tmp/resilient_ecmp_test.ResilientEcmpTest.{}".format(test_case)
    params = {
                "test_case": test_case,
                "dst_ip": dst_ip,
                "exp_flow_count": exp_flow_count,
                "config_file": RESILIENT_ECMP_CFG
             }
    params.update(kwargs)

    ptf_runner(ptfhost,
               "ptftests",
               "resilient_ecmp_test.ResilientEcmpTest",
               platform_dir="ptftests",
               params=params,
               qlen=1000,
               log_file=log_file,
               is_python3=True)


def validate_packet_flow_without_neighbor_resolution(ptfhost, duthost, ip_to_port, prefix_list):
    logger.info("Validating packet flow of fine grained ecmp without neighbor resolution")
    # Init base test params
    if isinstance(ipaddress.ip_network(prefix_list[0]), ipaddress.IPv4Network):
        ipcmd = "ip route"
    else:
        ipcmd = "ipv6 route"

    vtysh_base_cmd = "vtysh -c 'configure terminal'"
    vtysh_base_cmd = duthost.get_vtysh_cmd_for_namespace(vtysh_base_cmd, DEFAULT_NAMESPACE)
    dst_ip = prefix_list[0].split('/')[0]

    cmd = vtysh_base_cmd
    for nexthop in ip_to_port:
        cmd = cmd + " -c '{} {} {}'".format(ipcmd, prefix_list[0], nexthop)
    configure_dut(duthost, cmd)

    # Validate packet flow works
    partial_ptf_runner(ptfhost, 'verify_packets_received', dst_ip, [])

    # Validate that neigh was resolved as part of packet flow
    if isinstance(ipaddress.ip_network(prefix_list[0]), ipaddress.IPv4Network):
        show_neigh = duthost.shell("show arp")['stdout']
    else:
        show_neigh = duthost.shell("show ndp")['stdout']

    neigh_resolved = False

    for nexthop in ip_to_port:
        if nexthop in show_neigh:
            neigh_resolved = True
            break
    assert neigh_resolved


def setup_static_neighbor_entry(duthost, ip, mac, prefix_list):
    """
    Performs addition of static entries of ipv4 and v6 neighbors in DUT
    """
    if isinstance(ipaddress.ip_network(prefix_list[0]), ipaddress.IPv4Network):
        logger.info("adding ipv4 static arp entry for ip %s on DUT" % (ip))
        duthost.shell("sudo arp -s {0} {1}".format(ip, mac))
    else:
        logger.info("adding ipv6 static arp entry for ip %s on DUT" % (ip))
        duthost.shell("sudo ip -6 neigh replace {0} lladdr {1} dev Vlan{2}".format(ip, mac, DEFAULT_VLAN_ID))


def link_startup(duthost, ip_to_port, prefix_list, shutdown_link):
    """
    Performs link startup on DUT
    """
    dut_if_shutdown = ptf_to_dut_port_map[shutdown_link]
    configure_dut(duthost, "config interface startup " + dut_if_shutdown)

    # add static neighbor
    for nexthop, port in list(ip_to_port.items()):
        if port == shutdown_link:
            setup_static_neighbor_entry(duthost, nexthop, ptf_to_dut_mac_map[port], prefix_list)

    time.sleep(30)


def compute_exp_flow_count(port_list, down_links, exp_flows=NUM_FLOWS):
    exp_flow_count = {}
    down_link_count = len(down_links)
    exp_flows += down_link_count * (exp_flows / (len(port_list) - down_link_count))
    for port in port_list:
        exp_flow_count[port] = exp_flows
    for link in down_links:
        del exp_flow_count[link]
    return exp_flow_count


def resilient_ecmp(ptfhost, duthost, router_mac, net_ports, port_list, ip_to_port, prefix_list):

    # Init base test params
    if isinstance(ipaddress.ip_network(prefix_list[0]), ipaddress.IPv4Network):
        ipcmd = "ip route"
    else:
        ipcmd = "ipv6 route"

    vtysh_base_cmd = "vtysh -c 'configure terminal'"
    vtysh_base_cmd = duthost.get_vtysh_cmd_for_namespace(vtysh_base_cmd, DEFAULT_NAMESPACE)
    dst_ip_list = []
    for prefix in prefix_list:
        dst_ip_list.append(prefix.split('/')[0])

    # Start test in state where 1 link is down, when nexthop addition occurs for link which is down, the nexthop
    # should not go to active
    shutdown_link = port_list[0]
    dut_if_shutdown = ptf_to_dut_port_map[shutdown_link]
    logger.info("Initialize test by creating flows and checking basic ecmp, "
                "we start in a state where link " + dut_if_shutdown + " is down")

    configure_dut(duthost, "config interface shutdown " + dut_if_shutdown)
    time.sleep(30)

    # Now add the route and nhs
    for prefix in prefix_list:
        cmd = vtysh_base_cmd
        for nexthop in ip_to_port:
            cmd = cmd + " -c '{} {} {}'".format(ipcmd, prefix, nexthop)
        configure_dut(duthost, cmd)

    time.sleep(3)

    # Calculate expected flow counts per port to verify in ptf host
    exp_flow_count = compute_exp_flow_count(port_list, [shutdown_link])

    # Send the packets with expected pkts from each egress port is NUM_FLOWS
    for dst_ip in dst_ip_list:
        partial_ptf_runner(ptfhost, 'create_flows', dst_ip, exp_flow_count)

    # Hashing verification: Send the same flows again,
    # and verify packets end up on the same ports for a given flow
    logger.info("Hashing verification: Send the same flows again, "
                "and verify packets end up on the same ports for a given flow")

    for dst_ip in dst_ip_list:
        partial_ptf_runner(ptfhost, 'initial_hash_check', dst_ip, exp_flow_count)

    # Send the same flows again, but unshut the port which was shutdown at the beginning of test
    # Check if hash buckets rebalanced as expected
    logger.info("Send the same flows again, but unshut " + dut_if_shutdown + " and check "
                "if flows rebalanced as expected and are seen on now brought up link")

    link_startup(duthost, ip_to_port, prefix_list, shutdown_link)

    exp_flow_count = compute_exp_flow_count(port_list, [])

    for dst_ip in dst_ip_list:
        partial_ptf_runner(ptfhost, 'add_nh', dst_ip, exp_flow_count, add_nh_port=shutdown_link)

    # Send the same flows again, but withdraw one next-hop before sending the flows, check if hash bucket
    # rebalanced as expected, and the number of flows received on a link is as expected
    logger.info("Send the same flows again, but withdraw one next-hop before sending the flows, check if hash bucket "
                "rebalanced as expected, and the number of flows received on a link is as expected")

    # Modify and test 1 prefix only for the rest of this test
    dst_ip = dst_ip_list[0]
    prefix = prefix_list[0]

    withdraw_nh_port = port_list[1]
    cmd = vtysh_base_cmd
    for nexthop, port in list(ip_to_port.items()):
        if port == withdraw_nh_port:
            cmd = cmd + " -c 'no {} {} {}'".format(ipcmd, prefix, nexthop)
    configure_dut(duthost, cmd)
    time.sleep(3)

    exp_flow_count = compute_exp_flow_count(port_list, [withdraw_nh_port])

    # Validate packets with withdrawn nhs
    partial_ptf_runner(ptfhost, 'withdraw_nh', dst_ip, exp_flow_count, withdraw_nh_port=withdraw_nh_port)

    exp_flow_count = compute_exp_flow_count(port_list, [])

    # Validate that the other 2 prefixes using Fine Grained ECMP were unaffected
    for ip in dst_ip_list:
        if ip == dst_ip:
            continue
        partial_ptf_runner(ptfhost, 'initial_hash_check', ip, exp_flow_count)

    # Send the same flows again, but disable one of the links,
    # and check flow hash redistribution
    shutdown_link = port_list[2]
    dut_if_shutdown = ptf_to_dut_port_map[shutdown_link]
    logger.info("Send the same flows again, but shutdown " + dut_if_shutdown + " and check "
                "the flow hash redistribution")

    configure_dut(duthost, "config interface shutdown " + dut_if_shutdown)
    time.sleep(30)

    exp_flow_count = compute_exp_flow_count(port_list, [shutdown_link, withdraw_nh_port])

    partial_ptf_runner(ptfhost, 'withdraw_nh', dst_ip, exp_flow_count, withdraw_nh_port=shutdown_link)

    # Send the same flows again, but enable the link we disabled the last time
    # and check flow hash redistribution
    logger.info("Send the same flows again, but startup " + dut_if_shutdown + " and check "
                "the flow hash redistribution")

    link_startup(duthost, ip_to_port, prefix_list, shutdown_link)

    exp_flow_count = compute_exp_flow_count(port_list, [withdraw_nh_port])

    partial_ptf_runner(ptfhost, 'add_nh', dst_ip, exp_flow_count, add_nh_port=shutdown_link)

    # Send the same flows again, but enable the next-hop which was down previously
    # and check flow hash redistribution
    logger.info("Send the same flows again, but enable the next-hop which was down previously "
                " and check flow hash redistribution")

    cmd = vtysh_base_cmd
    for nexthop, port in list(ip_to_port.items()):
        if port == withdraw_nh_port:
            cmd = cmd + " -c '{} {} {}'".format(ipcmd, prefix, nexthop)
    configure_dut(duthost, cmd)
    time.sleep(3)

    exp_flow_count = compute_exp_flow_count(port_list, [])

    partial_ptf_runner(ptfhost, 'add_nh', dst_ip, exp_flow_count, add_nh_port=withdraw_nh_port)

    # Simulate route and link flap conditions by toggling the route
    # and ensure that there is no orch crash and data plane impact
    logger.info("Simulate route and link flap conditions by toggling the route "
                "and ensure that there is no orch crash and data plane impact")
    nexthop_to_toggle = list(ip_to_port.keys())[0]

    cmd = "for i in {1..50}; do "
    cmd = cmd + vtysh_base_cmd
    cmd = cmd + "  -c 'no {} {} {}';".format(ipcmd, prefix, nexthop_to_toggle)
    cmd = cmd + " sleep 0.5;"
    cmd = cmd + vtysh_base_cmd
    cmd = cmd + "  -c '{} {} {}';".format(ipcmd, prefix, nexthop_to_toggle)
    cmd = cmd + " sleep 0.5;"
    cmd = cmd + " done;"

    configure_dut(duthost, cmd)
    time.sleep(30)

    result = duthost.shell(argv=["pgrep", "orchagent"])
    pytest_assert(int(result["stdout"]) > 0, "Orchagent is not running")
    partial_ptf_runner(ptfhost, 'bank_check', dst_ip, exp_flow_count)

    logger.info("Completed ...")


def rh_ecmp_to_regular_ecmp_transitions(ptfhost, duthost, router_mac,
                                        net_ports, port_list, ip_to_port,
                                        prefix_list, cfg_facts):
    # Entry condition :
    #  - FG-NHG is already configured on all prefixes in prefix_list
    #  - Routes for all prefixes in prefix_list are setup.
    logger.info("rh_ecmp_to_regular_ecmp_transitions")

    # Init base test params
    ipv4 = False
    if isinstance(ipaddress.ip_network(prefix_list[0]), ipaddress.IPv4Network):
        ipcmd = "ip route"
        ipv4 = True
    else:
        ipcmd = "ipv6 route"

    vtysh_base_cmd = "vtysh -c 'configure terminal'"
    vtysh_base_cmd = duthost.get_vtysh_cmd_for_namespace(vtysh_base_cmd, DEFAULT_NAMESPACE)
    dst_ip_list = []
    for prefix in prefix_list:
        dst_ip_list.append(prefix.split('/')[0])

    prefix = prefix_list[0]
    dst_ip = dst_ip_list[0]

    # Init flows for non-dst_ip prefixes
    exp_flow_count = compute_exp_flow_count(port_list, [])
    for ip in dst_ip_list:
        if ip == dst_ip:
            continue
        partial_ptf_runner(ptfhost, 'create_flows', ip, exp_flow_count)

    logger.info("Transition prefix to non fine grained ecmp and validate packets")

    pc_ips = []
    for ip in cfg_facts['BGP_NEIGHBOR']:
        if ipv4 and '.' in ip:
            pc_ips.append(ip)
        elif not ipv4 and ':' in ip:
            pc_ips.append(ip)

    configure_dut(duthost, "config fg-nhg-prefix delete " + prefix)

    cmd = vtysh_base_cmd
    for ip in pc_ips:
        cmd = cmd + " -c '{} {} {}'".format(ipcmd, prefix, ip)
    for nexthop in list(ip_to_port.keys()):
        cmd = cmd + " -c 'no {} {} {}'".format(ipcmd, prefix, nexthop)
    configure_dut(duthost, cmd)

    time.sleep(3)

    exp_flows = (len(port_list) * NUM_FLOWS) / len(net_ports)
    exp_flow_count = compute_exp_flow_count(net_ports, [], exp_flows)

    partial_ptf_runner(ptfhost, 'net_port_hashing', dst_ip, exp_flow_count)

    # Validate that the other 2 prefixes using Fine Grained ECMP were unaffected
    exp_flow_count = compute_exp_flow_count(port_list, [])
    for ip in dst_ip_list:
        if ip == dst_ip:
            continue
        partial_ptf_runner(ptfhost, 'initial_hash_check', ip, exp_flow_count)

    # Transition prefix back to fine grained ecmp and validate packets
    logger.info("Transition prefix back to fine grained ecmp and validate packets")

    cmd = vtysh_base_cmd
    for nexthop in list(ip_to_port.keys()):
        cmd = cmd + " -c '{} {} {}'".format(ipcmd, prefix, nexthop)
    for ip in pc_ips:
        cmd = cmd + " -c 'no {} {} {}'".format(ipcmd, prefix, ip)
    configure_dut(duthost, cmd)
    time.sleep(3)

    partial_ptf_runner(ptfhost, 'create_flows', dst_ip, exp_flow_count)

    # Validate that the other 2 prefixes using Fine Grained ECMP were unaffected
    for ip in dst_ip_list:
        if ip == dst_ip:
            continue
        partial_ptf_runner(ptfhost, 'initial_hash_check', ip, exp_flow_count)


def cleanup(duthost, ptfhost):
    logger.info("Start cleanup")
    ptfhost.command('rm -f /tmp/resilient_ecmp_persist_map.json')
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True)


@pytest.fixture(scope="module")
def common_setup_teardown(tbinfo, duthosts, rand_one_dut_hostname, ptfhost):
    duthost = duthosts[rand_one_dut_hostname]

    try:
        mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
        cfg_facts = duthost.config_facts(host=duthost.hostname, source="persistent")['ansible_facts']
        router_mac = duthost.facts['router_mac']
        net_ports = []
        for name, val in list(mg_facts['minigraph_portchannels'].items()):
            members = [mg_facts['minigraph_ptf_indices'][member] for member in val['members']]
            net_ports.extend(members)

        yield duthost, cfg_facts, router_mac, net_ports

    finally:
        cleanup(duthost, ptfhost)


def test_resilient_ecmp(common_setup_teardown, ptfhost):
    duthost, cfg_facts, router_mac, net_ports = common_setup_teardown

    # IPv4 test
    port_list, ipv4_to_port = setup_test_config(duthost, ptfhost, cfg_facts,
                                                router_mac, net_ports,
                                                DEFAULT_VLAN_IPv4, PREFIX_IPV4_LIST)
    validate_packet_flow_without_neighbor_resolution(ptfhost, duthost, ipv4_to_port,
                                                     PREFIX_IPV4_LIST)
    setup_neighbors(duthost, ptfhost, ipv4_to_port)
    resilient_ecmp(ptfhost, duthost, router_mac, net_ports, port_list,
                   ipv4_to_port, PREFIX_IPV4_LIST)
    rh_ecmp_to_regular_ecmp_transitions(ptfhost, duthost, router_mac,
                                        net_ports, port_list, ipv4_to_port,
                                        PREFIX_IPV4_LIST, cfg_facts)

    # IPv6 test
    port_list, ipv6_to_port, = setup_test_config(duthost, ptfhost, cfg_facts,
                                                 router_mac, net_ports,
                                                 DEFAULT_VLAN_IPv6, PREFIX_IPV6_LIST)
    validate_packet_flow_without_neighbor_resolution(ptfhost, duthost, ipv6_to_port,
                                                     PREFIX_IPV6_LIST)
    setup_neighbors(duthost, ptfhost, ipv6_to_port)
    resilient_ecmp(ptfhost, duthost, router_mac, net_ports, port_list,
                   ipv6_to_port, PREFIX_IPV6_LIST)
    rh_ecmp_to_regular_ecmp_transitions(ptfhost, duthost, router_mac,
                                        net_ports, port_list, ipv6_to_port,
                                        PREFIX_IPV6_LIST, cfg_facts)
