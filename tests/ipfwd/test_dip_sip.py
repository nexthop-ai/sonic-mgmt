import pytest
import ptf.testutils as testutils
import logging

from tests.common.fixtures.ptfhost_utils import change_mac_addresses      # noqa F401
from tests.common.fixtures.ptfhost_utils import remove_ip_addresses       # noqa F401

DEFAULT_HLIM_TTL = 64
WAIT_EXPECTED_PACKET_TIMEOUT = 5
STATIC_ROUTE = '201.1.1.1/32'
STATIC_ROUTE_IPV6 = '2001:db8:1::1/128'

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t0', 't1', 't2', 'm0', 'mx')
]

@pytest.fixture(scope="module", autouse="True")
def lldp_setup(duthosts, enum_rand_one_per_hwsku_frontend_hostname, patch_lldpctl, unpatch_lldpctl, localhost):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    patch_lldpctl(localhost, duthost)
    yield
    unpatch_lldpctl(localhost, duthost)


@pytest.fixture(scope="function", autouse=True)
def setup_static_route(duthosts, enum_rand_one_per_hwsku_frontend_hostname, gather_facts, request):
    """
    Fixture to set up and tear down static routes for IPv4 and IPv6.

    This fixture performs the following actions:
    1. Adds IPv4 and IPv6 static routes to the DUT.
    2. Verifies that the routes are added correctly.
    3. Yields control back to the test.
    4. Removes the static routes after the test is complete.

    Args:
        duthosts: Fixture providing access to DUT hosts.
        enum_rand_one_per_hwsku_frontend_hostname: Fixture selecting a random frontend DUT.
        gather_facts: Fixture providing network facts.
        request: Pytest request object.

    Raises:
        pytest.fail: If any step in adding or verifying routes fails.

    Yields:
        None
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    
    # Configure IPv4 static route
    try:
        result = duthost.command("ip route add {} via {}".format(STATIC_ROUTE, gather_facts['dst_host_ipv4']))
        if result['rc'] != 0:
            raise Exception("Failed to add IPv4 static route: {}".format(result['stderr']))
    except Exception as e:
        logger.error("Error occurred while adding IPv4 static route: %s", str(e))
        pytest.fail("IPv4 static route addition failed")
    
    # Configure IPv6 static route
    try:
        result = duthost.command("ip -6 route add {} via {}".format(STATIC_ROUTE_IPV6, gather_facts['dst_host_ipv6']))
        if result['rc'] != 0:
            raise Exception("Failed to add IPv6 static route: {}".format(result['stderr']))
    except Exception as e:
        logger.error("Error occurred while adding IPv6 static route: %s", str(e))
        pytest.fail("IPv6 static route addition failed")
    
    # Verify IPv4 route is in the routing table
    try:
        result = duthost.command("ip route show {}".format(STATIC_ROUTE))
        assert result['rc'] == 0, "Failed to show IPv4 static route: {}".format(result['stderr'])
        assert "via " + gather_facts['dst_host_ipv4'] in result["stdout"], "IPv4 static route verification failed"
    except Exception as e:
        logger.error("Error occurred while verifying IPv4 static route: %s", str(e))
        pytest.fail("IPv4 static route verification failed")
    
    # Verify IPv6 route is in the routing table
    try:
        result = duthost.command("ip -6 route show {}".format(STATIC_ROUTE_IPV6))
        assert result['rc'] == 0, "Failed to show IPv6 static route: {}".format(result['stderr'])
        assert "via " + gather_facts['dst_host_ipv6'] in result["stdout"], "IPv6 static route verification failed"
    except Exception as e:
        logger.error("Error occurred while verifying IPv6 static route: %s", str(e))
        pytest.fail("IPv6 static route verification failed")
    
    # Continue with the test
    yield
    
    # Cleanup: Remove the static routes
    try:
        duthost.command("ip route del {}".format(STATIC_ROUTE))
        duthost.command("ip -6 route del {}".format(STATIC_ROUTE_IPV6))
    except Exception as e:
        logger.error("Error occurred while removing static routes: %s", str(e))

ipv4_test_cases = [
    pytest.param(
        'Same SIP and DIP',
        lambda facts: facts['dst_host_ipv4'],  
        lambda facts: facts['dst_host_ipv4'],  
        id='ipv4_same_sip_dip'
    ),
    pytest.param(
        'Different subnet SIP/DIP - Directly connected route',
        lambda facts: facts['src_host_ipv4'],
        lambda facts: facts['dst_host_ipv4'],
        id='ipv4_different_sip_dip_connectedroute'
    ),
    pytest.param(
        'Different subnet SIP/DIP - Destination not directly connected',
        lambda facts: facts['src_host_ipv4'],
        lambda facts: STATIC_ROUTE.split('/')[0],
        id='ipv4_different_sip_dip_staticrouteprefix'
    )
]

ipv6_test_cases = [
    pytest.param(
        'Same SIP and DIP',
        lambda facts: facts['dst_host_ipv6'],  
        lambda facts: facts['dst_host_ipv6'],  
        id='ipv6_same_sip_dip'
    ),
    pytest.param(
        'Different subnet SIP/DIP - Directly connected route',
        lambda facts: facts['src_host_ipv6'],
        lambda facts: facts['dst_host_ipv6'],
        id='ipv6_different_sip_dip_connectedroute'
    ),
    pytest.param(
        'Different subnet SIP/DIP - Destination not directly connected',
        lambda facts: facts['src_host_ipv6'],
        lambda facts: STATIC_ROUTE_IPV6.split('/')[0],
        id='ipv6_different_sip_dip_staticrouteprefix'
    )
]

@pytest.mark.parametrize('test_name, get_src_ip, get_dst_ip', ipv4_test_cases)
def test_ipv4_forwarding(tbinfo, ptfadapter, gather_facts, enum_rand_one_frontend_asic_index,
                        test_name, get_src_ip, get_dst_ip):
    """Test IPv4 forwarding with various source/destination IP combinations"""
    ptfadapter.reinit()
    
    logger.info("Testing case: {}".format(test_name))
    
    ip_src = get_src_ip(gather_facts)
    ip_dst = get_dst_ip(gather_facts)
    
    pkt = testutils.simple_udp_packet(
        eth_dst=gather_facts['src_router_mac'],
        eth_src=gather_facts['src_host_mac'],
        ip_src=ip_src,
        ip_dst=ip_dst,
        ip_ttl=DEFAULT_HLIM_TTL
    )
    logger.info("\nSend Packet:\neth_dst: {}, eth_src: {}, ip_src: {}, ip_dst: {}".format(
        gather_facts['src_router_mac'], gather_facts['src_host_mac'],
        ip_src, ip_dst)
    )

    testutils.send(ptfadapter, gather_facts['src_port_ids'][0], pkt)

    exp_pkt = testutils.simple_udp_packet(
        eth_dst=gather_facts['dst_host_mac'],
        eth_src=gather_facts['dst_router_mac'],
        ip_src=ip_src,
        ip_dst=ip_dst,
        ip_ttl=DEFAULT_HLIM_TTL-1
    )
    logger.info("\nExpect Packet:\neth_dst: {}, eth_src: {}, ip_src: {}, ip_dst: {}".format(
        gather_facts['dst_host_mac'], gather_facts['dst_router_mac'],
        ip_src, ip_dst)
    )

    try:
        testutils.verify_packet_any_port(ptfadapter, exp_pkt,
                                       gather_facts['dst_port_ids'],
                                       timeout=WAIT_EXPECTED_PACKET_TIMEOUT)
    except AssertionError as e:
        logger.error("Expected packet was not received")
        pytest.fail("Test case failed: {} - {}".format(test_name, str(e)))
    logger.info("Test case passed: {}\n".format(test_name))

@pytest.mark.parametrize('test_name, get_src_ip, get_dst_ip', ipv6_test_cases)
def test_ipv6_forwarding(tbinfo, ptfadapter, gather_facts, enum_rand_one_frontend_asic_index,
                        test_name, get_src_ip, get_dst_ip):
    """Test IPv6 forwarding with various source/destination IP combinations"""
    ptfadapter.reinit()
    
    logger.info("Testing case: {}".format(test_name))
    
    ipv6_src = get_src_ip(gather_facts)
    ipv6_dst = get_dst_ip(gather_facts)
    
    pkt = testutils.simple_udpv6_packet(
        eth_dst=gather_facts['src_router_mac'],
        eth_src=gather_facts['src_host_mac'],
        ipv6_src=ipv6_src,
        ipv6_dst=ipv6_dst,
        ipv6_hlim=DEFAULT_HLIM_TTL
    )
    logger.info("\nSend Packet:\neth_dst: {}, eth_src: {}, ipv6_src: {}, ipv6_dst: {}".format(
        gather_facts['src_router_mac'], gather_facts['src_host_mac'],
        ipv6_src, ipv6_dst)
    )

    testutils.send(ptfadapter, gather_facts['src_port_ids'][0], pkt)

    exp_pkt = testutils.simple_udpv6_packet(
        eth_dst=gather_facts['dst_host_mac'],
        eth_src=gather_facts['dst_router_mac'],
        ipv6_src=ipv6_src,
        ipv6_dst=ipv6_dst,
        ipv6_hlim=DEFAULT_HLIM_TTL-1
    )
    logger.info("\nExpect Packet:\neth_dst: {}, eth_src: {}, ipv6_src: {}, ipv6_dst: {}".format(
        gather_facts['dst_host_mac'], gather_facts['dst_router_mac'],
        ipv6_src, ipv6_dst)
    )

    try:
        testutils.verify_packet_any_port(ptfadapter, exp_pkt,
                                       gather_facts['dst_port_ids'],
                                       timeout=WAIT_EXPECTED_PACKET_TIMEOUT)
    except AssertionError as e:
        logger.error("Expected packet was not received")
        pytest.fail("Test case failed: {} - {}".format(test_name, str(e)))
    logger.info("Test case passed: {}\n".format(test_name))

