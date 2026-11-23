"""
Tests sub-port interfaces in SONiC.
"""

import random
import pytest

from ipaddress import ip_network

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from sub_ports_helpers import generate_and_verify_traffic
from sub_ports_helpers import get_port_mtu
from sub_ports_helpers import shutdown_port
from sub_ports_helpers import startup_port
from sub_ports_helpers import setup_vlan
from sub_ports_helpers import remove_vlan
from sub_ports_helpers import check_sub_port
from sub_ports_helpers import remove_sub_port
from sub_ports_helpers import create_sub_port_on_dut
from sub_ports_helpers import check_interface_ip
from sub_ports_helpers import check_ip_reachable
from sub_ports_helpers import add_ip_to_dut_port
from sub_ports_helpers import get_portchannel_members

pytestmark = [
    pytest.mark.topology('t0', 't1')
]

PTF_PORT_MAPPING_MODE = 'use_orig_interface'


class TestSubPorts(object):
    """
    TestSubPorts class for testing sub-port interfaces
    """
    def test_packet_routed_with_valid_vlan(self, duthost, ptfhost, ptfadapter, apply_config_on_the_dut,
                                           apply_config_on_the_ptf):
        """
        Validates that packet routed if sub-ports have valid VLAN ID.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Create ICMP packet.
            4.) Send ICMP request packet from PTF to DUT.
            5.) Verify that DUT sends ICMP reply packet to PTF.
            6.) Clear configuration of sub-ports on the DUT.
            7.) Clear configuration of sub-ports on the DUT.

        Pass Criteria: PTF gets ICMP reply packet from DUT.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        for sub_port, value in list(sub_ports.items()):
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='fwd')

    def test_untagged_packet_not_routed(self, duthost, ptfadapter, apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates that untagged packet aren't routed.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Create untagged ICMP packet.
            4.) Send untagged ICMP request packet from PTF to DUT.
            5.) Verify that DUT doesn't sends ICMP reply packet to PTF.
            6.) Clear configuration of sub-ports on the DUT.
            7.) Clear configuration of sub-ports on the DUT.

        Pass Criteria: PTF doesn't gets ICMP reply packet from DUT.
        """
        sub_ports = apply_config_on_the_dut["sub_ports"]

        for sub_port, config in list(sub_ports.items()):
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=config["neighbor_port"],
                                        dst_port=sub_port,
                                        ip_src=config["neighbor_ip"],
                                        ip_dst=config["ip"],
                                        pkt_action="drop",
                                        untagged_icmp_request=True
                                        )

    def test_admin_status_down_disables_forwarding(self, duthost, ptfadapter, apply_config_on_the_dut,
                                                   apply_config_on_the_ptf):
        """
        Validates that admin status DOWN disables packet forwarding.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Shutdown sub-ports on the DUT
            4.) Create ICMP packet.
            5.) Send ICMP request packet from PTF to DUT.
            6.) Verify that DUT doesn't send ICMP reply packet to PTF.
            7.) Create ICMP packet.
            8.) Send ICMP request packet from PTF to another sub-port of DUT.
            9.) Verify that DUT sends ICMP reply packet to PTF.
            10.) Startup sub-port on the DUT
            11.) Create ICMP packet.
            12.) Send ICMP request packet from PTF to DUT.
            13.) Verify that DUT sends ICMP reply packet to PTF.
            14.) Clear configuration of sub-ports on the DUT.
            15.) Clear configuration of sub-ports on the PTF.

        Pass Criteria: PTF doesn't get ICMP reply packet from disabled sub-ports of DUT.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        for sub_port, value in list(sub_ports.items()):
            shutdown_port(duthost, sub_port)
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='drop')

            for next_sub_port, next_value in list(sub_ports.items()):
                if next_sub_port != sub_port:
                    generate_and_verify_traffic(duthost=duthost,
                                                ptfadapter=ptfadapter,
                                                src_port=next_value['neighbor_port'],
                                                ip_src=next_value['neighbor_ip'],
                                                dst_port=next_sub_port,
                                                ip_dst=next_value['ip'],
                                                pkt_action='fwd')

            startup_port(duthost, sub_port)
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='fwd')

    def test_mtu_inherited_from_parent_port(self, duthost, apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates that MTU settings of sub-ports inherited from parent port

        Test steps:
            1.) Setup correct configuration of sub-ports on the DUT.
            3.) Get MTU value of sub-port
            4.) Get MTU value of parent port
            6.) Clear configuration of sub-ports on the DUT.

        Pass Criteria: MTU settings of sub-ports inherited from parent port.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        for sub_port in list(sub_ports.keys()):
            sub_port_mtu = int(get_port_mtu(duthost, sub_port))
            # Get name of parent port from name of sub-port
            port = sub_port.split('.')[0]
            port_mtu = int(get_port_mtu(duthost, port))

            pytest_assert(sub_port_mtu == port_mtu, "MTU of {} doesn't inherit MTU of {}".format(sub_port, port))

    def test_vlan_config_impact(self, duthost, ptfadapter, apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates that removal of VLAN doesn't impact sub-port RIF with same VLAN ID.

        Test steps:
            1.) Setup correct configuration of sub-ports on the DUT.
            3.) Create a VLAN RIF with the same VLAN ID of sub-port.
            4.) Added PortChannel interface to VLAN members.
            5.) Delete a VLAN RIF.
            6.) Make sure sub-port is available in redis-db.
            7.) Verify that DUT sends ICMP reply packet to PTF.
            8.) Clear configuration of sub-ports on the DUT.
            9.) Clear configuration of sub-ports on the PTF.

        Pass Criteria:
            1.) Sub-port is available in redis-db.
            2.) PTF gets ICMP reply packet from DUT.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        for sub_port, value in list(sub_ports.items()):
            # Get VLAN ID from name of sub-port
            vlan_vid = int(sub_port.split('.')[1])
            # Create a VLAN RIF
            setup_vlan(duthost, vlan_vid)
            # Delete a VLAN RIF
            remove_vlan(duthost, vlan_vid)

            pytest_assert(check_sub_port(duthost, sub_port), "Sub-port {} was deleted".format(sub_port))

            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='fwd')

    @pytest.mark.parametrize("type_of_traffic", ['TCP-UDP-ICMP', ])
    def test_routing_between_sub_ports(self, type_of_traffic, duthost, ptfadapter, apply_route_config):
        """
        Validates that packets are routed between sub-ports.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Add one of the sub-ports to namespace on the PTF.
            4.) Setup static routes between sub-port and sub-port in namespace on the PTF
            5.) Create packet (TCP, UDP or ICMP).
            6.) Send packet from sub-port to sub-port in namespace on the PTF.
            7.) Verify that sub-port gets received packet on the PTF.
            8.) Remove static routes from PTF
            9.) Remove namespaces from PTF
            10.) Clear configuration of sub-ports on the DUT.
            11.) Clear configuration of sub-ports on the PTF.

        Note:
            Test verifies two cases of routing between sub-ports:
                1.) Routing between sub-ports on the same port
                2.) Routing between sub-ports on the different ports

        Pass Criteria: PTF port gets ICMP reply packet from port in namespace on the PTF.
        """
        new_sub_ports = apply_route_config['new_sub_ports']
        sub_ports = apply_route_config['sub_ports']
        type_of_traffic = type_of_traffic.split('-')

        for src_port, next_hop_sub_ports in list(new_sub_ports.items()):
            for sub_port, _ in next_hop_sub_ports:
                generate_and_verify_traffic(duthost=duthost,
                                            ptfadapter=ptfadapter,
                                            src_port=sub_ports[src_port]['neighbor_port'],
                                            ip_src=sub_ports[src_port]['neighbor_ip'],
                                            dst_port=sub_ports[sub_port]['neighbor_port'],
                                            ip_dst=sub_ports[sub_port]['neighbor_ip'],
                                            pkt_action='fwd',
                                            type_of_traffic=type_of_traffic,
                                            ttl=63)

    @pytest.mark.parametrize("type_of_traffic", ["TCP-UDP-ICMP", ])
    def test_routing_between_sub_ports_unaffected_by_sub_ports_removal(self, type_of_traffic, duthost, ptfadapter,
                                                                       apply_route_config):
        """
        Validates that the routing of packets between sub-ports are not affected by the removal of other sub ports.

        Test steps:
        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Add one of the sub-ports to namespace on the PTF.
            4.) Setup static routes between sub-port and sub-port in namespace on the PTF
            5.) Create packet (TCP, UDP or ICMP).
            6.) Remove some other sub ports
            7.) Send packet from sub-port to sub-port in namespace on the PTF.
            8.) Verify that sub-port gets received packet on the PTF.
            9.) Remove static routes from PTF
            10.) Remove namespaces from PTF
            11.) Clear configuration of sub-ports on the DUT.
            12.) Clear configuration of sub-ports on the PTF.

        Note:
            Test verifies two cases of routing between sub-ports:
                1.) Routing between sub-ports on the same port
                2.) Routing between sub-ports on the different ports

        Pass Criteria: PTF port gets packets from port in namespace on the PTF.
        """
        new_sub_ports = apply_route_config['new_sub_ports']
        sub_ports = apply_route_config['sub_ports']
        type_of_traffic = type_of_traffic.split('-')

        # find to-be-removed sub ports
        sub_ports_to_remove = set(sub_ports.keys())
        for sub_port, next_hop_sub_ports in list(new_sub_ports.items()):
            sub_ports_to_remove.remove(sub_port)
            for next_hop_sub_port, _ in next_hop_sub_ports:
                sub_ports_to_remove.remove(next_hop_sub_port)

        # remove those to-be-removed sub ports
        for sub_port in sub_ports_to_remove:
            remove_sub_port(duthost, sub_port, sub_ports[sub_port]["ip"])

        for src_port, next_hop_sub_ports in list(new_sub_ports.items()):
            for sub_port, _ in next_hop_sub_ports:
                generate_and_verify_traffic(duthost=duthost,
                                            ptfadapter=ptfadapter,
                                            src_port=sub_ports[src_port]['neighbor_port'],
                                            ip_src=sub_ports[src_port]['neighbor_ip'],
                                            dst_port=sub_ports[sub_port]['neighbor_port'],
                                            ip_dst=sub_ports[sub_port]['neighbor_ip'],
                                            pkt_action='fwd',
                                            type_of_traffic=type_of_traffic,
                                            ttl=63)

        for sub_port in sub_ports_to_remove:
            create_sub_port_on_dut(duthost, sub_port, sub_ports[sub_port]["ip"])

    @pytest.mark.parametrize("type_of_traffic", ['TCP-UDP-ICMP', ])
    def test_routing_between_sub_ports_and_port(self, request, type_of_traffic, duthost, ptfadapter,
                                                apply_route_config_for_port):
        """
        Validates that packets are routed between sub-ports.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Setup L3 RIF or SVI on the DUT.
            4.) Setup neighbor port for L3 RIF or SVI on the PTF.
            5.) Add one of the sub-ports to namespace on the PTF.
            6.) Setup static routes between port and sub-port in namespace on the PTF
            7.) Create packet (TCP, UDP or ICMP).
            8.) Send packet from sub-port to sub-port in namespace on the PTF.
            9.) Verify that sub-port gets received packet on the PTF.
            10.) Remove static routes from PTF
            11.) Remove namespaces from PTF
            12.) Clear configuration of sub-ports on the DUT.
            13.) Clear configuration of sub-ports on the PTF.

        Note:
            Test verifies two cases of routing between sub-ports:
                1.) Routing between sub-ports and L3 RIF.
                2.) Routing between sub-ports and SVI.

        Pass Criteria: PTF port gets ICMP reply packet from port in namespace on the PTF.
        """
        pktlen = 100
        port_map = apply_route_config_for_port['port_map']
        sub_ports = apply_route_config_for_port['sub_ports']
        type_of_traffic = type_of_traffic.split('-')

        if 'l3' in request.node.name:
            pktlen = 104

        for src_port, next_hop_sub_ports in list(port_map.items()):
            for sub_port, _ in next_hop_sub_ports['dst_ports']:
                generate_and_verify_traffic(duthost=duthost,
                                            ptfadapter=ptfadapter,
                                            src_port=src_port,
                                            ip_src=next_hop_sub_ports['ip'],
                                            dst_port=sub_ports[sub_port]['neighbor_port'],
                                            ip_dst=sub_ports[sub_port]['neighbor_ip'],
                                            pkt_action='fwd',
                                            type_of_traffic=type_of_traffic,
                                            ttl=63,
                                            pktlen=pktlen)

    def test_tunneling_between_sub_ports(self, duthost, ptfadapter, apply_tunnel_table_to_dut,
                                         apply_route_config):
        """
        Validates that packets are routed between sub-ports.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Add one of the sub-ports to namespace on the PTF.
            4.) Setup tunnel configuration on sub-ports of the DUT.
            5.) Create encapsulated packet.
            6.) Send encapsulated packet from sub-port to sub-port in namespace on the PTF.
            7.) Verify that sub-port in namespace gets decapsulated packet on the PTF.
            8.) Remove namespaces from PTF.
            9.) Remove tunnel configuration from PTF.
            10.) Clear configuration of sub-ports on the DUT.
            11.) Clear configuration of sub-ports on the PTF.

        Pass Criteria: PTF port gets decapsulated packet from port in namespace on the PTF.
        """
        new_sub_ports = apply_route_config['new_sub_ports']
        sub_ports = apply_route_config['sub_ports']

        for src_port, next_hop_sub_ports in list(new_sub_ports.items()):
            for sub_port, _ in next_hop_sub_ports:
                generate_and_verify_traffic(duthost=duthost,
                                            ptfadapter=ptfadapter,
                                            src_port=sub_ports[src_port]['neighbor_port'],
                                            ip_src=sub_ports[src_port]['neighbor_ip'],
                                            dst_port=sub_ports[sub_port]['neighbor_port'],
                                            ip_dst=sub_ports[sub_port]['neighbor_ip'],
                                            ip_tunnel=sub_ports[src_port]['ip'],
                                            pkt_action='fwd',
                                            type_of_traffic='decap',
                                            ttl=63)

    def test_balancing_sub_ports(self, duthost, ptfhost, ptfadapter,
                                 apply_balancing_config):
        """
        Validates load-balancing when sub-port is part of ECMP
        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Setup static routes to the network by different sub-ports on the DUT.
            4.) Create packets with different source ip addresses.
            5.) Send packets.
            6.) Verify that sub-port gets received packets on the PTF.
            7.) Verify load-balancing by using number of packets on different sub-ports.
            8.) Remove static routes from DUT.
            9.) Clear configuration of sub-ports on the DUT.
            10.) Clear configuration of sub-ports on the PTF.
        Pass Criteria:
            1.) PTF sub-ports get received packet.
            2.) Balancing range between sub-ports is less than 25%.
        """
        new_sub_ports = apply_balancing_config['new_sub_ports']
        sub_ports = apply_balancing_config['sub_ports']
        src_ports = apply_balancing_config['src_ports']
        src_port = random.choice(src_ports)

        for ports, subnet in new_sub_ports:
            ip_dst = [str(ip) for ip in subnet.hosts()][0]
            dst_ports = [sub_ports[sub_port]['neighbor_port'] for sub_port in ports]
            generate_and_verify_traffic(duthost=duthost,
                                        ptfhost=ptfhost,
                                        ptfadapter=ptfadapter,
                                        src_port=src_port,
                                        dst_port=dst_ports,
                                        ip_dst=ip_dst,
                                        type_of_traffic='balancing',
                                        ttl=63)


class TestSubPortsNegative(object):
    def test_packet_routed_with_invalid_vlan(self, duthost, ptfadapter, apply_config_on_the_dut,
                                             apply_config_on_the_ptf):
        """
        Validates that packet aren't routed if sub-ports have invalid VLAN ID.

        Test steps:
            1.) Setup correct configuration of sub-ports on the DUT.
            2.) Setup different VLAN IDs on directly connected interfaces of sub-ports on the PTF.
            3.) Create ICMP packet.
            4.) Send ICMP request packet from PTF to DUT.
            5.) Verify that DUT doesn't send ICMP reply packet to PTF.
            6.) Clear configuration of sub-ports on the DUT.
            7.) Clear configuration of sub-ports on the DUT.

        Pass Criteria: PTF doesn't get ICMP reply packet from DUT.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        for sub_port, value in sub_ports.items():
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='drop')


