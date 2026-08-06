"""
Verify a TAM drop-monitor session survives warm reboot.

Warm boot re-creates the TAM transport during state restore. If the transport
attributes (notably the L4 source port) differ from the pre-reboot ones,
sairedis APPLY_VIEW emits a SET on the old in-use transport, which Broadcom
SAI rejects with SAI_STATUS_OBJECT_IN_USE — syncd enters shutdown-wait and
orchagent aborts. This test configures a drop-monitor session, warm reboots,
and verifies orchagent stays healthy and the session comes back active with
an identical transport source port.
"""
import copy
import json
import logging

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common import config_reload
from tests.common.reboot import reboot
from tests.common.platform.processes_utils import wait_critical_processes
from tests.common.platform.device_utils import verify_dut_health  # noqa: F401
from tests.common.plugins.loganalyzer.loganalyzer import LogAnalyzer, LogAnalyzerError
from tests.common.helpers.sonic_db import redis_get_keys, redis_hgetall_all_asics

from test_tam_mod import (
    TAM_MOD_CONFIG_TEMPLATE,
    TAM_COLLECTOR_IPV4,
    tam_asicdb_state,
    verify_tam_mod_config_applied,
    TAM_ASICDB_TIMEOUT,
    TAM_ASICDB_INTERVAL,
)

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("t1"),
    # Warm reboot is noisy; a scoped LogAnalyzer inside the test checks syslog.
    pytest.mark.disable_loganalyzer,
]

SESSION = next(iter(TAM_MOD_CONFIG_TEMPLATE["TAM_SESSION"]))
STATE_DB_SESSION_KEY = f"TAM_DROP_MONITOR_SESSION_TABLE|{SESSION}"

# Syslog signature of the warm-boot APPLY_VIEW failure and orchagent abort.
ORCH_FAILURE_REGEX = (
    r".*SAI_STATUS_OBJECT_IN_USE.*",
    r".*Aborting orchagent.*",
    r".*entering shutdown-wait mode.*",
)

SESSION_ACTIVE_TIMEOUT_SECONDS = 180
SESSION_ACTIVE_INTERVAL_SECONDS = 10


def _get_session_state(duthost):
    return redis_hgetall_all_asics(duthost, "STATE_DB", STATE_DB_SESSION_KEY)


def _wait_for_session_active(duthost, timeout=SESSION_ACTIVE_TIMEOUT_SECONDS,
                             interval=SESSION_ACTIVE_INTERVAL_SECONDS):
    return wait_until(timeout, interval, 0,
                      lambda: _get_session_state(duthost).get("status") == "active")


def _get_transport_src_ports(duthost):
    """Return {oid_key: src_port} for every TAM transport in ASIC_DB."""
    keys = redis_get_keys(duthost, "ASIC_DB",
                          "ASIC_STATE:SAI_OBJECT_TYPE_TAM_TRANSPORT:oid:*") or []
    return {key: redis_hgetall_all_asics(duthost, "ASIC_DB", key)
            .get("SAI_TAM_TRANSPORT_ATTR_SRC_PORT") for key in keys}


@pytest.fixture(scope="module")
def tam_drop_monitor_config(duthosts, rand_one_dut_hostname):
    """Apply the drop-monitor config (IPv4 collector) and clean up after."""
    duthost = duthosts[rand_one_dut_hostname]

    tam_config = copy.deepcopy(TAM_MOD_CONFIG_TEMPLATE)
    tam_config["TAM_COLLECTOR"]["COLLECTOR1"]["src_ip"] = TAM_COLLECTOR_IPV4["src_ip"]
    tam_config["TAM_COLLECTOR"]["COLLECTOR1"]["dst_ip"] = TAM_COLLECTOR_IPV4["dst_ip"]

    cfg_path = "/tmp/tam_warm_reboot_config.json"
    duthost.copy(content=json.dumps(tam_config, indent=2), dest=cfg_path)
    res = duthost.shell(f"sonic-cfggen -j {cfg_path} --write-to-db")
    pytest_assert(res["rc"] == 0, f"Failed to apply TAM drop-monitor config: {res}")

    verify_tam_mod_config_applied(duthost, "ipv4", flow_aware=False)

    yield duthost

    logger.info("Cleaning up TAM configuration")
    for table, entries in TAM_MOD_CONFIG_TEMPLATE.items():
        for key in entries:
            duthost.shell(f'sonic-db-cli CONFIG_DB DEL "{table}|{key}"',
                          module_ignore_errors=True)
    duthost.shell('config save -y', module_ignore_errors=True)

    # Deleting the TAM config does not reliably clear ASIC_DB (known
    # add/delete re-add bug), so reload and verify afterwards.
    config_reload(duthost, safe_reload=True, check_intf_up_ports=True, wait_for_bgp=True)
    pytest_assert(wait_until(TAM_ASICDB_TIMEOUT, TAM_ASICDB_INTERVAL, 0,
                             lambda: tam_asicdb_state(duthost, False)),
                  "TAM cleanup failed: ASIC_DB still has TAM keys after config reload")


def test_tam_drop_monitor_warm_reboot(tam_drop_monitor_config, localhost,
                                      verify_dut_health):  # noqa: F811
    duthost = tam_drop_monitor_config

    # Session must be active before the reboot.
    pytest_assert(_wait_for_session_active(duthost),
                  f"Session {SESSION} did not become active before warm reboot; "
                  f"state: {_get_session_state(duthost)}")

    pre_transport_ports = _get_transport_src_ports(duthost)
    pytest_assert(pre_transport_ports,
                  "No TAM transport found in ASIC_DB before warm reboot")
    pre_src_ports = sorted(pre_transport_ports.values())
    logger.info("Pre-reboot TAM transport src ports: %s", pre_transport_ports)

    duthost.shell('config save -y')

    loganalyzer = LogAnalyzer(ansible_host=duthost, marker_prefix="tam_warm_reboot")
    loganalyzer.match_regex = ORCH_FAILURE_REGEX
    try:
        with loganalyzer:
            reboot(duthost, localhost, reboot_type="warm",
                   wait_warmboot_finalizer=True, safe_reboot=True)

            wait_critical_processes(duthost)

            pytest_assert(_wait_for_session_active(duthost),
                          f"Session {SESSION} did not return to active after warm "
                          f"reboot; state: {_get_session_state(duthost)}")
    except LogAnalyzerError:
        pytest.fail("orchagent/syncd logged TAM failure signature during warm "
                    "reboot (SAI_STATUS_OBJECT_IN_USE / orchagent abort)")

    # OIDs may differ across warm boot, but the L4 source port must not.
    post_transport_ports = _get_transport_src_ports(duthost)
    post_src_ports = sorted(post_transport_ports.values())
    logger.info("Post-reboot TAM transport src ports: %s", post_transport_ports)
    pytest_assert(post_src_ports == pre_src_ports,
                  f"TAM transport src port changed across warm reboot: "
                  f"{pre_src_ports} -> {post_src_ports}")
