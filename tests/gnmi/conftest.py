import pytest
import shutil
import logging
import os
import glob
import grpc

from grpc_tools import protoc

from tests.common.helpers.assertions import pytest_require as pyrequire
from tests.common.helpers.dut_utils import check_container_state
from tests.gnmi.helper import gnmi_container, apply_cert_config, recover_cert_config, create_ext_conf, create_ca_conf
from tests.gnmi.helper import GNMI_SERVER_START_WAIT_TIME, check_ntp_sync_status, is_mgmt_vrf_enabled
from tests.common.gu_utils import create_checkpoint, rollback
from tests.common.helpers.gnmi_utils import GNMIEnvironment
from tests.common.helpers.ntp_helper import setup_ntp_context
from tests.common.utilities import DEFAULT_VRF_NAME, MGMT_VRF_NAME


logger = logging.getLogger(__name__)
SETUP_ENV_CP = "test_setup_checkpoint"

VRF_SCENARIOS = [
    {"name": "default_1", "vrf": None, "description": "Default (no VRF)"},
    {"name": "default_2", "vrf": DEFAULT_VRF_NAME, "description": "Default (explicit 'default')"},
    {"name": "mgmt", "vrf": MGMT_VRF_NAME, "description": "Management VRF"},
    {"name": "custom", "vrf": "Vrf-FOO", "description": "Custom VRF (Vrf-FOO)"}
]
DEFAULT_SNMP_PORT = 161


@pytest.fixture(scope="module", params=VRF_SCENARIOS, ids=lambda scenario: f"vrf_{scenario['name']}")
def vrf_config(request):
    return request.param


def configure_snmp_with_vrf(duthost, agent_ip, vrf_name):
    """
    Configures SNMP agent address with VRF.
    Misconfigured snmp agent address causes snmpd and snmp-subagent to fail
    during startup.
    While the GNMI tests do not depend directly on SNMP, some tests fail while
    waiting for all critical processes to be up and running.
    """
    output = duthost.shell(
        f'sudo sonic-db-cli CONFIG_DB KEYS "SNMP_AGENT_ADDRESS_CONFIG|{agent_ip}|*"'
    )
    output = output['stdout'].split("|")
    if len(output) < 4:
        duthost.shell(
            f'sonic-db-cli CONFIG_DB HSET '
            f'"SNMP_AGENT_ADDRESS_CONFIG|{agent_ip}|{DEFAULT_SNMP_PORT}|{vrf_name}" '
            f'"agent_ip" "{agent_ip}" "port" "{DEFAULT_SNMP_PORT}" "vrf_name" "{vrf_name}"'
        )
    else:
        port = output[2]
        duthost.shell(
            f'sonic-db-cli CONFIG_DB DEL '
            f'"SNMP_AGENT_ADDRESS_CONFIG|{agent_ip}|{port}|{output[3]}"'
        )
        duthost.shell(
            f'sonic-db-cli CONFIG_DB HSET '
            f'"SNMP_AGENT_ADDRESS_CONFIG|{agent_ip}|{port}|{vrf_name}" '
            f'"agent_ip" "{agent_ip}" "port" "{port}" "vrf_name" "{vrf_name}"'
        )


