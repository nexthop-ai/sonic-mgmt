import logging

from collections import namedtuple
import pytest
import time

from tests.common.devices.csonic import CsonicHost
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.gcu_utils import create_checkpoint, rollback, delete_checkpoint
from bgp_helpers import AddressAllocator, AddrAssignment, get_test_peerings

"""
This test verifies BGP MD5 authentication behavior in a VRF + subinterface + peer group setting

BGP MD5 authentication is basically TCP MD5 authentication on the initial handshake logic. The
logic is implemented in Linux kernel network stack. There are documented issues in various
versions of the kernel related to the combination of VRF + subinterface + ipv4/ipv6 addr family.

The test steps are:

1. Locate one "physical" peer link between DUT and an Arista cEOS switch
2. Remove all initial setup on the link ports, e.g. Ethernet100 on DUT side and Ethernet1 on cEOS
   side, including binding to ipv4 and ipv6 addresses. Make the ports in the default state
3. Create vrfs Vrf1 and Vrf2 on DUT
4. Create sub interfaces on the ports on DUT and cEOS, using the same vlan id on both sides
5. Use AddressAllocator to allocate new ipv4 and v6 addresses. Bind these addresses to the
   sub interfaces
6. On DUT side add the newly allocated cEOS peering addresses into the peer groups that already
   exists on the DUT. On cEOS side configure DUT peering addresses in BGP
7. Verify that BGP peerings establish as expected
8. Now configure MD5 authentication on one and both sides of the peering, and verify that
   ipv4 and ipv6 behave the same. With net.ipv4.tcp_l3mdev_exempt_ports=179 set, a VRF-bound
   MD5 listener is selected for VRF-slave traffic, so a peer whose key is missing/mismatched
   is held in Connect (rather than establishing unauthenticated on a global listener).
9. Restore DUT and cEOS to configurations before the test
"""

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.topology("t1")]

# The peering link selected for testing, and its associated BGP neighbor data
TestPeering = namedtuple(
    "TestPeering",
    [
        "dut_asn",
        "neighbor_asn",
        "local_port",
        "neighbor_port",
        "neighbor_device",
        "local_addr",
        "neighbor_addr",
        "local_addr_v6",
        "neighbor_addr_v6",
        "local_peer_group_v4",
        "local_peer_group_v6",
    ],
)


def select_test_peering(duthost, nbrhosts, bgp_facts, tbinfo):
    """
    Select one Ethernet peering link for the VRF auth test.

    Candidate filtering lives in get_test_peerings. This test only needs the first peer, and
    the rest of the file speaks in local_port / neighbor_device names, so adapt that result.
    """
    collected = get_test_peerings(duthost, nbrhosts, bgp_facts, tbinfo)
    if not collected.peerings:
        return None
    peering = collected.peerings[0]
    return TestPeering(
        dut_asn=collected.dut_asn,
        neighbor_asn=peering.eos_asn,
        local_port=peering.dut_peer_port,
        neighbor_port=peering.eos_peer_port,
        neighbor_device=peering.eos_device,
        local_addr=peering.dut_peer_addr,
        local_addr_v6=peering.dut_peer_addr_v6,
        neighbor_addr=peering.eos_peer_addr,
        neighbor_addr_v6=peering.eos_peer_addr_v6,
        local_peer_group_v4=peering.dut_peer_group,
        local_peer_group_v6=peering.dut_peer_group_v6,
    )


class TestSetup:
    def __init__(self, test_peering: TestPeering):
        self.test_peering = test_peering
        self.test_addrs_used = []


@pytest.fixture(scope="module")
def setup(duthosts, nbrhosts, rand_one_dut_hostname, tbinfo):
    # The neighbor port is set up with EOS interface commands (dot1q sub-interfaces, `no
    # switchport`), which vtysh on a cSONiC neighbor cannot apply.
    if any(isinstance(nbr["host"], CsonicHost) for nbr in nbrhosts.values()):
        pytest.skip("test_bgp_vrf_authentication requires EOS neighbors (EOS sub-interface config)")

    duthost = duthosts[rand_one_dut_hostname]
    bgp_facts = duthost.bgp_facts()["ansible_facts"]
    test_peering = select_test_peering(duthost, nbrhosts, bgp_facts, tbinfo)
    if not test_peering:
        pytest.fail("Cannot find BGP peering for test")
        return

    create_checkpoint(duthost)

    test_setup = TestSetup(test_peering)

    yield test_setup

    try:
        # Remove the BGP VRF instances before removing the VRFs themselves.
        for vrf in ["Vrf1", "Vrf2"]:
            duthost.shell(
                f'vtysh -c "configure terminal" -c "no router bgp {test_setup.test_peering.dut_asn} vrf {vrf}"',
                module_ignore_errors=True,
            )
        duthost.no_vrf("Vrf1")
        duthost.no_vrf("Vrf2")
        restore_eos_device_settings(test_setup)
        rollback(duthost)
    finally:
        delete_checkpoint(duthost)


