# Copyright 2026 Nexthop Systems Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import logging

import pytest

from tests.common.helpers.assertions import pytest_assert

logger = logging.getLogger(__name__)

INTERNAL_PORT_PREFIXES = ("Ethernet-Rec", "Ethernet-IB", "Ethernet-BP")


def select_ingress_port(duthost, ptf_indices, exclude_ports=()):
    """
    Returns the name of an oper UP front-panel Ethernet interface that has a PTF index and is not
    in exclude_ports, for PTF to send traffic to. Internal ports (recirculation, inband, backplane)
    are skipped by name as well: some topologies give them a PTF index that belongs to a front-panel port.
    """
    interfaces_status = duthost.show_interface(command="status")["ansible_facts"]["int_status"]
    for intf, info in interfaces_status.items():
        if (info["oper_state"].lower() == "up" and intf.startswith("Ethernet")
                and not intf.startswith(INTERNAL_PORT_PREFIXES)
                and intf in ptf_indices and intf not in exclude_ports):
            logger.info(f"Selected '{intf}' as ingress port.")
            return intf
    pytest.skip("No suitable ingress port found on the DUT.")


def get_queue_key(duthost, port, queue, sonic_db_cli="sonic-db-cli"):
    """
    Returns the CONFIG_DB QUEUE key for a port/queue. VOQ switches key QUEUE by system port
    (<host>|<asic>|<port>), whose host part is not always the hostname, and many queues have no
    QUEUE entry yet, so the key is built from the port's SYSTEM_PORT entry.
    sonic_db_cli may carry a namespace option, e.g. "sonic-db-cli -n asic0".
    """
    system_ports = duthost.shell(f"{sonic_db_cli} CONFIG_DB KEYS 'SYSTEM_PORT|*|{port}'")["stdout"].split()
    system_ports = [key for key in system_ports if key.endswith(f"|{port}")]
    if not system_ports:
        return f"QUEUE|{port}|{queue}"
    pytest_assert(len(system_ports) == 1, f"Expected one SYSTEM_PORT entry for {port}, found {system_ports}.")
    return f"QUEUE|{system_ports[0].split('|', 1)[1]}|{queue}"
