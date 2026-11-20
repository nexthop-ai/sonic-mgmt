import pytest
import time
import random
import logging

from tests.srv6.srv6_utils import MySIDs, verify_appl_db_sid_entry_exist, SRv6, \
    validate_techsupport_generation, validate_srv6_counters, clear_srv6_counters, \
<<<<<<< HEAD
    get_neighbor_mac, verify_asic_db_sid_entry_exist, ROUTE_BASE, run_srv6_traffic_test, \
    run_srv6_no_sid_blackhole_test
=======
    get_neighbor_mac, verify_asic_db_sid_entry_exist, ROUTE_BASE, \
    run_srv6_no_sid_blackhole_test, get_ptf_src_port_and_dut_port_and_neighbor
>>>>>>> aef440db5 (NOS-23659: [srv6] Pick the dataplane neighbor from minigraph, not LLDP (#5301))
from tests.common.reboot import reboot
from tests.common.config_reload import config_reload
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from tests.common.mellanox_data import is_mellanox_device
from tests.common.dualtor.mux_simulator_control import toggle_all_simulator_ports_to_rand_selected_tor  # noqa: F401
from tests.common.helpers.srv6_helper import create_srv6_packet, send_verify_srv6_packet, \
    validate_srv6_in_appl_db, validate_srv6_in_asic_db, validate_srv6_route, is_bgp_route_synced

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.asic("mellanox", "broadcom", "vpp"),
    pytest.mark.topology("t0", "t1")
]


<<<<<<< HEAD
=======
def run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh,
                          sid_mode=USID):
    # Convert single port to list for uniform handling
    if isinstance(ptf_src_ports, int):
        ptf_src_ports_list = [ptf_src_ports]
    else:
        ptf_src_ports_list = ptf_src_ports

    # Use the first port for sending packets
    ptf_src_port = ptf_src_ports_list[0]

    for i in range(0, 10):
        # generate a random payload
        payload = ''.join(random.choices(string.ascii_letters + string.digits, k=20))
        if sid_mode.full_sid:
            injected_pkt = simple_ipv6_sr_packet(
                eth_dst=dut_mac,
                eth_src=ptfadapter.dataplane.get_mac(0, ptf_src_port).decode(),
                ipv6_src=ptfhost.mgmt_ipv6 if ptfhost.mgmt_ipv6 else "1000::1",
                ipv6_dst=sid_mode.sid("fcbb:bbbb:1::/48").split('/')[0],
                srh_seg_left=1,
                srh_seg_list=["fcbb:bbbb:2::"],
                srh_nh=41,
                inner_frame=IPv6() / UDP(dport=4791) / Raw(load=payload)
            )
        elif with_srh:
            injected_pkt = simple_ipv6_sr_packet(
                eth_dst=dut_mac,
                eth_src=ptfadapter.dataplane.get_mac(0, ptf_src_port).decode(),
                ipv6_src=ptfhost.mgmt_ipv6 if ptfhost.mgmt_ipv6 else "1000::1",
                ipv6_dst="fcbb:bbbb:1:2::",
                srh_seg_left=1,
                srh_nh=41,
                inner_frame=IPv6() / UDP(dport=4791) / Raw(load=payload)
            )
        else:
            injected_pkt = Ether(dst=dut_mac, src=ptfadapter.dataplane.get_mac(0, ptf_src_port).decode()) \
                           / IPv6(src=ptfhost.mgmt_ipv6 if ptfhost.mgmt_ipv6 else "1000::1", dst="fcbb:bbbb:1:2::") \
                           / IPv6() / UDP(dport=4791) / Raw(load=payload)

        expected_pkt = injected_pkt.copy()
        expected_pkt['Ether'].dst = get_neighbor_mac(duthost, neighbor_ip)
        expected_pkt['Ether'].src = dut_mac
        expected_pkt['IPv6'].dst = "fcbb:bbbb:2::"
        expected_pkt['IPv6'].hlim -= 1
        if sid_mode.full_sid:
            expected_pkt[IPv6ExtHdrRouting].segleft = 0
        logger.debug("Expected packet #{}: {}".format(i, expected_pkt.summary()))
        runSendReceive(injected_pkt, ptf_src_port, expected_pkt, ptf_src_ports_list, True, ptfadapter)


