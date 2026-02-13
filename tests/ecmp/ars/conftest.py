"""
ARS (Adaptive Routing and Switching) / DLB (Dynamic Load Balancing) helpers

This module provides utilities for ARS testing:
- setup an ECMP route
- setup ARS config
"""
from ipaddress import ip_address
from collections import namedtuple
import json
import jsonpatch
import logging
import os
import pytest
import time
import copy

from tests.common.fixtures.ptfhost_utils import copy_ptftests_directory  # noqa: F401

logger = logging.getLogger(__name__)

# Utility functions to control ARS NHG counter polling


def enable_ars_counters(duthost, interval=1000):
    """
    Enable ARS NHG counters and set the polling interval (milliseconds).
    """
    duthost.shell("counterpoll ars-nhg enable", module_ignore_errors=True)
    duthost.shell(f"counterpoll ars-nhg interval {int(interval)}", module_ignore_errors=True)


def disable_ars_counters(duthost):
    """
    Disable ARS NHG counters.
    """
    duthost.shell("counterpoll ars-nhg disable", module_ignore_errors=True)


class IPRoutes:
    """
    Program IP routes with next hops on to the DUT
    """
    def __init__(self, duthost, asic):
        self.arp_list = []
        self.asic = asic
        self.duthost = duthost

        fileloc = os.path.join(os.path.sep, "tmp")
        self.filename = os.path.join(fileloc, "static_ip.sh")
        self.ip_nhops = []
        self.IP_NHOP = namedtuple("IP_NHOP", "prefix nhop")

    def add_ip_route(self, ip_route, nhop_path_ips):
        """
        Add IP route with ECMP paths
        """
        # add IP route, nhop to list
        self.ip_nhops.append(self.IP_NHOP(ip_route, nhop_path_ips))

    def program_routes(self):
        """
        Create a file with static ip route add commands, copy file
        to DUT and run it from DUT
        """
        with open(self.filename, "w") as fn:
            for ip_nhop in self.ip_nhops:

                ip_route = "sudo {} ip route replace {}".format(
                    self.asic.ns_arg, ip_nhop.prefix
                )
                ip_nhop_str = ""

                for ip in ip_nhop.nhop:
                    ip_nhop_str += "nexthop via {} ".format(ip)

                ip_cmd = "{} {}".format(ip_route, ip_nhop_str)
                fn.write(ip_cmd + "\n")

        fn.close()
        # copy file to DUT and run it on DUT
        self.duthost.copy(src=self.filename, dest=self.filename, mode="0755")
        result = self.duthost.shell(self.filename)
        if result["rc"] != 0:
            pytest.fail("IP add failed on duthost:{}".format(self.filename))

    def delete_routes(self):
        """
        Create a file with static ip route del commands, copy file
        to DUT and run it from DUT
        """
        with open(self.filename, "w") as fn:
            for ip_nhop in self.ip_nhops:
                ip_route = "sudo {} ip route del {}".format(self.asic.ns_arg, ip_nhop.prefix)
                fn.write(ip_route + "\n")

        fn.close()
        self.duthost.copy(src=self.filename, dest=self.filename, mode="0755")
        try:
            self.duthost.shell(self.filename)
            self.duthost.shell("rm {}".format(self.filename))
            os.remove(self.filename)
        except:  # noqa: E722
            pass


def wait_for_db_entry(duthost, key_pattern, expected_attrs=None, db=1, expect_empty_list=False, timeout=15):
    """
    Poll ASIC DB (redis DB `db`) looking for keys matching key_pattern.
    If expected_attrs is provided (dict of {attr_key: expected_value}), verify
    the attribute values appear in the hgetall output.
    Returns (matching_key, hgetall_stdout) on success; raises AssertionError on timeout.
    """
    for _ in range(timeout):
        # list keys. If expect_empty_list, check for empty list
        res = duthost.shell(f"redis-cli -n {db} keys '{key_pattern}'", module_ignore_errors=True)
        if expect_empty_list:
            if res.get("stdout", "") == "":
                return
            else:
                time.sleep(1)
                continue
        stdout = res.get("stdout", "") or ""
        keys = [k.strip() for k in stdout.splitlines() if k.strip()]
        if keys:
            for key in keys:
                ent = duthost.shell(f"redis-cli -n {db} hgetall '{key}'",
                                    module_ignore_errors=True).get("stdout", "") or ""
                if expected_attrs:
                    ok = True
                    for exp_val in expected_attrs.values():
                        if str(exp_val) not in ent:
                            ok = False
                            break
                    if ok:
                        logger.debug("Found ASIC entry %s with expected attrs", key)
                        return key, ent
                else:
                    logger.debug("Found ASIC entry %s", key)
                    return key, ent
        time.sleep(1)
    raise AssertionError(f"DB({db}) entry matching '{key_pattern}' not found after {timeout}s")


