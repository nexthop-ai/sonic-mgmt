"""
Tests for Monitor Link functionality.

Fixtures:
    handler: TestHandler instance for interacting with DUT
    group: Pre-created group named 'test-group' with:
        - 2 uplinks (index 0, 1)
        - 1 downlink (index 0)
        - min_uplinks=1
        - startup_delay=1
        - Automatically cleaned up after each test
"""

import pytest

pytestmark = [
    pytest.mark.topology('any', 't0-sonic', 't1-multi-asic'),
    pytest.mark.device_type('vs')
]


# =============================================================================
# GROUP LIFECYCLE TESTS
# =============================================================================


class TestGroupLifecycle:
    """Tests for group creation and removal."""

    def test_create_group(self, handler):
        """Test creating a monitor link group."""
        handler.create_group('lifecycle-group', num_uplinks=2, num_downlinks=1)
        handler.verify('lifecycle-group', {'state': 'up'})
        handler.remove_group('lifecycle-group')

    def test_create_group_with_description(self, handler):
        """Test creating a group with description."""
        handler.create_group(
            'lifecycle-group',
            num_uplinks=2,
            num_downlinks=1,
            description='Test group'
        )
        handler.verify('lifecycle-group', {'state': 'up'})
        handler.remove_group('lifecycle-group')

    def test_remove_group(self, handler):
        """Test removing a monitor link group."""
        handler.create_group('lifecycle-group', num_uplinks=2, num_downlinks=1)
        handler.remove_group('lifecycle-group')
        handler.verify_not_exists('lifecycle-group')

    def test_create_group_with_down_uplinks(self, handler):
        """Group state is down when created with all uplinks already down."""
        handler.create_group(
            'lifecycle-group',
            num_uplinks=2,
            num_downlinks=1,
            min_uplinks=1,
            uplinks_down=True
        )
        handler.verify('lifecycle-group', {
            'state': 'down',
            'all_uplinks': 'down',
            'all_downlinks': 'down'
        })
        handler.remove_group('lifecycle-group')


# =============================================================================
# UPLINK TOGGLE TESTS
# =============================================================================


class TestUplinkToggle:
    """Tests for toggling uplink states."""

    def test_single_uplink_down_group_stays_up(self, handler, group):
        """Group stays up when one uplink goes down (min_uplinks=1)."""
        handler.set_uplink_down(group, 0)
        handler.verify(group, {
            'state': 'up',
            'uplink_status': {0: 'down', 1: 'up'}
        })

    def test_all_uplinks_down_group_goes_down(self, handler, group):
        """Group goes down when all uplinks are down."""
        handler.set_uplink_down(group, 0)
        handler.set_uplink_down(group, 1)
        handler.verify(group, {
            'state': 'down',
            'all_uplinks': 'down',
            'all_downlinks': 'down'
        })

    def test_uplink_recovery(self, handler, group):
        """Group recovers when uplink comes back up."""
        # Bring all uplinks down
        handler.set_uplink_down(group, 0)
        handler.set_uplink_down(group, 1)
        handler.verify(group, {'state': 'down'})

        # Bring one uplink back up
        handler.set_uplink_up(group, 0)
        handler.verify(group, {
            'state': 'up',
            'uplink_status': {0: 'up', 1: 'down'}
        })


# =============================================================================
# DOWNLINK TOGGLE TESTS
# =============================================================================


