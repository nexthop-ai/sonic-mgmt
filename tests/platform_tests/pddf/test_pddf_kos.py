"""
Test to verify PDDF kernel modules are loaded.

This test checks that all kernel modules listed in the pddf-device.json file
under the "pddf_kos" key are loaded on the DUT.
"""

import logging
import pytest

from tests.common.helpers.assertions import pytest_assert
from .pddf_helpers import check_pddf_device_json_exists, read_pddf_device_json

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology('any')
]


def test_pddf_kernel_modules_loaded(duthosts, enum_rand_one_per_hwsku_hostname):
    """
    Test to verify that all PDDF kernel modules are loaded.

    This test:
    1. Checks if pddf-device.json exists on the DUT
    2. Reads the JSON file and extracts the "pddf_kos" list
    3. Runs lsmod command to get loaded kernel modules
    4. Verifies that each module in pddf_kos is present in lsmod output

    Args:
        duthosts: Fixture for DUT hosts
        enum_rand_one_per_hwsku_hostname: Fixture to select one DUT per hwsku
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    # Check if pddf-device.json file exists (skip test if not)
    check_pddf_device_json_exists(duthost, skip_if_missing=True)

    # Read and parse the pddf-device.json file
    pddf_device_data = read_pddf_device_json(duthost)

    # Extract pddf_kos list
    # The pddf_kos list can be at the root level or under "PLATFORM" key
    pddf_kos_list = None

    if 'PLATFORM' in pddf_device_data and 'pddf_kos' in pddf_device_data['PLATFORM']:
        pddf_kos_list = pddf_device_data['PLATFORM']['pddf_kos']
    elif 'pddf_kos' in pddf_device_data:
        pddf_kos_list = pddf_device_data['pddf_kos']
    else:
        pytest.skip("No 'pddf_kos' key found in pddf-device.json (checked root and PLATFORM levels), skipping test")

    if not pddf_kos_list:
        pytest.skip("'pddf_kos' list is empty in pddf-device.json, skipping test")

    logger.info("Found {} PDDF kernel modules to check: {}".format(len(pddf_kos_list), pddf_kos_list))

    # Run lsmod command to get loaded kernel modules
    logger.info("Running lsmod command on DUT")
    lsmod_output = duthost.command("lsmod")
    lsmod_lines = lsmod_output['stdout_lines']

    # Parse lsmod output to get list of loaded module names
    # lsmod output format:
    # Module                  Size  Used by
    # module_name            12345  0
    loaded_modules = set()
    for line in lsmod_lines[1:]:  # Skip header line
        if line.strip():
            # Module name is the first field
            module_name = line.split()[0]
            loaded_modules.add(module_name)

    logger.info("Found {} loaded kernel modules".format(len(loaded_modules)))

    # Check each pddf_kos entry is loaded
    missing_modules = []
    for pddf_module in pddf_kos_list:
        if pddf_module not in loaded_modules:
            missing_modules.append(pddf_module)
            logger.error("PDDF kernel module '{}' is NOT loaded".format(pddf_module))
        else:
            logger.info("PDDF kernel module '{}' is loaded".format(pddf_module))

    # Assert that all modules are loaded
    pytest_assert(
        len(missing_modules) == 0,
        "The following PDDF kernel modules are not loaded: {}".format(missing_modules)
    )

    logger.info("All {} PDDF kernel modules are loaded successfully".format(len(pddf_kos_list)))
