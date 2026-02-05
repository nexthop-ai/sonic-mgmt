import logging
import pytest
import os
import sys

from tests.common.helpers.assertions import pytest_assert as py_assert
from tests.common.utilities import wait_until
from tests.telemetry.telemetry_utils import get_list_stdout
from tests.common.helpers.telemetry_helper import setup_streaming_telemetry_context
from tests.common.helpers.gnmi_utils import GNMIEnvironment

EVENTS_TESTS_PATH = "./telemetry/events"
sys.path.append(EVENTS_TESTS_PATH)

BASE_DIR = "logs/telemetry"
DATA_DIR = os.path.join(BASE_DIR, "files")

logger = logging.getLogger(__name__)


@pytest.fixture
def skip_non_container_test(request):
    container_test = request.config.getoption("--container_test", default="")
    if not container_test:
        pytest.skip("Testcase skipped for non container test")


@pytest.fixture(scope="module", autouse=True)
def setup_user_auth(duthosts, enum_rand_one_per_hwsku_hostname, setup_streaming_telemetry):
    """
    Setup user authentication for telemetry tests.

    This fixture depends on setup_streaming_telemetry to ensure the telemetry
    container is set up before configuring user authentication.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    env = GNMIEnvironment(duthost, GNMIEnvironment.TELEMETRY_MODE)
    duthost.shell('sonic-db-cli CONFIG_DB hset "%s|gnmi" user_auth none' % (env.gnmi_config_table),
                  module_ignore_errors=False)
    duthost.shell('config save -y', module_ignore_errors=False)
    yield
    duthost.shell('sonic-db-cli CONFIG_DB hdel "%s|gnmi" user_auth' % (env.gnmi_config_table),
                  module_ignore_errors=False)
    duthost.shell('config save -y', module_ignore_errors=False)


@pytest.fixture(scope="module", autouse=True)
def verify_telemetry_dockerimage(duthosts, enum_rand_one_per_hwsku_hostname):
    """
    Verify telemetry docker image is available.

    This fixture runs first to check if the docker image exists before
    attempting to set up the telemetry container.
    """
    docker_out_list = []
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    docker_out = duthost.shell('docker images', module_ignore_errors=False)['stdout_lines']
    docker_out_list = get_list_stdout(docker_out)
    matching = [s for s in docker_out_list if b"docker-sonic-gnmi" in s or b"docker-sonic-telemetry" in s]
    if not (len(matching) > 0):
        pytest.skip("docker-sonic-gnmi and docker-sonic-telemetry are not part of the image")


@pytest.fixture(scope="module", autouse=True)
def setup_streaming_telemetry(duthosts, enum_rand_one_per_hwsku_hostname, localhost, ptfhost, gnxi_path,
                              verify_telemetry_dockerimage):
    """
    Setup streaming telemetry for all tests in this module.

    This fixture automatically sets up streaming telemetry with IPv4 (is_ipv6=False)
    for all tests in the telemetry directory.

    Depends on verify_telemetry_dockerimage to ensure the docker image exists
    before attempting setup.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    was_running = duthost.is_service_fully_started("telemetry")
    if was_running:
        logger.info("telemetry container running at start of test, will not tear down " +
                    "at the end of the test")

    try:
        with setup_streaming_telemetry_context(False, duthost, localhost, ptfhost, gnxi_path) as result:
            yield result
    finally:
        if not was_running:
            try:
                duthost.service(name="telemetry", state="stopped")
                logger.info("telemetry container stopped")

                duthost.shell("docker rm telemetry", module_ignore_errors=True)
                logger.info("telemetry container removed successfully")
            except Exception as e:
                logger.error("Failed to stop/remove telemetry container: %s", str(e))
                # Don't raise - we want cleanup to continue even if stop/remove fails


def do_init(duthost):
    for i in [BASE_DIR, DATA_DIR]:
        try:
            os.makedirs(i, exist_ok=True)
        except OSError as e:
            logger.error("Unexpected error while creating directory: {}".format(e))

    # Copy validate_yang_events.py from sonic-mgmt to DUT
    duthost.copy(src="telemetry/validate_yang_events.py", dest="~/")


@pytest.fixture(scope="module")
def test_eventd_healthy(duthosts, tbinfo, enum_rand_one_per_hwsku_hostname, ptfhost, ptfadapter,
                        setup_streaming_telemetry, gnxi_path):
    """
    @summary: Test eventd heartbeat before testing all testcases
    """

    duthost = duthosts[enum_rand_one_per_hwsku_hostname]

    if duthost.is_multi_asic:
        pytest.skip("Skip eventd testing on multi-asic")

    features_dict, succeeded = duthost.get_feature_status()
    if succeeded and ('eventd' not in features_dict or features_dict['eventd'] == 'disabled'):
        pytest.skip("eventd is disabled on the system")

    do_init(duthost)

    module = __import__("eventd_events")

    duthost.shell("systemctl restart eventd")

    py_assert(wait_until(100, 10, 0, duthost.is_service_fully_started, "eventd"), "eventd not started.")

    module.test_event(duthost, tbinfo, gnxi_path, ptfhost, ptfadapter, DATA_DIR, None)

    logger.info("Completed test file: {}".format("eventd_events test completed."))
