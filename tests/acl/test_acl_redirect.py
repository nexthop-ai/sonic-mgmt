"""
Test ACL redirect functionality for both IPv4 and IPv6 packets.

Verifies that ACL rules with REDIRECT_ACTION forward matching traffic to the
specified nexthop, both via a physical port and via a LAG.
"""

import logging
import time
import pytest
import json
import ptf.testutils as testutils
import ptf.mask as mask
import ptf.packet as packet

from tests.common.helpers.assertions import pytest_assert, pytest_require
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("any")
]

DUT_TMP_DIR = "/tmp/acl_redirect_test"
PBR_TABLE_TYPE = "PBR"
PBRV6_TABLE_TYPE = "PBRV6"
PBR_TABLE_NAME_V4 = "ACL_REDIRECT_TEST_V4"
PBR_TABLE_NAME_V6 = "ACL_REDIRECT_TEST_V6"
PBR_RULE_1 = "redirect_rule1"
PBR_RULE_2 = "redirect_rule2"
SECURITY_TABLE_NAME_V4 = "SECURITY_ACL_TEST_V4"
SECURITY_TABLE_NAME_V6 = "SECURITY_ACL_TEST_V6"
SECURITY_RULE_1 = "security_rule1"
SECURITY_RULE_2 = "security_rule2"
ECMP_TABLE_NAME = "ACL_REDIRECT_ECMP"
ECMP_RULE_NAME = "ecmp_redirect_rule1"

# Test packet source IPs (arbitrary — used only for ACL matching, not routing)
TEST_SRC_IP_V4 = "10.0.0.100"
TEST_SRC_IP_V6 = "2001:db8:1::100"
TEST_TCP_SPORT = 12345
TEST_TCP_DPORT = 80

# Additional source IPs for interplay test scenarios
INTERPLAY_REDIRECT_SRC_V4 = "10.0.0.200"
INTERPLAY_REDIRECT_SRC_V6 = "2001:db8:1::200"
INTERPLAY_NORMAL_SRC_V4 = "10.0.0.50"
INTERPLAY_NORMAL_SRC_V6 = "2001:db8:1::50"


