import pytest
import logging
import time

from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.bgp import get_bgp_peer_addr
from tests.common.helpers.dut_utils import is_container_running
from tests.common.helpers.route_helpers import add_static_route_to_dut, del_static_route_from_dut, get_route_count
from tests.common.utilities import wait_until
from tests.syslog.syslog_utils import create_vrf, remove_vrf, check_vrf

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any')
]


IP_CIDR_ROUTE_NUMBER_OID = '1.3.6.1.2.1.4.24.3.0'
INET_CIDR_ROUTE_NUMBER_OID = '1.3.6.1.2.1.4.24.6.0'

# Route counter service runs on a 30s timer, wait a little extra in case it's slow
ROUTE_COUNTER_UPDATE_TIMEOUT = 45


def get_snmp_route_count(duthost, hostip, community, oid):
    """
    Get route count via SNMP

    Returns:
        int: Route count from SNMP
    """
    result = None
    snmp_cmd = f"docker exec snmp snmpget -v2c -c {community} {hostip} {oid}"

    def fetch_route_count():
        nonlocal result
        result = duthost.shell(snmp_cmd, module_ignore_errors=True)
        return result is not None and "No Such Instance" not in result["stdout"]
    pytest_assert(wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 5, 0, lambda: fetch_route_count()),
                  f"failed to find OID executing {snmp_cmd}")

    pytest_assert(result['rc'] == 0, f"SNMP query failed: {result.get('stderr', 'Unknown error')}")
    pytest_assert(result['stdout'], "SNMP query returned empty result")

    output = result['stdout']
    count = int(output.split()[-1])
    logger.info(f"Route count from SNMP OID {oid}: {count}")
    return count


def test_snmp_ipCidrRouteNumber(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts):
    """
    Test ipCidrRouteNumber SNMP OID (RFC 2096)
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]

    logger.info(f"Testing ipCidrRouteNumber on {duthost.hostname} ({hostip})")

    ipv4_count_cli, _ = get_route_count(duthost)
    snmp_count = get_snmp_route_count(duthost, hostip, community, IP_CIDR_ROUTE_NUMBER_OID)

    pytest_assert(snmp_count == ipv4_count_cli,
                  f"SNMP ipCidrRouteNumber ({snmp_count}) does not match CLI IPv4 count ({ipv4_count_cli})")


def test_snmp_inetCidrRouteNumber(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts):
    """
    Test inetCidrRouteNumber SNMP OID (RFC 4292)
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]

    logger.info(f"Testing inetCidrRouteNumber on {duthost.hostname} ({hostip})")

    ipv4_count_cli, ipv6_count_cli = get_route_count(duthost)
    total_count_cli = ipv4_count_cli + ipv6_count_cli

    snmp_count = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)

    pytest_assert(snmp_count == total_count_cli,
                  f"SNMP inetCidrRouteNumber ({snmp_count}) does not match CLI total count ({total_count_cli})")


