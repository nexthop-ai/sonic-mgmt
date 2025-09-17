"""
This test checks secure upgrade feature. If we have a secure system with secured image installed
on it, the system is expected to install only secured images on it. So trying to install non-secure image
will cause fail and a print of failure message to console indicating it is not a secured image.
This test case validates the error flow mentioned above.

You can optionally specify the following argument:

    --target_image_list (to contain one non-secure image path e.g. /tmp/images/my_non_secure_img.bin)

If --target_image_list is not provided, the test will automatically select an unsigned upstream SONiC image
appropriate for the DUT platform and attempt to install it (expecting signature verification to fail).

Example run from tests directory:
    "pytest platform_tests/test_secure_upgrade.py <regular arguments> [--target_image_list non_secure_image.bin]"
"""
import logging
import pytest
import re
import json
from urllib.request import urlopen, Request
from tests.common.errors import RunAnsibleModuleFail
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.upgrade_helpers import install_sonic

pytestmark = [
    pytest.mark.topology('any'),
    pytest.mark.disable_loganalyzer,
]

logger = logging.getLogger(__name__)


# Helper to fetch JSON from Azure DevOps with context-aware error handling
def _fetch_json(url: str, operation_description: str) -> dict:
    try:
        req = Request(url)
        with urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        pytest.fail(
            f"Failed {operation_description}: {e}. "
            "Provide --target_image_list.")


@pytest.fixture(scope='function', autouse=True)
def keep_same_version_installed(duthost):
    '''
    @summary: extract the current version installed as shown in the "show boot" output
    and restore original image installed after the test run
    :param duthost: device under test
    '''
    output = duthost.shell("show boot")['stdout']
    results = re.findall(r"Current\s*\:\s*(.*)\n", output)
    pytest_assert(len(results) > 0, "Current image is empty!")
    current_version = results[0]
    yield
    duthost.shell("sudo sonic-installer set-default {}".format(current_version))


@pytest.fixture(scope='session')
def non_secure_image_path(request, duthost) -> str:
    '''
    @summary: Determine the non-secure image to install. If --target_image_list is provided,
    use it. Otherwise, auto-select an unsigned upstream SONiC image URL based on the DUT ASIC/platform.
    :return: non-secure image path or URL (string)
    '''
    # If provided explicitly, use it
    non_secure_img_path = request.config.getoption('target_image_list')
    if non_secure_img_path:
        return str(non_secure_img_path)

    # Auto-select an unsigned upstream SONiC image URL from Azure DevOps official pipelines
    platform = duthost.command("sonic-cfggen -y /etc/sonic/sonic_version.yml -v asic_type")['stdout'].strip()
    asic_subtype = duthost.command("sonic-cfggen -y /etc/sonic/sonic_version.yml -v asic_subtype")['stdout'].strip()

    machine_conf_content = duthost.shell("cat /host/machine.conf")['stdout']
    img_name_prefix = "sonic"
    img_name_suffix = ".bin"
    for line in machine_conf_content.splitlines():
        if line.startswith("aboot"):
            img_name_prefix = "sonic-aboot"
            img_name_suffix = ".swi"
            break

    pipeline_name = f"Azure.sonic-buildimage.official.{platform}"
    image_file = f"{img_name_prefix}-{asic_subtype}{img_name_suffix}"

    logger.info(f"pipeline name is {pipeline_name}")
    logger.info(f"image file is {image_file}")

    # Resolve pipeline (definition) ID from its name
    defs_url = (
        "https://dev.azure.com/mssonic/build/_apis/build/definitions?"
        f"name={pipeline_name}&api-version=6.0"
    )
    defs_json = _fetch_json(defs_url, "resolving pipeline ID")
    definitions = defs_json.get("value", [])
    if not definitions:
        pytest.fail(f"Cannot find Azure DevOps pipeline '{pipeline_name}'. Provide --target_image_list.")
    pipeline_id = definitions[0].get("id")
    if not pipeline_id:
        pytest.fail(f"Invalid pipeline definition for '{pipeline_name}'. Provide --target_image_list.")

    logger.info(f"pipeline id is {pipeline_id}")

    # Find latest successful build on master for that pipeline
    builds_url = (
        "https://dev.azure.com/mssonic/build/_apis/build/builds?"
        f"definitions={pipeline_id}&branchName=refs/heads/master&resultFilter=succeeded&"
        "statusFilter=completed&api-version=6.0"
    )
    builds_json = _fetch_json(builds_url, "querying latest successful builds")
    builds = builds_json.get("value", [])
    if not builds:
        pytest.fail(
            f"No successful 'master' builds found for pipeline '{pipeline_name}'. Provide --target_image_list.")
    build_id = builds[0].get("id")
    if not build_id:
        pytest.fail(f"Malformed build entry for pipeline '{pipeline_name}'. Provide --target_image_list.")

    logger.info(f"build id is {build_id}")

    # Get artifact download URL for the platform
    artifact_name = f"sonic-buildimage.{platform}"
    art_url = (
        "https://dev.azure.com/mssonic/build/_apis/build/builds/"
        f"{build_id}/artifacts?artifactName={artifact_name}&api-version=5.0"
    )
    art_json = _fetch_json(art_url, f"fetching artifact URL for '{artifact_name}' in build {build_id}")
    download_url = art_json["resource"]["downloadUrl"]

    # Convert artifact zip URL to direct file URL for the image and return it
    file_url = download_url.replace("zip", "file") + f"&subPath=%2Ftarget%2F{image_file}"

    logger.info(f"Auto-selected unsigned upstream image URL for test: {file_url}")
    return file_url


def test_non_secure_boot_upgrade_failure(duthost, non_secure_image_path, tbinfo):
    """
    @summary: This test case validates non successful upgrade of a given non secure image
    """
    # Preliminary check: ensure UEFI Secure Boot is enabled on the DUT
    sb_state = duthost.shell("mokutil --sb-state", module_ignore_errors=True)
    sb_out = (sb_state.get('stdout') or '').lower()
    if 'enabled' not in sb_out:
        pytest.skip("Secure Boot is not enabled on the DUT (mokutil --sb-state)")

    secure_boot_image = duthost.command("sonic-cfggen  -y /etc/sonic/sonic_version.yml -v secure_boot_image")['stdout']

    if secure_boot_image != 'yes':
        pytest.skip("Current Image is not secured so skipping")

    # install non secure image
    logger.info("install non secure image - expect fail, image path = {}".format(non_secure_image_path))
    result = "image install failure"  # because we expect fail
    try:
        # in case of success result will take the target image name
        result = install_sonic(duthost, non_secure_image_path, tbinfo)
    except RunAnsibleModuleFail as err:
        err_msg = str(err.results._check_key("msg"))
        logger.info("Expected fail, err msg is : {}".format(err_msg))
        pytest_assert(
            "Failure: CMS signature Verification Failed" in str(err_msg),
            "failure was not due to security limitations")
    finally:
        logger.info("install_sonic returned: {}".format(result))
        pytest_assert(result == "image install failure", "non-secure image was successfully installed")
