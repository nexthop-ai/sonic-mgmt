import pytest
import logging
import time
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.snmp_helpers import get_snmp_facts

pytestmark = [
    pytest.mark.topology('t0'),
    pytest.mark.device_type('vs')
]

logger = logging.getLogger(__name__)

def setup_snmpv3_user(duthost, username=None, auth_pass=None, priv_pass=None):
    """Setup SNMPv3 user on DUT"""
    if not username:
        username = f"snmpTest_{int(time.time())}"
    if not auth_pass:
        auth_pass = "password123"
    if not priv_pass:
        priv_pass = "password123"

    try:
        # Remove existing user if any
        logger.info(f"Removing existing SNMPv3 user if exists: {username}")
        duthost.shell(f"sudo config snmp user del {username}", module_ignore_errors=True)
        time.sleep(2)

        # Create new SNMPv3 user
        create_cmd = f"sudo config snmp user add {username} Priv RO SHA {auth_pass} AES {priv_pass}"
        logger.info(f"Creating SNMPv3 user with command: {create_cmd}")
        result = duthost.shell(create_cmd)
        pytest_assert(result['rc'] == 0, f"Failed to create SNMPv3 user: {result['stderr']}")

        time.sleep(5)  # Wait for user creation

        return {
            "username": username,
            "auth_protocol": "sha",
            "auth_password": auth_pass,
            "priv_protocol": "aes",
            "priv_password": priv_pass
        }

    except Exception as e:
        logger.error(f"Failed to setup SNMPv3 user: {str(e)}")
        raise

def cleanup_snmpv3_user(duthost, username):
    """Cleanup SNMPv3 user from DUT"""
    try:
        logger.info(f"Cleaning up SNMPv3 user: {username}")
        duthost.shell(f"sudo config snmp user del {username}", module_ignore_errors=True)
    except Exception as e:
        logger.error(f"Failed to cleanup SNMPv3 user: {str(e)}")

@pytest.mark.parametrize("oid", [
    ".1.3.6.1.2.1.1.1.0",  # sysDescr
    ".1.3.6.1.2.1.1.3.0",  # sysUpTime
    ".1.3.6.1.2.1.1.5.0",  # sysName
])
def test_snmpv3_get(duthosts, rand_one_dut_hostname, localhost, oid):
    """Test SNMPv3 GET operation for various OIDs"""
    duthost = duthosts[rand_one_dut_hostname]
    
    # Get DUT IP
    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    
    # Setup SNMPv3 user
    v3_config = None
    
    try:
        # Create SNMPv3 user
        v3_config = setup_snmpv3_user(duthost)
        logger.info(f"Created SNMPv3 user: {v3_config['username']}")

        # Perform SNMP GET
        logger.info(f"Performing SNMPv3 GET for OID: {oid}")
        snmp_facts = get_snmp_facts(
            localhost,
            host=hostip,
            version="v3",
            username=v3_config["username"],
            auth_protocol=v3_config["auth_protocol"],
            auth_key=v3_config["auth_password"],
            priv_protocol=v3_config["priv_protocol"],
            priv_key=v3_config["priv_password"],
            security_level="authPriv",
            wait=True
        )

        # Verify we got a response
        pytest_assert(snmp_facts is not None, "Failed to get SNMP facts")
        pytest_assert('ansible_facts' in snmp_facts, "No ansible_facts in SNMP response")
        
        facts = snmp_facts['ansible_facts']
        logger.info(f"Retrieved SNMP facts for OID {oid}: {facts}")

    except Exception as e:
        pytest.fail(f"SNMPv3 GET test failed: {str(e)}")

    finally:
        # Cleanup
        if v3_config:
            cleanup_snmpv3_user(duthost, v3_config["username"])

def test_snmpv3_get_custom(duthosts, rand_one_dut_hostname, localhost, request):
    """Test SNMPv3 GET operation with custom parameters"""
    duthost = duthosts[rand_one_dut_hostname]
    
    # Get custom parameters from pytest command line
    oid = request.config.getoption("--oid", default=".1.3.6.1.2.1.1.1.0")
    username = request.config.getoption("--snmp-user", default=None)
    auth_pass = request.config.getoption("--snmp-auth-pass", default=None)
    priv_pass = request.config.getoption("--snmp-priv-pass", default=None)
    
    # Get DUT IP
    hostip = duthost.host.options['inventory_manager'].get_host(duthost.hostname).vars['ansible_host']
    
    # Setup SNMPv3 user
    v3_config = None
    
    try:
        # Create SNMPv3 user
        v3_config = setup_snmpv3_user(duthost, username, auth_pass, priv_pass)
        logger.info(f"Created SNMPv3 user: {v3_config['username']}")

        # Perform SNMP GET
        logger.info(f"Performing SNMPv3 GET for OID: {oid}")
        snmp_facts = get_snmp_facts(
            localhost,
            host=hostip,
            version="v3",
            username=v3_config["username"],
            auth_protocol=v3_config["auth_protocol"],
            auth_key=v3_config["auth_password"],
            priv_protocol=v3_config["priv_protocol"],
            priv_key=v3_config["priv_password"],
            security_level="authPriv",
            wait=True
        )

        # Verify we got a response
        pytest_assert(snmp_facts is not None, "Failed to get SNMP facts")
        pytest_assert('ansible_facts' in snmp_facts, "No ansible_facts in SNMP response")
        
        facts = snmp_facts['ansible_facts']
        logger.info(f"Retrieved SNMP facts for OID {oid}: {facts}")

    except Exception as e:
        pytest.fail(f"SNMPv3 GET test failed: {str(e)}")

    finally:
        # Cleanup
        if v3_config:
            cleanup_snmpv3_user(duthost, v3_config["username"])

def pytest_addoption(parser):
    """Add custom command line options"""
    parser.addoption("--oid", action="store", help="OID to query")
    parser.addoption("--snmp-user", action="store", help="SNMPv3 username")
    parser.addoption("--snmp-auth-pass", action="store", help="SNMPv3 auth password")
    parser.addoption("--snmp-priv-pass", action="store", help="SNMPv3 priv password")