@pytest.fixture(scope="module", autouse=True)
def setup_vrf_configuration(duthosts, rand_one_dut_hostname, vrf_config):
    """
    This fixture runs before setup_gnmi_server to ensure VRF config is in place.
    """
    duthost = duthosts[rand_one_dut_hostname]
    vrf_name = vrf_config.get("vrf")
    mgmt_vrf_enabled = is_mgmt_vrf_enabled(duthost)

    try:
        if vrf_name == MGMT_VRF_NAME and not mgmt_vrf_enabled:
            duthost.shell('sonic-db-cli CONFIG_DB hset "MGMT_VRF_CONFIG|vrf_global" "mgmtVrfEnabled" "true"')
            configure_snmp_with_vrf(duthost, duthost.mgmt_ip, vrf_name)
            configure_snmp_with_vrf(duthost, duthost.mgmt_ipv6, vrf_name)
        elif vrf_name and vrf_name not in {DEFAULT_VRF_NAME, MGMT_VRF_NAME}:
            duthost.shell(f'sonic-db-cli CONFIG_DB hset "VRF|{vrf_name}" "NULL" "NULL"')
        yield vrf_config

    finally:
        if vrf_name == MGMT_VRF_NAME and not mgmt_vrf_enabled:
            duthost.shell('sonic-db-cli CONFIG_DB hset "MGMT_VRF_CONFIG|vrf_global" "mgmtVrfEnabled" "false"')
            duthost.shell('sonic-db-cli CONFIG_DB hdel "MGMT_VRF_CONFIG|vrf_global" "mgmtVrfEnabled"')
            configure_snmp_with_vrf(duthost, duthost.mgmt_ip, "")
            configure_snmp_with_vrf(duthost, duthost.mgmt_ipv6, "")
        elif vrf_name and vrf_name not in {DEFAULT_VRF_NAME, MGMT_VRF_NAME}:
            duthost.shell(f'sonic-db-cli CONFIG_DB del "VRF|{vrf_name}"', module_ignore_errors=True)


@pytest.fixture(scope="function", autouse=True)
def skip_non_x86_platform(duthosts, rand_one_dut_hostname):
    """
    Skip the current test if DUT is not x86_64 platform.
    """
    duthost = duthosts[rand_one_dut_hostname]
    platform = duthost.facts["platform"]
    if 'x86_64' not in platform:
        pytest.skip("Test not supported for current platform. Skipping the test")


@pytest.fixture(scope="module", autouse=True)
def download_gnmi_client(duthosts, rand_one_dut_hostname, localhost):
    duthost = duthosts[rand_one_dut_hostname]
    for file in ["gnmi_cli", "gnmi_set", "gnmi_get", "gnoi_client"]:
        duthost.shell("docker cp %s:/usr/sbin/%s /tmp" % (gnmi_container(duthost), file))
        duthost.shell("chmod +x /tmp/%s" % file)
        ret = duthost.fetch(src="/tmp/%s" % file, dest=".")
        gnmi_bin = ret.get("dest", None)
        shutil.copyfile(gnmi_bin, "gnmi/%s" % file)
        localhost.shell("sudo chmod +x gnmi/%s" % file)


@pytest.fixture(scope="module", autouse=True)
def setup_gnmi_ntp_client_server(duthosts, rand_one_dut_hostname, ptfhost):
    """Auto-setup NTP for all gNMI tests using existing helper."""
    duthost = duthosts[rand_one_dut_hostname]

    if duthost.facts['platform'] == 'x86_64-kvm_x86_64-r0':
        logger.info("check_system_time_sync is skipped for this platform, so skip ntp setup")
        yield
        return

    if check_ntp_sync_status(duthost) is True:
        logger.info("DUT is already in sycn with NTP server, so skip ntp setup")
        yield
        return

    with setup_ntp_context(ptfhost, duthost, False):
        yield