@pytest.fixture(scope="module")
def setup_acl_redirect(duthosts, rand_one_dut_hostname, tbinfo):
    """
    Gather test topology info: source port, physical nexthop port, and optional LAG nexthop.

    Discovers L3 interfaces and their peer addresses from minigraph_interfaces
    (physical ports) and minigraph_portchannel_interfaces (LAGs), rather than
    from minigraph_neighbors (LLDP), since LLDP runs on physical ports only.

    Returns a dict with port names, PTF port IDs, and nexthop peer addresses.
    """
    duthost = duthosts[rand_one_dut_hostname]
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
    ptf_indices = mg_facts['minigraph_ptf_indices']

    # Build interface -> {v4: peer_addr, v6: peer_addr} from L3 interface tables.
    # minigraph_interfaces covers routed physical ports;
    # minigraph_portchannel_interfaces covers PortChannels.
    intf_peers = {}
    for entry in mg_facts.get('minigraph_interfaces', []):
        port = entry['attachto']
        intf_peers.setdefault(port, {})
        af = 'v6' if ':' in entry['addr'] else 'v4'
        intf_peers[port][af] = entry['peer_addr']

    for entry in mg_facts.get('minigraph_portchannel_interfaces', []):
        port = entry['attachto']
        intf_peers.setdefault(port, {})
        af = 'v6' if ':' in entry['addr'] else 'v4'
        intf_peers[port][af] = entry['peer_addr']

    # Physical ports: routed interfaces that have PTF port mappings and both v4+v6 peers
    physical_ports = {
        port: peers for port, peers in intf_peers.items()
        if not port.startswith('PortChannel')
        and port in ptf_indices
        and 'v4' in peers and 'v6' in peers
    }

    physical_port_list = sorted(physical_ports.keys())
    pytest_require(len(physical_port_list) >= 2,
                   "Need at least 2 physical ports with both IPv4 and IPv6 peers")

    src_port = physical_port_list[0]
    nh_port = physical_port_list[1]
    nh_addrs = physical_ports[nh_port]

    # LAG nexthop (optional — tests requiring a LAG will skip if absent)
    lag_name = None
    lag_nh_v4 = None
    lag_nh_v6 = None
    lag_member_port_ids = []

    for port, peers in intf_peers.items():
        if not port.startswith('PortChannel'):
            continue
        if 'v4' not in peers and 'v6' not in peers:
            continue
        lag_name = port
        lag_nh_v4 = peers.get('v4')
        lag_nh_v6 = peers.get('v6')
        lag_members = list(config_facts.get('PORTCHANNEL_MEMBER', {}).get(lag_name, {}).keys())
        lag_member_port_ids = [ptf_indices[m] for m in lag_members if m in ptf_indices]
        break

    # Normal forwarding target for the interplay test — needs to be a different
    # port than nh_port so that normal routing is distinguishable from redirect.
    # Prefer a LAG (any LAG has a distinct set of PTF ports); fall back to a
    # third physical port if no LAG is available.
    if lag_name and (lag_nh_v4 or lag_nh_v6):
        normal_fwd_nh_v4 = lag_nh_v4
        normal_fwd_nh_v6 = lag_nh_v6
        normal_fwd_port_ids = lag_member_port_ids
    elif len(physical_port_list) >= 3:
        third_port = physical_port_list[2]
        normal_fwd_nh_v4 = physical_ports[third_port].get('v4')
        normal_fwd_nh_v6 = physical_ports[third_port].get('v6')
        normal_fwd_port_ids = [ptf_indices[third_port]]
    else:
        normal_fwd_nh_v4 = None
        normal_fwd_nh_v6 = None
        normal_fwd_port_ids = []

    logger.info(f"ACL redirect setup: src={src_port}, nh_port={nh_port} "
                f"(v4={nh_addrs['v4']}, v6={nh_addrs['v6']}), "
                f"lag={lag_name} (v4={lag_nh_v4}, v6={lag_nh_v6}, "
                f"member_ptf_ids={lag_member_port_ids}), "
                f"normal_fwd (v4={normal_fwd_nh_v4}, v6={normal_fwd_nh_v6}, "
                f"port_ids={normal_fwd_port_ids})")

    duthost.shell(f"mkdir -p {DUT_TMP_DIR}")

    # Set ACL counter poll interval to 1s for faster counter verification.
    # Save the current interval so we can restore it in teardown.
    acl_poll_result = duthost.shell(
        'sonic-db-cli CONFIG_DB hget "FLEX_COUNTER_TABLE|ACL" "POLL_INTERVAL"',
        module_ignore_errors=True
    )
    original_acl_poll_interval = acl_poll_result['stdout'].strip() or "10000"
    duthost.shell('counterpoll acl interval 1000')
    logger.info(f"Set ACL counter poll interval to 1000ms (was {original_acl_poll_interval}ms)")

    # Save existing ACL config and remove all ACL tables/rules so they don't
    # interfere with redirect testing (e.g. existing L3 tables on the same ports).
    acl_backup = {}
    for table_key in ["ACL_TABLE", "ACL_RULE"]:
        result = duthost.shell(
            f'sonic-cfggen -d --var-json "{table_key}"',
            module_ignore_errors=True
        )
        if result['rc'] == 0 and result['stdout'].strip():
            acl_backup[table_key] = json.loads(result['stdout'])
        else:
            acl_backup[table_key] = {}

    if acl_backup["ACL_TABLE"]:
        logger.info(f"Backing up existing ACL tables: {list(acl_backup['ACL_TABLE'].keys())}")
        # Remove all existing rules first, then tables
        for rule_key in acl_backup["ACL_RULE"]:
            duthost.shell(
                f'sonic-db-cli CONFIG_DB DEL "ACL_RULE|{rule_key.replace("|", "|")}"',
                module_ignore_errors=True
            )
        for table_name in acl_backup["ACL_TABLE"]:
            duthost.shell(
                f'sonic-db-cli CONFIG_DB DEL "ACL_TABLE|{table_name}"',
                module_ignore_errors=True
            )
        # Wait for orchagent to process the removals
        time.sleep(5)

    # Define custom PBR/PBRV6 ACL table types for redirect rules.
    # Using a separate type from L3/L3V6 allows a redirect table and a security
    # table to coexist on the same port.
    pbr_type_config = {
        "ACL_TABLE_TYPE": {
            PBR_TABLE_TYPE: {
                "MATCHES": [
                    "SRC_IP",
                    "DST_IP",
                    "IP_PROTOCOL",
                    "L4_SRC_PORT",
                    "L4_DST_PORT",
                    "DSCP",
                    "IN_PORTS"
                ],
                "ACTIONS": [
                    "PACKET_ACTION",
                    "REDIRECT_ACTION",
                    "COUNTER"
                ],
                "BIND_POINTS": [
                    "PORT",
                    "PORTCHANNEL"
                ]
            },
            PBRV6_TABLE_TYPE: {
                "MATCHES": [
                    "SRC_IPV6",
                    "DST_IPV6",
                    "IP_PROTOCOL",
                    "L4_SRC_PORT",
                    "L4_DST_PORT",
                    "DSCP",
                    "IN_PORTS"
                ],
                "ACTIONS": [
                    "PACKET_ACTION",
                    "REDIRECT_ACTION",
                    "COUNTER"
                ],
                "BIND_POINTS": [
                    "PORT",
                    "PORTCHANNEL"
                ]
            }
        }
    }
    pbr_config_file = f"{DUT_TMP_DIR}/pbr_table_types.json"
    duthost.copy(content=json.dumps(pbr_type_config, indent=4), dest=pbr_config_file)
    result = duthost.shell(f"sonic-cfggen -j {pbr_config_file} --write-to-db")
    pytest_assert(result['rc'] == 0, f"Failed to create PBR table types: {result['stderr']}")
    logger.info(f"Created custom ACL table types: {PBR_TABLE_TYPE}, {PBRV6_TABLE_TYPE}")

    yield {
        'duthost': duthost,
        'router_mac': duthost.facts['router_mac'],
        'src_port': src_port,
        'src_port_id': mg_facts['minigraph_ptf_indices'][src_port],
        'physical_nh_port': nh_port,
        'physical_nh_port_id': mg_facts['minigraph_ptf_indices'][nh_port],
        'physical_nh_v4': nh_addrs['v4'],
        'physical_nh_v6': nh_addrs['v6'],
        'lag_name': lag_name,
        'lag_nh_v4': lag_nh_v4,
        'lag_nh_v6': lag_nh_v6,
        'lag_member_port_ids': lag_member_port_ids,
        'normal_fwd_nh_v4': normal_fwd_nh_v4,
        'normal_fwd_nh_v6': normal_fwd_nh_v6,
        'normal_fwd_port_ids': normal_fwd_port_ids,
        'intf_peers': intf_peers,
        'ptf_indices': ptf_indices,
        'config_facts': config_facts,
    }

    # Remove custom PBR table types
    for type_name in [PBR_TABLE_TYPE, PBRV6_TABLE_TYPE]:
        duthost.shell(
            f'sonic-db-cli CONFIG_DB DEL "ACL_TABLE_TYPE|{type_name}"',
            module_ignore_errors=True
        )

    # Restore original ACL config
    if acl_backup.get("ACL_TABLE"):
        logger.info(f"Restoring ACL tables: {list(acl_backup['ACL_TABLE'].keys())}")
        restore_config = {}
        if acl_backup["ACL_TABLE"]:
            restore_config["ACL_TABLE"] = acl_backup["ACL_TABLE"]
        if acl_backup["ACL_RULE"]:
            restore_config["ACL_RULE"] = acl_backup["ACL_RULE"]
        restore_file = f"{DUT_TMP_DIR}/acl_restore.json"
        duthost.copy(content=json.dumps(restore_config, indent=4), dest=restore_file)
        duthost.shell(f"sonic-cfggen -j {restore_file} --write-to-db",
                      module_ignore_errors=True)

    # Restore ACL counter poll interval
    duthost.shell(f'counterpoll acl interval {original_acl_poll_interval}',
                  module_ignore_errors=True)
    logger.info(f"Restored ACL counter poll interval to {original_acl_poll_interval}ms")

    duthost.shell(f"rm -rf {DUT_TMP_DIR}", module_ignore_errors=True)


