#!/bin/bash
# Copyright 2026 Nexthop Systems Inc. All rights reserved.
#
# This software is proprietary and confidential. Unauthorized use, distribution,
# or modification is strictly prohibited.

# Runs sonic-mgmt's offline unit tests, the ones that need no testbed.
# CI runs this inside docker-sonic-mgmt with no network (build-and-test.yml);
# to run it locally, see the "Run unit tests" step there.
# Writes one JUnit XML per pytest run into unit-test-results/.
#
# Each run is grouped by the pytest config it needs: pytest takes its rootdir
# and ini file from the common ancestor of its paths, so mixing tests/ with
# other top-level dirs would swap tests/pytest.ini for pyproject.toml.

set -euo pipefail
cd "$(dirname "$0")/../.."
results="$PWD/unit-test-results"
rm -rf "$results"
mkdir -p "$results"
failed=()

run() {
    local name=$1 dir=$2
    shift 2
    echo "::group::$name"
    # tests/pytest.ini's log formats use a field only tests/conftest.py adds.
    (cd "$dir" && python3 -m pytest -p no:cacheprovider --color=no -rfE \
        -o 'log_format=%(message)s' -o 'log_file_format=%(message)s' \
        -o 'log_cli_format=%(message)s' \
        --junitxml="$results/$name.xml" "$@") || failed+=("$name")
    echo "::endgroup::"
}

# --noconftest keeps tests/conftest.py and its testbed imports out, but skips
# every other conftest.py too: a folder that needs one goes in its own run.
# unittest_*.py is not in pytest.ini's python_files, so it is named directly.
run tests . --noconftest \
    tests/common/unit_tests \
    tests/packet_trimming/unit_tests \
    tests/stp/unit_tests \
    tests/redfish/unit_tests \
    tests/radius/unit_tests \
    tests/generic_config_updater/unit_tests \
    tests/common/plugins/memory_utilization/unit_test \
    tests/common/plugins/conditional_mark/unit_test/unittest_find_all_matches.py \
    tests/snappi_tests/unit_tests/pfc

# No conftest.py here today; --noconftest would skip one added later, as above.
# A glob that matches nothing stays literal, so pytest fails the run. Don't set
# nullglob: a moved folder would then drop out silently.
run tools-and-ansible . --noconftest \
    ansible/roles/vm_set/tests \
    ansible/roles/sonic/templates/tests \
    ansible/scripts/meta/validators/tests/unit_test_console_validator.py \
    ansible/module_utils/test_sonic_release_utils.py \
    ansible/library/test_upgrade_dpu_sonic_image.py \
    tools/skip_expiry/tests/unit_test_*.py \
    tools/common2_migration/tests/unit_test_*.py \
    .azure-pipelines/test_plan_test.py \
    .azure-pipelines/impacted_area_testing/test_get_test_scripts.py

# These two have their own conftest.py (and saitests its own pytest.ini).
run saitests-mock-it tests/saitests/mock/it .
run gobgp . --confcutdir=ansible/roles/vm_set/files/gobgp/tests \
    ansible/roles/vm_set/files/gobgp/tests

if ((${#failed[@]})); then
    echo "::error::Unit test runs failed: ${failed[*]}"
    exit 1
fi
