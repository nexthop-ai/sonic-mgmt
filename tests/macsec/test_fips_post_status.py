import logging

import pytest

from tests.common.helpers.assertions import pytest_assert


logger = logging.getLogger(__name__)


pytestmark = [
    # Run only when MACsec tests are enabled ("--enable_macsec"), on the same
    # topologies as the rest of the MACsec suite.
    pytest.mark.macsec_required,
    pytest.mark.topology("t0", "t2", "t0-sonic"),
]


@pytest.fixture(scope="module", autouse=True)
def macsec_mtu_adjustment():
    """Override MACsec MTU adjustment for this module.

    The generic MACsec tests use a module-level autouse fixture with the same
    name (defined in ``tests/macsec/conftest.py``) which depends on
    MACsec-specific topology (``ctrl_links``, neighbors, profiles, etc.).

    This FIPS/POST sanity check does not need to reconfigure MACsec links or
    touch neighbors, so we replace that fixture with a no-op to avoid
    unnecessary MACsec setup and per-profile parametrization.
    """

    yield


@pytest.fixture(scope="module", autouse=True)
def load_macsec_info():
    """Override the global MACsec "load_macsec_info" autouse fixture.

    The MACsec plugin registers a module-scope autouse fixture with this
    name that depends on ``macsec_profile`` and neighbor-derived control
    links. Overriding it here makes this module completely independent of
    MACsec profile parametrization and neighbor discovery, while still
    allowing us to gate execution on ``--enable_macsec`` via the
    ``macsec_required`` marker above.
    """

    yield


def test_macsec_fips_post_status(duthosts, enum_rand_one_per_hwsku_macsec_frontend_hostname):
    """Verify MACsec FIPS POST status via "show macsec --post-status".

    The test:
    * Checks image-level FIPS flag via ``sonic-installer get-fips``.
    * If FIPS is not enabled, it logs and returns without failing.
    * Runs ``show macsec --post-status`` and verifies that all reported
      modules have POST status marked as pass.
    """

    duthost = duthosts[enum_rand_one_per_hwsku_macsec_frontend_hostname]

    # Only validate MACsec POST when FIPS is enabled on the image.
    fips_status = duthost.shell("sudo sonic-installer get-fips")["stdout"]
    if "FIPS is enabled" not in fips_status:
        logger.info("FIPS is not enabled on this image; skipping MACsec POST status validation")
        return

    # Cross-check that the runtime FIPS flags are actually set. When
    # "sonic-installer get-fips" reports FIPS as enabled, we expect either
    # the kernel cmdline to contain "sonic_fips=1" or the user-space FIPS
    # enable file to contain "1". A mismatch here indicates a broken FIPS
    # setup rather than a MACsec-specific issue.
    fips_runtime = duthost.shell(
        'grep -q "sonic_fips=1" /proc/cmdline || grep -q "1" /etc/fips/fips_enable',
        module_ignore_errors=True,
    )
    pytest_assert(
        fips_runtime["rc"] == 0,
        "FIPS reported as enabled, but runtime FIPS flags are not set (checked "
        "/proc/cmdline and /etc/fips/fips_enable): {}".format(fips_runtime),
    )

    result = duthost.shell("show macsec --post-status")
    output = result.get("stdout", "")

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    pytest_assert(lines, "Empty output from 'show macsec --post-status'")

    # Ensure at least one module line is printed.
    has_module = any(line.startswith("Module") for line in lines)
    pytest_assert(
        has_module,
        "No module entries found in MACsec POST status output: {}".format(output),
    )

    # Orchagent writes the field as "post_state" in STATE_DB, which the CLI
    # renders as "Post_state". Restrict the check to that field name so we
    # only validate the actual FIPS MACsec POST state.
    status_lines = [line for line in lines if "Post_state" in line]
    pytest_assert(
        status_lines,
        "No Post_state lines found in MACsec POST status output: {}".format(output),
    )

    all_pass = all("pass" in line.lower() for line in status_lines)
    pytest_assert(
        all_pass,
        "MACsec POST status is not 'pass' for all modules. Output:\n{}".format(output),
    )
