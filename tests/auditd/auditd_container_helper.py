"""
Auditd Container Management Helper
Provides conditional auditd container management for testing.
Use --manage-auditd-containers flag to enable container lifecycle management.
"""

import logging
import pytest
from tests.common.helpers.dut_utils import is_container_running
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

# Constants
AUDITD_WATCHDOG_PORT = 50058
CONTAINER_START_TIMEOUT = 30
HTTP_STATUS_OK = "200"


class AuditdContainerManager:
    """
    Manages auditd containers for testing
    """

    def __init__(self, duthost):
        self.duthost = duthost
        self.containers = {"auditd": "docker-auditd:latest", "auditd_watchdog": "docker-auditd-watchdog:latest"}
        self.started_containers = []

    def stop_system_auditd(self):
        """Stop system auditd to allow container auditd to bind to kernel socket"""
        logger.info("Stopping system auditd service")
        result = self.duthost.shell("sudo systemctl stop auditd", module_ignore_errors=True)
        if result["rc"] != 0:
            pytest.fail(f"Failed to stop system auditd: {result.get('stderr', 'Unknown error')}")

    def start_system_auditd(self):
        """Restart system auditd after container testing"""
        logger.info("Restarting system auditd service")
        result = self.duthost.shell("sudo systemctl start auditd", module_ignore_errors=True)
        if result["rc"] != 0:
            logger.warning(f"Failed to restart system auditd: {result.get('stderr', 'Unknown error')}")

    def start_containers(self):
        """Start auditd containers if not running"""
        for container_name, image_name in self.containers.items():
            if not is_container_running(self.duthost, container_name):
                logger.info(f"Starting {container_name} container for testing")

                # Clean up any existing stopped container
                self.duthost.shell(f"docker stop {container_name} 2>/dev/null || true", module_ignore_errors=True)
                self.duthost.shell(f"docker rm {container_name} 2>/dev/null || true", module_ignore_errors=True)

                # Start with proper configuration for testing
                start_cmd = f"""docker run -d \\
                    --name {container_name} \\
                    -t --privileged --pid=host --network=host \\
                    -v /lib/systemd/system:/lib/systemd/system:rw \\
                    -v /etc/audit:/etc/audit:rw \\
                    -v /etc/sonic:/etc/sonic:ro \\
                    -v /etc/localtime:/etc/localtime:ro \\
                    {image_name}"""

                result = self.duthost.shell(start_cmd)
                if result["rc"] == 0:
                    self.started_containers.append(container_name)
                    logger.info(f"Started {container_name} successfully")

                    # Wait for container to be running
                    if not wait_until(
                        CONTAINER_START_TIMEOUT, 1, 0, is_container_running, self.duthost, container_name
                    ):
                        pytest.fail(
                            f"Container {container_name} failed to start within {CONTAINER_START_TIMEOUT} seconds"
                        )
                    logger.info(f"{container_name} is running and ready")
                else:
                    error_msg = result.get("stderr", result.get("stdout", "Unknown error"))
                    logger.error(f"Failed to start {container_name}: {error_msg}")
                    raise Exception(f"Failed to start {container_name}: {error_msg}")
            else:
                logger.info(f"{container_name} is already running")

    def stop_containers(self):
        """Stop containers and restart system auditd"""
        for container_name in self.started_containers:
            logger.info(f"Stopping {container_name} container")
            self.duthost.shell(f"docker stop {container_name}", module_ignore_errors=True)
            self.duthost.shell(f"docker rm {container_name}", module_ignore_errors=True)
        self.started_containers = []
        self.start_system_auditd()

    def verify_containers_healthy(self):
        # Check auditd container has auditd process
        try:
            result = self.duthost.shell("docker exec auditd ps aux | grep '[a]uditd'", module_ignore_errors=True)
            if result["rc"] != 0:
                pytest.fail("auditd process not found in auditd container")
        except Exception as e:
            pytest.fail(f"Could not verify auditd process: {e}")

        # Check auditd_watchdog is responding
        try:
            result = self.duthost.shell(
                f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:{AUDITD_WATCHDOG_PORT}",
                module_ignore_errors=True,
            )
            if result.get("stdout", "").strip() != HTTP_STATUS_OK:
                logger.warning(f"auditd_watchdog not responding correctly: {result.get('stdout', 'no response')}")
            else:
                logger.info("auditd_watchdog is responding correctly")
        except Exception as e:
            logger.warning(f"Could not verify auditd_watchdog: {e}")

    def get_container_status(self):
        """Get status of all auditd containers"""
        status = {}
        for container_name in self.containers.keys():
            status[container_name] = is_container_running(self.duthost, container_name)
        return status

# Pytest fixtures for automatic container management


@pytest.fixture(scope="module", autouse=True)
def auditd_module_container_setup(request, duthosts, enum_rand_one_per_hwsku_hostname):
    """Conditionally manage auditd containers based on --manage-auditd-containers flag"""
    duthost = duthosts[enum_rand_one_per_hwsku_hostname]
    manage_containers = request.config.getoption("--manage-auditd-containers")

    if manage_containers:
        logger.info(f"Managing auditd containers on {duthost.hostname}")
        manager = AuditdContainerManager(duthost)
        manager.stop_system_auditd()
        manager.start_containers()
        manager.verify_containers_healthy()

        yield manager

        logger.info(f"Cleaning up auditd containers on {duthost.hostname}")
        manager.stop_containers()
    else:
        logger.info(f"Using externally managed auditd containers on {duthost.hostname}")
        yield None
