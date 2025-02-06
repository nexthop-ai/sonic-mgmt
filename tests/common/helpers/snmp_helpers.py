import logging
import ipaddress
import subprocess
from enum import Enum

from tests.common.utilities import wait_until
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.common.devices.eos import EosHost

logger = logging.getLogger(__name__)

class SnmpVersion(Enum):
    V2C = "2c"
    V3 = "3"
    
    def __str__(self):
        return self.value

DEF_WAIT_TIMEOUT = 300
DEF_CHECK_INTERVAL = 10

# Centralized SNMP OID definitions
class SnmpOIDs:
    """Centralized SNMP OID definitions"""
    
    # System MIB OIDs
    SYSTEM = '1.3.6.1.2.1.1'
    SYS_DESCR = '1.3.6.1.2.1.1.1.0'
    SYS_OBJECT_ID = '1.3.6.1.2.1.1.2.0'
    SYS_UPTIME = '1.3.6.1.2.1.1.3.0'
    SYS_CONTACT = '1.3.6.1.2.1.1.4.0'
    SYS_NAME = '1.3.6.1.2.1.1.5.0'
    SYS_LOCATION = '1.3.6.1.2.1.1.6.0'
    SYS_SERVICES = '1.3.6.1.2.1.1.7.0'
    
    # Interfaces MIB OIDs
    INTERFACES = '1.3.6.1.2.1.2'
    IF_NUMBER = '1.3.6.1.2.1.2.1.0'
    IF_TABLE = '1.3.6.1.2.1.2.2'
    IF_XTABLE = '1.3.6.1.2.1.31.1.1'
    IF_DESCR = '1.3.6.1.2.1.2.2.1.2'
    IF_TYPE = '1.3.6.1.2.1.2.2.1.3'
    IF_MTU = '1.3.6.1.2.1.2.2.1.4'
    IF_SPEED = '1.3.6.1.2.1.2.2.1.5'
    IF_PHYS_ADDR = '1.3.6.1.2.1.2.2.1.6'
    IF_ADMIN_STATUS = '1.3.6.1.2.1.2.2.1.7'
    IF_OPER_STATUS = '1.3.6.1.2.1.2.2.1.8'
    IF_IN_OCTETS = '1.3.6.1.2.1.2.2.1.10'
    IF_OUT_OCTETS = '1.3.6.1.2.1.2.2.1.16'
    IF_IN_ERRORS = '1.3.6.1.2.1.2.2.1.14'
    IF_OUT_ERRORS = '1.3.6.1.2.1.2.2.1.20'
    
    # Host Resources MIB
    HOST = '1.3.6.1.2.1.25'
    HOST_STORAGE = '1.3.6.1.2.1.25.2'
    HOST_MEMORY = '1.3.6.1.2.1.25.2.2'
    HOST_SYSTEM_PROCESSES = '1.3.6.1.2.1.25.1.6.0'
    HOST_CPU_LOAD_1 = '1.3.6.1.4.1.2021.10.1.3.1'
    HOST_CPU_LOAD_5 = '1.3.6.1.4.1.2021.10.1.3.2'
    HOST_CPU_LOAD_15 = '1.3.6.1.4.1.2021.10.1.3.3'
    
    # Bridge MIB (FDB)
    DOT1D_BRIDGE = '1.3.6.1.2.1.17'
    DOT1D_TP_FDB_TABLE = '1.3.6.1.2.1.17.4.3'
    DOT1D_TP_FDB_ENTRY = '1.3.6.1.2.1.17.4.3.1'
    DOT1D_BASE_PORT_TABLE = '1.3.6.1.2.1.17.1.4'
    DOT1D_BASE_PORT_ENTRY = '1.3.6.1.2.1.17.1.4.1'
    
    # Entity MIB
    ENTITY_MIB = '1.3.6.1.2.1.47'
    ENT_PHYSICAL_TABLE = '1.3.6.1.2.1.47.1.1.1'
    ENT_LOGICAL_TABLE = '1.3.6.1.2.1.47.1.2.1'
    ENT_PHYSICAL_DESCR = '1.3.6.1.2.1.47.1.1.1.1.2'
    ENT_PHYSICAL_CLASS = '1.3.6.1.2.1.47.1.1.1.1.5'
    ENT_PHYSICAL_SERIAL_NUM = '1.3.6.1.2.1.47.1.1.1.1.11'
    ENT_PHYSICAL_MFG_NAME = '1.3.6.1.2.1.47.1.1.1.1.12'
    ENT_PHYSICAL_MODEL_NAME = '1.3.6.1.2.1.47.1.1.1.1.13'
    
    # LLDP MIB
    LLDP = '1.0.8802.1.1.2'
    LLDP_LOCAL_SYS_NAME = '1.0.8802.1.1.2.1.3.3'
    LLDP_LOCAL_SYS_DESC = '1.0.8802.1.1.2.1.3.4'
    LLDP_LOCAL_PORT_TABLE = '1.0.8802.1.1.2.1.3.7'
    LLDP_REM_TABLE = '1.0.8802.1.1.2.1.4.1'
    
    # IP MIB
    IP = '1.3.6.1.2.1.4'
    IP_ADDR_TABLE = '1.3.6.1.2.1.4.20'
    IP_NET_TO_MEDIA = '1.3.6.1.2.1.4.22'
    IP_FORWARD_TABLE = '1.3.6.1.2.1.4.24'