def restore_eos_device_settings(test_setup: TestSetup):
    """
    Restore the peering link on eos device to original configuration
    """
    test_peering = test_setup.test_peering
    eos_dev = test_peering.neighbor_device
    eos_dev.config(lines=[f"default interface {test_peering.neighbor_port}"])
    eos_dev.config(
        lines=[
            f"no interface {test_peering.neighbor_port}.10",
            f"no interface {test_peering.neighbor_port}.20",
        ]
    )

    eos_dev.config(
        lines=[
            "no switchport",
            f"ip address {test_peering.neighbor_addr}/31",
            f"ipv6 address {test_peering.neighbor_addr_v6}/126",
            "ipv6 enable",
            "ipv6 nd ra disabled",
        ],
        parents=[f"interface {test_peering.neighbor_port}"],
    )
    for addrs in test_setup.test_addrs_used:
        eos_dev.config(
            lines=[f"no neighbor {addrs.local_addr}"],
            parents=[f"router bgp {test_peering.neighbor_asn}"],
        )
        eos_dev.config(
            lines=[f"no neighbor {addrs.local_addr_v6}"],
            parents=[f"router bgp {test_peering.neighbor_asn}"],
        )


def test_bgp_vrf_authentication(duthosts, rand_one_dut_hostname, nbrhosts, tbinfo, setup):

    addr_allocator = AddressAllocator("20.0.0.2", "fc01::02")

    dut_host = duthosts[rand_one_dut_hostname]

    # This test asserts the fixed behaviour, which requires BGP (port 179) to be exempted from
    # tcp_l3mdev_accept via net.ipv4.tcp_l3mdev_exempt_ports (kernel patch from
    # private-sonic-linux-kernel PR #216, applied in the image by the companion sonic-buildimage
    # sysctl config). Assert it up front so a missing/unapplied exemption fails here with a clear
    # signature instead of a cryptic wait_until timeout later.
    exempt = dut_host.shell(
        "sysctl -n net.ipv4.tcp_l3mdev_exempt_ports", module_ignore_errors=True
    )
    pytest_assert(
        exempt["rc"] == 0,
        "net.ipv4.tcp_l3mdev_exempt_ports sysctl is missing; the kernel lacks the patch "
        "(private-sonic-linux-kernel PR #216). stderr={!r}".format(exempt.get("stderr")),
    )
    pytest_assert(
        "179" in exempt["stdout"].replace(",", " ").split(),
        "BGP port 179 is not exempted (net.ipv4.tcp_l3mdev_exempt_ports={!r}); the image "
        "sysctl config is missing or not applied.".format(exempt["stdout"]),
    )

    dut_host.create_vrf("Vrf1")
    dut_host.create_vrf("Vrf2")

    test_peering = setup.test_peering

    # Reset local and neighbor port configuration
    dut_host.remove_ip_from_port(test_peering.local_port, test_peering.local_addr)
    dut_host.remove_ip_from_port(test_peering.local_port, test_peering.local_addr_v6)

    # Clear configuration on the selected port on peering switch, and set it as trunk port
    eos_dev = test_peering.neighbor_device
    eos_dev.config(lines=[f"default interface {test_peering.neighbor_port}"])
    eos_dev.config(lines=["no switchport"], parents=[f"interface {test_peering.neighbor_port}"])

    def verify_local_interface_in_vrf(vrf_name, intf_name):
        result = dut_host.shell(f"ip link show | grep {intf_name}")["stdout"]
        for line in result.split("\n"):
            if intf_name in line and vrf_name in line:
                return True
        return False

    def add_bgp_sub_interface_peering(vrf_name, vlan_number):
        """
        Allocate sub interface on local and remote port with the same vlan id
        Allocate pairs of IP and IPv6 addresses to local and remote sub interfaces
        Configure BGP peering using the newly allocated IP and IPv6 addresses, using existing
        V4 and V6 peer groups
        """
        local_sub_intf = f"{test_peering.local_port}.{vlan_number}"
        dut_host.add_sub_interface(test_peering.local_port, vlan_number)
        dut_host.move_interface_to_vrf(local_sub_intf, vrf_name)

        addrs = addr_allocator.next_assignment()
        dut_host.add_ip_addr_to_port(local_sub_intf, f"{addrs.local_addr}/31")
        dut_host.add_ip_addr_to_port(local_sub_intf, f"{addrs.local_addr_v6}/127")

        eos_sub_intf = f"{test_peering.neighbor_port}.{vlan_number}"
        eos_dev.config(
            [
                f"encapsulation dot1q vlan {vlan_number}",
                f"ip address {addrs.remote_addr}/31",
                f"ipv6 address {addrs.remote_addr_v6}/127",
            ],
            parents=[f"interface {eos_sub_intf}"],
        )

        # Add v4 neighbor peering
        dut_host.bgp_add_neighbor(
            test_peering.dut_asn, addrs.remote_addr, test_peering.neighbor_asn, vrf=vrf_name
        )
        dut_host.bgp_add_neighbor_to_peer_group(
            test_peering.dut_asn, addrs.remote_addr, test_peering.local_peer_group_v4, vrf=vrf_name
        )

        eos_dev.bgp_add_neighbor(test_peering.neighbor_asn, addrs.local_addr, test_peering.dut_asn)
        eos_dev.bgp_set_nexthop_self(test_peering.neighbor_asn, addrs.local_addr)

        # Add v6 neighbor peering
        dut_host.bgp_add_neighbor(
            test_peering.dut_asn, addrs.remote_addr_v6, test_peering.neighbor_asn, vrf=vrf_name
        )
        dut_host.bgp_add_neighbor_to_peer_group(
            test_peering.dut_asn,
            addrs.remote_addr_v6,
            test_peering.local_peer_group_v6,
            vrf=vrf_name,
        )

        eos_dev.bgp_add_neighbor(
            test_peering.neighbor_asn, addrs.local_addr_v6, test_peering.dut_asn
        )
        eos_dev.bgp_set_nexthop_self(test_peering.neighbor_asn, addrs.local_addr_v6)

        return addrs

    def shutdown_interfaces():
        dut_host.shutdown(test_peering.local_port)
        eos_dev.shutdown(test_peering.neighbor_port)

    def activate_interfaces():
        dut_host.no_shutdown(test_peering.local_port)
        eos_dev.no_shutdown(test_peering.neighbor_port)

    def activate_neighbors(addrs: AddrAssignment, vrf_name):
        # Activate neighbor in one address family
        for addr, addr_family, no_addr_family in zip(
            [addrs.remote_addr, addrs.remote_addr_v6], ["ipv4", "ipv6"], ["ipv6", "ipv4"]
        ):
            dut_host.bgp_activate_neighbor(
                test_peering.dut_asn, addr, addr_family=addr_family, vrf=vrf_name
            )
            dut_host.no_bgp_activate_neighbor(
                test_peering.dut_asn, addr, addr_family=no_addr_family, vrf=vrf_name
            )

        for addr, addr_family, no_addr_family in zip(
            [addrs.local_addr, addrs.local_addr_v6], ["ipv4", "ipv6"], ["ipv6", "ipv4"]
        ):
            eos_dev.bgp_activate_neighbor(test_peering.neighbor_asn, addr, addr_family=addr_family)
            eos_dev.no_bgp_activate_neighbor(
                test_peering.neighbor_asn, addr, addr_family=no_addr_family
            )

    def verify_bgp_neighbor(neighbors, vrf_name, state):
        if isinstance(state, str):
            state = [state]
        time.sleep(0.1)

        def verify_():
            collected = set()
            for addr_family, dict_key in zip(["ipv4", "ipv6"], ["ipv4Unicast", "ipv6Unicast"]):
                peers = dut_host.bgp_show_ip_bgp_summary_json(addr_family, vrf=vrf_name)
                for peer, peer_data in peers[dict_key]["peers"].items():
                    if peer in neighbors and peer_data["state"] in state:
                        collected.add(peer)
            return len(collected) == len(neighbors)

        return wait_until(120, 5, 0, verify_)

    shutdown_interfaces()

    # Add bgp peer group names in vrfs
    for vrf in ["Vrf1", "Vrf2"]:
        for group_name in [test_peering.local_peer_group_v4, test_peering.local_peer_group_v6]:
            dut_host.bgp_add_peer_group(test_peering.dut_asn, group_name, vrf=vrf)

    # Configure BGP peering in vrf, with new sub interfaces, and with new allocated ip and ipv6 addresses
    vrf1_addrs = add_bgp_sub_interface_peering("Vrf1", 10)
    vrf2_addrs = add_bgp_sub_interface_peering("Vrf2", 20)

    setup.test_addrs_used.append(vrf1_addrs)
    setup.test_addrs_used.append(vrf2_addrs)

    activate_interfaces()

    pytest_assert(verify_local_interface_in_vrf("Vrf1", f"{test_peering.local_port}.10"))
    pytest_assert(verify_local_interface_in_vrf("Vrf2", f"{test_peering.local_port}.20"))

    activate_neighbors(vrf1_addrs, "Vrf1")
    activate_neighbors(vrf2_addrs, "Vrf2")

    # Without any MD5 password, verify both v4 and v6 peerings are established
    pytest_assert(
        verify_bgp_neighbor(
            [vrf1_addrs.remote_addr, vrf1_addrs.remote_addr_v6], "Vrf1", state="Established"
        )
    )
    pytest_assert(
        verify_bgp_neighbor(
            [vrf2_addrs.remote_addr, vrf2_addrs.remote_addr_v6], "Vrf2", state="Established"
        )
    )

    # Set v4 peer group password in Vrf1, verify v4 BGP peer is in Connect state, v6 still in Established state
    dut_host.bgp_set_peer_password(
        test_peering.dut_asn, test_peering.local_peer_group_v4, "12345", vrf="Vrf1"
    )
    eos_dev.bgp_restart_all()

    pytest_assert(verify_bgp_neighbor([vrf1_addrs.remote_addr], "Vrf1", state="Connect"))
    pytest_assert(not verify_bgp_neighbor([vrf1_addrs.remote_addr_v6], "Vrf1", state="Connect"))

    # Set the v6 peer group password in Vrf1. EOS has no matching key yet, so the v6 Vrf1
    # peer now moves to Connect (the fixed behaviour explained at the assertion below).
    dut_host.bgp_set_peer_password(
        test_peering.dut_asn, test_peering.local_peer_group_v6, "12345", vrf="Vrf1"
    )
    eos_dev.bgp_restart_all()

    pytest_assert(verify_bgp_neighbor([vrf1_addrs.remote_addr], "Vrf1", state="Connect"))
    # With net.ipv4.tcp_l3mdev_exempt_ports=179 the VRF-bound MD5 listener wins the listener
    # lookup (the unbound default-VRF listener is no longer eligible for l3mdev-slave traffic),
    # so the unsigned SYN (EOS has no password yet) is dropped and the v6 Vrf1 peer is Connect.
    pytest_assert(verify_bgp_neighbor([vrf1_addrs.remote_addr_v6], "Vrf1", state="Connect"))

    # Set neighbor password for all neighbors in neighbor switch
    for addr in [
        vrf1_addrs.local_addr,
        vrf1_addrs.local_addr_v6,
        vrf2_addrs.local_addr,
        vrf2_addrs.local_addr_v6,
    ]:
        eos_dev.bgp_set_peer_password(test_peering.neighbor_asn, addr, "12345")
    eos_dev.bgp_restart_all()

    # Verify v4 peer on Vrf1 is Established because both sides have passwords.
    # v4 peer on Vrf2 is Connect because dut_host does not set password on Vrf2 local_peer_group_v4.
    # For v6 EOS switch does require auth now. Vrf2 now in Connect
    pytest_assert(verify_bgp_neighbor([vrf1_addrs.remote_addr], "Vrf1", state="Established"))
    pytest_assert(verify_bgp_neighbor([vrf2_addrs.remote_addr], "Vrf2", state="Connect"))

    # Both sides now have the v6 Vrf1 password, so with net.ipv4.tcp_l3mdev_exempt_ports=179 the
    # VRF-bound MD5 listener authenticates the session and the v6 Vrf1 peer is Established.
    pytest_assert(verify_bgp_neighbor([vrf1_addrs.remote_addr_v6], "Vrf1", state="Established"))
    pytest_assert(verify_bgp_neighbor([vrf2_addrs.remote_addr_v6], "Vrf2", state="Connect"))

    logging.info("test_bgp_vrf_authentication done")