@pytest.fixture(scope="module", autouse=True)
def srv6_enabled_dut(rand_selected_dut):
    """Turn SRv6 on where the SAI needs it, and leave it on.

    On DNX SRv6 is off by default, and with it off the SAI rejects every SID.
    Enabling restarts swss, so wait for BGP to come back before the cases run.
    """
    duthost = rand_selected_dut
    if not check_srv6_platform_requirements(duthost) or get_srv6_global_status(duthost) == 'true':
        return
    bgp_neighbors = duthost.get_bgp_neighbors_per_asic_from_config()
    config_srv6_global_status(duthost, 'true')
    pytest_assert(wait_until(180, 15, 5, verify_srv6_global_status, duthost, 'true'),
                  "SRv6 could not be enabled")
    pytest_assert(wait_until(300, 10, 30, duthost.check_bgp_session_state_all_asics, bgp_neighbors),
                  "BGP sessions did not come back after enabling SRv6")
    pytest_assert(refresh_macsec_info(duthost), "MACsec links did not come back after enabling SRv6")


def skip_full_sid_without_srh(setup_info, with_srh):
    if setup_info['sid_mode'].full_sid and not with_srh:
        pytest.skip("End needs an SRH")


def check_sids_after_event(duthost, cores):
    """APPL_DB, the chip and FRR agree on the SIDs, and no core was generated."""
    if not duthost.is_multi_asic:
        assert wait_until(180, 10, 0, sid_state_consistent, duthost), \
            "APPL_DB {}, chip {} and FRR {} disagree on the SIDs".format(
                appl_sid_count(duthost), asic_mysid_count(duthost), frr_sid_count(duthost))
    assert core_count(duthost) == cores, "a core was generated"