class TestDownlinkToggle:
    """Tests for toggling downlink states."""

    def test_admin_down_downlink_stays_down_when_group_up(self, handler, group):
        """
        Downlink stays down when administratively shutdown, even if group is up.

        Administrative shutdown should take precedence over monitor-link group state.
        """
        # Verify group is up and downlink is up
        handler.verify(group, {'state': 'up', 'all_downlinks': 'up'})

        # Administratively shutdown the downlink
        handler.set_downlink_down(group, 0)

        # Downlink should be down even though group is still up
        handler.verify(group, {
            'state': 'up',
            'downlink_status': {0: 'down'}
        })

    def test_admin_down_downlink_stays_down_through_group_transitions(self, handler, group):
        """
        Admin-down downlink stays down through group up/down transitions.

        When a downlink is administratively shutdown, it should remain down
        regardless of group state changes.
        """
        # Administratively shutdown the downlink while group is up
        handler.set_downlink_down(group, 0)
        handler.verify(group, {'state': 'up', 'downlink_status': {0: 'down'}})

        # Bring group down (all uplinks down)
        handler.set_uplink_down(group, 0)
        handler.set_uplink_down(group, 1)
        handler.verify(group, {'state': 'down', 'downlink_status': {0: 'down'}})

        # Bring group back up
        handler.set_uplink_up(group, 0)

        # Downlink should still be down (admin state takes precedence)
        handler.verify(group, {
            'state': 'up',
            'downlink_status': {0: 'down'}
        })

    def test_admin_up_downlink_restores_when_group_up(self, handler, group):
        """
        Downlink comes back up when admin-enabled and group is up.
        """
        # Admin shutdown downlink
        handler.set_downlink_down(group, 0)
        handler.verify(group, {'state': 'up', 'downlink_status': {0: 'down'}})

        # Admin enable downlink - should come back up since group is up
        handler.set_downlink_up(group, 0)
        handler.verify(group, {
            'state': 'up',
            'downlink_status': {0: 'up'}
        })


# =============================================================================
# ADD/REMOVE UPLINK/DOWNLINK TESTS
# =============================================================================


class TestLinkManagement:
    """Tests for adding and removing uplinks/downlinks."""

    def test_add_uplink(self, handler, group):
        """Test adding an uplink to a group."""
        handler.add_uplink(group, num_uplinks=1)
        handler.verify(group, {
            'state': 'up',
            'uplink_status': {0: 'up', 1: 'up', 2: 'up'}
        })

    def test_remove_uplink(self, handler, group):
        """Test removing an uplink from a group."""
        handler.remove_uplink(group, index=0)
        handler.verify(group, {
            'state': 'up',
            'uplink_status': {0: 'up'}  # Only one uplink left
        })

    def test_add_downlink(self, handler, group):
        """Test adding a downlink to a group."""
        handler.add_downlink(group, num_downlinks=1)
        handler.verify(group, {
            'state': 'up',
            'downlink_status': {0: 'up', 1: 'up'}
        })

    def test_remove_downlink(self, handler, group):
        """Test removing a downlink from a group."""
        # First add a downlink so we have 2
        handler.add_downlink(group, num_downlinks=1)
        handler.verify(group, {'downlink_status': {0: 'up', 1: 'up'}})

        # Remove one
        handler.remove_downlink(group, index=0)
        handler.verify(group, {
            'state': 'up',
            'downlink_status': {0: 'up'}  # Only one downlink left
        })

    def test_remove_downlink_from_down_group_restores_link(self, handler, group):
        """
        Test that removing a downlink from a down group restores that link.

        When a group is down, all downlinks are held down. Removing a downlink
        from the group should release it, allowing it to come back up.
        """
        # Add an extra downlink so we have 2
        handler.add_downlink(group, num_downlinks=1)
        handler.verify(group, {'downlink_status': {0: 'up', 1: 'up'}})

        # Bring all uplinks down - group and downlinks go down
        handler.set_uplink_down(group, 0)
        handler.set_uplink_down(group, 1)
        handler.verify(group, {
            'state': 'down',
            'all_downlinks': 'down'
        })

        # Remove downlink at index 0 - it should come back up
        removed_link = handler.remove_downlink(group, index=0)
        handler.verify_link_up(removed_link)

        # Remaining downlink in group should still be down
        handler.verify(group, {
            'state': 'down',
            'downlink_status': {0: 'down'}
        })

    def test_remove_down_group_restores_all_downlinks(self, handler):
        """
        Test that removing a down group restores all downlink interfaces.

        When a group is down, all downlinks are held down. Removing the entire
        group should release all downlinks, allowing them to come back up.
        """
        # Create group with 2 downlinks
        handler.create_group(
            'down-group',
            num_uplinks=2,
            num_downlinks=2,
            min_uplinks=1,
            startup_delay=1
        )
        handler.verify('down-group', {
            'state': 'up',
            'downlink_status': {0: 'up', 1: 'up'}
        })

        # Get downlink interfaces before removing group
        downlinks = handler.get_downlinks('down-group')

        # Bring all uplinks down - group and downlinks go down
        handler.set_uplink_down('down-group', 0)
        handler.set_uplink_down('down-group', 1)
        handler.verify('down-group', {
            'state': 'down',
            'all_downlinks': 'down'
        })

        # Remove the group - all downlinks should come back up
        handler.remove_group('down-group')
        handler.verify_not_exists('down-group')

        # Verify all downlinks are back up
        for downlink in downlinks:
            handler.verify_link_up(downlink)