def test_snmp_ipCidrRouteNumber_add_remove(duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                                           creds_all_duts, tbinfo):
    """
    Test that ipCidrRouteNumber accurately reflects IPv4 route additions and removals
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]

    logger.info(f"Testing ipCidrRouteNumber with IPv4 route add/remove on {duthost.hostname}")

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    peer_ipv4 = get_bgp_peer_addr(mg_facts, ipv6=False)

    if not peer_ipv4:
        pytest.skip("No IPv4 BGP peer found for testing")

    test_prefix = "192.168.100.0/24"
    route_added = False

    try:
        ipv4_count_before, _ = get_route_count(duthost)
        snmp_count_before = get_snmp_route_count(duthost, hostip, community, IP_CIDR_ROUTE_NUMBER_OID)

        pytest_assert(snmp_count_before == ipv4_count_before,
                      f"Initial count mismatch: SNMP={snmp_count_before}, CLI={ipv4_count_before}")

        add_static_route_to_dut(duthost, test_prefix, peer_ipv4)
        route_added = True

        # Wait for CLI route count to update
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_route_count(duthost)[0] == ipv4_count_before + 1),
            f"CLI route count did not increase to {ipv4_count_before + 1} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        ipv4_count_after, _ = get_route_count(duthost)
        logger.info(f"CLI IPv4 route count after add: {ipv4_count_after}")

        # Wait for SNMP route count to update
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_snmp_route_count(duthost, hostip,
                                                    community, IP_CIDR_ROUTE_NUMBER_OID) == ipv4_count_after),
            f"SNMP route count did not update to {ipv4_count_after} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        snmp_count_after = get_snmp_route_count(duthost, hostip, community, IP_CIDR_ROUTE_NUMBER_OID)
        logger.info(f"SNMP route count after add: {snmp_count_after}")

        del_static_route_from_dut(duthost, test_prefix, peer_ipv4)
        route_added = False

        # Wait for CLI route count to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_route_count(duthost)[0] == ipv4_count_before),
            f"CLI route count did not return to {ipv4_count_before} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        ipv4_count_final, _ = get_route_count(duthost)
        logger.info(f"CLI IPv4 route count after remove: {ipv4_count_final}")

        # Wait for SNMP route count to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_snmp_route_count(duthost, hostip,
                                                    community, IP_CIDR_ROUTE_NUMBER_OID) == ipv4_count_final),
            f"SNMP route count did not return to {ipv4_count_final} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        snmp_count_final = get_snmp_route_count(duthost, hostip, community, IP_CIDR_ROUTE_NUMBER_OID)
        logger.info(f"SNMP route count after remove: {snmp_count_final}")

        logger.info(f"Test passed: before={ipv4_count_before}, after_add={ipv4_count_after}, "
                    f"after_remove={ipv4_count_final}")
    finally:
        if route_added:
            logger.info(f"Cleanup: removing test route {test_prefix}")
            del_static_route_from_dut(duthost, test_prefix, peer_ipv4)


@pytest.mark.parametrize("ip_version", ["ipv4", "ipv6"])
def test_snmp_inetCidrRouteNumber_add_remove(duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                                             creds_all_duts, tbinfo, ip_version):
    """
    Test that inetCidrRouteNumber accurately reflects route additions and removals
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]

    is_ipv6 = ip_version == "ipv6"
    af_name = "IPv6" if is_ipv6 else "IPv4"

    logger.info(f"Testing inetCidrRouteNumber with {af_name} route add/remove on {duthost.hostname}")

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    peer_addr = get_bgp_peer_addr(mg_facts, ipv6=is_ipv6)

    if not peer_addr:
        pytest.skip(f"No {af_name} BGP peer found for testing")

    test_prefix = "2001:db8:100::/64" if is_ipv6 else "192.168.101.0/24"
    route_added = False

    try:
        ipv4_count_before, ipv6_count_before = get_route_count(duthost)
        total_count_before = ipv4_count_before + ipv6_count_before
        snmp_count_before = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)

        pytest_assert(snmp_count_before == total_count_before,
                      f"Initial count mismatch: SNMP={snmp_count_before}, CLI={total_count_before}")

        add_static_route_to_dut(duthost, test_prefix, peer_addr)
        route_added = True

        # Wait for CLI route count to update
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: sum(get_route_count(duthost)) == total_count_before + 1),
            f"CLI total route count did not increase to {total_count_before + 1} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        ipv4_count_after, ipv6_count_after = get_route_count(duthost)
        total_count_after = ipv4_count_after + ipv6_count_after
        logger.info(f"CLI total route count after add: {total_count_after} (IPv4={ipv4_count_after},"
                    f"IPv6={ipv6_count_after})")

        # Wait for SNMP route count to update
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_snmp_route_count(duthost, hostip,
                                                    community, INET_CIDR_ROUTE_NUMBER_OID) == total_count_after),
            f"SNMP route count did not update to {total_count_after} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        snmp_count_after = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)
        logger.info(f"SNMP route count after add: {snmp_count_after}")

        del_static_route_from_dut(duthost, test_prefix, peer_addr)
        route_added = False

        # Wait for CLI route count to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: sum(get_route_count(duthost)) == total_count_before),
            f"CLI total route count did not return to {total_count_before} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        ipv4_count_final, ipv6_count_final = get_route_count(duthost)
        total_count_final = ipv4_count_final + ipv6_count_final
        logger.info(f"CLI total route count after remove: {total_count_final} (IPv4={ipv4_count_final},"
                    f"IPv6={ipv6_count_final})")

        # Wait for SNMP route count to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_snmp_route_count(duthost, hostip,
                                                    community, INET_CIDR_ROUTE_NUMBER_OID) == total_count_final),
            f"SNMP route count did not return to {total_count_final} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        snmp_count_final = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)
        logger.info(f"SNMP route count after remove: {snmp_count_final}")

        logger.info(f"Test passed: before={total_count_before}, after_add={total_count_after}, "
                    f"after_remove={total_count_final}")
    finally:
        if route_added:
            logger.info(f"Cleanup: removing test route {test_prefix}")
            del_static_route_from_dut(duthost, test_prefix, peer_addr)