@pytest.fixture()
def setup_uN(duthosts, enum_frontend_dut_hostname, enum_frontend_asic_index, tbinfo, sid_mode):
    duthost = duthosts[enum_frontend_dut_hostname]
    asic_index = enum_frontend_asic_index
    if sid_mode.full_sid and not check_srv6_platform_requirements(duthost) \
            and switch_chip_id(duthost) not in SID_CHIPS:
        pytest.skip("full SID runs on DNX, TH6 and TH5-512")
    sid = sid_mode.sid("fcbb:bbbb:1::/48")

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    ptf_port_ids = []
    for interface in list(mg_facts["minigraph_ptf_indices"].keys()):
        port_id = mg_facts["minigraph_ptf_indices"][interface]
        ptf_port_ids.append(port_id)

    if duthost.is_multi_asic:
        cli_options = " -n " + duthost.get_namespace_from_asic_id(asic_index)
        dut_asic = duthost.asic_instance(asic_index)
        dut_mac = dut_asic.get_router_mac()
        dut_port, ptf_src_ports, neighbor = get_ptf_src_port_and_dut_port_and_neighbor(dut_asic, tbinfo)
    else:
        cli_options = ''
        dut_mac = duthost.facts["router_mac"]
        dut_port, ptf_src_ports, neighbor = get_ptf_src_port_and_dut_port_and_neighbor(duthost, tbinfo)

    logger.info("Doing test on DUT port {} | PTF ports {}".format(dut_port, ptf_src_ports))

    neighbor_ip = None
    # get neighbor IP
    lines = duthost.command("show ipv6 bgp sum")['stdout'].split("\n")
    for line in lines:
        if neighbor in line:
            neighbor_ip = line.split()[0]
    assert neighbor_ip, "Unable to find neighbor {} IP".format(neighbor)

    # use DUT portchannel if applicable
    pc_info = duthost.command("show int portchannel")['stdout']
    if dut_port in pc_info:
        lines = pc_info.split("\n")
        for line in lines:
            if dut_port in line:
                dut_port = line.split()[1]
                logger.info("Using portchannel interface: {}".format(dut_port))
                break

    sonic_db_cli = "sonic-db-cli" + cli_options

    # add a locator configuration entry
    duthost.command(sonic_db_cli + " CONFIG_DB HSET SRV6_MY_LOCATORS\\|loc1 "
                    "prefix fcbb:bbbb:1::/48 block_len 32 node_len 16 func_len {}{}"
                    .format(sid_mode.func_len, " full_sid true" if sid_mode.full_sid else ""))
    # add a uN (or End) sid configuration entry
    duthost.command(sonic_db_cli + " CONFIG_DB HSET SRV6_MY_SIDS\\|loc1\\|{} action {} decap_dscp_mode pipe"
                    .format(sid, sid_mode.behavior))
    random.seed(time.time())

    def _add_forward_route():
        duthost.command(sonic_db_cli + " CONFIG_DB HSET STATIC_ROUTE\\|default\\|fcbb:bbbb:2::/48 NULL NULL")
        duthost.command(
            sonic_db_cli +
            " CONFIG_DB HSET STATIC_ROUTE_NEXTHOP\\|default\\|fcbb:bbbb:2::/48\\|{}\\|{} NULL NULL"
            .format(neighbor_ip, dut_port))

    def _add_blackhole_route():
        duthost.command(sonic_db_cli + " CONFIG_DB HSET STATIC_ROUTE\\|default\\|fcbb:bbbb::/32 NULL NULL")
        duthost.command(
            sonic_db_cli +
            " CONFIG_DB HSET STATIC_ROUTE_NEXTHOP\\|default\\|fcbb:bbbb::/32\\|none\\|none blackhole true")

    # add the static route for IPv6 forwarding towards PTF's uSID and the blackhole route in a random order
    if random.randint(0, 1) == 0:
        _add_forward_route()
        _add_blackhole_route()
    else:
        _add_blackhole_route()
        _add_forward_route()
    duthost.command("config save -y")
    # Verify that the ASIC DB has the SRv6 SID entries
    assert wait_until(20, 5, 0, verify_asic_db_sid_entry_exist, duthost, sonic_db_cli), \
        "ASIC_STATE:SAI_OBJECT_TYPE_MY_SID_ENTRY entries are missing in ASIC_DB"

    setup_info = {
        "asic_index": asic_index,
        "duthost": duthost,
        "dut_mac": dut_mac,
        "dut_port": dut_port,
        "ptf_src_ports": ptf_src_ports,
        "neighbor_ip": neighbor_ip,
        "cli_options": cli_options,
        "ptf_port_ids": ptf_port_ids,
        "sid_mode": sid_mode,
        "appl_key": "SRV6_MY_SID_TABLE:{}:{}:{}:0:{}".format(
            BLOCK_LEN, NODE_LEN, sid_mode.func_len if sid_mode.full_sid else 0, sid.split('/')[0]),
    }

    yield setup_info

    # delete the SRv6 configuration
    duthost.command(sonic_db_cli + " CONFIG_DB DEL SRV6_MY_LOCATORS\\|loc1")
    duthost.command(sonic_db_cli + " CONFIG_DB DEL SRV6_MY_SIDS\\|loc1\\|{}".format(sid))
    # STATIC_ROUTE_NEXTHOP leafrefs back to STATIC_ROUTE, so delete it first.
    duthost.command(
        sonic_db_cli +
        " CONFIG_DB DEL STATIC_ROUTE_NEXTHOP\\|default\\|fcbb:bbbb:2::/48\\|{}\\|{}".format(neighbor_ip, dut_port))
    duthost.command(sonic_db_cli + " CONFIG_DB DEL STATIC_ROUTE_NEXTHOP\\|default\\|fcbb:bbbb::/32\\|none\\|none")
    duthost.command(sonic_db_cli + " CONFIG_DB DEL STATIC_ROUTE\\|default\\|fcbb:bbbb:2::/48")
    duthost.command(sonic_db_cli + " CONFIG_DB DEL STATIC_ROUTE\\|default\\|fcbb:bbbb::/32")
    duthost.command("config save -y")