@pytest.fixture(scope="function")
def cleanup_acl_rules(setup_acl_redirect):
    """Remove ACL tables created by each test."""
    yield
    duthost = setup_acl_redirect['duthost']
    for table_name in [PBR_TABLE_NAME_V4, PBR_TABLE_NAME_V6]:
        duthost.shell(
            f'sonic-db-cli CONFIG_DB DEL "ACL_RULE|{table_name}|{PBR_RULE_1}"',
            module_ignore_errors=True
        )
        duthost.shell(
            f'sonic-db-cli CONFIG_DB DEL "ACL_TABLE|{table_name}"',
            module_ignore_errors=True
        )


def verify_acl_table_active(duthost, table_name, expected_rules=None):
    """Assert that an ACL table and its rules are installed and Active.

    Args:
        duthost: DUT host object
        table_name: Name of the ACL table
        expected_rules: List of rule names that should be present (optional).
                        If provided, verifies each rule appears in 'show acl rule' output.
    """
    def _table_and_rules_active():
        # Check table is Active
        table_result = duthost.shell(f"show acl table {table_name}")
        table_active = False
        for line in table_result['stdout'].split('\n'):
            fields = line.split()
            if fields and fields[0] == table_name and 'Active' in line:
                table_active = True
                break
        if not table_active:
            return False

        # Check all expected rules are present
        if expected_rules:
            rule_result = duthost.shell(f"show acl rule {table_name}")
            for rule_output in rule_result['stdout'].split('\n'):
                fields = rule_output.split()
                if fields and fields[1] in expected_rules and 'Active' in rule_output:
                    expected_rules.remove(fields[1])

        if expected_rules:
            return False

        return True

    pytest_assert(
        wait_until(30, 2, 0, _table_and_rules_active),
        f"ACL table {table_name} or its rules {expected_rules} not Active — "
    )
    logger.info(f"ACL table {table_name} is Active with rules {expected_rules}")


def create_acl_table_and_rule(duthost, table_name, table_type, src_port, nexthop_ip,
                              src_ip_field, src_ip_value, dst_ip_field, dst_ip_value):
    """
    Write an ACL table + redirect rule to ConfigDB via sonic-cfggen.
    """
    acl_config = {
        "ACL_TABLE": {
            table_name: {
                "policy_desc": "ACL redirect test",
                "type": table_type,
                "ports": [src_port],
                "stage": "INGRESS"
            }
        },
        "ACL_RULE": {
            f"{table_name}|{PBR_RULE_1}": {
                "priority": "100",
                src_ip_field: src_ip_value,
                dst_ip_field: dst_ip_value,
                "REDIRECT_ACTION": nexthop_ip,
            }
        },
    }

    config_file = f"{DUT_TMP_DIR}/acl_redirect_{table_name}.json"
    duthost.copy(content=json.dumps(acl_config, indent=4), dest=config_file)

    result = duthost.shell(f"sonic-cfggen -j {config_file} --write-to-db")
    pytest_assert(result['rc'] == 0, f"Failed to apply ACL config: {result['stderr']}")

    verify_acl_table_active(duthost, table_name, expected_rules=[PBR_RULE_1])


def get_acl_rule_counter(duthost, table_name, rule_name):
    """Get the packet count for a specific ACL rule via aclshow.

    With -t and -r flags, aclshow returns a single data line for the rule.
    """
    result = duthost.shell(f"aclshow -t {table_name} -r {rule_name}")
    for line in result['stdout'].split('\n'):
        fields = line.split()
        # Skip header/separator lines; data line has rule_name as first field
        if fields and fields[0] == rule_name:
            try:
                return int(fields[3])
            except (ValueError, IndexError):
                return 0
    return 0


