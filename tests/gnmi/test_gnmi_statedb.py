import logging
import pytest

from .helper import gnmi_get
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.bgp import get_bgp_peer_addr
from tests.common.helpers.route_helpers import add_static_route_to_dut, del_static_route_from_dut, get_route_count
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any')
]


# Route counter service runs every 30 seconds
# We need to wait for at least one full cycle plus buffer
ROUTE_COUNTER_UPDATE_TIMEOUT = 60


def get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default"):
    """
    Get total route count from gNMI for a specific address family and VRF

    Args:
        duthost: DUT host object
        ptfhost: PTF host object
        af: "IPV4" or "IPV6"
        vrf: VRF name (default: "default")

    Returns:
        int: Total route count from gNMI
    """
    path_list = [f"/sonic-db:STATE_DB/localhost/RIB_ROUTE_SUMMARY[key={af}|{vrf}]"]
    msg_list = gnmi_get(duthost, ptfhost, path_list)
    result = msg_list[0]
    count = int(result.split('"total_routes": "')[1].split('"')[0])
    return count


def get_protocol_route_count_from_gnmi(duthost, ptfhost, af, protocol, vrf="default"):
    """
    Get per-protocol route count from gNMI for a specific address family and VRF

    Args:
        duthost: DUT host object
        ptfhost: PTF host object
        af: "IPV4" or "IPV6"
        protocol: Protocol name (e.g., "connected", "static", "bgp")
        vrf: VRF name (default: "default")

    Returns:
        int: Protocol-specific route count from gNMI, or 0 if not found
    """
    path_list = [f"/sonic-db:STATE_DB/localhost/RIB_ROUTE_SUMMARY[key={af}|{vrf}]"]
    msg_list = gnmi_get(duthost, ptfhost, path_list)
    result = msg_list[0]

    field_name = f'"{protocol}_routes": "'
    if field_name in result:
        count = int(result.split(field_name)[1].split('"')[0])
        return count
    return 0


def test_gnmi_rib_route_summary_get(duthosts, rand_one_dut_hostname, ptfhost):
    """
    Test gNMI GET for RIB_ROUTE_SUMMARY table in STATE_DB
    Verifies that IPv4 and IPv6 total route counts can be retrieved via gNMI
    """
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("Skipping test as supervisor node does not have FRR routes")

    logger.info('Testing gNMI GET for RIB_ROUTE_SUMMARY fields')

    ipv4_count_cli, ipv6_count_cli = get_route_count(duthost)

    ipv4_count_gnmi = get_route_count_from_gnmi(duthost, ptfhost, "IPV4", vrf="default")
    pytest_assert(ipv4_count_gnmi == ipv4_count_cli,
                  f"IPv4 count mismatch: gNMI={ipv4_count_gnmi}, CLI={ipv4_count_cli}")

    ipv6_count_gnmi = get_route_count_from_gnmi(duthost, ptfhost, "IPV6", vrf="default")
    pytest_assert(ipv6_count_gnmi == ipv6_count_cli,
                  f"IPv6 count mismatch: gNMI={ipv6_count_gnmi}, CLI={ipv6_count_cli}")


def test_gnmi_rib_route_summary_table(duthosts, rand_one_dut_hostname, ptfhost):
    """
    Test gNMI GET for entire RIB_ROUTE_SUMMARY table
    Verifies that both IPv4 and IPv6 entries are returned with VRF-aware keys
    """
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("Skipping test as supervisor node does not have FRR routes")

    logger.info('Testing gNMI GET for RIB_ROUTE_SUMMARY table')

    path_list = ["/sonic-db:STATE_DB/localhost/RIB_ROUTE_SUMMARY"]
    msg_list = gnmi_get(duthost, ptfhost, path_list)
    result = msg_list[0]
    logger.info(f"gNMI GET result for RIB_ROUTE_SUMMARY table: {result}")

    # Verify IPv4 and IPv6 entries exist for default VRF
    pytest_assert("IPV4|default" in result, f"IPV4|default key not found in result: {result}")
    pytest_assert("IPV6|default" in result, f"IPV6|default key not found in result: {result}")

    # Verify total_routes field exists for both address families
    pytest_assert(result.count("total_routes") >= 2,
                  f"Expected at least 2 'total_routes' fields (one for IPv4, one for IPv6), result: {result}")


