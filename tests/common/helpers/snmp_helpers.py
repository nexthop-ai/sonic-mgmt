import logging
import ipaddress
import subprocess

from tests.common.utilities import wait_until
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.common.devices.eos import EosHost

logger = logging.getLogger(__name__)

DEF_WAIT_TIMEOUT = 300
DEF_CHECK_INTERVAL = 10

global_snmp_facts = {}
global_snmpv3_facts = {}

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

def snmpwalk(duthosts, duthost, oid, version="2c", timeout=30, **kwargs):
    """
    Performs an SNMP walk.
    
    Args:
        duthosts: DUT hosts
        duthost: DUT host
        oid: OID to query
        version: SNMP version ('2c' or '3')
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

        command = ["snmpwalk", f"-v{version}"]
        
        if version == "2c":
            if 'community' not in kwargs:
                raise ValueError("community parameter is required for SNMPv2c")
            command.extend([f"-c{kwargs['community']}"])
        elif version == "3":
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
            raise ValueError("version must be either '2c' or '3'")

        command.extend([str(management_ip), oid])
        
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True)

        snmp_data = {}
        for line in process.stdout.splitlines():
            if line:
                parts = line.split(" = ")
                if len(parts) == 2:
                    oid, value = parts
                    snmp_data[oid.strip()] = value.strip()

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


def get_snmp_output(ip, duthost, nbr, creds_all_duts, oid='.1.3.6.1.2.1.1.1.0', version="2c"):
    """
    Get SNMP output from duthost using specific ip to query.
    Supports both SNMPv2c and SNMPv3.
    
    Args:
        ip: IP of DUT to query
        duthost: DUT host object
        nbr: Neighbor from where to execute query
        creds_all_duts: Credentials dictionary
        oid: OID to query
        version: SNMP version ('2c' or '3')
    """
    ipaddr = ipaddress.ip_address(ip)
    iptables_cmd = "ip6tables" if isinstance(ipaddr, ipaddress.IPv6Address) else "iptables"

    ip_tbl_rule_add = f"sudo {iptables_cmd} -I INPUT 1 -p udp --dport 161 -d {ip} -j ACCEPT"
    duthost.shell(ip_tbl_rule_add)

    try:
        creds = creds_all_duts[duthost.hostname]
        if isinstance(nbr["host"], EosHost):
            if version == "2c":
                command = f"bash snmpget -v2c -c {creds['snmp_rocommunity']} {ip} {oid}"
            else:  # v3
                command = (f"bash snmpget -v3 -l {creds['snmp_v3level']} -u {creds['snmp_v3user']} "
                         f"-a {creds['snmp_v3authprotocol']} -A {creds['snmp_v3authpasswd']} "
                         f"-x {creds['snmp_v3privprotocol']} -X {creds['snmp_v3privpasswd']} "
                         f"{ip} {oid}")
            out = nbr['host'].eos_command(commands=[command])
        else:
            if version == "2c":
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