def verify_acl_rule_hit(duthost, table_name, rule_name, count_before, packets_sent):
    """Assert that an ACL rule's packet counter increased by exactly packets_sent.

    Counters may take time to update in hardware, so this polls with wait_until.
    """
    def _check_counter():
        count_after = get_acl_rule_counter(duthost, table_name, rule_name)
        logger.info(f"ACL counter for {table_name}|{rule_name}: "
                    f"{count_before} -> {count_after} (expecting +{packets_sent})")
        return count_after >= count_before + packets_sent

    pytest_assert(
        wait_until(5, 1, 0, _check_counter),
        f"ACL rule {table_name}|{rule_name} counter did not reach expected value "
        f"(expected {count_before + packets_sent}, sent {packets_sent})"
    )


def get_neighbor_mac(duthost, nexthop_ip):
    """Get the MAC address for a nexthop IP from the DUT's neighbor table."""
    result = duthost.shell(f"ip neigh show {nexthop_ip}")
    for line in result['stdout'].strip().split('\n'):
        if 'lladdr' in line:
            parts = line.split()
            return parts[parts.index('lladdr') + 1]
    pytest.fail(f"No ARP/NDP entry found for nexthop {nexthop_ip}")


def build_test_packet(ptfadapter, router_mac, src_ip, dst_ip, src_port_id):
    """Build a TCP test packet — IPv6 if src_ip contains ':', else IPv4."""
    common = dict(
        eth_dst=router_mac,
        eth_src=ptfadapter.dataplane.get_mac(0, src_port_id),
        tcp_sport=TEST_TCP_SPORT,
        tcp_dport=TEST_TCP_DPORT,
    )
    if ':' in src_ip:
        return testutils.simple_tcpv6_packet(
            ipv6_src=src_ip, ipv6_dst=dst_ip, ipv6_hlim=64, **common
        )
    return testutils.simple_tcp_packet(
        ip_src=src_ip, ip_dst=dst_ip, ip_ttl=64, **common
    )


def build_expected_packet(pkt, is_ipv6, expected_dst_mac=None, expected_src_mac=None):
    """
    Build a masked expected packet.

    When expected_dst_mac / expected_src_mac are provided the L2 addresses are
    checked (used to verify the DUT rewrote them correctly on redirect).
    Otherwise they are masked out.
    """
    exp = pkt.copy()
    if expected_dst_mac:
        exp[packet.Ether].dst = expected_dst_mac
    if expected_src_mac:
        exp[packet.Ether].src = expected_src_mac

    # TTL/hlim is decremented by 1 during L3 forwarding
    if is_ipv6:
        exp[packet.IPv6].hlim -= 1
    else:
        exp[packet.IP].ttl -= 1

    exp_pkt = mask.Mask(exp)
    if expected_dst_mac is None:
        exp_pkt.set_do_not_care_scapy(packet.Ether, "dst")
    if expected_src_mac is None:
        exp_pkt.set_do_not_care_scapy(packet.Ether, "src")
    exp_pkt.set_do_not_care_scapy(packet.TCP, "chksum")
    if not is_ipv6:
        exp_pkt.set_do_not_care_scapy(packet.IP, "chksum")
    return exp_pkt


def send_and_verify_redirect(ptfadapter, pkt, exp_pkt, src_port_id, redirect_port_ids, count=500):
    """Send packets and verify at least one arrives on one of the expected redirect ports."""
    ptfadapter.dataplane.flush()
    logger.info(f"Sending {count} packets from port {src_port_id}, "
                f"expecting on ports {redirect_port_ids}")
    testutils.send(ptfadapter, src_port_id, pkt, count=count)
    testutils.verify_packet_any_port(ptfadapter, exp_pkt, redirect_port_ids, timeout=10)
    logger.info("Packet redirect verified successfully")


