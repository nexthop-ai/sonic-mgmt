import pytest
import logging

from tests.common.plugins.allure_wrapper import allure_step_wrapper as allure
from tests.gnmi_e2e.helper import setup_invalid_client_cert_cname, telemetry_enabled     # noqa: F401
from tests.common.helpers.gnmi_utils import GNMIEnvironment
from tests.common.helpers.assertions import pytest_assert as py_assert
from tests.common.utilities import wait_until
from tests.gnmi_e2e.conftest import setup_service_config

logger = logging.getLogger(__name__)
allure.logger = logger

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer
]


@pytest.fixture(scope="module", autouse=True)
def setup_telemetry_container(duthosts, rand_one_dut_hostname, setup_gnmi_server_e2e):
    """
    Ensure telemetry container is started for tests in this file.

    Depends on setup_gnmi_server_e2e to ensure certificates and configuration are set up first.

    This fixture ensures the telemetry container is started at the beginning of tests and
    stopped at the end (if it wasn't running before the tests).
    """
    duthost = duthosts[rand_one_dut_hostname]

    was_running = duthost.is_service_fully_started("telemetry")
    telemetry_was_enabled = False

    try:
        # Check if telemetry feature is enabled and enable it if needed
        features_dict, succeeded = duthost.get_feature_status()
        if succeeded and 'telemetry' in features_dict:
            telemetry_was_enabled = (features_dict['telemetry'] == 'enabled')
            if not telemetry_was_enabled:
                logger.info("Telemetry feature is disabled, enabling it for the test")
                duthost.shell("sudo config feature state telemetry enabled", module_ignore_errors=False)
                logger.info("Telemetry feature enabled successfully")
        else:
            logger.warning("Could not determine telemetry feature status")
    except Exception as e:
        logger.warning("Failed to check/enable telemetry feature: %s", str(e))

    try:
        if was_running:
            logger.info("telemetry container already running at start of test")
        else:
            logger.info("telemetry container not running, starting it now")
            duthost.service(name="telemetry", state="started")
            py_assert(wait_until(30, 5, 0, duthost.is_service_fully_started, "telemetry"),
                      "telemetry not started.")
            logger.info("telemetry container started successfully")

        # Configure the telemetry service with certificates
        tele_env = GNMIEnvironment(duthost, GNMIEnvironment.TELEMETRY_MODE)
        setup_service_config(duthost, tele_env.gnmi_config_table, tele_env.gnmi_port)

        # Restart telemetry service to apply the configuration
        command = "docker exec {} supervisorctl stop {}".format(tele_env.gnmi_container, tele_env.gnmi_program)
        duthost.shell(command, module_ignore_errors=True)

        command = "docker exec {} supervisorctl start {}".format(tele_env.gnmi_container, tele_env.gnmi_program)
        duthost.shell(command, module_ignore_errors=True)
        py_assert(wait_until(30, 5, 0, duthost.is_service_fully_started, "telemetry"),
                  "telemetry not started.")
        logger.info("telemetry service configured and restarted")

        yield
    finally:
        # Cleanup: stop and remove container if we started it
        if not was_running:
            try:
                duthost.service(name="telemetry", state="stopped")
                logger.info("telemetry container stopped")

                duthost.shell("docker rm telemetry", module_ignore_errors=True)
                logger.info("telemetry container removed successfully")
            except Exception as e:
                logger.error("Failed to stop/remove telemetry container: %s", str(e))
                # Don't raise - we want cleanup to continue even if it fails

        # Restore telemetry feature state if it was disabled before the test
        if not telemetry_was_enabled:
            try:
                logger.info("Restoring telemetry feature to disabled state")
                duthost.shell("sudo config feature state telemetry disabled", module_ignore_errors=True)
                logger.info("Telemetry feature disabled successfully")
            except Exception as e:
                logger.warning("Failed to restore telemetry feature state: %s", str(e))


def ptf_telemetry_get(duthost, ptfhost):
    output = ptfhost.shell("whoami", module_ignore_errors=True)
    logger.debug("whoami: {}".format(output))

    env = GNMIEnvironment(duthost, GNMIEnvironment.TELEMETRY_MODE)
    ip = duthost.mgmt_ip
    port = env.gnmi_port
    cmd = '/root/env-python3/bin/python /root/gnxi/gnmi_cli_py/py_gnmicli.py '
    cmd += '--timeout 30 '
    cmd += '-t %s -p %u ' % (ip, port)
    cmd += '-xo sonic-db '
    cmd += '-rcert /root/gnmiCA.pem '
    cmd += '-pkey /root/gnmiclient.key '
    cmd += '-cchain /root/gnmiclient.crt '
    cmd += '-m get -x DEVICE_METADATA/localhost -xt CONFIG_DB'
    output = ptfhost.shell(cmd, module_ignore_errors=True)
    logger.debug("ptf_telemetry_capabilities: {} output: {}".format(cmd, output))
    return output['failed'], "\n".join(output['stdout_lines'])


def test_telemetry_authorize_passed_with_valid_cname(duthosts,
                                                     rand_one_dut_hostname,
                                                     ptfhost):
    '''
    Verify telemetry authorization using a valid certificate to ensure secure access
    '''
    duthost = duthosts[rand_one_dut_hostname]

    failed, msg = ptf_telemetry_get(duthost, ptfhost)
    logger.debug("test_telemetry_authorize_passed_with_valid_cname: {}".format(msg))

    assert not failed, ("Telemetry 'get' command failed to execute: {}").format(msg)

    assert "Unauthenticated" not in msg, (
        "'Unauthenticated' error message found in Telemetry response. "
        "- Actual message: '{}'"
    ).format(msg)


def test_telemetry_authorize_failed_with_invalid_cname(duthosts,
                                                       rand_one_dut_hostname,
                                                       ptfhost,
                                                       setup_invalid_client_cert_cname):    # noqa: F811
    '''
    Verify telemetry authorization using an invalid certificate to confirm rejection behavior
    '''
    duthost = duthosts[rand_one_dut_hostname]

    failed, msg = ptf_telemetry_get(duthost, ptfhost)
    logger.debug("test_telemetry_authorize_failed_with_invalid_cname: {}".format(msg))

    assert failed, ("Telemetry 'get' command executed successfully: {}").format(msg)

    assert "Unauthenticated" in msg, (
        "'Unauthenticated' error message not found in Telemetry response. "
        "- Actual message: '{}'"
    ).format(msg)
