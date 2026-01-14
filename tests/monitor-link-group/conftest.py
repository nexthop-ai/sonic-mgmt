"""
Pytest configuration for monitor-link tests.

Provides:
- Logging configuration
- Shared fixtures (handler, group)
"""

import logging

import pytest

from lib import TestHandler, DutHandler
from tests.common.helpers.intf_mgr import IntfMgr


def pytest_configure(config):  # noqa: ARG001
    """Configure logging for monitor-link tests."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    logging.getLogger('tests.monitor-link.lib').setLevel(logging.INFO)


@pytest.fixture(params=['ports', 'mixed'])
def handler(duthost, request):
    """
    Provide a TestHandler instance.

    Parameterized with mode:
        - 'ports': All interfaces are individual ports (all topologies)
        - 'mixed': 1 portchannel + rest as individual ports (requires >=4 available portchannels)

    Args:
        duthost: The sonic-mgmt duthost fixture.
        request: pytest request fixture for parameterization.

    Returns:
        TestHandler instance ready for use.
    """
    mode = request.param

    # Check portchannel availability for mixed mode
    if mode == 'mixed':
        intf_mgr = IntfMgr(duthost)
        available_pcs = len(intf_mgr.available_portchannels())
        if available_pcs < 4:
            pytest.skip(f"Insufficient portchannels for mixed mode (available: {available_pcs})")

    return TestHandler(DutHandler(duthost), mode=mode)


@pytest.fixture
def group(handler):
    """
    Create a monitor link group and clean up after test.

    Creates a group with:
    - 2 uplinks
    - 1 downlink
    - min_uplinks=1
    - startup_delay=10 seconds

    Yields:
        Group name ('test-group')

    Cleanup:
        Removes the group after test completes.
    """
    group_name = 'test-group'
    handler.create_group(
        group_name,
        num_uplinks=2,
        num_downlinks=1,
        min_uplinks=1,
        startup_delay=10
    )
    yield group_name

    # Cleanup - runs after test (even if test fails)
    try:
        handler.remove_group(group_name)
    except Exception:
        pass  # Group may already be removed by test