>>>>>>> aef440db5 (NOS-23659: [srv6] Pick the dataplane neighbor from minigraph, not LLDP (#5301))
class SRv6Base():

    @pytest.fixture(autouse=True)
    def use_param(self, prepare_param):
        self.params = prepare_param

    def _validate_srv6_function(self, duthost, ptfadapter, dscp_mode, weak_server):
        srv6_pkt_list = []
        logger.info('Clear the SRv6 counters')
        clear_srv6_counters(duthost)

        logger.info('Validate SRv6 table in APPL DB')
        pytest_assert(wait_until(60, 5, 0, validate_srv6_in_appl_db, duthost, MySIDs.MY_SID_LIST),
                      "SRv6 table in APPL DB is not as expected")

        logger.info('Validate SRv6 table in ASIC DB')
        pytest_assert(wait_until(60, 5, 0, validate_srv6_in_asic_db, duthost, MySIDs.MY_SID_LIST),
                      "SRv6 table in ASIC DB is not as expected")

        logger.info('Validate SRv6 route in ASIC DB')
        pytest_assert(wait_until(120, 5, 0, validate_srv6_route, duthost, ROUTE_BASE),
                      "SRv6 route in ASIC DB is not as expected")

        delay_interval = 0
        if weak_server:
            delay_interval = 0.4
            self.params['packet_num'] = 10
        ptf_src_mac = ptfadapter.dataplane.get_mac(0, self.params['ptf_downlink_port']).decode('utf-8')
        for srv6_packet in self.params['srv6_packets']:
            if duthost.facts["asic_type"] == "broadcom" and \
               (srv6_packet['srh_seg_left'] or srv6_packet['srh_seg_list']):
                logger.info("Skip the test for Broadcom ASIC with SRH")
                continue

            if duthost.facts["asic_type"] == "vpp" and \
               (srv6_packet['validate_usd_flavor']):
                logger.info("Skip the test for VPP with USD flavor.")
                continue

            logger.info('-------------------------------------------------------------------------')
            if srv6_packet['validate_dip_shift']:
                logger.info('Validate DIP shift')
            if srv6_packet['validate_usd_flavor']:
                logger.info('Validate USD flavor')
            logger.info(f'SRv6 tunnel decapsulation mode: {dscp_mode}')
            logger.info(f'Send {self.params["packet_num"]} SRv6 packets with action: {srv6_packet["action"]}')
            logger.info(f'Pkt Src MAC: {ptf_src_mac}')
            logger.info(f'Pkt Dst MAC: {self.params["router_mac"]}')
            if srv6_packet['action'] == SRv6.uN:
                logger.info(f'Outer Pkt Src IP: {srv6_packet["outer_src_ipv6"]}')
                logger.info(f'Outer Pkt Dst IP: {srv6_packet["dst_ipv6"]}')
                if srv6_packet["exp_dst_ipv6"]:
                    logger.info(f'Expect Outer Pkt Dst IP: {srv6_packet["exp_dst_ipv6"]}')
            if dscp_mode == SRv6.uniform_mode:
                if srv6_packet['outer_dscp']:
                    logger.info(f'Outer DSCP value: {srv6_packet["outer_dscp"]}')
                if srv6_packet['exp_outer_dscp_uniform']:
                    logger.info(f'Expect inner DSCP value: {srv6_packet["exp_outer_dscp_uniform"]}')
            else:
                if srv6_packet['inner_dscp']:
                    logger.info(f'Inner DSCP value: {srv6_packet["inner_dscp"]}')
                if srv6_packet['exp_inner_dscp_pipe']:
                    logger.info(f'Expect inner DSCP value: {srv6_packet["exp_inner_dscp_pipe"]}')
            logger.info(f'SRH Segment List: {srv6_packet["srh_seg_list"]}')
            logger.info(f'SRH Segment Left: {srv6_packet["srh_seg_left"]}')

            logger.info(f'Expect Segment Left: {srv6_packet["exp_srh_seg_left"]}')
            logger.info(f'Expect process result: {srv6_packet["exp_process_result"]}')
            logger.info('-------------------------------------------------------------------------')

            srv6_pkt, exp_pkt = create_srv6_packet(
                outer_src_mac=ptf_src_mac,
                outer_dst_mac=self.params['router_mac'],
                outer_src_pkt_ip=srv6_packet['outer_src_ipv6'],
                outer_dst_pkt_ip=srv6_packet['dst_ipv6'],
                srv6_action=srv6_packet['action'],
                inner_dscp=srv6_packet['inner_dscp'],
                outer_dscp=srv6_packet['outer_dscp'],
                exp_outer_dst_pkt_ip=srv6_packet['exp_dst_ipv6'],
                exp_seg_left=srv6_packet['exp_srh_seg_left'],
                exp_dscp_pipe=srv6_packet['exp_inner_dscp_pipe'],
                exp_dscp_uniform=srv6_packet['exp_outer_dscp_uniform'],
                seg_left=srv6_packet['srh_seg_left'],
                sef_list=srv6_packet['srh_seg_list'],
                inner_pkt_ver=srv6_packet['inner_pkt_ver'],
                dscp_mode=dscp_mode,
                router_mac=self.params['router_mac'],
                inner_src_ip=srv6_packet['inner_src_ip'],
                inner_dst_ip=srv6_packet['inner_dst_ip'],
                inner_src_ipv6=srv6_packet['inner_src_ipv6'],
                inner_dst_ipv6=srv6_packet['inner_dst_ipv6']
            )

            send_verify_srv6_packet(
                ptfadapter=ptfadapter,
                pkt=srv6_pkt,
                exp_pkt=exp_pkt,
                exp_pro=srv6_packet["exp_process_result"],
                ptf_src_port_id=self.params['ptf_downlink_port'],
                ptf_dst_port_ids=self.params['ptf_uplink_ports'],
                packet_num=self.params['packet_num']
            )

            srv6_pkt_list.append(srv6_pkt)
            time.sleep(delay_interval)

        return srv6_pkt_list


class TestSRv6DataPlaneBase(SRv6Base):

    @pytest.mark.enable_monit_refresh
    def test_srv6_full_func(self, config_setup, srv6_crm_total_sids,
                            setup_standby_ports_on_rand_unselected_tor,       # noqa: F811
                            toggle_all_simulator_ports_to_rand_selected_tor,  # noqa: F811
                            ptfadapter, rand_selected_dut, localhost, request, enum_frontend_asic_index, weak_server):

        with allure.step('Validate SRv6 packet process'):
            srv6_pkt_list = self._validate_srv6_function(rand_selected_dut, ptfadapter, config_setup, weak_server)

        with allure.step('Validate SRv6 counters'):
            pytest_assert(wait_until(60, 5, 0, validate_srv6_counters, rand_selected_dut, srv6_pkt_list,
                                     MySIDs.MY_SID_LIST, self.params['packet_num']),
                          "SRv6 counters are not as expected")

        if random.random() < 0.5:

            with allure.step('Execute reboot test'):
                reboot_type = request.config.getoption("--srv6_reboot_type")

                if reboot_type == "random":
                    reboot_type = random.choice(["cold", "reload", "bgp"])

                if reboot_type == "cold":
                    with allure.step('Execute cold reboot'):
                        reboot(rand_selected_dut, localhost, reboot_type=reboot_type, wait_warmboot_finalizer=True,
                               safe_reboot=True, check_intf_up_ports=True, wait_for_bgp=True)
                elif reboot_type == "reload":
                    with allure.step('Execute config reload'):
                        config_reload(rand_selected_dut, safe_reload=True, check_intf_up_ports=True)
                else:
                    with allure.step('Execute BGP restart'):
                        if rand_selected_dut.is_multi_asic:
                            rand_selected_dut.command(
                                f"systemctl restart bgp@{enum_frontend_asic_index}")
                        else:
                            rand_selected_dut.command("systemctl restart bgp")

                with allure.step('Validate BGP docker UP'):
                    pytest_assert(wait_until(100, 10, 0, rand_selected_dut.is_service_fully_started_per_asic_or_host,
                                             "bgp"),
                                  "BGP not started.")

                with allure.step('Validate BGP route sync'):
                    pytest_assert(wait_until(120, 5, 0, is_bgp_route_synced,
                                             rand_selected_dut), "BGP route is not synced")

                with allure.step('Validate SRv6 packet process'):
                    self._validate_srv6_function(rand_selected_dut, ptfadapter, config_setup, weak_server)

                with allure.step('Validate SRv6 counters'):
                    pytest_assert(wait_until(60, 5, 0, validate_srv6_counters, rand_selected_dut, srv6_pkt_list,
                                             MySIDs.MY_SID_LIST, self.params['packet_num']),
                                  "SRv6 counters are not as expected")

            if is_mellanox_device(rand_selected_dut) and config_setup == SRv6.pipe_mode:
                with allure.step('Validate SAI SDK dump contains SRv6 information'):
                    validate_techsupport_generation(rand_selected_dut, feature_list=['SRv6'])


@pytest.mark.parametrize("with_srh", [True, False])
def test_srv6_dataplane_after_config_reload(setup_uN, ptfadapter, ptfhost, with_srh):
    duthost = setup_uN['duthost']
    dut_mac = setup_uN['dut_mac']
    ptf_src_ports = setup_uN['ptf_src_ports']
    neighbor_ip = setup_uN['neighbor_ip']

    # verify the forwarding works
    run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh)

    # reload the config
    duthost.command("config reload -y -f")
    time.sleep(180)

    sonic_db_cli = "sonic-db-cli" + setup_uN['cli_options']
    # wait for the config to be reprogrammed
    assert wait_until(180, 2, 0, verify_appl_db_sid_entry_exist, duthost, sonic_db_cli,
                      "SRV6_MY_SID_TABLE:32:16:0:0:fcbb:bbbb:1::", True), "SID is missing in APPL_DB"
    # Verify that the ASIC DB has the SRv6 SID entries
    assert wait_until(20, 5, 0, verify_asic_db_sid_entry_exist, duthost, sonic_db_cli), \
        "ASIC_STATE:SAI_OBJECT_TYPE_MY_SID_ENTRY entries are missing in ASIC_DB after config reload"

    pytest_assert(wait_until(60, 5, 0, is_bgp_route_synced, duthost), "BGP route is not synced")

    pytest_assert(wait_until(60, 5, 0, get_neighbor_mac, duthost, neighbor_ip),
                  "IP table not updating MAC for neighbour")

    # verify the forwarding works after config reload
    run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh)