global_snmp_facts = {}
global_snmpv3_facts = {}

def get_oid(oid_name):
    """Get OID by name from SnmpOIDs class."""
    if not hasattr(SnmpOIDs, oid_name):
        raise ValueError(f"Unknown OID name: {oid_name}")
    return getattr(SnmpOIDs, oid_name)

def strip_oid_prefix(oid):
    """Strip the leading '1.' from OID if present."""
    return oid.lstrip('1.')

def is_snmp_subagent_running(duthost):
    cmd = "docker exec snmp supervisorctl status snmp-subagent"
    output = duthost.shell(cmd)
    if "RUNNING" in output["stdout"]:
        logger.info("SNMP Sub-Agent is Running")
        return True
    logger.info("SNMP Sub-Agent is Not Running")
    return False


def _get_snmp_facts(localhost, host, version, community, is_dell, include_swap, module_ignore_errors):
    snmp_facts = localhost.snmp_facts(host=host, version=version, community=community, is_dell=is_dell,
                                      module_ignore_errors=module_ignore_errors, include_swap=include_swap)
    return snmp_facts


def _update_snmp_facts(localhost, host, version, community, is_dell, include_swap, duthost):
    global global_snmp_facts

    try:
        snmp_subagent_running = is_snmp_subagent_running(duthost)
        global_snmp_facts = _get_snmp_facts(localhost, host, version, community, is_dell, include_swap,
                                            module_ignore_errors=False)
    except RunAnsibleModuleFail as e:
        logger.info("encountered error when getting snmp facts: {}".format(e))
        global_snmp_facts = {}
        return False

    return snmp_subagent_running and True

def snmpwalk(duthosts, duthost, oid, version=SnmpVersion.V2C, timeout=30, **kwargs):
    """
    Performs an SNMP walk.
    
    Args:
        duthosts: DUT hosts
        duthost: DUT host
        oid: OID to query
        version: SNMP version (SnmpVersion.V2C or SnmpVersion.V3)
        timeout: Command timeout
        **kwargs: Additional arguments:
            For v2c: community (str)
            For v3: username, level, auth_protocol, priv_protocol, auth_key, priv_key
    """
    logger.debug(f"duthosts in snmpwalk: {duthosts}")
    try:
        management_ip = duthost.facts.get('ansible_host')
        logger.debug(f"mgmt_ip from duthost.facts: {management_ip}")

        if management_ip is None:
            management_ip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
        if not management_ip:
            raise ValueError(f"Could not determine management IP for {duthost.hostname}")

        command = ["snmpwalk", f"-v{version}", "-On"]  # Added -On for numeric OID output
        
        if version == SnmpVersion.V2C:
            if 'community' not in kwargs:
                raise ValueError("community parameter is required for SNMPv2c")
            command.extend([f"-c{kwargs['community']}"])
        elif version == SnmpVersion.V3:
            required_params = ['username', 'level', 'auth_protocol', 'priv_protocol', 'auth_key', 'priv_key']
            missing_params = [param for param in required_params if param not in kwargs]
            if missing_params:
                raise ValueError(f"Missing required SNMPv3 parameters: {missing_params}")
            
            command.extend([
                "-l", kwargs['level'],
                "-u", kwargs['username'],
                "-a", kwargs['auth_protocol'],
                "-A", kwargs['auth_key'],
                "-x", kwargs['priv_protocol'],
                "-X", kwargs['priv_key']
            ])
        else:
            raise ValueError("version must be either 'v2c' or 'v3'")

        command.extend([str(management_ip), oid])
        logger.debug(f"Executing SNMP command: {' '.join(command)}")
        
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)
        
        # Parse SNMP output lines into (oid_part, value) pairs
        snmp_lines = [line.split(" = ", 1) for line in process.stdout.splitlines() if line and " = " in line]
        
        # Process each value to remove quotes and type information
        processed_values = {
            oid_part.strip('.'): (value.split(':', 1)[1] if ':' in value else value).strip().strip('"')
            for oid_part, value in snmp_lines
        }
        
        # Create the final snmp_data dictionary with multiple OID formats
        snmp_data = {
            **{oid: value for oid, value in processed_values.items()},                          # Full OID
            **{oid.lstrip('1.'): value for oid, value in processed_values.items()},            # Without leading '1.'
            **{oid.split('.')[-1]: value for oid, value in processed_values.items()},          # Last OID part
            **{oid.replace('.', ''): value                                                     # Without dots
               for oid, value in processed_values.items() if '.' in oid}
        }

        logger.debug(f"Raw SNMP output:\n{process.stdout}")
        logger.debug(f"Processed SNMP data keys: {list(snmp_data.keys())}")
        return snmp_data

    except subprocess.CalledProcessError as e:
        logger.error(f"snmpwalk command failed: \n return code: {e.returncode}, \n stdout: {e.stdout}, \n stderr: {e.stderr}")
        raise

    except subprocess.TimeoutExpired as e:
        logger.error(f"snmpwalk timed out: {e}")
        raise