def _run_acl_redirect_test(setup, ptfadapter, ip_version, nexthop_type):
    """Shared test logic for ACL redirect across IP versions and nexthop types.

    DST_IP is a directly connected peer so the DUT has a real route for it.
    The redirect nexthop sends the packet to a DIFFERENT port than the one the
    DST_IP route points to, proving the ACL redirect overrode normal routing.

    - port redirect: DST_IP = normal_fwd peer (routes to normal_fwd, redirect to nh_port)
    - lag  redirect: DST_IP = nh_port peer    (routes to nh_port, redirect to LAG)
    """
    is_v6 = (ip_version == "v6")

    if nexthop_type == "port":
        nexthop_ip = setup['physical_nh_v6'] if is_v6 else setup['physical_nh_v4']
        redirect_port_ids = [setup['physical_nh_port_id']]
        # DST_IP routes to normal_fwd; redirect overrides to nh_port (different port)
        dst_ip = setup['normal_fwd_nh_v6'] if is_v6 else setup['normal_fwd_nh_v4']
        pytest_require(dst_ip is not None,
                       "Need a third interface for directly-connected DST_IP")
    else:  # lag
        nexthop_ip = setup['lag_nh_v6'] if is_v6 else setup['lag_nh_v4']
        redirect_port_ids = setup['lag_member_port_ids']
        pytest_require(
            nexthop_ip is not None and redirect_port_ids,
            f"No LAG with IP{ip_version.upper()} BGP neighbor found in the system"
        )
        # DST_IP routes to nh_port; redirect overrides to LAG (different port)
        dst_ip = setup['physical_nh_v6'] if is_v6 else setup['physical_nh_v4']

    src_ip = TEST_SRC_IP_V6 if is_v6 else TEST_SRC_IP_V4
    table_name = PBR_TABLE_NAME_V6 if is_v6 else PBR_TABLE_NAME_V4
    table_type = PBRV6_TABLE_TYPE if is_v6 else PBR_TABLE_TYPE
    src_field = "SRC_IPV6" if is_v6 else "SRC_IP"
    dst_field = "DST_IPV6" if is_v6 else "DST_IP"

    duthost = setup['duthost']
    logger.info(f"Testing IP{ip_version.upper()} redirect to {nexthop_ip} via {nexthop_type}")

    # Resolve nexthop MAC for L2 verification
    nh_mac = get_neighbor_mac(duthost, nexthop_ip)
    pkt = build_test_packet(ptfadapter, setup['router_mac'], src_ip, dst_ip, setup['src_port_id'])
    exp_pkt = build_expected_packet(pkt, is_v6,
                                    expected_dst_mac=nh_mac,
                                    expected_src_mac=setup['router_mac'])

    create_acl_table_and_rule(
        duthost=duthost,
        table_name=table_name,
        table_type=table_type,
        src_port=setup['src_port'],
        nexthop_ip=nexthop_ip,
        src_ip_field=src_field,
        src_ip_value=src_ip,
        dst_ip_field=dst_field,
        dst_ip_value=dst_ip,
    )

    count_before = get_acl_rule_counter(duthost, table_name, PBR_RULE_1)

    send_and_verify_redirect(
        ptfadapter=ptfadapter,
        pkt=pkt,
        exp_pkt=exp_pkt,
        src_port_id=setup['src_port_id'],
        redirect_port_ids=redirect_port_ids,
    )

    verify_acl_rule_hit(duthost, table_name, PBR_RULE_1, count_before, 500)


@pytest.mark.parametrize("nexthop_type", ["port", "lag"])
def test_acl_redirect_ipv4(setup_acl_redirect, ptfadapter, cleanup_acl_rules, nexthop_type):
    """Test ACL redirect for IPv4 packets via a physical port or LAG nexthop."""
    pytest_require(setup_acl_redirect['duthost'].facts['asic_type'] != 'vs',
                   "ACL redirect test not supported on VS platform")
    _run_acl_redirect_test(setup_acl_redirect, ptfadapter, "v4", nexthop_type)


@pytest.mark.parametrize("nexthop_type", ["port", "lag"])
def test_acl_redirect_ipv6(setup_acl_redirect, ptfadapter, cleanup_acl_rules, nexthop_type):
    """Test ACL redirect for IPv6 packets via a physical port or LAG nexthop."""
    pytest_require(setup_acl_redirect['duthost'].facts['asic_type'] != 'vs',
                   "ACL redirect test not supported on VS platform")
    _run_acl_redirect_test(setup_acl_redirect, ptfadapter, "v6", nexthop_type)


# ---------------------------------------------------------------------------
# Security ACL + PBR redirect interplay tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def cleanup_acl_interplay(setup_acl_redirect):
    """Remove both security and PBR ACL tables created by interplay tests."""
    yield
    duthost = setup_acl_redirect['duthost']
    # Rules first, then tables
    rules_to_delete = [
        (SECURITY_TABLE_NAME_V4, SECURITY_RULE_1),
        (SECURITY_TABLE_NAME_V6, SECURITY_RULE_1),
        (SECURITY_TABLE_NAME_V4, SECURITY_RULE_2),
        (SECURITY_TABLE_NAME_V6, SECURITY_RULE_2),
        (PBR_TABLE_NAME_V4, PBR_RULE_1),
        (PBR_TABLE_NAME_V4, PBR_RULE_2),
        (PBR_TABLE_NAME_V6, PBR_RULE_1),
        (PBR_TABLE_NAME_V6, PBR_RULE_2),
    ]
    for table_name, rule_name in rules_to_delete:
        duthost.shell(
            f'sonic-db-cli CONFIG_DB DEL "ACL_RULE|{table_name}|{rule_name}"',
            module_ignore_errors=True
        )
    for table_name in [SECURITY_TABLE_NAME_V4, SECURITY_TABLE_NAME_V6,
                       PBR_TABLE_NAME_V4, PBR_TABLE_NAME_V6]:
        duthost.shell(
            f'sonic-db-cli CONFIG_DB DEL "ACL_TABLE|{table_name}"',
            module_ignore_errors=True
        )


