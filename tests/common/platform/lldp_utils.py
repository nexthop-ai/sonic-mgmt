"""
Helper script for checking status of LLDP

This script contains re-usable functions for checking status of LLDP on SONiC.
"""

import logging
from tests.common.utilities import wait_until
from tests.common.helpers.sonic_db import redis_get_keys_all_asics


def are_lldp_neighbors_present(duthost) -> bool:
    """
    Returns whether LLDP_ENTRY_TABLE has same number of entries as DEVICE_NEIGHBOR table.
    """
    try:
        device_neighbor_keys = redis_get_keys_all_asics(duthost, "CONFIG_DB", "DEVICE_NEIGHBOR|*")
        lldp_entry_keys = redis_get_keys_all_asics(duthost, "APPL_DB", "LLDP_ENTRY_TABLE:*")
        neighbors_present = len(device_neighbor_keys) == len(lldp_entry_keys)
        if not neighbors_present:
            logging.debug(
                f"DEVICE_NEIGHBOR: {device_neighbor_keys}, LLDP_ENTRY_TABLE: {lldp_entry_keys},"
                " not in sync yet"
            )
        return neighbors_present
    except Exception as e:
        logging.warning(f"Error checking LLDP neighbors: {e}")
        return False


def wait_for_lldp_neighbors(duthost, timeout) -> bool:
    """
    Waits for LLDP_ENTRY_TABLE to have same number of entries as DEVICE_NEIGHBOR table.
    This ensures lldpmgrd has populated all expected LLDP neighbors.
    """
    # Wait for LLDP neighbors, checking every 5 seconds
    neighbors_present = wait_until(timeout, 5, 0, are_lldp_neighbors_present, duthost)
    if not neighbors_present:
        logging.warning(f"Timeout waiting for LLDP neighbors after {timeout} seconds")
    return neighbors_present
