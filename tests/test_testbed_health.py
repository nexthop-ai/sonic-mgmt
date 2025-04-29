import logging
import pytest
import concurrent.futures
from tests.common.platform.device_utils import check_interfaces_and_transceivers, \
    check_neighbors
# list_dut_fanout_connections
from tests.common.helpers.constants import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer,
    pytest.mark.skip_check_dut_health,
    pytest.mark.sanity_check(skip_sanity=True)
]


def collect_vmhost_facts(nbrhosts):
    vmhosts = {}
    for name, v in list(nbrhosts.items()):
        vmhosts[name] = {}
        vmhosts[name]["name"] = name
        vmhosts[name]["vmname"] = v["host"].hostname
        vmhosts[name]["host"] = v["host"]
        vmhosts[name]["ip_ifs"] = v["host"].show_ip_interface()["ansible_facts"]["ip_interfaces"]
    return vmhosts


def check_peers_expected_interfaces(tbinfo, vmhosts):
    for peer, val in tbinfo["topo"]["properties"]["configuration"].items():
        # Validate interfaces
        for intf, intf_val in val["interfaces"].items():
            if intf_val.get("ipv4") is None and intf_val.get("ipv6") is None:
                continue
            # Config uses Port-Channel1, whereas SONiC uses PortChannel1
            intf = intf.replace("-", "")
            if not vmhosts[peer]["ip_ifs"].get(intf):
                pytest.fail("PEER {}({}) does not have required interface {}".format(
                    vmhosts[peer]["vmname"], peer, intf))


def check_peers_expected_bgp(tbinfo, vmhosts):
    for peer, val in tbinfo["topo"]["properties"]["configuration"].items():
        # Validate BGP peers are shown as neighbors
        if val.get("bgp") is not None and val["bgp"].get("peers") is not None:
            for asn, remotelist in val["bgp"]["peers"].items():
                for ip in remotelist:
                    found = False
                    # Skip IPv6
                    if ":" in ip:
                        continue
                    # Search for a match of ip in any interface peer
                    for intf, intf_val in vmhosts[peer]["ip_ifs"].items():
                        if intf_val["peer_ipv4"] == ip:
                            found = True
                    if not found:
                        pytest.fail("PEER {}({}) does not have an interface with known neighbor {}".format(
                            vmhosts[peer]["vmname"], peer, ip))


def check_peers_link_status(vmhosts):
    for peer in vmhosts:
        for intf, val in vmhosts[peer]["ip_ifs"].items():
            if not val.get("peer_ipv4") or val["peer_ipv4"] == "N/A":
                continue
            if not val["admin"] == "up":
                pytest.fail("PEER {}({}) Port {} is not admin up".format(vmhosts[peer]["vmname"], peer, intf))
            if not val["oper_state"] == "up":
                pytest.fail("PEER {}({}) Port {} is not oper_state up".format(vmhosts[peer]["vmname"], peer, intf))


def check_peers_ping_dut(request, vmhosts):
    if request.config.getoption("neighbor_type") != "sonic":
        logging.info("Only SONiC neighbors are supported for ping")
        return

    def ping_host(vmhost, srcip, ip, intf):
        logging.info("Ping from {}({}) interface {} ip {} to {}".format(
            vmhost["vmname"], vmhost["name"], intf, srcip, ip))
        return vmhost["host"].ping_v4(ip, count=1, ns_arg=DEFAULT_NAMESPACE, intf=intf, ttl=1)

    success = True
    msg = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {}

        for peer in vmhosts:
            for intf, val in vmhosts[peer]["ip_ifs"].items():
                if not val.get("peer_ipv4") or val["peer_ipv4"] == "N/A":
                    continue

                data = {}
                data["vmhost"] = vmhosts[peer]
                data["intf"] = intf
                data["srcip"] = val["ipv4"]
                data["ip"] = val["peer_ipv4"]

                future = executor.submit(ping_host, data["vmhost"], data["srcip"], data["ip"], data["intf"])
                futures[future] = data

        for future in concurrent.futures.as_completed(futures):
            data = futures[future]
            result = future.result()
            if not result:
                success = False
                msg = "PEER {}({}) failed to ping {} from {} using ip {}".format(
                        data["vmhost"]["vmname"], data["vmhost"]["name"], data["srcip"], data["intf"], data["ip"])
                logging.warning(msg)

    if not success:
        pytest.fail(msg)


def check_dut_ping_peers(duthosts):
    def ping_host(duthost, srcip, ip, intf):
        logging.info("Ping from DUT {} interface {} ip {} to {}".format(duthost.hostname, intf, srcip, ip))
        return duthost.ping_v4(ip, count=1, ns_arg=DEFAULT_NAMESPACE, intf=intf, ttl=1)

    success = True
    msg = ""

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {}

        for duthost in duthosts:
            ip_ifs = duthost.show_ip_interface()["ansible_facts"]["ip_interfaces"]
            for intf, val in ip_ifs.items():
                if not val.get("peer_ipv4") or val["peer_ipv4"] == "N/A":
                    continue

                data = {}
                data["duthost"] = duthost
                data["intf"] = intf
                data["srcip"] = val["ipv4"]
                data["ip"] = val["peer_ipv4"]

                future = executor.submit(ping_host, data["duthost"], data["srcip"], data["ip"], data["intf"])
                futures[future] = data

        for future in concurrent.futures.as_completed(futures):
            data = futures[future]
            result = future.result()
            if not result:
                success = False
                msg = "DUT {}failed to ping {} from {} using ip {}".format(
                        data["duthost"].hostname, data["srcip"], data["intf"], data["ip"])
                logging.warning(msg)

    if not success:
        pytest.fail(msg)


def test_testbed_health(duthosts, fanouthosts, request, tbinfo, nbrhosts):
    """
       - Checks link status on DUTs and Peers
       - Checks all expected interfaces exist on the Peers
       - Checks BGP configuration is proper on the peers
       - Checks (sonic) peers can ping the DUT on peer interfaces
       - Checks the DUT can ping the peers on peer interfaces
       - Validates all expected BGP sessions are online on the DUT
    """
    # Check the DUT to make sure all interfaces expected to be online are actually
    # online. This is a built-in helper already available.
    logging.info("Check link status on all DUT interfaces")
    check_interfaces_and_transceivers(duthosts, request)

    # Collect facts
    vmhosts = collect_vmhost_facts(nbrhosts)

    # Cycle through TestBed info and make sure each expected peer interface exists
    logging.info("Check PEERs all have expected interfaces")
    check_peers_expected_interfaces(tbinfo, vmhosts)

    logging.info("Check PEERs are properly configured for BGP")
    check_peers_expected_bgp(tbinfo, vmhosts)

    # Check the Peers/Neighbors to ensure all interfaces expected to be online
    # are actually online.
    logging.info("Check link status on all PEERs")
    check_peers_link_status(vmhosts)

    # If the peers are Sonic, cycle through them and ping through the
    # connected interfaces to ensure they work.
    logging.info("Check PEERs ping to DUT")
    check_peers_ping_dut(request, vmhosts)

    logging.info("Checking DUT ping to PEERs")
    check_dut_ping_peers(duthosts)

    # Verify all BGP neighbors are showing as online on the DUT
    logging.info("Checking DUT BGP sessions are online")
    for duthost in duthosts:
        check_neighbors(duthost, tbinfo)

    logging.info("Testbed Health Good")
