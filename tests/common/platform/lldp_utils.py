"""
Helper script for checking status of LLDP

This script contains re-usable functions for checking status of LLDP on SONiC.
"""

import logging
from tests.common.utilities import wait_until
from tests.common.helpers.sonic_db import redis_get_keys_all_asics


def are_lldp_neighbors_present(duthost) -> bool:
    """
    Returns whether all interfaces in DEVICE_NEIGHBOR table has shown up in LLDP_ENTRY_TABLE.

    Note: LLDP_ENTRY_TABLE may contain entries for interfaces not in DEVICE_NEIGHBOR
    (e.g., eth0 management interface). But the tests only care about the neighbors
    predefined in the ConfigDB's DEVICE_NEIGHBOR.
    """
    try:
        device_neighbor_keys = redis_get_keys_all_asics(duthost, "CONFIG_DB", "DEVICE_NEIGHBOR|*")
        lldp_entry_keys = redis_get_keys_all_asics(duthost, "APPL_DB", "LLDP_ENTRY_TABLE:*")

        # Extract interface names from the keys
        expected_interfaces = {key.split("|")[-1] for key in device_neighbor_keys if "|" in key}
        lldp_interfaces = {key.split(":")[-1] for key in lldp_entry_keys if ":" in key}

        # Check if all expected interfaces have LLDP entries
        missing_interfaces = expected_interfaces - lldp_interfaces
        if len(missing_interfaces) > 0:
            logging.debug(
                "LLDP sync pending.\n"
                f"Missing {len(missing_interfaces)} interfaces: {missing_interfaces}.\n"
                f"Current LLDP entries: {lldp_interfaces}."
            )
            return False

        return True
    except Exception as e:
        logging.warning(f"Error checking LLDP neighbors: {e}")
        return False


def wait_for_lldp_neighbors(duthost, timeout) -> bool:
    """
    Waits for all interfaces in DEVICE_NEIGHBOR table to show up in LLDP_ENTRY_TABLE.
    This ensures lldpmgrd has populated all expected LLDP neighbors.
    """
    # Wait for LLDP neighbors, checking every 5 seconds
    neighbors_present = wait_until(timeout, 5, 0, are_lldp_neighbors_present, duthost)
    if not neighbors_present:
        logging.warning(f"Timeout waiting for LLDP neighbors after {timeout} seconds")
    return neighbors_present