def get_port_facts(dut, mg_facts, port_status, switch_arptable, ignore_intfs,
                   enum_rand_one_frontend_asic_index, key='src'):
    interfaces = mg_facts['minigraph_interfaces']

    if not interfaces:
        pytest.fail("interfaces is not defined.")

    selected_port_facts = {}
    up_port = None
    for a_intf_name, a_intf_data in list(port_status['int_status'].items()):
        if dut.is_backend_port(a_intf_name, mg_facts):
            continue
        if a_intf_data['oper_state'] == 'up' and a_intf_name not in ignore_intfs:
            # Got a port that is up and not already used.
            for intf in interfaces:
                attachto_match = intf['attachto'] == a_intf_name

                if attachto_match:
                    up_port = a_intf_name
                    selected_port_facts[key + '_port_ids'] = [mg_facts['minigraph_ptf_indices'][a_intf_name]]
                    selected_port_facts[key + '_router_mac'] = \
                        dut.asic_instance(enum_rand_one_frontend_asic_index).get_router_mac()
                    addr = ip_address(str(intf['addr']))
                    selected_port_facts[key + '_router_intf_name'] = intf['attachto']
                    selected_port_facts[key + '_port'] = [a_intf_name]
                    if addr.version == 4:
                        selected_port_facts[key + '_router_ipv4'] = intf['addr']
                        selected_port_facts[key + '_host_ipv4'] = intf['peer_addr']
                        selected_port_facts[key + '_host_mac'] = \
                            switch_arptable['arptable']['v4'][intf['peer_addr']]['macaddress']
                    elif addr.version == 6:
                        selected_port_facts[key + '_router_ipv6'] = intf['addr']
                        selected_port_facts[key + '_host_ipv6'] = intf['peer_addr']
            if up_port:
                logger.info("{} port is {}".format(key, up_port))
                break
    return up_port, selected_port_facts


@pytest.fixture(scope="module", autouse=True)
def ars_supported(duthost):
    """
    Ensure ARS is supported on this platform by checking STATE_DB capability.

    Checks key:
      ARS_CAPABILITY_TABLE|SAI_OBJECT_TYPE_SWITCH|SAI_SWITCH_ATTR_ARS_PROFILE
    and expects field 'create' to be 'true'. If not, skip the module.
    """
    try:
        res = duthost.shell(
            "sudo sonic-db-cli STATE_DB HGET "
            '"ARS_CAPABILITY_TABLE|SAI_OBJECT_TYPE_SWITCH|SAI_SWITCH_ATTR_ARS_PROFILE" set',
            module_ignore_errors=True,
        )
        if (res.get("stdout") or "").strip().lower() != "true":
            pytest.skip("ARS is not supported on this platform")
    except Exception:
        pytest.skip("Failed to read ARS capability; skipping as unsupported")


@pytest.fixture(scope='module')
def gather_facts(tbinfo, duthosts, enum_rand_one_per_hwsku_frontend_hostname, enum_rand_one_frontend_asic_index):
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    asichost = duthost.asic_instance(enum_rand_one_frontend_asic_index)
    facts = {}

    logger.info("Gathering facts on DUT ...")
    mg_facts = asichost.get_extended_minigraph_facts(tbinfo)

    # Use the arp table to get the mac address of the host (VM's) instead of lldp_facts as that is what is used
    # by the DUT to forward traffic - regardless of lag or port.

    switch_arptable = asichost.switch_arptable()['ansible_facts']
    if not switch_arptable:
        pytest.fail("ARP table is not rebuilt in given time")

    used_intfs = set()

    port_status = asichost.show_interface(command='status')['ansible_facts']
    src, src_port_facts = get_port_facts(duthost, mg_facts, port_status, switch_arptable,
                                         used_intfs, enum_rand_one_frontend_asic_index, key='src')
    used_intfs.add(src)
    facts.update(src_port_facts)

    # Collect 5 destination ports, so we can configure 2 ECMP groups for some tests
    for i in range(5):
        dst_key = f'dst{i+1}'  # dst1, dst2, dst3
        dst, dst_port_facts = get_port_facts(duthost, mg_facts, port_status, switch_arptable,
                                             used_intfs, enum_rand_one_frontend_asic_index, key=dst_key)
        if dst:
            used_intfs.add(dst)
            facts.update(dst_port_facts)
        else:
            logger.warning(f"Could not find destination port {i+1}")
            break

    if len(used_intfs) < 6:
        pytest.fail("Did not find 1 src and 5 dst interfaces that are up on host {}. Found ports: {}".format(
            duthost.hostname, len(used_intfs)))
    logger.info("gathered_new_facts={}".format(json.dumps(facts, indent=2)))

    yield facts