def create_revoked_cert_and_crl(localhost, ptfhost):
    # Create client key
    local_command = "openssl genrsa -out gnmiclient.revoked.key 2048"
    localhost.shell(local_command)

    # Create client CSR
    local_command = "openssl req \
                        -new \
                        -key gnmiclient.revoked.key \
                        -subj '/CN=test.client.revoked.gnmi.sonic' \
                        -out gnmiclient.revoked.csr"
    localhost.shell(local_command)

    # Sign client certificate
    crl_url = "http://{}:1234/crl".format(ptfhost.mgmt_ip)
    create_ca_conf(crl_url, "crlext.cnf")
    local_command = "openssl x509 \
                        -req \
                        -in gnmiclient.revoked.csr \
                        -CA gnmiCA.pem \
                        -CAkey gnmiCA.key \
                        -CAcreateserial \
                        -out gnmiclient.revoked.crt \
                        -days 825 \
                        -sha256 \
                        -extensions req_ext -extfile crlext.cnf"
    localhost.shell(local_command)

    # create crl config file
    local_command = "rm -f gnmi/crl/index.txt"
    localhost.shell(local_command)
    local_command = "touch gnmi/crl/index.txt"
    localhost.shell(local_command)

    local_command = "rm -f gnmi/crl/sonic_crl_number"
    localhost.shell(local_command)
    local_command = "echo 00 > gnmi/crl/sonic_crl_number"
    localhost.shell(local_command)

    # revoke cert CRL
    local_command = "openssl ca \
                        -revoke gnmiclient.revoked.crt \
                        -keyfile gnmiCA.key \
                        -cert gnmiCA.pem \
                        -config gnmi/crl/crl.cnf"

    localhost.shell(local_command)

    # re-create CRL
    local_command = "openssl ca \
                        -gencrl \
                        -keyfile gnmiCA.key \
                        -cert gnmiCA.pem \
                        -out sonic.crl.pem \
                        -config gnmi/crl/crl.cnf"

    localhost.shell(local_command)

    # copy to PTF for test
    ptfhost.copy(src='gnmiclient.revoked.crt', dest='/root/')
    ptfhost.copy(src='gnmiclient.revoked.key', dest='/root/')
    ptfhost.copy(src='sonic.crl.pem', dest='/root/')
    ptfhost.copy(src='gnmi/crl/crl_server.py', dest='/root/')

    local_command = "rm \
                        crlext.cnf \
                        gnmi/crl/index.* \
                        gnmi/crl/sonic_crl_number.*"
    localhost.shell(local_command)


@pytest.fixture(scope="module", autouse=True)
def setup_gnmi_server(duthosts, rand_one_dut_hostname, localhost, ptfhost, vrf_config, setup_vrf_configuration):
    '''
    Create GNMI client certificates
    '''
    duthost = duthosts[rand_one_dut_hostname]

    # Check if GNMI is enabled on the device
    pyrequire(
        check_container_state(duthost, gnmi_container(duthost), should_be_running=True),
        "Test was not supported on devices which do not support GNMI!")

    # Create Root key
    local_command = "openssl genrsa -out gnmiCA.key 2048"
    localhost.shell(local_command)

    # Create Root cert
    local_command = "openssl req \
                        -x509 \
                        -new \
                        -nodes \
                        -key gnmiCA.key \
                        -sha256 \
                        -days 1825 \
                        -subj '/CN=test.gnmi.sonic' \
                        -out gnmiCA.pem"
    localhost.shell(local_command)

    # Create server key
    local_command = "openssl genrsa -out gnmiserver.key 2048"
    localhost.shell(local_command)

    # Create server CSR
    local_command = "openssl req \
                        -new \
                        -key gnmiserver.key \
                        -subj '/CN=test.server.gnmi.sonic' \
                        -out gnmiserver.csr"
    localhost.shell(local_command)

    # Sign server certificate
    create_ext_conf(duthost.mgmt_ip, "extfile.cnf")
    local_command = "openssl x509 \
                        -req \
                        -in gnmiserver.csr \
                        -CA gnmiCA.pem \
                        -CAkey gnmiCA.key \
                        -CAcreateserial \
                        -out gnmiserver.crt \
                        -days 825 \
                        -sha256 \
                        -extensions req_ext -extfile extfile.cnf"
    localhost.shell(local_command)

    # Create client key
    local_command = "openssl genrsa -out gnmiclient.key 2048"
    localhost.shell(local_command)

    # Create client CSR
    local_command = "openssl req \
                        -new \
                        -key gnmiclient.key \
                        -subj '/CN=test.client.gnmi.sonic' \
                        -out gnmiclient.csr"
    localhost.shell(local_command)

    # Sign client certificate
    local_command = "openssl x509 \
                        -req \
                        -in gnmiclient.csr \
                        -CA gnmiCA.pem \
                        -CAkey gnmiCA.key \
                        -CAcreateserial \
                        -out gnmiclient.crt \
                        -days 825 \
                        -sha256"
    localhost.shell(local_command)

    create_revoked_cert_and_crl(localhost, ptfhost)

    # Copy CA certificate, server certificate and client certificate over to the DUT
    duthost.copy(src='gnmiCA.pem', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiserver.crt', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiserver.key', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiclient.crt', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiclient.key', dest='/etc/sonic/telemetry/')
    # Copy CA certificate and client certificate over to the PTF
    ptfhost.copy(src='gnmiCA.pem', dest='/root/')
    ptfhost.copy(src='gnmiclient.crt', dest='/root/')
    ptfhost.copy(src='gnmiclient.key', dest='/root/')

    create_checkpoint(duthost, SETUP_ENV_CP)
    apply_cert_config(duthost, vrf_config.get("vrf"))

    yield
    # Delete all created certs
    local_command = "rm \
                        extfile.cnf \
                        gnmiCA.* \
                        gnmiserver.* \
                        gnmiclient.*"
    localhost.shell(local_command)

    # Rollback configuration
    rollback(duthost, SETUP_ENV_CP)
    # Save the configuration
    cmd = "config save -y"
    duthost.shell(cmd, module_ignore_errors=True)
    recover_cert_config(duthost)


