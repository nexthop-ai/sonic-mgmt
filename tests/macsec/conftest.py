import pytest
import logging

from tests.common.macsec.macsec_helper import check_appl_db
from tests.common.macsec.macsec_config_helper import adjust_mtu
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "macsec_required: mark test as MACsec required to run")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("enable_macsec"):
        skip_macsec = pytest.mark.skip(reason="macsec test cases")
        for item in items:
            if "macsec_required" in item.keywords:
                item.add_marker(skip_macsec)


@pytest.fixture(scope="module")
def profile_name(macsec_profile):
    return macsec_profile['name']


@pytest.fixture(scope="module")
def default_priority(macsec_profile):
    return macsec_profile['priority']


@pytest.fixture(scope="module")
def cipher_suite(macsec_profile):
    return macsec_profile['cipher_suite']


@pytest.fixture(scope="module")
def primary_ckn(macsec_profile):
    return macsec_profile['primary_ckn']


@pytest.fixture(scope="module")
def primary_cak(macsec_profile):
    return macsec_profile['primary_cak']


@pytest.fixture(scope="module")
def policy(macsec_profile):
    return macsec_profile['policy']


@pytest.fixture(scope="module")
def send_sci(macsec_profile):
    return macsec_profile['send_sci']


@pytest.fixture(scope="module")
def rekey_period(macsec_profile):
    return macsec_profile['rekey_period']


@pytest.fixture(scope="module")
def wait_mka_establish(duthost, ctrl_links, policy, cipher_suite, send_sci):
    assert wait_until(300, 6, 12, check_appl_db, duthost, ctrl_links, policy, cipher_suite, send_sci)


@pytest.fixture(scope="module", autouse=True)
def macsec_mtu_adjustment(duthost, ctrl_links):
    """
    Automatically adjust MTU for MACsec-enabled interfaces

    This fixture:
    - Runs automatically for all tests in MACsec test modules
    - Reduces interface MTU by 32 bytes (MACsec overhead) during setup
    - Restores original MTU values during teardown

    Args:
        duthost: DUT host fixture
        ctrl_links: MACsec-enabled control links fixture

    Yields:
        dict: Original MTU values
    """
    logger.info("Starting MACsec MTU adjustment")
    # SETUP PHASE: Reduce MTU by 32 bytes
    original_mtus = adjust_mtu(duthost, ctrl_links, True, None)

    # Yield control to tests
    yield original_mtus

    # TEARDOWN PHASE: Restore original MTU values
    logger.info("Restoring original MTU values")
    adjust_mtu(duthost, ctrl_links, False, original_mtus)