def get_snmp_facts(localhost, host, version, is_dell=False, module_ignore_errors=False,
                   wait=False, include_swap=False, timeout=DEF_WAIT_TIMEOUT, interval=DEF_CHECK_INTERVAL,
                   **kwargs):
    """
    Get SNMP facts with optional wait. Supports both SNMPv2c and SNMPv3.
    """
    if version == "v2c":
        if "community" not in kwargs:
            raise ValueError("community parameter is required for SNMPv2c")
        module_args = dict(host=host, version=version, community=kwargs["community"])
    elif version == "v3":
        # Map modern parameter names to what snmp_facts module expects
        module_args = {
            'host': host,
            'version': version,
            'username': kwargs['username'],
            'level': kwargs.get('security_level', 'authPriv'),
            'integrity': kwargs['auth_protocol'].lower(),  # sha or md5
            'authkey': kwargs['auth_key'],
            'privacy': kwargs['priv_protocol'].lower(),    # aes or des
            'privkey': kwargs['priv_key']
        }
    else:
        raise ValueError("Version must be either 'v2c' or 'v3'")

    if wait:
        def _get_snmp_facts():
            try:
                facts = localhost.snmp_facts(**module_args)
                return bool(facts)
            except Exception:
                return False

        if not wait_until(timeout, interval, 0, _get_snmp_facts):
            if not module_ignore_errors:
                raise Exception("Failed to get SNMP facts")
            return {}

    return localhost.snmp_facts(**module_args)


def get_snmp_output(ip, duthost, nbr, creds_all_duts, oid=SnmpOIDs.SYS_DESCR, version=SnmpVersion.V2C):
    """
    Get SNMP output from duthost using specific ip to query.
    Supports both SNMPv2c and SNMPv3.
    
    Args:
        ip: IP of DUT to query
        duthost: DUT host object
        nbr: Neighbor from where to execute query
        creds_all_duts: Credentials dictionary
        oid: OID to query (default: SYS_DESCR)
        version: SNMP version (SnmpVersion.V2C or SnmpVersion.V3)
    """
    ipaddr = ipaddress.ip_address(ip)
    iptables_cmd = "ip6tables" if isinstance(ipaddr, ipaddress.IPv6Address) else "iptables"

    ip_tbl_rule_add = f"sudo {iptables_cmd} -I INPUT 1 -p udp --dport 161 -d {ip} -j ACCEPT"
    duthost.shell(ip_tbl_rule_add)

    try:
        creds = creds_all_duts[duthost.hostname]
        if isinstance(nbr["host"], EosHost):
            if version == SnmpVersion.V2C:
                command = f"bash snmpget -v2c -c {creds['snmp_rocommunity']} {ip} {oid}"
            else:  # v3
                command = (f"bash snmpget -v3 -l {creds['snmp_v3level']} -u {creds['snmp_v3user']} "
                         f"-a {creds['snmp_v3authprotocol']} -A {creds['snmp_v3authpasswd']} "
                         f"-x {creds['snmp_v3privprotocol']} -X {creds['snmp_v3privpasswd']} "
                         f"{ip} {oid}")
            out = nbr['host'].eos_command(commands=[command])
        else:
            if version == SnmpVersion.V2C:
                command = f"docker exec snmp snmpwalk -v 2c -c {creds['snmp_rocommunity']} {ip} {oid}"
            else:  # v3
                command = (f"docker exec snmp snmpwalk -v3 -l {creds['snmp_v3level']} -u {creds['snmp_v3user']} "
                         f"-a {creds['snmp_v3authprotocol']} -A {creds['snmp_v3authpasswd']} "
                         f"-x {creds['snmp_v3privprotocol']} -X {creds['snmp_v3privpasswd']} "
                         f"{ip} {oid}")
            out = nbr['host'].command(command)

        return out
    finally:
        ip_tbl_rule_del = f"sudo {iptables_cmd} -D INPUT -p udp --dport 161 -d {ip} -j ACCEPT"
        duthost.shell(ip_tbl_rule_del)

