import binascii
import logging
import pytest
import paramiko
import time
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.tacacs.tacacs_helper import start_tacacs_server, tacacs_running
from tests.common.utilities import wait_until, paramiko_ssh
from ansible.errors import AnsibleConnectionFailure

logger = logging.getLogger(__name__)

TIMEOUT_LIMIT = 120

DEVICE_UNREACHABLE_MAX_RETRIES = 3

# Starting tac_plus on the PTF is occasionally flaky, so the teardown restart retries for this long.
TACACS_SERVER_START_TIMEOUT = 20


@pytest.fixture
def ensure_tacacs_server_running_after_ut(ptfhost):
    """Restart the PTF TACACS+ server after a test that stops it, even if the test failed.

    A server left stopped makes every later test in the module fail on TACACS+ login.
    """
    yield

    restart_tacacs_server_if_down(ptfhost)


def restart_tacacs_server_if_down(ptfhost):
    if tacacs_running(ptfhost):
        return
    logger.warning("TACACS+ server not running on PTF after test, restarting")
    pytest_assert(wait_until(TACACS_SERVER_START_TIMEOUT, 1, 0, start_tacacs_server, ptfhost),
                  "TACACS+ server on PTF failed to restart")
    logger.info("TACACS+ server on PTF restarted")


def check_server_received(ptfhost, data, timeout=30):
    """
        Check if tacacs server received the data.
    """
    hex = binascii.hexlify(data.encode('ascii'))
    hex_string = hex.decode()

    """
      Extract received data from tac_plus.log, then use grep to check if the received data contains hex_string:
            1. tac_plus server start with '-d 2058' parameter to log received data in following format in tac_plus.log:
                    Thu Mar  9 06:26:16 2023 [75483]: data[140] = 0xf8, xor'ed with hash[12] = 0xab -> 0x53
                    Thu Mar  9 06:26:16 2023 [75483]: data[141] = 0x8d, xor'ed with hash[13] = 0xc2 -> 0x4f
                In above log, the 'data[140] = 0xf8' is received data.

            2. Following sed command will extract the received data from tac_plus.log:
                    sed -n 's/.*-> 0x\(..\).*/\\1/p'  /var/log/tac_plus.log     # noqa: W605

            3. Following set command will join all received data to hex string:
                    sed ':a; N; $!ba; s/\\n//g'

            4. Then the grep command will check if the received hex data containes expected hex string.
                    grep '{0}'".format(hex_string)

      Also suppress following Flake8 error/warning:
            W605 : Invalid escape sequence. Flake8 can't handle sed command escape sequence, so will report false alert.
            E501 : Line too long. Following sed command difficult to split to multiple line.
    """
    sed_command = "sed -n 's/.*-> 0x\(..\).*/\\1/p'  /var/log/tac_plus.log | sed ':a; N; $!ba; s/\\n//g' | grep '{0}'".format(hex_string)   # noqa: W605 E501

    # After tacplus service receive data, it need take some time to update to log file.
    def log_exist(ptfhost, sed_command):
        res = ptfhost.shell(sed_command)
        logger.info(sed_command)
        logger.info(res["stdout_lines"])
        return len(res["stdout_lines"]) > 0

    exist = wait_until(timeout, 1, 0, log_exist, ptfhost, sed_command)
    pytest_assert(exist, "Not found data: {} in tacplus server log".format(data))


def get_auditd_config_reload_timestamp(duthost):
    res = duthost.shell("sudo journalctl -u auditd --boot --no-pager | grep 'audisp-tacplus re-initializing configuration'", module_ignore_errors=True)  # noqa: E501
    logger.info("aaa config file timestamp {}".format(res))

    if len(res["stdout_lines"]) == 0:
        return ""

    return res["stdout_lines"][-1]


def change_and_wait_aaa_config_update(duthost, command, last_timestamp=None, timeout=10):
    if not last_timestamp:
        last_timestamp = get_auditd_config_reload_timestamp(duthost)

    duthost.shell(command)

    # After AAA config update, hostcfgd will modify config file and notify auditd reload config
    # Wait auditd reload config finish
    def log_exist(duthost):
        latest_timestamp = get_auditd_config_reload_timestamp(duthost)
        reload = latest_timestamp != last_timestamp
        if not reload:
            # Send the HUP signal to auditd-tacplus to trigger a config reload
            duthost.shell("sudo kill -1 $(pidof audisp-tacplus)")
        return reload

    exist = wait_until(timeout, 1, 0, log_exist, duthost)
    if exist:
        # audisp-tacplus logs "re-initializing configuration" when re-init STARTS, not FINISHES.
        # Wait for it to complete reconnection to auditd before proceeding.
        time.sleep(3)
    pytest_assert(exist, "Not found aaa config update log: {}".format(command))


def ssh_run_command(ssh_client, command, expect_exit_code=0, verify=False):
    stdin, stdout, stderr = ssh_client.exec_command(command, timeout=TIMEOUT_LIMIT)
    exit_code = stdout.channel.recv_exit_status()
    if verify is True:
        if exit_code != expect_exit_code:
            # This if-block is here so that stdout.readlines() and
            # stderr.readlines() get evaluated if and only if the exit code
            # doesn't match the expected exit code. If they do match, and they
            # do get evaluated, then the state of the object will be different,
            # which will cause issues for other functions that use those
            # objects.
            pytest.fail(
                f"Command: '{command}' failed with exit code: {exit_code}, "
                f"stdout: {stdout.readlines()}, stderr: {stderr.readlines()}")
    return exit_code, stdout, stderr