@pytest.mark.parametrize("with_srh", [True, False])
def test_srv6_dataplane_after_bgp_restart(setup_uN, ptfadapter, ptfhost, with_srh):
    duthost = setup_uN['duthost']
    dut_mac = setup_uN['dut_mac']
    ptf_src_ports = setup_uN['ptf_src_ports']
    neighbor_ip = setup_uN['neighbor_ip']

    # verify the forwarding works
    run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh)

    # restart BGP service, which will restart the BGP container
    if duthost.is_multi_asic:
        duthost.command("systemctl restart bgp@{}".format(setup_uN['asic_index']))
    else:
        duthost.command("systemctl restart bgp")
    time.sleep(180)

    sonic_db_cli = "sonic-db-cli" + setup_uN['cli_options']
    # wait for the config to be reprogrammed
    assert wait_until(180, 2, 0, verify_appl_db_sid_entry_exist, duthost, sonic_db_cli,
                      "SRV6_MY_SID_TABLE:32:16:0:0:fcbb:bbbb:1::", True), "SID is missing in APPL_DB"
    # Verify that the ASIC DB has the SRv6 SID entries
    assert wait_until(20, 5, 0, verify_asic_db_sid_entry_exist, duthost, sonic_db_cli), \
        "ASIC_STATE:SAI_OBJECT_TYPE_MY_SID_ENTRY entries are missing in ASIC_DB after BGP restart"

    pytest_assert(wait_until(60, 5, 0, is_bgp_route_synced, duthost), "BGP route is not synced")
    # verify the forwarding works after BGP restart
    run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh)