# =============================================================================
# SHARED LINKS TESTS
# =============================================================================


class TestSharedLinks:
    """Tests for multiple groups sharing the same links."""

    def test_three_groups_shared_uplinks(self, handler):
        """
        Test 3 groups sharing the same 2 uplinks.

        When shared uplinks go down, all groups should go down.
        When uplinks recover, all groups should recover.
        """
        # Allocate shared uplinks
        shared_uplinks = handler.intf_mgr.allocate(2)

        # Create 3 groups sharing the same uplinks
        handler.create_group(
            'shared-group-1',
            uplinks=shared_uplinks,
            num_downlinks=1,
            min_uplinks=1,
            startup_delay=1
        )
        handler.create_group(
            'shared-group-2',
            uplinks=shared_uplinks,
            num_downlinks=1,
            min_uplinks=1,
            startup_delay=1
        )
        handler.create_group(
            'shared-group-3',
            uplinks=shared_uplinks,
            num_downlinks=1,
            min_uplinks=1,
            startup_delay=1
        )

        # Verify all groups are up
        handler.verify('shared-group-1', {'state': 'up', 'all_uplinks': 'up'})
        handler.verify('shared-group-2', {'state': 'up', 'all_uplinks': 'up'})
        handler.verify('shared-group-3', {'state': 'up', 'all_uplinks': 'up'})

        # Bring all shared uplinks down
        handler.set_uplink_down('shared-group-1', 0)
        handler.set_uplink_down('shared-group-1', 1)

        # All groups should go down
        handler.verify('shared-group-1', {'state': 'down', 'all_downlinks': 'down'})
        handler.verify('shared-group-2', {'state': 'down', 'all_downlinks': 'down'})
        handler.verify('shared-group-3', {'state': 'down', 'all_downlinks': 'down'})

        # Bring one uplink back up
        handler.set_uplink_up('shared-group-1', 0)

        # All groups should recover
        handler.verify('shared-group-1', {'state': 'up', 'uplink_status': {0: 'up', 1: 'down'}})
        handler.verify('shared-group-2', {'state': 'up', 'uplink_status': {0: 'up', 1: 'down'}})
        handler.verify('shared-group-3', {'state': 'up', 'uplink_status': {0: 'up', 1: 'down'}})

        # Cleanup - remove groups (don't release shared uplinks until all groups removed)
        handler.remove_group('shared-group-1')
        handler.remove_group('shared-group-2')
        handler.remove_group('shared-group-3')

    def test_three_groups_shared_downlinks(self, handler):
        """
        Test 3 groups sharing the same 2 downlinks.

        When any group goes down, the shared downlinks should go down.
        Downlinks only come back up when ALL groups are up.
        """
        # Allocate shared downlinks
        shared_downlinks = handler.intf_mgr.allocate(2)

        # Create 3 groups sharing the same downlinks
        handler.create_group(
            'shared-group-1',
            num_uplinks=2,
            downlinks=shared_downlinks,
            min_uplinks=1,
            startup_delay=1
        )
        handler.create_group(
            'shared-group-2',
            num_uplinks=2,
            downlinks=shared_downlinks,
            min_uplinks=1,
            startup_delay=1
        )
        handler.create_group(
            'shared-group-3',
            num_uplinks=2,
            downlinks=shared_downlinks,
            min_uplinks=1,
            startup_delay=1
        )

        # Verify all groups are up and downlinks are up
        handler.verify('shared-group-1', {'state': 'up', 'all_downlinks': 'up'})
        handler.verify('shared-group-2', {'state': 'up', 'all_downlinks': 'up'})
        handler.verify('shared-group-3', {'state': 'up', 'all_downlinks': 'up'})

        # Bring group-1 down (all its uplinks down)
        handler.set_uplink_down('shared-group-1', 0)
        handler.set_uplink_down('shared-group-1', 1)

        # Group-1 is down, shared downlinks should be down
        handler.verify('shared-group-1', {'state': 'down', 'all_downlinks': 'down'})
        # Group-2 and Group-3 are still up but downlinks are held down by group-1
        handler.verify('shared-group-2', {'state': 'up', 'all_downlinks': 'down'})
        handler.verify('shared-group-3', {'state': 'up', 'all_downlinks': 'down'})

        # Bring group-1 back up
        handler.set_uplink_up('shared-group-1', 0)

        # All groups up, downlinks should be up
        handler.verify('shared-group-1', {'state': 'up', 'all_downlinks': 'up'})
        handler.verify('shared-group-2', {'state': 'up', 'all_downlinks': 'up'})
        handler.verify('shared-group-3', {'state': 'up', 'all_downlinks': 'up'})

        # Cleanup
        handler.remove_group('shared-group-1')
        handler.remove_group('shared-group-2')
        handler.remove_group('shared-group-3')

    def test_shared_downlinks_removal_restores_link(self, handler):
        """
        Test that removing a down group restores shared downlinks when remaining groups are up.

        Scenario:
        - 2 groups share the same downlinks
        - Group-1 goes down, causing shared downlinks to go down
        - Group-2 is still up but downlinks are held down by group-1
        - Removing group-1 releases the hold, downlinks come back up
        """
        # Allocate shared downlinks
        shared_downlinks = handler.intf_mgr.allocate(2)

        # Create 2 groups sharing the same downlinks
        handler.create_group(
            'shared-group-1',
            num_uplinks=2,
            downlinks=shared_downlinks,
            min_uplinks=1,
            startup_delay=1
        )
        handler.create_group(
            'shared-group-2',
            num_uplinks=2,
            downlinks=shared_downlinks,
            min_uplinks=1,
            startup_delay=1
        )

        # Verify both groups are up and downlinks are up
        handler.verify('shared-group-1', {'state': 'up', 'all_downlinks': 'up'})
        handler.verify('shared-group-2', {'state': 'up', 'all_downlinks': 'up'})

        # Bring group-1 down (all its uplinks down)
        handler.set_uplink_down('shared-group-1', 0)
        handler.set_uplink_down('shared-group-1', 1)

        # Group-1 is down, shared downlinks should be down
        handler.verify('shared-group-1', {'state': 'down', 'all_downlinks': 'down'})
        # Group-2 is still up but downlinks are held down by group-1
        handler.verify('shared-group-2', {'state': 'up', 'all_downlinks': 'down'})

        # Remove group-1 - this should release the hold on downlinks
        handler.remove_group('shared-group-1')
        handler.verify_not_exists('shared-group-1')

        # Group-2 is up and no longer blocked - downlinks should come back up
        handler.verify('shared-group-2', {'state': 'up', 'all_downlinks': 'up'})

        # Cleanup
        handler.remove_group('shared-group-2')