def create_ecmp_route(duthost, asic, prefix, gather_facts, dst_numbers):
    """
    Create an ECMP route (IPv4 or IPv6) with the given prefix and destination numbers.
    Automatically detects whether the prefix is IPv4 or IPv6 and uses appropriate addresses.

    Args:
        duthost: The DUT host object
        asic: The ASIC instance
        prefix: The route prefix (e.g., "10.0.0.0/24" for IPv4 or "2001:db8:85a3::/64" for IPv6)
        gather_facts: Dictionary containing gathered facts with dst1, dst2, etc.
        dst_numbers: List of destination numbers (e.g., [1, 2, 3] for dst1, dst2, dst3)

    Returns:
        Tuple of (IPRoutes object, nexthop_ips list, selected_ifaces dict)
    """
    from ipaddress import ip_network

    # Detect if prefix is IPv4 or IPv6
    try:
        network = ip_network(prefix, strict=False)
        is_ipv6 = network.version == 6
    except ValueError:
        raise ValueError(f"Invalid prefix: {prefix}")

    # Determine which address key to look for
    addr_key = 'host_ipv6' if is_ipv6 else 'host_ipv4'
    addr_type = 'IPv6' if is_ipv6 else 'IPv4'

    # Extract the destination interfaces and their peer IPs from gather_facts
    dst_interfaces = []
    nexthop_ips = []
    selected_ifaces = {}

    # Collect destination interface info using actual peer IPs
    for dst_num in dst_numbers:
        dst_key = f'dst{dst_num}'
        if f'{dst_key}_router_intf_name' in gather_facts and f'{dst_key}_{addr_key}' in gather_facts:
            dst_intf = gather_facts[f'{dst_key}_router_intf_name']
            peer_ip = gather_facts[f'{dst_key}_{addr_key}']

            dst_interfaces.append(dst_intf)
            nexthop_ips.append(peer_ip)
            selected_ifaces[dst_intf] = peer_ip
            logger.info(f"Using destination interface {dst_intf} with peer {addr_type} {peer_ip} as nexthop")

    if len(dst_interfaces) < len(dst_numbers):
        raise ValueError(f"Expected {len(dst_numbers)} destination interfaces, found {len(dst_interfaces)}")

    logger.info(f"Creating {addr_type} ECMP route {prefix} via nexthops {nexthop_ips} on interfaces {dst_interfaces}")

    # Create ECMP route using IPRoutes class with actual peer IPs
    nhop = IPRoutes(duthost, asic)
    nhop.add_ip_route(prefix, nexthop_ips)
    nhop.program_routes()

    # Wait for routes to be programmed
    time.sleep(2)

    return nhop, nexthop_ips, selected_ifaces


@pytest.fixture(scope="module")
def setup_ecmp_route(duthost, gather_facts, enum_rand_one_frontend_asic_index, request):
    """
    Create ECMP routes using the 3 destination ports (dst1, dst2, dst3) gathered by gather_facts.
    Uses IPRoutes classes to create and program ECMP routes.
    Uses ROUTE_PREFIX from the test module (request.module.ROUTE_PREFIX).
    Yields: (prefix, nexthops, selected_ifaces_dict)
    """
    asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)
    nhop = None

    try:
        # Get the route prefix from the test module
        prefix = request.module.ROUTE_PREFIX

        # Create ECMP route with dst1, dst2, dst3
        nhop, nexthop_ips, selected_ifaces = create_ecmp_route(
            duthost, asic, prefix, gather_facts, [1, 2, 3]
        )

        yield prefix, nexthop_ips, selected_ifaces

    finally:
        logger.info(f"Cleaning up ECMP route {prefix}")

        # Clean up routes
        if nhop:
            nhop.delete_routes()