class TestSubPortStress(object):
    def test_max_numbers_of_sub_ports(self, duthost, ptfadapter, apply_config_on_the_dut,
                                      apply_config_on_the_ptf):
        """
        Validates that 256 sub-ports can be created per port or LAG

        Test steps:
            1.) Setup configuration of 256 sub-ports on the DUT.
            2.) Setup configuration of 256 sub-ports on the PTF.
            3.) Create ICMP packet.
            4.) Send ICMP request packet from PTF to DUT.
            5.) Verify that DUT sends ICMP reply packet to PTF.
            6.) Clear configuration of sub-ports on the DUT.
            7.) Clear configuration of sub-ports on the PTF.

        Pass Criteria: PTF gets ICMP reply packet from DUT.

        Note:
            The running of the test case takes about 80 minutes.
        """
        sub_ports_new = dict()
        sub_ports = apply_config_on_the_dut['sub_ports']
        sub_ports_new[list(sub_ports.keys())[0]] = sub_ports[list(sub_ports.keys())[0]]
        sub_ports_new[list(sub_ports.keys())[-1]] = sub_ports[list(sub_ports.keys())[-1]]

        rand_sub_ports = list(sub_ports.keys())[random.randint(1, len(sub_ports)-1)]
        sub_ports_new[rand_sub_ports] = sub_ports[rand_sub_ports]

        for sub_port, value in sub_ports_new.items():
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='fwd')