def test_snmp_inetCidrRouteNumber_ignores_non_default_vrf(duthosts, enum_rand_one_per_hwsku_frontend_hostname,
                                                          creds_all_duts, tbinfo, loganalyzer):
    """
    Test that SNMP route count only reports default VRF routes and ignores non-default VRF routes.
    This verifies that adding/removing routes in a non-default VRF does not affect the SNMP count.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    hostip = duthost.host.options['inventory_manager'].get_host(
        duthost.hostname).vars['ansible_host']
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]

    test_vrf = "VrfRed"
    test_prefix = "192.168.200.0/24"
    vrf_created = False
    route_added = False

    logger.info(f"Testing that SNMP route counts ignore non-default VRF routes on {duthost.hostname}")

    # Ignore expected FRR errors when creating VRF interface
    if loganalyzer:
        loganalyzer[duthost.hostname].ignore_regex.extend([
            r".*INTERFACE_STATE: Cannot find IF .* in VRF.*",
        ])

    try:
        # Get a nexthop from BGP peers to use for the test route
        mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
        peer_ipv4 = get_bgp_peer_addr(mg_facts, ipv6=False)

        if not peer_ipv4:
            pytest.skip("No IPv4 BGP peer found for testing")

        # Get initial counts from both CLI and SNMP
        ipv4_count_before, ipv6_count_before = get_route_count(duthost)
        snmp_count_before = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)
        total_count_before = ipv4_count_before + ipv6_count_before

        logger.info(f"Initial counts - CLI: {total_count_before}, SNMP: {snmp_count_before}")
        pytest_assert(snmp_count_before == total_count_before,
                      f"Initial count mismatch: SNMP={snmp_count_before}, CLI={total_count_before}")

        # Create the test VRF
        logger.info(f"Creating VRF {test_vrf}")
        create_vrf(duthost, test_vrf)
        vrf_created = True
        wait_until(10, 1, 0, lambda: check_vrf(duthost, test_vrf))

        # Add a route to the test VRF
        logger.info(f"Adding route {test_prefix} to VRF {test_vrf} via {peer_ipv4}")
        add_static_route_to_dut(duthost, test_prefix, peer_ipv4, vrf=test_vrf)
        route_added = True

        # Wait for route-counter service to run (it runs every 30 seconds)
        time.sleep(ROUTE_COUNTER_UPDATE_TIMEOUT)

        # Verify that CLI counts (default VRF only) have NOT changed
        ipv4_count_after, ipv6_count_after = get_route_count(duthost)
        total_count_after = ipv4_count_after + ipv6_count_after
        logger.info(f"After adding {test_vrf} route - CLI: {total_count_after}")

        pytest_assert(total_count_after == total_count_before,
                      f"CLI count should not change when adding {test_vrf} route: "
                      f"before={total_count_before}, after={total_count_after}")

        # Verify that SNMP counts have NOT changed
        snmp_count_after = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)
        logger.info(f"After adding {test_vrf} route - SNMP: {snmp_count_after}")

        pytest_assert(snmp_count_after == snmp_count_before,
                      f"SNMP count should not change when adding {test_vrf} route: "
                      f"before={snmp_count_before}, after={snmp_count_after}")

        # Remove the route from test VRF
        logger.info(f"Removing route {test_prefix} from VRF {test_vrf}")
        del_static_route_from_dut(duthost, test_prefix, peer_ipv4, vrf=test_vrf)
        route_added = False

        # Wait for route-counter service to run again
        time.sleep(ROUTE_COUNTER_UPDATE_TIMEOUT)

        # Verify counts are still unchanged
        ipv4_count_final, ipv6_count_final = get_route_count(duthost)
        total_count_final = ipv4_count_final + ipv6_count_final
        snmp_count_final = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)

        logger.info(f"After removing {test_vrf} route - CLI: {total_count_final}, SNMP: {snmp_count_final}")

        pytest_assert(total_count_final == total_count_before,
                      f"CLI count should remain unchanged: before={total_count_before}, final={total_count_final}")
        pytest_assert(snmp_count_final == snmp_count_before,
                      f"SNMP count should remain unchanged: before={snmp_count_before}, final={snmp_count_final}")

        logger.info(f"Test passed: {test_vrf} routes correctly ignored by SNMP agent")

    finally:
        # Cleanup in reverse order: route first, then VRF
        if route_added:
            logger.info(f"Cleanup: removing test route {test_prefix} from VRF {test_vrf}")
            del_static_route_from_dut(duthost, test_prefix, peer_ipv4, vrf=test_vrf)

        if vrf_created:
            logger.info(f"Cleanup: removing VRF {test_vrf}")
            remove_vrf(duthost, test_vrf)
            wait_until(10, 1, 0, lambda: not check_vrf(duthost, test_vrf))


def _test_snmp_bgp_down(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts, snmp_oid):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]

    logger.info(f"Testing inetCidrRouteNumber with BGP down on {duthost.hostname}")

    bgp_stopped = False
    try:
        # First verify we can get a route count with BGP running
        initial_count = get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)
        logger.info(f"Initial route count with BGP running: {initial_count}")
        pytest_assert(initial_count > 0, "Expected non-zero route count with BGP running")

        logger.info("Stopping BGP service")
        duthost.shell("sudo config feature state bgp disabled", module_ignore_errors=False)
        bgp_stopped = True
        wait_until(60, 10, 1, lambda: not is_container_running(duthost, "bgp"))

        logger.info(f"Waiting {ROUTE_COUNTER_UPDATE_TIMEOUT}s for route-counter to detect BGP is down")
        time.sleep(ROUTE_COUNTER_UPDATE_TIMEOUT)

        # Verify SNMP returns no value (not 0)
        snmp_cmd = f"docker exec snmp snmpget -v2c -c {community} {hostip} {INET_CIDR_ROUTE_NUMBER_OID}"
        result = duthost.shell(snmp_cmd, module_ignore_errors=True)

        logger.info(f"SNMP query result with BGP down: rc={result['rc']}, stdout={result.get('stdout', '')}, "
                    f"stderr={result.get('stderr', '')}")

        pytest_assert(
            "No Such Instance" in result.get('stdout', '') or
            "No Such Object" in result.get('stdout', ''),
            f"Expected SNMP to return no value when BGP is down, but got: {result}"
        )
    finally:
        if bgp_stopped:
            logger.info("Restarting BGP service")
            duthost.shell("sudo config feature state bgp enabled", module_ignore_errors=False)
            wait_until(60, 10, 1, lambda: is_container_running(duthost, "bgp"))
            # successful data retreival indicates data has quiesced.
            get_snmp_route_count(duthost, hostip, community, INET_CIDR_ROUTE_NUMBER_OID)


def test_snmp_ipCidrRouteNumber_bgp_down(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts):
    """
    Test that ipCidrRouteNumber behavior is consistent when BGP/FRR is down
    """
    _test_snmp_bgp_down(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts,
                        IP_CIDR_ROUTE_NUMBER_OID)


def test_snmp_inetCidrRouteNumber_bgp_down(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts):
    """
    Test that inetCidrRouteNumber behavior is consistent when BGP/FRR is down
    """
    _test_snmp_bgp_down(duthosts, enum_rand_one_per_hwsku_frontend_hostname, creds_all_duts,
                        INET_CIDR_ROUTE_NUMBER_OID)
