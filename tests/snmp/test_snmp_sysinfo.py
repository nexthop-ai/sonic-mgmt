import pytest
import re
import logging
from tests.common.helpers.snmp_helpers import snmpwalk

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('t0', 't1', 't2', 'm0', 'mx'),  # Adjust topologies as needed
    pytest.mark.snmp
]

def test_snmp_system_info_matches_show_version(duthosts, rand_one_dut_hostname, creds_all_duts):
    """
    Verifies that system information retrieved via SNMP matches "show version" output.
    """
    duthost = duthosts[rand_one_dut_hostname]
    community = creds_all_duts[duthost.hostname]["snmp_rocommunity"]
    snmp_version = '2c'
    system_oid = '1.3.6.1.2.1.1'


    # 1. Get "show version" output and extract HwSKU and SONiC version
    show_version_output = duthost.shell('show version')['stdout']
    try:
        hwsku_match = re.search(r"HwSKU:\s+(\S+)", show_version_output)
        sonic_version_match = re.search(r"SONiC Software Version:\s+(\S+)", show_version_output)

        assert hwsku_match, "HwSKU not found in 'show version' output"
        assert sonic_version_match, "SONiC version not found in 'show version' output"

        hwsku = hwsku_match.group(1)
        sonic_version = sonic_version_match.group(1)
        logger.debug(f"show version output:\n{show_version_output}") #Optional print

    except Exception as e:
        pytest.fail(f"Error parsing 'show version': {e}")



    # 2. Perform SNMP walk
    try:
        snmp_data = snmpwalk(
            duthosts, duthost, system_oid, version=snmp_version, community=community
        )
        assert snmp_data, "SNMP walk returned no data"

    except Exception as e:
        pytest.fail(f"SNMP walk failed: {e}")



    # 3. Verify SNMP data against "show version"
    try:
        snmp_sysdescr = snmp_data.get('iso.3.6.1.2.1.1.1.0')  # sysDescr OID
        assert snmp_sysdescr, "sysDescr OID not found in SNMP data"
        #Example assertions:
        assert hwsku in snmp_sysdescr, f"HwSKU ({hwsku}) from 'show version' not in sysDescr"
        assert sonic_version in snmp_sysdescr, f"SONiC version ({sonic_version}) from 'show version' not in sysDescr"


    except Exception as e:
        pytest.fail(f"SNMP data verification failed: {e}")