@pytest.mark.parametrize("with_srh", [True, False])
def test_srv6_dataplane_after_reboot(setup_uN, ptfadapter, ptfhost, localhost, with_srh, loganalyzer):
    duthost = setup_uN['duthost']
    dut_mac = setup_uN['dut_mac']
    ptf_src_ports = setup_uN['ptf_src_ports']
    neighbor_ip = setup_uN['neighbor_ip']

    # Reloading the configuration will restart eth0 and update the TACACS settings.
    # This change may introduce a delay, potentially causing temporary TACACS reporting errors.
    if loganalyzer and duthost.hostname and duthost.hostname in loganalyzer:
        loganalyzer[duthost.hostname].ignore_regex.extend([r".*tac_connect_single: .*",
                                                           r".*nss_tacplus: .*"])

    # verify the forwarding works
    run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh)

    # reboot DUT
    reboot(duthost, localhost, wait=300, safe_reboot=True, check_intf_up_ports=True, wait_for_bgp=True)

    sonic_db_cli = "sonic-db-cli" + setup_uN['cli_options']
    # wait for the config to be reprogrammed
    assert wait_until(180, 2, 0, verify_appl_db_sid_entry_exist, duthost, sonic_db_cli,
                      "SRV6_MY_SID_TABLE:32:16:0:0:fcbb:bbbb:1::", True), "SID is missing in APPL_DB"
    # Verify that the ASIC DB has the SRv6 SID entries
    assert wait_until(20, 5, 0, verify_asic_db_sid_entry_exist, duthost, sonic_db_cli), \
        "ASIC_STATE:SAI_OBJECT_TYPE_MY_SID_ENTRY entries are missing in ASIC_DB after reboot"

    pytest_assert(wait_until(60, 5, 0, is_bgp_route_synced, duthost), "BGP route is not synced")
    # verify the forwarding works after reboot
    run_srv6_traffic_test(duthost, dut_mac, ptf_src_ports, neighbor_ip, ptfadapter, ptfhost, with_srh)


@pytest.mark.parametrize("with_srh", [True, False])
def test_srv6_no_sid_blackhole(setup_uN, ptfadapter, ptfhost, with_srh):
    run_srv6_no_sid_blackhole_test(setup_uN, ptfadapter, ptfhost, with_srh)
