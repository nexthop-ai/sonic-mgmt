import pytest
import logging
import random

from tests.zmq.gnmi_zmq_utils import gnmi_set, enable_zmq_fixture, cleanup_zmq_fixture

logger = logging.getLogger(__name__)


pytestmark = [
    pytest.mark.disable_loganalyzer,
    pytest.mark.topology('any')
]


@pytest.fixture
def enable_zmq(duthost):
    """Fixture to enable ZMQ without management VRF."""
    initial_mgmt_vrf_enabled, subtype = enable_zmq_fixture(duthost, enable_mgmt_vrf=False)

    yield

<<<<<<< HEAD
    cleanup_zmq_fixture(duthost, initial_mgmt_vrf_enabled, subtype, enable_mgmt_vrf=False)
=======
    # revert change
    command = 'sonic-db-cli CONFIG_DB hdel "DEVICE_METADATA|localhost" subtype'
    result = duthost.shell(command, module_ignore_errors=True)
    logger.debug("revert subtype subtype: {}".format(result))
    save_reload_config(duthost)


def gnmi_set(duthost, ptfhost, delete_list, update_list, replace_list):
    ip = duthost.mgmt_ip
    port = 8080
    cmd = '/root/env-python3/bin/python /root/gnxi/gnmi_cli_py/py_gnmicli.py '
    cmd += '--timeout 30 --notls '
    cmd += '--notls '
    cmd += '-t %s -p %u ' % (ip, port)
    cmd += '-xo sonic-db '
    cmd += '-m set-update '
    xpath = ''
    xvalue = ''
    for path in delete_list:
        path = path.replace('sonic-db:', '')
        xpath += ' ' + path
        xvalue += ' ""'
    for update in update_list:
        update = update.replace('sonic-db:', '')
        result = update.rsplit(':', 1)
        xpath += ' ' + result[0]
        xvalue += ' ' + result[1]
    for replace in replace_list:
        replace = replace.replace('sonic-db:', '')
        result = replace.rsplit(':', 1)
        xpath += ' ' + result[0]
        if '#' in result[1]:
            xvalue += ' ""'
        else:
            xvalue += ' ' + result[1]
    cmd += '--xpath ' + xpath
    cmd += ' '
    cmd += '--value ' + xvalue
    output = ptfhost.shell(cmd, module_ignore_errors=True)
    error = "GRPC error\n"
    if error in output['stdout']:
        result = output['stdout'].split(error, 1)
        raise Exception("GRPC error:" + result[1])
    return
>>>>>>> upstream/master


def test_gnmi_zmq(duthosts,
                  rand_one_dut_hostname,
                  ptfhost,
                  enable_zmq):
    duthost = duthosts[rand_one_dut_hostname]

    command = 'ps -auxww | grep "/usr/sbin/telemetry -logtostderr --noTLS --port 8080"'
    gnmi_process = duthost.shell(command, module_ignore_errors=True)["stdout"]
    logger.debug("gnmi_process: {}".format(gnmi_process))

    file_name = "vnet.txt"
    vnet_key = "Vnet{}".format(random.randint(0, 1000))
    text = "{\"" + vnet_key + "\": {\"vni\": \"1000\", \"guid\": \"559c6ce8-26ab-4193-b946-ccc6e8f930b2\"}}"
    with open(file_name, 'w') as file:
        file.write(text)
    ptfhost.copy(src=file_name, dest='/root')
    # Add DASH_VNET_TABLE
    update_list = ["/sonic-db:APPL_DB/localhost/DASH_VNET_TABLE:@/root/%s" % (file_name)]
    gnmi_set(duthost, ptfhost, [], update_list, [])

    command = 'sonic-db-cli APPL_DB keys "*" | grep "DASH_VNET_TABLE:{}"'.format(vnet_key)
    appl_db_key = duthost.shell(command, module_ignore_errors=True)["stdout"]
    logger.debug("appl_db_key: {}".format(appl_db_key))
    assert appl_db_key == "DASH_VNET_TABLE:{}".format(vnet_key)