def create_interplay_acls(duthost, src_port, security_table, security_type,
                          pbr_table, pbr_type, nexthop_ip,
                          src_field, dst_field, drop_src, drop_dst,
                          redirect_src, redirect_dst):
    """Create security ACL (L3/L3V6, DROP) and PBR ACL (two redirect rules) in one shot.

    Security table:
      - rule: DROP matching drop_src / drop_dst

    PBR table:
      - PBR_RULE_1: REDIRECT matching drop_src / drop_dst (same as security drop)
      - PBR_RULE_2:    REDIRECT matching redirect_src / redirect_dst (not in security ACL)
    """
    acl_config = {
        "ACL_TABLE": {
            security_table: {
                "policy_desc": "Security ACL - drop",
                "type": security_type,
                "ports": [src_port],
                "stage": "INGRESS"
            },
            pbr_table: {
                "policy_desc": "PBR ACL - redirect",
                "type": pbr_type,
                "ports": [src_port],
                "stage": "INGRESS"
            }
        },
        "ACL_RULE": {
            f"{security_table}|{SECURITY_RULE_1}": {
                "priority": "100",
                src_field: drop_src,
                dst_field: drop_dst,
                "PACKET_ACTION": "DROP",
            },
            f"{security_table}|{SECURITY_RULE_2}": {
                "priority": "100",
                src_field: redirect_src,
                dst_field: redirect_dst,
                "PACKET_ACTION": "FORWARD",
            },
            f"{pbr_table}|{PBR_RULE_1}": {
                "priority": "100",
                src_field: drop_src,
                dst_field: drop_dst,
                "REDIRECT_ACTION": nexthop_ip,
            },
            f"{pbr_table}|{PBR_RULE_2}": {
                "priority": "99",
                src_field: redirect_src,
                dst_field: redirect_dst,
                "REDIRECT_ACTION": nexthop_ip,
            },
        },
    }

    config_file = f"{DUT_TMP_DIR}/acl_interplay.json"
    duthost.copy(content=json.dumps(acl_config, indent=4), dest=config_file)

    result = duthost.shell(f"sonic-cfggen -j {config_file} --write-to-db")
    pytest_assert(result['rc'] == 0, f"Failed to apply interplay ACL config: {result['stderr']}")

    verify_acl_table_active(duthost, security_table,
                            expected_rules=[SECURITY_RULE_1, SECURITY_RULE_2])
    verify_acl_table_active(duthost, pbr_table,
                            expected_rules=[PBR_RULE_1, PBR_RULE_2])


def _run_acl_interplay_test(setup, ptfadapter, ip_version):
    """Send three packets to verify security ACL / PBR redirect interplay.

    Tables created (both bound to src_port at ingress):
      Security ACL (L3):  DROP rule matching TEST_SRC / dst_ip
      PBR ACL:            REDIRECT rule 1 matching TEST_SRC / dst_ip (same as security)
                          REDIRECT rule 2 matching INTERPLAY_REDIRECT_SRC / dst_ip

    All rules share the same DST_IP (directly connected peer of normal_fwd).
    Different SRC_IPs select which rules match:
      1. TEST_SRC         -> dst_ip : matches security DROP + PBR rule 1   -> dropped
      2. REDIRECT_SRC     -> dst_ip : matches PBR rule 2 only              -> redirected to nh_port
      3. NORMAL_SRC       -> dst_ip : matches neither ACL                  -> normal routing to normal_fwd
    """
    is_v6 = (ip_version == "v6")

    dst_ip = setup['normal_fwd_nh_v6'] if is_v6 else setup['normal_fwd_nh_v4']
    normal_fwd_port_ids = setup['normal_fwd_port_ids']
    pytest_require(
        dst_ip is not None and normal_fwd_port_ids,
        "Need a third interface (LAG or physical port) with BGP peer for interplay test"
    )

    nexthop_ip = setup['physical_nh_v6'] if is_v6 else setup['physical_nh_v4']
    redirect_port_id = setup['physical_nh_port_id']

    drop_src = TEST_SRC_IP_V6 if is_v6 else TEST_SRC_IP_V4
    redirect_src = INTERPLAY_REDIRECT_SRC_V6 if is_v6 else INTERPLAY_REDIRECT_SRC_V4
    normal_src = INTERPLAY_NORMAL_SRC_V6 if is_v6 else INTERPLAY_NORMAL_SRC_V4

    security_table = SECURITY_TABLE_NAME_V6 if is_v6 else SECURITY_TABLE_NAME_V4
    pbr_table = PBR_TABLE_NAME_V6 if is_v6 else PBR_TABLE_NAME_V4
    src_field = "SRC_IPV6" if is_v6 else "SRC_IP"
    dst_field = "DST_IPV6" if is_v6 else "DST_IP"

    duthost = setup['duthost']

    # Resolve nexthop MAC for L2 verification on redirect
    nh_mac = get_neighbor_mac(duthost, nexthop_ip)

    create_interplay_acls(
        duthost=duthost,
        src_port=setup['src_port'],
        security_table=security_table,
        security_type="L3V6" if is_v6 else "L3",
        pbr_table=pbr_table,
        pbr_type=PBRV6_TABLE_TYPE if is_v6 else PBR_TABLE_TYPE,
        nexthop_ip=nexthop_ip,
        src_field=src_field,
        dst_field=dst_field,
        drop_src=drop_src,
        drop_dst=dst_ip,
        redirect_src=redirect_src,
        redirect_dst=dst_ip,
    )

    # --- Scenario 1: matches security DROP + PBR rule 1 → dropped ---
    logger.info(f"Scenario 1: {drop_src} -> {dst_ip} — expect DROP")
    pkt = build_test_packet(ptfadapter, setup['router_mac'], drop_src, dst_ip,
                            setup['src_port_id'])
    exp_pkt = build_expected_packet(pkt, is_v6)
    sec_count_before = get_acl_rule_counter(duthost, security_table, SECURITY_RULE_1)
    ptfadapter.dataplane.flush()
    testutils.send(ptfadapter, setup['src_port_id'], pkt, count=100)
    testutils.verify_no_packet(ptfadapter, exp_pkt, redirect_port_id, timeout=5)
    verify_acl_rule_hit(duthost, security_table, SECURITY_RULE_1, sec_count_before, 100)
    logger.info("Scenario 1 passed: packet dropped by security ACL")

    # --- Scenario 2: matches security FORWARD + PBR rule 2 → redirected to nh_port ---
    logger.info(f"Scenario 2: {redirect_src} -> {dst_ip} — expect REDIRECT to port "
                f"{redirect_port_id}")
    pkt = build_test_packet(ptfadapter, setup['router_mac'], redirect_src, dst_ip,
                            setup['src_port_id'])
    exp_pkt = build_expected_packet(pkt, is_v6,
                                    expected_dst_mac=nh_mac,
                                    expected_src_mac=setup['router_mac'])
    sec_count_before = get_acl_rule_counter(duthost, security_table, SECURITY_RULE_2)
    send_and_verify_redirect(ptfadapter, pkt, exp_pkt, setup['src_port_id'], [redirect_port_id])
    verify_acl_rule_hit(duthost, security_table, SECURITY_RULE_2, sec_count_before, 500)
    logger.info("Scenario 2 passed: packet redirected with correct L2")

    # --- Scenario 3: matches neither ACL → normal routing to normal_fwd ---
    logger.info(f"Scenario 3: {normal_src} -> {dst_ip} — expect normal forwarding via "
                f"ports {normal_fwd_port_ids}")
    pkt = build_test_packet(ptfadapter, setup['router_mac'], normal_src, dst_ip,
                            setup['src_port_id'])
    exp_pkt = build_expected_packet(pkt, is_v6)
    send_and_verify_redirect(ptfadapter, pkt, exp_pkt, setup['src_port_id'], normal_fwd_port_ids)
    logger.info("Scenario 3 passed: packet forwarded normally")