@pytest.fixture(scope="module")
def setup_ecmp_route_v6(duthost, gather_facts, enum_rand_one_frontend_asic_index, request):
    """
    Create IPv6 ECMP routes using the 3 destination ports (dst1, dst2, dst3) gathered by gather_facts.
    Uses IPRoutes classes to create and program ECMP routes with IPv6 addresses.
    Uses ROUTE_PREFIX_V6 from the test module (request.module.ROUTE_PREFIX_V6).
    Yields: (prefix, nexthops, selected_ifaces_dict, src_ipv6, dst_ipv6)
    """
    asic = duthost.asic_instance(enum_rand_one_frontend_asic_index)
    nhop = None

    try:
        # Get the IPv6 route prefix from the test module
        prefix = request.module.ROUTE_PREFIX_V6

        # Create ECMP route with dst1, dst2, dst3 using IPv6
        nhop, nexthop_ips, selected_ifaces = create_ecmp_route(
            duthost, asic, prefix, gather_facts, [1, 2, 3]
        )

        # Extract IPv6 addresses from gather_facts
        src_ipv6 = gather_facts.get('src_host_ipv6')
        dst_ipv6 = prefix.split('/')[0]  # Get the destination IPv6 from the prefix

        if not src_ipv6:
            pytest.fail("IPv6 source address not found in gather_facts")

        logger.info(f"IPv6 ECMP route setup: prefix={prefix}, src_ipv6={src_ipv6}, dst_ipv6={dst_ipv6}")

        yield prefix, nexthop_ips, selected_ifaces, src_ipv6, dst_ipv6

    finally:
        logger.info(f"Cleaning up IPv6 ECMP route {prefix}")

        # Clean up routes
        if nhop:
            nhop.delete_routes()


def apply_cfg(duthost, config_dict):
    """
    Apply configuration by generating a diff against the live running config and
    applying the resulting JSON patch via 'config apply-patch'. This preserves
    unrelated keys and avoids replacing whole tables.

    Args:
        duthost: The DUT host object
        config_dict: Configuration dictionary to apply (None values are filtered out)
    """
    patch_file = "/tmp/ars_patch.json"

    # Helpers
    def filter_none_values(d):
        """Remove None values from dictionary at all levels"""
        result = {}
        for k, v in d.items():
            if v is None:
                continue
            if isinstance(v, dict):
                filtered_v = filter_none_values(v)
                if filtered_v:  # Only add if not empty after filtering
                    result[k] = filtered_v
            else:
                result[k] = v
        return result

    def deep_merge(dst, src):
        """Recursively merge src into dst (dicts only); lists and scalars overwrite."""
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v
        return dst

    # 1) Get live running config from DUT
    res = duthost.shell("sonic-cfggen -d --print-data", module_ignore_errors=True)
    if res.get("rc") != 0 or not res.get("stdout"):
        logger.warning("sonic-cfggen failed, falling back to /etc/sonic/config_db.json (rc=%s, err=%s)",
                       res.get("rc"), res.get("stderr", ""))
        res = duthost.shell("cat /etc/sonic/config_db.json", module_ignore_errors=True)
        if res.get("rc") != 0 or not res.get("stdout"):
            raise Exception(f"Failed to read running config on DUT: {res}")

    try:
        current_cfg = json.loads(res.get("stdout", ""))
    except Exception as e:
        raise Exception(f"Failed to parse live config JSON: {e}")

    # 2) Build desired config by overlaying requested keys onto live config
    filtered_config = filter_none_values(config_dict)
    desired_cfg = deep_merge(copy.deepcopy(current_cfg), filtered_config)

    # 3) Create JSON Patch (diff current -> desired)
    patch_ops = list(jsonpatch.JsonPatch.from_diff(current_cfg, desired_cfg))
    if not patch_ops:
        logger.info("No changes detected in requested config; skipping apply-patch")
        return

    patch_content = json.dumps(patch_ops, indent=2)
    logger.info("Generated JSON patch to apply:\n%s", patch_content)

    # 4) Copy patch to DUT and apply
    duthost.copy(content=patch_content, dest=patch_file)
    result = duthost.shell(f"sudo config apply-patch {patch_file}", module_ignore_errors=True)

    try:
        if result.get("rc") != 0:
            raise Exception(result.get("stderr", result.get("stdout", "")))
    finally:
        # Clean up temporary patch file regardless of success/failure
        duthost.shell(f"rm -f {patch_file}", module_ignore_errors=True)

    logger.info("Configuration patch applied successfully")