@pytest.mark.parametrize("ip_version", ["ipv4", "ipv6"])
def test_gnmi_rib_route_summary_add_remove(duthosts, rand_one_dut_hostname, ptfhost, tbinfo, ip_version):
    """
    Test that RIB_ROUTE_SUMMARY accurately reflects route additions and removals
    """
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("Skipping test as supervisor node does not have FRR routes")

    is_ipv6 = ip_version == "ipv6"
    af_name = "IPv6" if is_ipv6 else "IPv4"
    af = "IPV6" if is_ipv6 else "IPV4"

    logger.info(f'Testing route count changes with {af_name} route add/remove')

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    peer_addr = get_bgp_peer_addr(mg_facts, ipv6=is_ipv6)

    if not peer_addr:
        pytest.skip(f"No {af_name} BGP peer found for testing")

    test_prefix = "2001:db8:100::/64" if is_ipv6 else "192.168.100.0/24"
    route_added = False

    try:
        ipv4_count_before, ipv6_count_before = get_route_count(duthost)
        count_before = ipv6_count_before if is_ipv6 else ipv4_count_before

        count_gnmi_before = get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default")

        pytest_assert(count_gnmi_before == count_before,
                      f"Initial count mismatch: gNMI={count_gnmi_before}, CLI={count_before}")

        add_static_route_to_dut(duthost, test_prefix, peer_addr)
        route_added = True

        # Wait for CLI route count to update
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_route_count(duthost)[1 if is_ipv6 else 0] == count_before + 1),
            f"CLI route count did not increase to {count_before + 1} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        ipv4_count_after, ipv6_count_after = get_route_count(duthost)
        count_after = ipv6_count_after if is_ipv6 else ipv4_count_after

        # Wait for gNMI route count to update
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default") == count_after),
            f"gNMI route count did not update to {count_after} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        del_static_route_from_dut(duthost, test_prefix, peer_addr)

        # Wait for CLI route count to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_route_count(duthost)[1 if is_ipv6 else 0] == count_before),
            f"CLI route count did not return to {count_before} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        ipv4_count_final, ipv6_count_final = get_route_count(duthost)
        count_final = ipv6_count_final if is_ipv6 else ipv4_count_final

        # Wait for gNMI route count to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default") == count_final),
            f"gNMI route count did not return to {count_final} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        # Only mark as not added after we've verified deletion worked
        route_added = False
    finally:
        if route_added:
            try:
                del_static_route_from_dut(duthost, test_prefix, peer_addr)
            except Exception as e:
                # Ignore errors if route doesn't exist (may have been deleted already)
                if "doesnt exist" not in str(e):
                    raise


def test_gnmi_rib_route_summary_per_protocol(duthosts, rand_one_dut_hostname, ptfhost):
    """
    Test gNMI GET for per-protocol route counts in RIB_ROUTE_SUMMARY
    Verifies that protocol-specific route counts are available
    """
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("Skipping test as supervisor node does not have FRR routes")

    logger.info('Testing gNMI GET for per-protocol route counts')

    path_list = ["/sonic-db:STATE_DB/localhost/RIB_ROUTE_SUMMARY[key=IPV4|default]"]
    msg_list = gnmi_get(duthost, ptfhost, path_list)
    result = msg_list[0]

    pytest_assert("total_routes" in result, f"total_routes field not found in result: {result}")

    protocol_found = False
    for protocol in ["connected", "static", "bgp", "ospf", "kernel"]:
        field_name = f"{protocol}_routes"
        if field_name in result:
            protocol_found = True
            break

    pytest_assert(protocol_found,
                  "No protocol-specific route counts found in result. Expected at least one of: "
                  "connected_routes, static_routes, bgp_routes, ospf_routes, kernel_routes")


@pytest.mark.parametrize("ip_version", ["ipv4", "ipv6"])
def test_gnmi_rib_route_summary_static_protocol_count(duthosts, rand_one_dut_hostname, ptfhost,
                                                      tbinfo, ip_version):
    """
    Test that per-protocol route counts accurately reflect static route additions
    """
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("Skipping test as supervisor node does not have FRR routes")

    is_ipv6 = ip_version == "ipv6"
    af_name = "IPv6" if is_ipv6 else "IPv4"
    af = "IPV6" if is_ipv6 else "IPV4"

    logger.info(f'Testing {af_name} static route protocol count changes')

    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    peer_addr = get_bgp_peer_addr(mg_facts, ipv6=is_ipv6)

    if not peer_addr:
        pytest.skip(f"No {af_name} BGP peer found for testing")

    test_prefix = "2001:db8:200::/64" if is_ipv6 else "192.168.200.0/24"
    route_added = False

    try:
        # Get initial static route count
        static_count_before = get_protocol_route_count_from_gnmi(duthost, ptfhost, af, "static", vrf="default")
        total_count_before = get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default")

        add_static_route_to_dut(duthost, test_prefix, peer_addr)
        route_added = True

        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_protocol_route_count_from_gnmi(duthost, ptfhost, af, "static",
                                                                  vrf="default") == static_count_before + 1),
            f"Static route count did not increase to {static_count_before + 1} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        static_count_after = get_protocol_route_count_from_gnmi(duthost, ptfhost, af, "static", vrf="default")
        total_count_after = get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default")

        pytest_assert(static_count_after == static_count_before + 1,
                      f"Static route count should increase by 1: before={static_count_before}, "
                      f"after={static_count_after}")
        pytest_assert(total_count_after == total_count_before + 1,
                      f"Total route count should increase by 1: before={total_count_before}, "
                      f"after={total_count_after}")

        # Remove the static route
        del_static_route_from_dut(duthost, test_prefix, peer_addr)

        # Wait for counts to return to original
        pytest_assert(
            wait_until(ROUTE_COUNTER_UPDATE_TIMEOUT, 2, 0,
                       lambda: get_protocol_route_count_from_gnmi(duthost, ptfhost, af, "static",
                                                                  vrf="default") == static_count_before),
            f"Static route count did not return to {static_count_before} within {ROUTE_COUNTER_UPDATE_TIMEOUT}s"
        )

        static_count_final = get_protocol_route_count_from_gnmi(duthost, ptfhost, af, "static", vrf="default")
        total_count_final = get_route_count_from_gnmi(duthost, ptfhost, af, vrf="default")

        pytest_assert(static_count_final == static_count_before,
                      f"Static route count should return to original: before={static_count_before}, "
                      f"final={static_count_final}")
        pytest_assert(total_count_final == total_count_before,
                      f"Total route count should return to original: before={total_count_before}, "
                      f"final={total_count_final}")

        route_added = False
    finally:
        if route_added:
            try:
                del_static_route_from_dut(duthost, test_prefix, peer_addr)
            except Exception as e:
                # Ignore errors if route doesn't exist (may have been deleted already)
                if "doesnt exist" not in str(e):
                    raise