def test_acl_security_redirect_interplay_ipv4(setup_acl_redirect, ptfadapter,
                                              cleanup_acl_interplay):
    """Test security ACL (L3) / PBR redirect interplay for IPv4.

    - Packet matching security DROP rule: dropped (PBR redirect does not fire)
    - Packet matching only PBR redirect rule: redirected to nexthop
    - Packet matching neither ACL: forwarded normally via route table
    """
    pytest_require(setup_acl_redirect['duthost'].facts['asic_type'] != 'vs',
                   "ACL interplay test not supported on VS platform")
    _run_acl_interplay_test(setup_acl_redirect, ptfadapter, "v4")


def test_acl_security_redirect_interplay_ipv6(setup_acl_redirect, ptfadapter,
                                              cleanup_acl_interplay):
    """Test security ACL (L3V6) / PBR redirect interplay for IPv6.

    - Packet matching security DROP rule: dropped (PBR redirect does not fire)
    - Packet matching only PBR redirect rule: redirected to nexthop
    - Packet matching neither ACL: forwarded normally via route table
    """
    pytest_require(setup_acl_redirect['duthost'].facts['asic_type'] != 'vs',
                   "ACL interplay test not supported on VS platform")
    _run_acl_interplay_test(setup_acl_redirect, ptfadapter, "v6")


# ---------------------------------------------------------------------------
# ACL redirect to ECMP nexthop test
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def cleanup_acl_ecmp(setup_acl_redirect):
    """Remove ECMP ACL table and rule after test."""
    yield
    duthost = setup_acl_redirect['duthost']
    duthost.shell(
        f'sonic-db-cli CONFIG_DB DEL "ACL_RULE|{ECMP_TABLE_NAME}|{ECMP_RULE_NAME}"',
        module_ignore_errors=True
    )
    duthost.shell(
        f'sonic-db-cli CONFIG_DB DEL "ACL_TABLE|{ECMP_TABLE_NAME}"',
        module_ignore_errors=True
    )