def ssh_connect_remote_retry(remote_ip, remote_username, remote_password, duthost):
    retry_count = 3
    while retry_count > 0:
        try:
            return paramiko_ssh(remote_ip, remote_username, remote_password)
        except paramiko.ssh_exception.AuthenticationException as e:
            logger.info("Paramiko SSH connect failed with authentication: " + repr(e))
<<<<<<< HEAD

            # get syslog for debug
            recent_syslog = duthost.shell('sudo tail -100 /var/log/syslog')['stdout']
            logger.debug("Target device syslog: {}".format(recent_syslog))

        time.sleep(1)
        retry_count -= 1
=======
            # Diagnostic only: a failed fetch must not end the retries.
            try:
                recent_syslog = duthost.shell('sudo tail -100 /var/log/syslog', module_ignore_errors=True)['stdout']
                logger.debug("Target device syslog: {}".format(recent_syslog))
            except Exception as fetch_error:
                logger.warning("Could not fetch DUT syslog: {}".format(repr(fetch_error)))
            if retry_count <= 1:
                logger.error("SSH authentication failed after all retries: {}".format(repr(e)))
                raise
            logger.info("SSH authentication failed, retrying in {}s... ({} retries left)".format(
                retry_delay, retry_count - 1))
        except paramiko.ssh_exception.SSHException as e:
            error_msg = str(e)
            if "Not allowed at this time" not in error_msg and "Error reading SSH protocol banner" not in error_msg:
                logger.error("SSH connection failed with exception: {}".format(repr(e)))
                raise
            if retry_count <= 1:
                logger.error("TACACS+ authorization failed after all retries: {}".format(error_msg))
                raise
            logger.info("TACACS+ authorization not ready ({}), retrying in {}s... ({} retries left)".format(
                error_msg, retry_delay, retry_count - 1))
        except Exception as e:
            logger.error("Unexpected error during SSH connection: {}".format(repr(e)))
            raise

        time.sleep(retry_delay)
        retry_count -= 1
        retry_delay += 1  # linear backoff


def wait_for_tacacs_authorization_ready(duthost, remote_ip, remote_username, remote_password, timeout=30):
    """
    Wait until TACACS+ authorization is ready to accept SSH connections.

    This function polls the DUT by attempting SSH connections until successful,
    or until the timeout is reached. This is necessary after changing AAA authorization
    settings to ensure the SSH/PAM modules have reloaded the TACACS+ configuration.

    Args:
        duthost: The DUT host object
        remote_ip: IP address to connect to
        remote_username: Username for SSH connection
        remote_password: Password for SSH connection
        timeout: Maximum time to wait in seconds (default: 30)

    Returns:
        True if TACACS+ authorization is ready, False otherwise
    """
    def try_connect():
        try:
            # Attempt a quick SSH connection to verify TACACS+ authorization is working
            ssh_client = paramiko_ssh(remote_ip, remote_username, remote_password)
            ssh_client.close()
            logger.info("TACACS+ authorization is ready - SSH connection successful")
            return True
        except paramiko.ssh_exception.AuthenticationException as e:
            # Must precede SSHException, which it subclasses.
            logger.warning("Authentication failed during readiness check: {}".format(repr(e)))
            return False
        except paramiko.ssh_exception.SSHException as e:
            error_msg = str(e)
            # These errors indicate TACACS+ authorization is not ready yet
            if "Not allowed at this time" in error_msg or "Error reading SSH protocol banner" in error_msg:
                logger.debug("TACACS+ authorization not ready yet: {}".format(repr(e)))
            else:
                logger.warning("Unexpected SSH error during readiness check: {}".format(repr(e)))
            return False
        except Exception as e:
            logger.debug("Connection attempt failed: {}".format(repr(e)))
            return False

    logger.info("Waiting for TACACS+ authorization to be ready (timeout: {}s)...".format(timeout))
    result = wait_until(timeout, 2, 0, try_connect)

    if result:
        logger.info("TACACS+ authorization is ready")
    else:
        logger.warning("TACACS+ authorization readiness check timed out after {}s".format(timeout))

    return result
>>>>>>> 4c83cb8de (NOS-11732: [tacacs] Always restart the PTF TACACS+ server after server-down tests (#4914))


def duthost_shell_with_unreachable_retry(duthost, command):
    retries = 0
    while True:
        try:
            return duthost.shell(command)
        except AnsibleConnectionFailure as e:
            retries += 1
            logger.warning("retry_when_dut_unreachable exception: {}, retry {}/{}"
                           .format(e, retries, DEVICE_UNREACHABLE_MAX_RETRIES))
            if retries > DEVICE_UNREACHABLE_MAX_RETRIES:
                raise e


def cleanup_tacacs_log(ptfhost, rw_user_client):
    try:
        ptfhost.command('rm /var/log/tac_plus.acct')
    except RunAnsibleModuleFail:
        logger.info("/var/log/tac_plus.acct does not exist.")

    res = ptfhost.command('touch /var/log/tac_plus.acct')
    logger.info(res["stdout_lines"])

    ssh_run_command(rw_user_client, 'sudo truncate -s 0 /var/log/syslog')
    ptfhost.command('truncate -s 0 /var/log/tac_plus.log')


def count_authorization_request(ptfhost):
    hex_string = binascii.hexlify("cmd=/".encode('ascii')).decode()
    sed_command = "sed -n 's/.*-> 0x\(..\).*/\\1/p'  /var/log/tac_plus.log | sed ':a; N; $!ba; s/\\n//g'"  # noqa: W605 E501
    res = ptfhost.shell(sed_command)["stdout"]
    logger.warning("TACACS authorization request hex: {}".format(res))
    return res.count(hex_string)
