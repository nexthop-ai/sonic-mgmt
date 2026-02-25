import logging
import time
import pytest

logger = logging.getLogger(__name__)


# Route counter service runs on a 30s timer by default
# During tests we speed it up to 5s for faster test execution
ROUTE_COUNTER_TEST_INTERVAL = 3
ROUTE_COUNTER_UPDATE_TIMEOUT = 6  # Wait for 2 cycles to be safe


@pytest.fixture(scope='module', autouse=True)
def speed_up_route_counter(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """
    Speed up route-counter timer for faster test execution.
    Changes the timer from 30s to 5s for the duration of the test module.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    logger.info(f"Configuring route-counter timer to {ROUTE_COUNTER_TEST_INTERVAL}s for testing")

    # Create systemd drop-in override to speed up the timer
    override_content = f"""[Timer]
OnUnitInactiveSec={ROUTE_COUNTER_TEST_INTERVAL}sec
"""

    duthost.shell("sudo mkdir -p /etc/systemd/system/route-counter.timer.d/")
    duthost.shell(f"echo '{override_content}' | sudo tee /etc/systemd/system/route-counter.timer.d/test-override.conf")
    duthost.shell("sudo systemctl daemon-reload")
    duthost.shell("sudo systemctl restart route-counter.timer")

    # Wait for one cycle to ensure the new timing is active
    time.sleep(ROUTE_COUNTER_TEST_INTERVAL)

    logger.info("Route-counter timer configured for testing")

    yield

    # Restore original timer settings
    logger.info("Restoring original route-counter timer settings")
    duthost.shell("sudo rm -f /etc/systemd/system/route-counter.timer.d/test-override.conf")
    duthost.shell("sudo systemctl daemon-reload")
    duthost.shell("sudo systemctl restart route-counter.timer")


@pytest.fixture(autouse=True)
def wait_for_route_count_sync():
    """
    Wait for route counter service to sync before each test.
    """
    time.sleep(ROUTE_COUNTER_UPDATE_TIMEOUT)