def test_acl_redirect_ecmp(setup_acl_redirect, ptfadapter, cleanup_acl_ecmp):
    """Test ACL redirect with ECMP nexthop group.

    1. Pick 2 directly connected IPv4 peers on different interfaces.
    2. Create a PBR ACL rule with REDIRECT_ACTION set to "nh1,nh2"
       (comma-separated nexthop group).
    3. Send packets varying the L4 source port so the ECMP hash distributes
       them across both nexthops.
    4. Verify that packets are received on both nexthop interfaces with
       correct L2 rewrite.
    """
    setup = setup_acl_redirect
    duthost = setup['duthost']
    intf_peers = setup['intf_peers']
    ptf_indices = setup['ptf_indices']
    config_facts = setup['config_facts']
    src_port = setup['src_port']

    pytest_require(duthost.facts['asic_type'] != 'vs',
                   "ACL redirect ECMP test not supported on VS platform")

    # Collect all non-src interfaces with IPv4 peers and PTF port mappings.
    # Each entry is (port_name, peer_v4, [ptf_port_ids]).
    candidates = []
    for port, peers in sorted(intf_peers.items()):
        if port == src_port or 'v4' not in peers:
            continue
        if port.startswith('PortChannel'):
            members = list(
                config_facts.get('PORTCHANNEL_MEMBER', {})
                .get(port, {}).keys()
            )
            port_ids = [ptf_indices[m] for m in members
                        if m in ptf_indices]
        elif port in ptf_indices:
            port_ids = [ptf_indices[port]]
        else:
            continue
        if not port_ids:
            continue
        candidates.append((port, peers['v4'], port_ids))

    # Need at least 3: 2 for ECMP nexthops + 1 whose peer serves as DST_IP
    # (so the redirect overrides normal routing to that third interface).
    pytest_require(len(candidates) >= 3,
                   "Need at least 3 interfaces with IPv4 peers "
                   "(excluding src_port) for ECMP redirect test")

    # First 2 candidates are ECMP nexthops; third provides DST_IP.
    ecmp_members = [(c[1], c[2]) for c in candidates[:2]]
    dst_ip = candidates[2][1]  # peer of the third interface

    ecmp_nexthops = [m[0] for m in ecmp_members]
    logger.info(f"ECMP nexthops: {ecmp_nexthops}, "
                f"PTF port IDs per nexthop: {[m[1] for m in ecmp_members]}, "
                f"DST_IP (routes to {candidates[2][0]}): {dst_ip}")

    # Create PBR ACL table + rule with comma-separated nexthop group.
    # DST_IP is a peer on a different interface than the ECMP nexthops,
    # proving the redirect overrides normal routing.
    redirect_nexthops = ",".join(ecmp_nexthops)

    acl_config = {
        "ACL_TABLE": {
            ECMP_TABLE_NAME: {
                "policy_desc": "ACL redirect ECMP test",
                "type": PBR_TABLE_TYPE,
                "ports": [src_port],
                "stage": "INGRESS"
            }
        },
        "ACL_RULE": {
            f"{ECMP_TABLE_NAME}|{ECMP_RULE_NAME}": {
                "priority": "100",
                "DST_IP": dst_ip,
                "REDIRECT_ACTION": redirect_nexthops,
            }
        },
    }
    config_file = f"{DUT_TMP_DIR}/acl_ecmp.json"
    duthost.copy(content=json.dumps(acl_config, indent=4), dest=config_file)
    result = duthost.shell(f"sonic-cfggen -j {config_file} --write-to-db")
    pytest_assert(result['rc'] == 0,
                  f"Failed to apply ECMP ACL config: {result['stderr']}")
    verify_acl_table_active(duthost, ECMP_TABLE_NAME,
                            expected_rules=[ECMP_RULE_NAME])

    # Build per-nexthop expected packets for L2 rewrite verification.
    base_pkt = testutils.simple_tcp_packet(
        eth_dst=setup['router_mac'],
        eth_src=ptfadapter.dataplane.get_mac(0, setup['src_port_id']),
        ip_src=TEST_SRC_IP_V4,
        ip_dst=dst_ip,
        tcp_sport=0,
        tcp_dport=TEST_TCP_DPORT,
        ip_ttl=64
    )
    nh_exp_pkts = []
    for nh_ip, port_ids in ecmp_members:
        nh_mac = get_neighbor_mac(duthost, nh_ip)
        exp_pkt = build_expected_packet(
            base_pkt, is_ipv6=False,
            expected_dst_mac=nh_mac,
            expected_src_mac=setup['router_mac']
        )
        exp_pkt.set_do_not_care_scapy(packet.TCP, "sport")
        nh_exp_pkts.append((nh_ip, nh_mac, port_ids, exp_pkt))

    # Send packets one at a time with varying L4 source ports.
    # After each send, check if the packet arrived on a nexthop we haven't
    # confirmed yet.  Stop early once both nexthops are hit.
    nexthops_hit = set()
    num_flows = 50
    ptfadapter.dataplane.flush()

    for i in range(num_flows):
        if len(nexthops_hit) == 2:
            break
        sport = 10000 + i
        pkt = testutils.simple_tcp_packet(
            eth_dst=setup['router_mac'],
            eth_src=ptfadapter.dataplane.get_mac(0, setup['src_port_id']),
            ip_src=TEST_SRC_IP_V4,
            ip_dst=dst_ip,
            tcp_sport=sport,
            tcp_dport=TEST_TCP_DPORT,
            ip_ttl=64
        )
        testutils.send(ptfadapter, setup['src_port_id'], pkt, count=100)

        for idx, (nh_ip, nh_mac, port_ids, exp_pkt) in enumerate(nh_exp_pkts):
            if idx in nexthops_hit:
                continue
            try:
                testutils.verify_packet_any_port(
                    ptfadapter, exp_pkt, port_ids, timeout=5
                )
                nexthops_hit.add(idx)
                logger.info(f"  Flow {i}: nexthop {idx} ({nh_ip}, "
                            f"mac={nh_mac}) hit")
            except AssertionError:
                pass

    logger.info(f"ECMP nexthops hit: {nexthops_hit} out of {{0, 1}} "
                f"after {min(i + 1, num_flows)} flows")
    pytest_assert(
        len(nexthops_hit) == 2,
        f"ECMP load balancing incomplete — only nexthop(s) "
        f"{nexthops_hit} received traffic out of {ecmp_nexthops}"
    )