@pytest.fixture(scope="module", autouse=True)
def setup_gnmi_rotated_server(duthosts, rand_one_dut_hostname, localhost, ptfhost):
    '''
    Create GNMI client certificates
    '''
    duthost = duthosts[rand_one_dut_hostname]

    # Check if GNMI is enabled on the device
    pyrequire(
        check_container_state(duthost, gnmi_container(duthost), should_be_running=True),
        "Test was not supported on devices which do not support GNMI!"
    )

    # Create Root key
    local_command = "openssl genrsa -out gnmiCA.key 2048"
    localhost.shell(local_command)

    # Create Root cert
    local_command = "openssl req \
                        -x509 \
                        -new \
                        -nodes \
                        -key gnmiCA.key \
                        -sha256 \
                        -days 1825 \
                        -subj '/CN=test.gnmi.sonic' \
                        -out gnmiCA.pem"
    localhost.shell(local_command)

    # Create server key
    local_command = "openssl genrsa -out gnmiserver.key 2048"
    localhost.shell(local_command)

    # Create server CSR
    local_command = "openssl req \
                        -new \
                        -key gnmiserver.key \
                        -subj '/CN=test.server.gnmi.sonic' \
                        -out gnmiserver.csr"
    localhost.shell(local_command)

    # Sign server certificate
    create_ext_conf(duthost.mgmt_ip, "extfile.cnf")
    local_command = "openssl x509 \
                        -req \
                        -in gnmiserver.csr \
                        -CA gnmiCA.pem \
                        -CAkey gnmiCA.key \
                        -CAcreateserial \
                        -out gnmiserver.crt \
                        -days 825 \
                        -sha256 \
                        -extensions req_ext -extfile extfile.cnf"
    localhost.shell(local_command)

    # Create client key
    local_command = "openssl genrsa -out gnmiclient.key 2048"
    localhost.shell(local_command)

    # Create client CSR
    local_command = "openssl req \
                        -new \
                        -key gnmiclient.key \
                        -subj '/CN=test.client.gnmi.sonic' \
                        -out gnmiclient.csr"
    localhost.shell(local_command)

    # Sign client certificate
    local_command = "openssl x509 \
                        -req \
                        -in gnmiclient.csr \
                        -CA gnmiCA.pem \
                        -CAkey gnmiCA.key \
                        -CAcreateserial \
                        -out gnmiclient.crt \
                        -days 825 \
                        -sha256"
    localhost.shell(local_command)

    create_revoked_cert_and_crl(localhost, ptfhost)

    # Copy CA certificate, server certificate and client certificate over to the DUT
    duthost.copy(src='gnmiCA.pem', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiserver.crt', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiserver.key', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiclient.crt', dest='/etc/sonic/telemetry/')
    duthost.copy(src='gnmiclient.key', dest='/etc/sonic/telemetry/')


@pytest.fixture(scope="module", autouse=True)
def check_dut_timestamp(duthosts, rand_one_dut_hostname, localhost):
    '''
    Check DUT time to detect NTP issue
    '''
    duthost = duthosts[rand_one_dut_hostname]
    # Seconds since 1970-01-01 00:00:00 UTC
    time_cmd = "date +%s"
    dut_res = duthost.shell(time_cmd, module_ignore_errors=True)
    local_res = localhost.shell(time_cmd, module_ignore_errors=True)
    local_time = int(local_res["stdout"])
    dut_time = int(dut_res["stdout"])
    logger.info("Local time %d, DUT time %d" % (local_time, dut_time))
    time_diff = local_time - dut_time
    if time_diff >= GNMI_SERVER_START_WAIT_TIME:
        logger.warning("DUT time is wrong (%d), please check NTP" % (-time_diff))


def compile_protos(proto_files, proto_root):
    """Compile all .proto files using grpc_tools.protoc."""
    for proto_file in proto_files:

        # Command arguments for protoc
        args = [
            "grpc_tools.protoc",
            f"--proto_path={proto_root}",  # Root directory for proto imports
            f"--python_out={proto_root}",     # Output for message classes
            f"--grpc_python_out={proto_root}",  # Output for gRPC stubs
            proto_file                     # Input .proto file
        ]

        print(f"Compiling: {proto_file}")
        ret_code = protoc.main(args)
        if ret_code != 0:
            raise Exception(f"Failed to compile {proto_file} with return code {ret_code}")


def cleanup_generated_files():
    """Remove all generated proto .py files."""
    generated_files = glob.glob("gnmi/protos/**/*.py")
    for file in generated_files:
        os.remove(file)


@pytest.fixture(scope="module", autouse=True)
def setup_and_cleanup_protos():
    """Compile proto files before running tests and remove them afterward."""
    PROTO_ROOT = "gnmi/protos"
    PROTO_FILES = ["gnmi/protos/gnoi/system/system.proto"]

    # Compile proto files into Python gRPC stubs
    compile_protos(PROTO_FILES, PROTO_ROOT)

    # Run tests, then clean up
    yield
    cleanup_generated_files()


@pytest.fixture(scope="function")
def grpc_channel(duthosts, rand_one_dut_hostname):
    """
    Fixture to set up a gRPC channel with secure credentials.
    """
    duthost = duthosts[rand_one_dut_hostname]

    # Get DUT gRPC server address and port
    ip = duthost.mgmt_ip
    env = GNMIEnvironment(duthost, GNMIEnvironment.GNMI_MODE)
    port = env.gnmi_port
    target = f"{ip}:{port}"

    # Load the TLS certificates
    with open("gnmiCA.pem", "rb") as f:
        root_certificates = f.read()
    with open("gnmiclient.crt", "rb") as f:
        client_certificate = f.read()
    with open("gnmiclient.key", "rb") as f:
        client_key = f.read()

    # Create SSL credentials
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root_certificates,
        private_key=client_key,
        certificate_chain=client_certificate,
    )

    # Create gRPC channel
    logging.info("Creating gRPC secure channel to %s", target)
    channel = grpc.secure_channel(target, credentials)

    try:
        grpc.channel_ready_future(channel).result(timeout=10)
        logging.info("gRPC channel is ready")
    except grpc.FutureTimeoutError as e:
        logging.error("Error: gRPC channel not ready: %s", e)
        pytest.fail("Failed to connect to gRPC server")

    yield channel

    # Close the channel
    channel.close()