class TestSubPortParentPort(object):
    """The parent port must keep serving its own IP while sub-ports exist on it.

    Creating the first sub-port changes the parent port's hostif vlan tag mode.
    If untagged frames then reach the CPU still carrying the port's internal
    vlan the kernel drops them, arp stops resolving and the parent port IP goes
    unreachable, while the sub-ports keep working.

    Runs for both port types, so a parent that is a LAG is covered too.
    """

    # One /30 per parent, outside the sub-port pool (two parents x 256 /30s
    # stop at 172.16.7.255), so these cannot collide with sub-port addresses.
    PARENT_SUBNET = u'172.16.250.0/24'

    @pytest.fixture
    def parent_port_ips(self, duthost, ptfhost, apply_config_on_the_dut):
        """Give every parent port and its PTF counterpart an address of their own."""
        sub_ports = apply_config_on_the_dut['sub_ports']

        parents, seen = [], set()
        for sub_port in sorted(sub_ports):
            parent = sub_port.split('.')[0]
            if parent in seen:
                continue
            seen.add(parent)
            parents.append((parent, sub_ports[sub_port]['neighbor_port'].split('.')[0]))

        configured = []
        for (parent, ptf_parent), subnet in zip(parents, ip_network(self.PARENT_SUBNET).subnets(new_prefix=30)):
            dut_ip, ptf_ip = ('{}/{}'.format(host, 30) for host in subnet.hosts())
            add_ip_to_dut_port(duthost, parent, dut_ip)
            # `ip address add` is not idempotent, and the PTF teardown only clears
            # sub-interfaces and bonds, so an interrupted run would fail setup here.
            ptfhost.shell('ip address replace {} dev {}'.format(ptf_ip, ptf_parent))
            configured.append((parent, ptf_parent, dut_ip, ptf_ip))

        # The config command returns once CONFIG_DB is written; orchagent programs
        # the RIF afterwards. Wait for the neighbour to answer before sending
        # traffic, so a slow build is not reported as a regression.
        for parent, ptf_parent, dut_ip, ptf_ip in configured:
            pytest_assert(wait_until(30, 1, 0, check_ip_reachable, duthost, ptf_ip.split('/')[0]),
                          'DUT could not reach PTF {} on parent {} within 30s'.format(ptf_ip, parent))

        yield configured

        for parent, ptf_parent, dut_ip, ptf_ip in configured:
            duthost.shell('config interface ip remove {} {}'.format(parent, dut_ip),
                          module_ignore_errors=True)
            ptfhost.shell('ip address del {} dev {}'.format(ptf_ip, ptf_parent),
                          module_ignore_errors=True)
            # An address left on a PortChannel makes 'config portchannel del' fail
            # during LAG teardown, which surfaces with no trace of the cause.
            pytest_assert(wait_until(10, 1, 0, lambda: not check_interface_ip(duthost, parent, dut_ip)),
                          '{} was not removed from {}'.format(dut_ip, parent))

    def _verify_parents(self, duthost, ptfadapter, configured):
        """Every parent port answers on its own IP."""
        for parent, ptf_parent, dut_ip, ptf_ip in configured:
            # untagged, because the PTF port name carries no vlan
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=ptf_parent,
                                        ip_src=ptf_ip,
                                        dst_port=parent,
                                        ip_dst=dut_ip,
                                        pkt_action='fwd')

    def _verify_sub_ports(self, duthost, ptfadapter, sub_ports):
        """Every sub-port answers on its own IP."""
        for sub_port, value in list(sub_ports.items()):
            generate_and_verify_traffic(duthost=duthost,
                                        ptfadapter=ptfadapter,
                                        src_port=value['neighbor_port'],
                                        ip_src=value['neighbor_ip'],
                                        dst_port=sub_port,
                                        ip_dst=value['ip'],
                                        pkt_action='fwd')

    def test_parent_port_reachable_with_sub_ports(self, duthost, ptfadapter, parent_port_ips,
                                                  apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates that every parent port answers on its own IP while sub-ports exist.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT.
            2.) Setup configuration of sub-ports on the PTF.
            3.) Add an IP to each parent port and to its PTF counterpart.
            4.) Send untagged ICMP request from PTF to each parent port IP.
            5.) Verify that DUT sends an ICMP reply.
            6.) Verify each sub-port still answers on its own IP.

        Pass Criteria: PTF gets ICMP reply from every parent port and from every sub-port.
        """
        self._verify_parents(duthost, ptfadapter, parent_port_ips)
        # the sub-ports must not have been disturbed
        self._verify_sub_ports(duthost, ptfadapter, apply_config_on_the_dut['sub_ports'])

    def test_parent_port_reachable_when_sub_ports_removed(self, duthost, ptfadapter, parent_port_ips,
                                                          apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates that the parent port survives the sub-ports going away and coming back.

        The parent's hostif vlan tag mode changes when the first sub-port is
        created and changes back when the last one is removed, so the removal
        edge is the next most likely place for the parent to break.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT and the PTF.
            2.) Add an IP to each parent port and to its PTF counterpart.
            3.) Verify every parent port answers.
            4.) Remove every sub-port from the DUT.
            5.) Verify every parent port still answers.
            6.) Create the sub-ports again.
            7.) Verify every parent port and every sub-port answers.

        Pass Criteria: the parent ports answer with sub-ports present, with none
                       present, and after they are created a second time.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        self._verify_parents(duthost, ptfadapter, parent_port_ips)

        for sub_port, value in list(sub_ports.items()):
            remove_sub_port(duthost, sub_port, value['ip'])
            pytest_assert(wait_until(30, 2, 0, check_sub_port, duthost, sub_port, True),
                          'Sub-port {} was not removed from the DUT'.format(sub_port))

        self._verify_parents(duthost, ptfadapter, parent_port_ips)

        for sub_port, value in list(sub_ports.items()):
            create_sub_port_on_dut(duthost, sub_port, value['ip'])

        self._verify_parents(duthost, ptfadapter, parent_port_ips)
        self._verify_sub_ports(duthost, ptfadapter, sub_ports)

    def test_parent_port_reachable_after_lag_member_readded(self, duthost, ptfadapter, parent_port_ips,
                                                            apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates that a lag member added while sub-ports exist gets the same treatment.

        A member joining a lag that already carries sub-ports needs the parent's
        hostif vlan tag mode applied to it as well. If it does not, traffic over
        that member behaves differently from the rest of the lag.

        Test steps:
            1.) Setup configuration of sub-ports on a lag and on the PTF.
            2.) Add an IP to each parent and to its PTF counterpart.
            3.) Remove one member from the lag and add it back.
            4.) Verify every parent port still answers.
            5.) Verify every sub-port still answers.

        Pass Criteria: parent ports and sub-ports answer after the member rejoins.
        """
        parent = parent_port_ips[0][0]
        if not parent.startswith('PortChannel'):
            pytest.skip('Only applies when the parent is a lag')

        members = get_portchannel_members(duthost, parent)
        pytest_assert(members, 'No members found on {}'.format(parent))
        member = sorted(members)[0]

        duthost.shell('config portchannel member del {} {}'.format(parent, member))
        pytest_assert(wait_until(30, 2, 0, lambda: member not in get_portchannel_members(duthost, parent)),
                      '{} was not removed from {}'.format(member, parent))

        duthost.shell('config portchannel member add {} {}'.format(parent, member))
        pytest_assert(wait_until(60, 2, 0, lambda: member in get_portchannel_members(duthost, parent)),
                      '{} did not rejoin {}'.format(member, parent))

        for p, ptf_p, dut_ip, ptf_ip in parent_port_ips:
            pytest_assert(wait_until(60, 2, 0, check_ip_reachable, duthost, ptf_ip.split('/')[0]),
                          'DUT could not reach PTF {} after {} rejoined'.format(ptf_ip, member))

        self._verify_parents(duthost, ptfadapter, parent_port_ips)
        self._verify_sub_ports(duthost, ptfadapter, apply_config_on_the_dut['sub_ports'])

    def test_sub_port_ip_answers_over_the_parent(self, duthost, ptfadapter, parent_port_ips,
                                                 apply_config_on_the_dut, apply_config_on_the_ptf):
        """
        Validates traffic arriving untagged on the parent for a sub-port IP.

        The existing routing tests use a different physical port. This one keeps
        both addresses on the same physical port, so an untagged frame on the
        parent has to be accepted and answered for an address that belongs to a
        sub-port of that same port.

        Test steps:
            1.) Setup configuration of sub-ports on the DUT and the PTF.
            2.) Add an IP to each parent and to its PTF counterpart.
            3.) Send untagged ICMP from the PTF parent to each sub-port IP of that parent.
            4.) Verify that DUT sends an ICMP reply.

        Pass Criteria: the DUT answers its sub-port addresses over the parent port.
        """
        sub_ports = apply_config_on_the_dut['sub_ports']

        for parent, ptf_parent, dut_ip, ptf_ip in parent_port_ips:
            for sub_port, value in list(sub_ports.items()):
                if sub_port.split('.')[0] != parent:
                    continue
                generate_and_verify_traffic(duthost=duthost,
                                            ptfadapter=ptfadapter,
                                            src_port=ptf_parent,
                                            ip_src=ptf_ip,
                                            dst_port=parent,
                                            ip_dst=value['ip'],
                                            pkt_action='fwd')