def setup_ars_profile(duthost, ars_profile, skip_validation=False):
    """
    Configure ARS_PROFILE using the provided ars_profile.
    Wait for the ARS_PROFILE object to appear in ASIC_DB.

    Args:
        duthost: The DUT host object
        ars_profile: Dictionary containing ARS profile configuration

    Returns:
        str: profile_name for cleanup purposes
    """
    # Decide profile name (default 'global' if not provided)
    profile_name = "global"  # Default profile name

    logger.info("Configuring ARS_PROFILE")

    # Apply ARS_PROFILE
    logger.info("Applying ARS_PROFILE|%s", profile_name)
    apply_cfg(duthost, {"ARS_PROFILE": {profile_name: ars_profile}})

    # ------------------------------------------------------------------
    # Wait for OrchAgent to program ASIC_DB objects
    # ------------------------------------------------------------------
    # Map algorithm -> expected SAI enum string (only handle known mapping here)
    alg = str(ars_profile.get("algorithm", "")).lower()
    algo_map = {"ewma": "SAI_ARS_PROFILE_ALGO_EWMA"}
    expected_algo = algo_map.get(alg)

    # Wait for ARS_PROFILE object in ASIC_DB with expected algo (if known)
    logger.info("Waiting for ARS_PROFILE in ASIC_DB (algo=%s)", expected_algo)
    expected_attrs = {"SAI_ARS_PROFILE_ATTR_ALGO": expected_algo} if expected_algo else None
    if not skip_validation:
        wait_for_db_entry(duthost, "ASIC_STATE:SAI_OBJECT_TYPE_ARS_PROFILE:*", expected_attrs=expected_attrs,
                          db=1, timeout=20)

    return profile_name


def setup_ars_object(duthost, ars_object, name):
    logger.info("Applying ARS_OBJECT|%s", name)
    apply_cfg(duthost, {"ARS_OBJECT": {name: ars_object}})


def setup_ars_interface(duthost, ars_interface):
    """
    Configure ARS_INTERFACE using the provided ars_interface configuration.

    Args:
        duthost: The DUT host object
        ars_interface: Dictionary containing ARS interface configuration
                      Format: {interface_name: {"scaling_factor": "value"}}

    Returns:
        dict: The interface configuration for cleanup purposes
    """
    logger.info("Configuring ARS_INTERFACE")

    # Apply ARS_INTERFACE
    logger.info("Applying ARS_INTERFACE configs")
    apply_cfg(duthost, {"ARS_INTERFACE": ars_interface})

    return ars_interface


