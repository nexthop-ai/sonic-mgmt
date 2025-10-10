import pytest
import logging

from tests.common.utilities import wait_until
from tests.common.macsec.macsec_helper import check_appl_db, restart_macsec_service_with_retry

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.macsec_required,
    pytest.mark.topology('t2')
]


@pytest.fixture(scope="function")
def ignore_expected_loganalyzer_exceptions(duthosts, loganalyzer):
    """Ignore only the known-transient MACsec restart logs during this test's window.

    Notes:
    - Broadcom/DNX-specific messages are gated by ASIC type
    - The generic flex-counter and meta warnings are still limited to this test
    - TODO(SONiC/SAI): Remove these ignores once MACsec teardown ordering and
      flex-counter drain are fixed upstream
    """
    if not loganalyzer:
        return

    # Generic messages seen transiently during MACsec teardown across platforms
    generic_ignores = [
        # Flex counter cleanup may log missing port OIDs; benign during teardown
        (
            r".*ERR syncd[0-9]*#syncd: .*processFlexCounterEvent: port VID oid:0x[0-9a-f]+,"
            r" was not found .* will remove from counters now"
        ),
        # MACSEC_SA OIDs may not exist after teardown order; expected transiently
        (
            r".*ERR swss#orchagent: .*meta_sai_validate_oid: object key "
            r"SAI_OBJECT_TYPE_MACSEC_SA:oid:0x[0-9a-f]+ doesn't exist"
        ),
        # OBJECT_IN_USE during restart cleanup while refs wind down
        r".*ERR syncd[0-9]*#syncd: .*sendApiResponse: api SAI_COMMON_API_REMOVE failed .* SAI_STATUS_OBJECT_IN_USE",
        r".*ERR swss#orchagent: .*remove: remove status: SAI_STATUS_OBJECT_IN_USE",
        (
            r".*ERR swss#orchagent: .*meta_generic_validation_remove: object 0x[0-9a-f]+"
            r" reference count is 1, can't remove"
        ),
    ]

    # Vendor/ASIC-specific messages (Broadcom DNX)
    brcm_dnx_ignores = [
        # During restart, MACsec SA attribute read may see invalid object type
        (
            r".*ERR syncd[0-9]*#syncd: .*SAI_API_MACSEC:brcm_sai_dnx_get_macsec_sa_attribute:.*Invalid object type "
            r".* passed"
        ),
        # Removing SCs while SAs are still winding down
        r".*ERR syncd[0-9]*#syncd: .*SAI_API_MACSEC:brcm_sai_dnx_remove_macsec_sc:.*Active SAs are present",
    ]

    for duthost in duthosts:
        asic = duthost.get_facts().get("asic_type", "")
        # Always apply the generic, restart-window ignores for this test
        loganalyzer[duthost.hostname].ignore_regex.extend(generic_ignores)
        # Apply Broadcom-specific ignores only on Broadcom ASICs
        if asic == "broadcom":
            loganalyzer[duthost.hostname].ignore_regex.extend(brcm_dnx_ignores)


def test_restart_macsec_docker(duthosts, ctrl_links, policy, cipher_suite, send_sci,
                               enum_rand_one_per_hwsku_macsec_frontend_hostname,
                               ignore_expected_loganalyzer_exceptions):
    duthost = duthosts[enum_rand_one_per_hwsku_macsec_frontend_hostname]

    # Restart macsec.service and validate stabilization strictly via APPL_DB
    logger.info(duthost.shell(cmd="docker ps", module_ignore_errors=True)['stdout'])
    restart_success = restart_macsec_service_with_retry(
        duthost,
        max_retries=5,
        delay_seconds=30,
        stop_clean_timeout=180,
    )
    assert restart_success, "Failed to restart macsec.service"

    # Wait until MACsec is stable in DB/kernel and add a short settle for pollers to quiet down
    logger.info(duthost.shell(cmd="docker ps", module_ignore_errors=True)['stdout'])
    assert wait_until(300, 6, 12, check_appl_db, duthost, ctrl_links, policy, cipher_suite, send_sci)
    logger.info("MACsec stable; settling 5s to let pollers/flex counters unwind")
    import time
    time.sleep(5)
