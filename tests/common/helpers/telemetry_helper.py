import logging
from contextlib import contextmanager
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.gnmi_utils import GNMIEnvironment
from tests.common.helpers.assertions import pytest_assert as py_assert
from tests.common.utilities import wait_until, get_mgmt_ipv6, wait_tcp_connection

logger = logging.getLogger(__name__)


def check_gnmi_config(duthost):
    cmd = 'sonic-db-cli CONFIG_DB HGET "GNMI|gnmi" port'
    port = duthost.shell(cmd, module_ignore_errors=False)['stdout']
    return port != ""


def create_gnmi_config(duthost):
    cmd = "sonic-db-cli CONFIG_DB hset 'GNMI|gnmi' port 50052"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hset 'GNMI|gnmi' client_auth true"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hset 'GNMI|certs' "\
          "ca_crt /etc/sonic/telemetry/dsmsroot.cer"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hset 'GNMI|certs' "\
          "server_crt /etc/sonic/telemetry/streamingtelemetryserver.cer"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hset 'GNMI|certs' "\
          "server_key /etc/sonic/telemetry/streamingtelemetryserver.key"
    duthost.shell(cmd, module_ignore_errors=True)


def delete_gnmi_config(duthost):
    cmd = "sonic-db-cli CONFIG_DB hdel 'GNMI|gnmi' port"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hdel 'GNMI|gnmi' client_auth"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hdel 'GNMI|certs' ca_crt"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hdel 'GNMI|certs' server_crt"
    duthost.shell(cmd, module_ignore_errors=True)
    cmd = "sonic-db-cli CONFIG_DB hdel 'GNMI|certs' server_key"
    duthost.shell(cmd, module_ignore_errors=True)


def setup_telemetry_forpyclient(duthost):
    """ Set client_auth=false. This is needed for pyclient to successfully set up channel with gnmi server.
        Restart telemetry process
    """
    if not duthost.is_service_fully_started("telemetry"):
        logger.info("Telemetry container is not running in setup_telemetry_forpyclient, starting it")

        duthost.service(name="telemetry", state="started")
        py_assert(wait_until(100, 10, 0, duthost.is_service_fully_started, "telemetry"),
                  "telemetry not started.")
        logger.info("Telemetry container started successfully")

    env = GNMIEnvironment(duthost, GNMIEnvironment.TELEMETRY_MODE)
    client_auth_out = duthost.shell('sonic-db-cli CONFIG_DB HGET "%s|gnmi" "client_auth"' % (env.gnmi_config_table),
                                    module_ignore_errors=False)['stdout_lines']
    client_auth = str(client_auth_out[0])

    if client_auth == "true":
        duthost.shell('sonic-db-cli CONFIG_DB HSET "%s|gnmi" "client_auth" "false"' % (env.gnmi_config_table),
                      module_ignore_errors=False)
        duthost.shell("systemctl reset-failed %s" % (env.gnmi_container))
        duthost.service(name=env.gnmi_container, state="restarted")
        # Wait until telemetry was restarted
        py_assert(wait_until(100, 10, 0, duthost.is_service_fully_started, env.gnmi_container),
                  "%s not started." % (env.gnmi_container))
        logger.info("telemetry process restarted")
    else:
        logger.info('client auth is false. No need to restart telemetry')

    return client_auth


def restore_telemetry_forpyclient(duthost, default_client_auth):
    env = GNMIEnvironment(duthost, GNMIEnvironment.TELEMETRY_MODE)
    client_auth_out = duthost.shell('sonic-db-cli CONFIG_DB HGET "%s|gnmi" "client_auth"' % (env.gnmi_config_table),
                                    module_ignore_errors=False)['stdout_lines']
    client_auth = str(client_auth_out[0])
    if client_auth != default_client_auth:
        duthost.shell('sonic-db-cli CONFIG_DB HSET "%s|gnmi" "client_auth" %s'
                      % (env.gnmi_config_table, default_client_auth),
                      module_ignore_errors=False)
        duthost.shell("systemctl reset-failed %s" % (env.gnmi_container))
        duthost.service(name=env.gnmi_container, state="restarted")


@contextmanager
def setup_streaming_telemetry_context(is_ipv6, duthost, localhost, ptfhost, gnxi_path):
    """
    @summary: Post setting up the streaming telemetry before running the test.

    This function ensures the telemetry container is started if it's not already running,
    configures it for testing, and waits for it to be ready.
    """
    telemetry_was_enabled = False
    try:
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
        logger.warning(f"Failed to check/enable telemetry feature: {e}")

    default_client_auth = None
    try:
        has_gnmi_config = check_gnmi_config(duthost)
        if not has_gnmi_config:
            create_gnmi_config(duthost)

        if not duthost.is_service_fully_started("telemetry"):
            logger.info("Telemetry container is not running, starting it now")

            duthost.service(name="telemetry", state="started")
            py_assert(wait_until(100, 10, 0, duthost.is_service_fully_started, "telemetry"),
                      "telemetry not started.")
            logger.info("Telemetry container started successfully")

        env = GNMIEnvironment(duthost, GNMIEnvironment.TELEMETRY_MODE)
        default_client_auth = setup_telemetry_forpyclient(duthost)

        # Wait until the TCP port was opened
        dut_ip = duthost.mgmt_ip
        if is_ipv6:
            dut_ip = get_mgmt_ipv6(duthost)
        wait_tcp_connection(localhost, dut_ip, env.gnmi_port, timeout_s=60)

        # pyclient should be available on ptfhost. If it was not available, then fail pytest.
        if is_ipv6:
            cmd = "docker cp %s:/usr/sbin/gnmi_get ~/" % (env.gnmi_container)
            ret = duthost.shell(cmd)['rc']
            py_assert(ret == 0)
        else:
            file_exists = ptfhost.stat(path=gnxi_path + "gnmi_cli_py/py_gnmicli.py")
            py_assert(file_exists["stat"]["exists"] is True)
    except RunAnsibleModuleFail as e:
        logger.info("Error happens in the setup period of setup_streaming_telemetry, recover the telemetry.")
        restore_telemetry_forpyclient(duthost, default_client_auth)
        # Restore telemetry feature state if it was disabled before
        if not telemetry_was_enabled:
            try:
                logger.info("Restoring telemetry feature to disabled state")
                duthost.shell("sudo config feature state telemetry disabled", module_ignore_errors=True)
            except Exception as ex:
                logger.warning(f"Failed to restore telemetry feature state: {ex}")

        raise e

    try:
        yield
    finally:
        # Cleanup always runs, even if test fails
        restore_telemetry_forpyclient(duthost, default_client_auth)
        if not has_gnmi_config:
            delete_gnmi_config(duthost)

        # Restore telemetry feature state if it was disabled before the test
        if not telemetry_was_enabled:
            try:
                logger.info("Restoring telemetry feature to disabled state")
                duthost.shell("sudo config feature state telemetry disabled", module_ignore_errors=True)
                logger.info("Telemetry feature disabled successfully")
            except Exception as e:
                logger.warning(f"Failed to restore telemetry feature state: {e}")
