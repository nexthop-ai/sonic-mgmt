import ipaddress
import json
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


def _compute_intf_to_peer_ips(ip_ifs, bgp_peers):
    """
    Returns a mapping of interface name to a list of peer IPs.

    This is helpful for EOS VM where show ip interface does not include the peer infos.
    So, we are combining the data here.
    """
    intf_to_peer_ips = {}
    # 1. For each interface
    for intf, val in ip_ifs.items():
        intf_to_peer_ips[intf] = []

        # 2. with an IP subnet,
        ip_addr = "{}/{}".format(
            val["interfaceAddressBrief"]["ipAddr"]["address"], val["interfaceAddressBrief"]["ipAddr"]["maskLen"]
        )
        intf_ip_net = ipaddress.ip_network(ip_addr, strict=False)

        # 3. add all the peer IPs that are in the same subnet.
        for peer_ip in bgp_peers.keys():
            if ipaddress.ip_address(peer_ip) in intf_ip_net:
                intf_to_peer_ips[intf].append(peer_ip)
    return intf_to_peer_ips


def collect_vmhost_facts(request, nbrhosts):
    vmhosts = {}
    for name, v in list(nbrhosts.items()):
        vmhosts[name] = {}
        vmhosts[name]["name"] = name
        vmhosts[name]["vmname"] = v["host"].hostname
        vmhosts[name]["host"] = v["host"]
        if request.config.getoption("neighbor_type") == "eos":
            # Example output:
            # {'interfaces': {
            #   'Ethernet1': {
            #     'name': 'Ethernet1',
            #     'lineProtocolStatus': 'up',
            #     'interfaceStatus': 'connected',
            #     'interfaceAddressBrief': {
            #       'ipAddr': {'address': '10.0.0.1', 'maskLen': 31}
            #     },
            #     ...
            #   },
            #   'Ethernet5': {...},
            #   'Loopback0': {...},
            #   'Management0': {...}
            # }}
            show_ip_ifs = v["host"].eos_command(commands=["show ip interface | json"])["stdout"][0]
            # Example output:
            # {'vrfs': {
            #   'default': {
            #     'peers': {
            #       '10.0.0.0': {'description': '65100', 'version': 4, 'asn': '65100', ...},
            #       '10.10.28.254': {...}
            #     },
            #     ...
            #   }
            # }}
            show_ip_bgp_sum = v["host"].eos_command(commands=["show ip bgp summary | json"])["stdout"][0]
            vmhosts[name]["ip_ifs"] = show_ip_ifs["interfaces"]
            vmhosts[name]["bgp_peers"] = show_ip_bgp_sum["vrfs"]["default"]["peers"]
            vmhosts[name]["ifs_to_peer_ips"] = _compute_intf_to_peer_ips(
                vmhosts[name]["ip_ifs"], vmhosts[name]["bgp_peers"]
            )
        else:
            vmhosts[name]["ip_ifs"] = v["host"].show_ip_interface()["ansible_facts"]["ip_interfaces"]
    logger.debug(f"raw vmhost facts:\n{json.dumps(vmhosts, indent=4, default=str)}")
    return vmhosts


def check_peers_expected_interfaces(request, tbinfo, vmhosts):
    for peer, val in tbinfo["topo"]["properties"]["configuration"].items():
        # Validate interfaces
        for intf, intf_val in val["interfaces"].items():
            if intf_val.get("ipv4") is None and intf_val.get("ipv6") is None:
                continue
            if request.config.getoption("neighbor_type") == "sonic":
                # Config uses Port-Channel1, whereas SONiC uses PortChannel1
                intf = intf.replace("-", "")
            if not vmhosts[peer]["ip_ifs"].get(intf):
                pytest.fail("PEER {}({}) does not have required interface {}".format(
                    vmhosts[peer]["vmname"], peer, intf))


def check_peers_expected_bgp(request, tbinfo, vmhosts):
    for peer, val in tbinfo["topo"]["properties"]["configuration"].items():
        # Validate BGP peers are shown as neighbors
        if val.get("bgp") is not None and val["bgp"].get("peers") is not None:
            for asn, remotelist in val["bgp"]["peers"].items():
                for ip in remotelist:
                    # Skip IPv6
                    if ":" in ip:
                        continue
                    found = False
                    if request.config.getoption("neighbor_type") == "eos":
                        found = ip in vmhosts[peer]["bgp_peers"]
                    else:
                        # Search for a match of ip in any interface peer
                        for intf, intf_val in vmhosts[peer]["ip_ifs"].items():
                            if intf_val["peer_ipv4"] == ip:
                                found = True
                    if not found:
                        pytest.fail("PEER {}({}) does not have an interface with known neighbor {}".format(
                            vmhosts[peer]["vmname"], peer, ip))


def check_peers_link_status(request, vmhosts):
    for peer in vmhosts:
        if request.config.getoption("neighbor_type") == "eos":
            for intf, peer_ips in vmhosts[peer]["ifs_to_peer_ips"].items():
                if not peer_ips:
                    continue
                if vmhosts[peer]["ip_ifs"][intf]["lineProtocolStatus"] != "up":
                    pytest.fail(
                        "PEER {}({}) Port {} is not lineProtocolStatus up".format(
                            vmhosts[peer]["vmname"], peer, intf
                        )
                    )
                if vmhosts[peer]["ip_ifs"][intf]["interfaceStatus"] != "connected":
                    pytest.fail(
                        "PEER {}({}) Port {} is not interfaceStatus connected".format(
                            vmhosts[peer]["vmname"], peer, intf
                        )
                    )
        else:
            for intf, val in vmhosts[peer]["ip_ifs"].items():
                if not val.get("peer_ipv4") or val["peer_ipv4"] == "N/A":
                    continue
                if val["admin"] != "up":
                    pytest.fail("PEER {}({}) Port {} is not admin up".format(vmhosts[peer]["vmname"], peer, intf))
                if val["oper_state"] != "up":
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
    vmhosts = collect_vmhost_facts(request, nbrhosts)

    # Cycle through TestBed info and make sure each expected peer interface exists
    logging.info("Check PEERs all have expected interfaces")
    check_peers_expected_interfaces(request, tbinfo, vmhosts)

    logging.info("Check PEERs are properly configured for BGP")
    check_peers_expected_bgp(request, tbinfo, vmhosts)

    # Check the Peers/Neighbors to ensure all interfaces expected to be online
    # are actually online.
    logging.info("Check link status on all PEERs")
    check_peers_link_status(request, vmhosts)

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
