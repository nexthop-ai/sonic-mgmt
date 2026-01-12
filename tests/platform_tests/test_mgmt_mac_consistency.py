"""
Test management port MAC address consistency

This test validates that the MAC address of the management interface (eth0)
is consistent with the base MAC address stored in the system EEPROM, taking
into account platform-specific MAC address offsets.
"""

import logging
import os
import re
import pytest
from tests.common.helpers.assertions import pytest_assert
from tests.platform_tests.utils import get_config_from_yaml

pytestmark = [pytest.mark.topology("any"), pytest.mark.device_type("physical")]

logger = logging.getLogger(__name__)

TEST_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "base_mac_offset.yml")


@pytest.fixture(scope="module")
def mac_offset_config(duthosts, enum_rand_one_per_hwsku_hostname):
    """
    Load MAC offset configuration from YAML file based on platform and hwsku.

    Returns:
        dict: Configuration dictionary with 'offset_from_base_mac' key
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    test_config = get_config_from_yaml(TEST_CONFIG_FILE)

    platform = duthost.facts["platform"]
    hwsku = duthost.facts["hwsku"]

    config = test_config.get("default")

    # Override with platform/hwsku specific configs
    for platform_regexp in test_config:
        if platform_regexp == "default":
            continue
        if re.match(platform_regexp, platform):
            platform_config = test_config[platform_regexp].get("default", {})
            config.update(platform_config)

            # Check for hwsku-specific overrides
            for hwsku_regexp in test_config[platform_regexp]:
                if hwsku_regexp == "default":
                    continue
                if re.match(hwsku_regexp, hwsku):
                    config.update(test_config[platform_regexp][hwsku_regexp])
            break

    if not config or "offset_from_base_mac" not in config:
        config = test_config.get("default", {"offset_from_base_mac": 0})

    logger.info("MAC offset configuration for platform: {} hwsku: {}: {}".format(platform, hwsku, config))

    return config


def _mac_to_int(mac_str):
    """
    Convert MAC address string to integer

    Args:
        mac_str (str): MAC address string with :, -, or . as delimiters (e.g. "00:11:22:33:44:55")

    Returns:
        int: MAC address as integer
    """
    return int(mac_str.translate(str.maketrans("", "", ":.-")), 16)


def _int_to_mac(mac_int):
    """
    Convert integer to MAC address string.

    Args:
        mac_int (int): Integer representation of MAC address

    Returns:
        str: MAC address in format 'xx:xx:xx:xx:xx:xx' (lowercase)
    """
    mac_hex = "{:012x}".format(mac_int)
    return ":".join([mac_hex[i:i+2] for i in range(0, 12, 2)])


def _parse_mac_address_count(output):
    """
    Parse MAC Addresses count from decode-syseeprom output

    Args:
        output (str): Output from decode-syseeprom command

    Returns:
        int: MAC Addresses count
        None: if unspecified
    """

    mac_count = None

    for line in output.splitlines():
        if line.startswith("MAC Addresses"):
            mac_count = int(line.split()[-1].strip())

    return mac_count


def _validate_mac_address(source, hostname, cmd_result):
    # Verify the command executed successfully
    pytest_assert(
        cmd_result["rc"] == 0,
        "Failed to read MAC address from '{}' on '{}'. Return code: {}, Error: {}".format(
            source, hostname, cmd_result["rc"], cmd_result.get("stderr", "N/A")
        ),
    )

    mac = cmd_result["stdout"].strip()
    logger.info("MAC address from {}: '{}'".format(source, mac))

    # Validate MAC address format
    pytest_assert(
        len(mac) == 17 and all([len(part) == 2 for part in mac.split(":")]),
        "Invalid MAC address format from {} on '{}': '{}'".format(source, hostname, mac),
    )

    return mac


def test_mgmt_mac_consistency(duthosts, enum_rand_one_per_hwsku_hostname, mac_offset_config):
    """
    @summary: Verify that the management port MAC address is consistent with EEPROM base MAC

    This test performs the following checks:
    1. Reads the MAC address from the management interface file (/sys/class/net/eth0/address)
    2. Extracts the base MAC address from EEPROM using decode-syseeprom command
    3. Loads platform-specific MAC offset configuration from YAML file
    4. Calculates expected management MAC = base MAC + configured offset
    5. Compares actual eth0 MAC against expected MAC
    6. If "MAC Addresses" field exists EEPROM additionally validates eth0 MAC is within range

    The test will fail if:
    - Either command fails to execute
    - The actual eth0 MAC doesn't match the expected MAC (base + offset)
    - The eth0 MAC is outside the allocated range (when MAC Addresses field is present)
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    logger.info("Testing management MAC address consistency on '{}'".format(duthost.hostname))

    cmd_read_mgmt_mac = "sudo cat /sys/class/net/eth0/address"
    cmd_eeprom_mac = "sudo decode-syseeprom -m"
    cmd_eeprom_full = "sudo decode-syseeprom"

    logger.info("Reading MAC address from management interface file on '{}'".format(duthost.hostname))
    mgmt_mac_result = duthost.command(cmd_read_mgmt_mac, module_ignore_errors=True)
    mgmt_mac = _validate_mac_address("/sys/class/net/eth0/address", duthost.hostname, mgmt_mac_result)

    logger.info("Reading base MAC address from EEPROM on '{}'".format(duthost.hostname))
    eeprom_mac_result = duthost.command(cmd_eeprom_mac, module_ignore_errors=True)
    base_mac_from_eeprom = _validate_mac_address("EEPROM", duthost.hostname, eeprom_mac_result)

    logger.info("Reading full EEPROM data to check for MAC Addresses field on '{}'".format(duthost.hostname))
    eeprom_full_result = duthost.command(cmd_eeprom_full, module_ignore_errors=True)

    pytest_assert(
        eeprom_full_result["rc"] == 0,
        "Failed to read full EEPROM data on '{}'. Return code: {}, Error: {}".format(
            duthost.hostname, eeprom_full_result["rc"], eeprom_full_result.get("stderr", "N/A")
        ),
    )

    mac_offset = mac_offset_config["offset_from_base_mac"]

    logger.info("Platform configuration: offset_from_base_mac = {}".format(mac_offset))

    base_mac_int = _mac_to_int(base_mac_from_eeprom)
    expected_mac_int = base_mac_int + mac_offset
    expected_mac = _int_to_mac(expected_mac_int)
    mgmt_mac_int = _mac_to_int(mgmt_mac)

    logger.info(
        "MAC address calculation: base='{}' ({}), offset={}, expected='{}' ({})".format(
            base_mac_from_eeprom.lower(), base_mac_int, mac_offset, expected_mac, expected_mac_int
        )
    )

    logger.info("Comparing MAC addresses: eth0='{}' vs expected='{}'".format(mgmt_mac, expected_mac))
    pytest_assert(
        mgmt_mac_int == expected_mac_int,
        "Management port MAC address mismatch on '{}'. "
        "eth0 MAC: '{}', EEPROM base MAC: '{}', Configured offset: {}, "
        "Expected MAC (base + offset): '{}'. "
        "The eth0 MAC should equal base MAC + configured offset.".format(
            duthost.hostname, mgmt_mac, base_mac_from_eeprom, mac_offset, expected_mac
        ),
    )

    # If MAC Addresses field is available, validate eth0 MAC is within range
    mac_count = _parse_mac_address_count(eeprom_full_result["stdout"].strip())
    if mac_count:
        logger.info(
            "Found MAC Addresses field in EEPROM: {} addresses allocated starting from '{}'".format(
                mac_count, base_mac_from_eeprom
            )
        )

        pytest_assert(
            base_mac_int <= mgmt_mac_int and mgmt_mac_int < base_mac_int + mac_count,
            "Management port MAC address is outside the allocated range on '{}'. "
            "eth0 MAC: '{}', EEPROM base MAC: '{}', Allocated range: {} addresses "
            "[{} - {}]. "
            "The eth0 MAC should fall within the allocated MAC address range.".format(
                duthost.hostname,
                mgmt_mac,
                base_mac_from_eeprom,
                mac_count,
                base_mac_from_eeprom,
                _int_to_mac(base_mac_int + mac_count - 1),
            ),
        )
    else:
        logger.info(
            "MAC Addresses field (0x2A) not found in EEPROM on '{}'. Skipping range validation.".format(
                duthost.hostname
            )
        )

    logger.info(
        "Management MAC address consistency check passed on '{}'. eth0 MAC: '{}', base MAC: '{}', offset: {}".format(
            duthost.hostname, mgmt_mac, base_mac_from_eeprom, mac_offset
        )
    )
