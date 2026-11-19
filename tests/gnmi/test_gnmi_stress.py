import json
import logging
<<<<<<< HEAD
import time

import grpc
import pytest

from tests.common.fixtures.grpc_fixtures import gnmi_tls  # noqa: F401
from tests.common.helpers.gnmi_connection import (
    build_native_set_request,
    gnmi_connection,
)
=======
import pytest
import shlex
import time

from tests.common.pygnmi_client import PygnmiClient
>>>>>>> 7e3cfbbfe (NOS-23996: [gnmi] Move test_gnmi_latency_01 to PygnmiClient (#5188))

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("any"),
    pytest.mark.disable_loganalyzer,
    pytest.mark.usefixtures("setup_gnmi_ntp_client_server", "check_dut_timestamp"),
]


def get_first_interface(duthost):
    cfg_facts = duthost.config_facts(host=duthost.hostname, source="running")["ansible_facts"]
    port_table = cfg_facts["PORT"]

    # Find the first interface with lanes (physical port, not portchannel)
    for interface_name, interface_config in port_table.items():
        if "lanes" in interface_config:
            # Check if admin status is up
            if interface_config.get("admin_status", "").lower() == "up":
                return interface_name

    # If no interface with admin_status up found, return the first one with lanes
    for interface_name, interface_config in port_table.items():
        if "lanes" in interface_config:
            return interface_name

    return None


<<<<<<< HEAD
def test_gnmi_latency_01(duthosts, rand_one_dut_hostname, ptfhost, gnmi_tls):  # noqa: F811
    """
=======
def test_gnmi_latency_01(duthosts, rand_one_dut_hostname, pygnmi_client: PygnmiClient):
    '''
>>>>>>> 7e3cfbbfe (NOS-23996: [gnmi] Move test_gnmi_latency_01 to PygnmiClient (#5188))
    Verify GNMI native write latency
    Update interface description repeatedly and check latency
    """
    duthost = duthosts[rand_one_dut_hostname]
    if duthost.is_supervisor_node():
        pytest.skip("gnmi test relies on port data not present on supervisor card '%s'" % rand_one_dut_hostname)
    interface = get_first_interface(duthost)
    if interface is None:
        pytest.skip("No valid interface found on DUT '%s'" % rand_one_dut_hostname)

    test_loop = 10
<<<<<<< HEAD
    text = '"down"'
    down_list = ["/sonic-db:CONFIG_DB/localhost/PORT/%s/description" % interface]
    down_request = build_native_set_request(down_list[0].split(":", 1)[1].split("/"), json.loads(text))
    text = '"up"'
    up_list = ["/sonic-db:CONFIG_DB/localhost/PORT/%s/description" % interface]
    up_request = build_native_set_request(up_list[0].split(":", 1)[1].split("/"), json.loads(text))
=======
    path = f"sonic-db:CONFIG_DB/localhost/PORT/{interface}/description"
    down_update = [(path, json.dumps("down"))]
    up_update = [(path, json.dumps("up"))]
    port_key = shlex.quote(f"PORT|{interface}")
    # hget prints an empty line both for an unset field and for an empty one, so ask hexists which it is.
    # sonic-db-cli prints hexists as "True"/"False"; accept a redis-style "1" too.
    hexists = duthost.shell(f"sonic-db-cli CONFIG_DB hexists {port_key} description")["stdout"].strip()
    was_set = hexists.lower() in ("1", "true")
    original_description = duthost.shell(f"sonic-db-cli CONFIG_DB hget {port_key} description")["stdout"]
>>>>>>> 7e3cfbbfe (NOS-23996: [gnmi] Move test_gnmi_latency_01 to PygnmiClient (#5188))

    # Initialize latency tracking
    total_latencies = []

<<<<<<< HEAD
    with gnmi_connection(gnmi_tls) as (channel, client):
        grpc.channel_ready_future(channel).result(timeout=30)
        logger.info("Latency measures paired Set calls on a ready shared TLS channel; excludes legacy PTF/CLI setup")
=======
    try:
>>>>>>> 7e3cfbbfe (NOS-23996: [gnmi] Move test_gnmi_latency_01 to PygnmiClient (#5188))
        for i in range(test_loop):
            logger.info(f"Starting iteration {i+1}/{test_loop}")

            # Measure total latency for both operations
            start_time = time.time()

            # Update description
<<<<<<< HEAD
            client.Set(down_request, timeout=60)
            # Update description
            client.Set(up_request, timeout=60)
=======
            pygnmi_client.set(update=down_update)
            # Update description
            pygnmi_client.set(update=up_update)
>>>>>>> 7e3cfbbfe (NOS-23996: [gnmi] Move test_gnmi_latency_01 to PygnmiClient (#5188))

            total_latency = (time.time() - start_time) / 2 * 1000  # Convert to milliseconds
            total_latencies.append(total_latency)
            logger.info(f"Total iteration latency: {total_latency:.2f} ms")
<<<<<<< HEAD
=======
    finally:
        # Restore the description directly in CONFIG_DB rather than through gNMI, so it is put back
        # even when the failure is in gNMI. Without this, a failed rollback in setup_gnmi_server's
        # teardown would be followed by its "config save" persisting the test value.
        if was_set:
            duthost.shell(f"sonic-db-cli CONFIG_DB hset {port_key} description {shlex.quote(original_description)}")
        else:
            duthost.shell(f"sonic-db-cli CONFIG_DB hdel {port_key} description")
>>>>>>> 7e3cfbbfe (NOS-23996: [gnmi] Move test_gnmi_latency_01 to PygnmiClient (#5188))

    # Calculate and log statistics
    avg_total = sum(total_latencies) / len(total_latencies)
    min_total = min(total_latencies)
    max_total = max(total_latencies)

    logger.info("=== GNMI SET LATENCY STATISTICS ===")
    logger.info(f"Total per iteration - Avg: {avg_total:.2f}ms, Min: {min_total:.2f}ms, Max: {max_total:.2f}ms")
    logger.info(f"Test completed: {test_loop} iterations on interface {interface}")
    cmd = "lscpu"
    output = duthost.shell(cmd)
    logger.info("CPU Info:\n%s" % output["stdout"])