def setup_ars_acl(duthost, src_port, flow, priority, enable):
    """
    Configure ARS ACL table and rule to control ARS forwarding.

    Args:
        duthost: The DUT host object
        src_port: Source port for the ACL table
        flow: Dictionary containing flow information with keys:
              - srcIp: Source IP address
              - dstIp: Destination IP address
              - protocol: IP protocol (e.g., "6" for TCP, "17" for UDP)
              - dstPort: Destination port number
              - bthOpcode: BTH Opcode (optional)
        priority: Priority value for the ACL rule
        enable: Boolean - True to enable ARS forwarding, False to disable

    Returns:
        tuple: (table_name, rule_name) for cleanup purposes
    """
    # ------------------------------------------------------------------
    # Build ACL configuration
    # ------------------------------------------------------------------
    table_name = "ARS_CONTROL_TABLE"
    rule_action = "ENABLE" if enable else "DISABLE"
    rule_name = f"{rule_action}_ARS_RULE"

    logger.info("Configuring ARS ACL")
    logger.info(f"Creating ACL table {table_name} on port {src_port}")
    logger.info(f"Creating ACL rule {rule_name} with priority {priority}, enable={enable}")

    # Build ACL table configuration
    acl_table_config = {
        table_name: {
            "policy_desc": "ARS forwarding control table",
            "type": "ARS",
            "stage": "INGRESS",
            "ports": [src_port]
        }
    }

    # Build ACL rule configuration
    acl_rule_key = f"{table_name}|{rule_name}"
    acl_rule_config = {
        acl_rule_key: {
            "PRIORITY": str(priority),
            "DISABLE_ARS_FORWARDING": str(not enable).lower()  # Invert enable for DISABLE_ARS_FORWARDING
        }
    }

    # Add source and destination IP if provided
    if "srcIp" in flow and flow["srcIp"]:
        acl_rule_config[acl_rule_key]["SRC_IP"] = flow["srcIp"]
    if "dstIp" in flow and flow["dstIp"]:
        acl_rule_config[acl_rule_key]["DST_IP"] = flow["dstIp"]
    if "srcIpv6" in flow and flow["srcIpv6"]:
        acl_rule_config[acl_rule_key]["SRC_IPV6"] = flow["srcIpv6"]
    if "dstIpv6" in flow and flow["dstIpv6"]:
        acl_rule_config[acl_rule_key]["DST_IPV6"] = flow["dstIpv6"]
    if "protocol" in flow and flow["protocol"]:
        acl_rule_config[acl_rule_key]["IP_PROTOCOL"] = str(flow["protocol"])
    if "dstPort" in flow and flow["dstPort"]:
        acl_rule_config[acl_rule_key]["L4_DST_PORT"] = str(flow["dstPort"])
    if "dscp" in flow and flow["dscp"]:
        acl_rule_config[acl_rule_key]["DSCP"] = str(flow["dscp"])
    if "bthOpcode" in flow and flow["bthOpcode"]:
        acl_rule_config[acl_rule_key]["BTH_ROPCODE"] = str(flow["bthOpcode"])

    # 1. Apply ACL_TABLE
    logger.info("Applying ACL_TABLE|%s", table_name)
    apply_cfg(duthost, {"ACL_TABLE": acl_table_config})

    # 2. Apply ACL_RULE
    logger.info("Applying ACL_RULE|%s", acl_rule_key)
    apply_cfg(duthost, {"ACL_RULE": acl_rule_config})

    logger.info(f"ARS ACL configuration completed: table={table_name}, rule={rule_name}")

    return table_name, rule_name


def _apply_config_removal(duthost, removal_key, operation_name):
    """
    Common helper function to remove configuration using sonic-db-cli.

    Args:
        duthost: DUT host object
        removal_key: Configuration key to remove
        operation_name: Name of the operation for logging and temp file naming
    """
    logger.info(f"Cleaning up {operation_name}")

    # Use sonic-db-cli to delete the key
    result = duthost.shell(f"sonic-db-cli CONFIG_DB del '{removal_key}'")

    if result["rc"] != 0:
        logger.warning("Failed to remove %s: %s", operation_name, result.get("stderr", ""))
    else:
        logger.info("%s cleanup completed successfully", operation_name)
    time.sleep(2)


def cleanup_ars_profile(duthost, profile_name):
    """
    Remove ARS_PROFILE configuration

    Args:
        duthost: DUT host object
        profile_name: Name of the ARS profile to remove
    """
    removal_key = f"ARS_PROFILE|{profile_name}"
    _apply_config_removal(duthost, removal_key, removal_key)


def cleanup_ars_interface(duthost, ars_interfaces):
    """
    Remove ARS_INTERFACE configuration

    Args:
        duthost: DUT host object
        ars_interfaces: Dictionary of interfaces to remove
    """
    for intf in ars_interfaces.keys():
        removal_key = f"ARS_INTERFACE|{intf}"
        _apply_config_removal(duthost, removal_key, removal_key)


def cleanup_ars_acl(duthost, table_name, rule_name):
    """
    Remove ACL_TABLE and ACL_RULE configuration
    Removes rule first, then table as separate operations.

    Args:
        duthost: DUT host object
        table_name: ACL table name to remove
        rule_name: ACL rule name to remove
    """
    # First remove the ACL rule
    removal_key = f"ACL_RULE|{table_name}|{rule_name}"
    _apply_config_removal(duthost, removal_key, removal_key)

    # Then remove the ACL table
    removal_key = f"ACL_TABLE|{table_name}"
    _apply_config_removal(duthost, removal_key, removal_key)


def cleanup_ars_object(duthost, ars_object_name):
    removal_key = f"ARS_OBJECT|{ars_object_name}"
    _apply_config_removal(duthost, removal_key, removal_key)
