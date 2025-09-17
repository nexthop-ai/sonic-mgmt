"""
Route Programming Performance Benchmark Test

This test measures route programming performance in SONiC using the route_programming_benchmark.py
script and outputs structured results for metrics collection.

Usage:
  # Default 100K routes
  pytest tests/route/test_route_programming_benchmark.py::test_route_programming_performance -v

  # Custom route scale
  pytest --route_scale 1000 tests/route/test_route_programming_benchmark.py::test_route_programming_performance -v
  pytest --route_scale 50000 tests/route/test_route_programming_benchmark.py::test_route_programming_performance -v
  pytest --route_scale 200000 tests/route/test_route_programming_benchmark.py::test_route_programming_performance -v

Metrics Output:
  The test outputs structured metrics in JSON format that can be consumed by external tools
  for publishing to monitoring systems.

  Schema:
    Measurement: route_programming_performance
    Tags: dut, route_count, stage
    Fields: total_time, asic_db_time, hardware_time, fpmsyncd_time, orchagent_time
"""

import json
import logging
import pytest
from tests.common import config_reload
from tests.common.helpers.assertions import pytest_assert

logger = logging.getLogger(__name__)

pytestmark = [
    pytest.mark.topology("t0", "t1", "any"),
]

# Default test parameters
DEFAULT_ROUTE_COUNT = 100000
DEFAULT_PREFIX = "192.168.0.0/16"
DEFAULT_NEXTHOP = "10.0.0.1"


def output_structured_metrics(dut_name, route_count, benchmark_results, extra_tags=None):
    """Output structured metrics in JSON format for external consumption"""
    logger.info(f"Outputting structured metrics for {route_count} routes...")

    # Base tags for all metrics
    base_tags = {"dut": dut_name, "route_count": str(route_count), "module": "route_programming_benchmark"}

    # Add extra tags if provided
    if extra_tags:
        base_tags.update(extra_tags)

    metrics = []

    # Total time metric
    if benchmark_results.get("total_time"):
        total_tags = base_tags.copy()
        total_tags["stage"] = "total"
        metrics.append(
            {
                "measurement": "route_programming_performance",
                "tags": total_tags,
                "fields": {"value": benchmark_results["total_time"], "total_time": benchmark_results["total_time"]},
            }
        )
        logger.info(f"Added total time metric: {benchmark_results['total_time']}s")

    # ASIC DB to Hardware programming time (syncd)
    if benchmark_results.get("asic_db_to_hardware_time"):
        hardware_tags = base_tags.copy()
        hardware_tags["stage"] = "hardware"
        metrics.append(
            {
                "measurement": "route_programming_performance",
                "tags": hardware_tags,
                "fields": {
                    "value": benchmark_results["asic_db_to_hardware_time"],
                    "hardware_time": benchmark_results["asic_db_to_hardware_time"],
                },
            }
        )
        logger.info(f"Added hardware time metric: {benchmark_results['asic_db_to_hardware_time']}s")

    # FPMsyncd timing
    if benchmark_results.get("fpmsyncd_timing") and len(benchmark_results["fpmsyncd_timing"]) >= 3:
        fpmsyncd_time = benchmark_results["fpmsyncd_timing"][2]  # time_diff
        fpmsyncd_tags = base_tags.copy()
        fpmsyncd_tags["stage"] = "fpmsyncd"
        metrics.append(
            {
                "measurement": "route_programming_performance",
                "tags": fpmsyncd_tags,
                "fields": {"value": fpmsyncd_time, "fpmsyncd_time": fpmsyncd_time},
            }
        )
        logger.info(f"Added fpmsyncd time metric: {fpmsyncd_time}s")

    # Orchagent timing
    if benchmark_results.get("orchagent_timing") and len(benchmark_results["orchagent_timing"]) >= 3:
        orchagent_time = benchmark_results["orchagent_timing"][2]  # time_diff
        orchagent_tags = base_tags.copy()
        orchagent_tags["stage"] = "orchagent"
        metrics.append(
            {
                "measurement": "route_programming_performance",
                "tags": orchagent_tags,
                "fields": {"value": orchagent_time, "orchagent_time": orchagent_time},
            }
        )
        logger.info(f"Added orchagent time metric: {orchagent_time}s")

    # Output metrics in a structured format that can be parsed by external tools
    metrics_output = {"test_name": "route_programming_benchmark", "metrics": metrics, "raw_results": benchmark_results}

    # Output as JSON with a special marker for easy parsing
    logger.warning("=== NEXTHOP_METRICS_START ===")
    logger.warning(json.dumps(metrics_output, indent=2))
    logger.warning("=== NEXTHOP_METRICS_END ===")

    # Also print to stdout for external parsing
    print("=== NEXTHOP_METRICS_START ===")
    print(json.dumps(metrics_output, indent=2))
    print("=== NEXTHOP_METRICS_END ===")

    logger.info(f"✓ Successfully output {len(metrics)} structured metrics for {route_count} routes")


def publish_metrics(dut_name, route_count, benchmark_results, extra_tags=None):
    """Output structured metrics in JSON format for external consumption"""
    output_structured_metrics(dut_name, route_count, benchmark_results, extra_tags)
    # TODO: Integrate with InfluxDB here, so that in addition to outputting metrics
    # in JSON format on console, we also publish to InfluxDB


@pytest.fixture(scope="function", autouse=True)
def restore_dut(duthosts, enum_rand_one_per_hwsku_frontend_hostname, request):
    """Restore DUT configuration after test to clean up routes"""
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    yield
    if request.node.rep_call.failed:
        # Issue a config_reload to clear statically added route table
        logging.info("Restoring config after test failure...")
        config_reload(duthost)


def cleanup_old_benchmark_files(duthost):
    """Clean up any old benchmark result files from previous test runs"""
    logger.info("Cleaning up old benchmark result files...")

    # Clean up files in both /home/admin and /tmp directories
    for directory in ["/home/admin", "/tmp"]:
        cleanup_result = duthost.shell(f"rm -f {directory}/route_benchmark_*.json", module_ignore_errors=True)
        if cleanup_result["rc"] == 0:
            if cleanup_result.get("stdout"):
                logger.info(f"Cleaned up old files in {directory}")
        else:
            logger.debug(f"No old files to clean up in {directory} (or cleanup failed)")


def run_benchmark_script(duthost, route_count, prefix=DEFAULT_PREFIX, nexthop=DEFAULT_NEXTHOP):
    """
    Run the route programming benchmark script on the DUT

    Args:
        duthost: DUT host object
        route_count: Number of routes to program
        prefix: Base prefix for route generation
        nexthop: Nexthop IP address

    Returns:
        dict: Benchmark results
    """
    # Clean up any old benchmark files first
    cleanup_old_benchmark_files(duthost)

    # Copy the benchmark script to the DUT
    script_path = "/tmp/route_programming_benchmark.py"
    local_script = "scripts/route_programming_benchmark.py"

    # Copy script to DUT
    duthost.copy(src=local_script, dest=script_path)

    # Make script executable
    duthost.shell(f"chmod +x {script_path}")

    # Run the benchmark from the admin home directory to ensure results file is saved there
    cmd = f"cd /home/admin && python3 {script_path} --routes {route_count} --prefix {prefix} --nexthop {nexthop}"
    logger.info(f"Running benchmark: {cmd}")

    result = duthost.shell(cmd, module_ignore_errors=True)

    # Log the benchmark script output for debugging
    logger.info(f"Benchmark script stdout: {result.get('stdout', 'No stdout')}")
    if result.get("stderr"):
        logger.warning(f"Benchmark script stderr: {result['stderr']}")

    if result["rc"] != 0:
        pytest.fail(f"Benchmark script failed with rc={result['rc']}: {result.get('stderr', 'No stderr')}")

    # Parse the JSON output file
    # The script saves results to a timestamped JSON file in the current directory (/home/admin)
    # First, let's check if any benchmark files exist in /home/admin
    find_result = duthost.shell("find /home/admin -name 'route_benchmark_*.json' -type f")

    # Check if find command succeeded
    if find_result["rc"] != 0:
        pytest.fail(f"Find command failed with rc={find_result['rc']}: {find_result.get('stderr', 'No stderr')}")

    # Check if we found any files
    if not find_result["stdout"].strip():
        # Try looking in /tmp directory as fallback
        find_result_tmp = duthost.shell("find /tmp -name 'route_benchmark_*.json' -type f")

        if find_result_tmp["rc"] == 0 and find_result_tmp["stdout"].strip():
            find_result = find_result_tmp
        else:
            # Debug: List all files in both directories to see what's there
            admin_files = duthost.shell(
                "ls -la /home/admin/route_benchmark_*.json 2>/dev/null || echo 'No files found in /home/admin'"
            )
            tmp_files = duthost.shell("ls -la /tmp/route_benchmark_*.json 2>/dev/null || echo 'No files found in /tmp'")
            logger.error(f"Debug - Admin directory: {admin_files.get('stdout', 'No output')}")
            logger.error(f"Debug - Tmp directory: {tmp_files.get('stdout', 'No output')}")

            # Also check what files were actually created
            all_admin_files = duthost.shell("ls -la /home/admin/")
            all_tmp_files = duthost.shell("ls -la /tmp/ | grep route")
            logger.error(f"All admin files: {all_admin_files.get('stdout', 'No output')}")
            logger.error(f"All tmp route files: {all_tmp_files.get('stdout', 'No output')}")

            pytest.fail("Could not find benchmark results file")

    # Get the most recent file (if multiple exist)
    files = [f.strip() for f in find_result["stdout"].strip().split("\n") if f.strip()]
    if len(files) > 1:
        # Get the most recent file by modification time
        get_newest_result = duthost.shell("ls -t " + " ".join(files) + " | head -1")
        if get_newest_result["rc"] == 0 and get_newest_result["stdout"].strip():
            results_file = get_newest_result["stdout"].strip()
        else:
            results_file = files[0]  # fallback to first file
    else:
        results_file = files[0]

    logger.info(f"Reading results from: {results_file}")

    # Read the results file
    cat_result = duthost.shell(f"cat {results_file}")
    if cat_result["rc"] != 0:
        pytest.fail(f"Could not read results file: {cat_result.get('stderr', 'No stderr')}")

    try:
        results = json.loads(cat_result["stdout"])
        logger.info(f"Benchmark results: {json.dumps(results, indent=2)}")

        # Clean up the results file after successful parsing
        cleanup_result = duthost.shell(f"rm -f {results_file}")
        if cleanup_result["rc"] == 0:
            logger.info(f"Successfully cleaned up results file: {results_file}")
        else:
            logger.warning(
                f"Failed to clean up results file {results_file}: {cleanup_result.get('stderr', 'No stderr')}"
            )

        return results
    except json.JSONDecodeError as e:
        logger.error(f"Raw results file content: {cat_result['stdout']}")
        pytest.fail(f"Could not parse benchmark results JSON: {e}")


def test_route_programming_performance(duthosts, enum_rand_one_per_hwsku_frontend_hostname, request):
    """
    Test route programming performance at configurable scale

    This test uses the route_programming_benchmark.py script to measure
    route programming performance through the SONiC pipeline and publishes
    the results to InfluxDB for tracking over time.

    Default: 100,000 routes
    Usage:
      pytest tests/route/test_route_programming_benchmark.py::test_route_programming_performance -v
      pytest --route_scale 50000 tests/route/test_route_programming_benchmark.py::test_route_programming_performance -v
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    dut_name = duthost.hostname

    # Get route scale from command line argument (default: 100000)
    route_scale = request.config.getoption("--route_scale")

    logger.info(f"Starting route programming benchmark for {route_scale} routes on {dut_name}")

    # Run the benchmark
    results = run_benchmark_script(duthost, route_scale)

    # Validate results
    pytest_assert(
        results.get("total_routes") == route_scale, f"Expected {route_scale} routes, got {results.get('total_routes')}"
    )

    pytest_assert(
        results.get("total_time") is not None and results.get("total_time") > 0, "Total time should be positive"
    )

    # Log key metrics
    logger.info("Route programming completed:")
    logger.info(f"  Routes: {results.get('total_routes', 'N/A')}")
    logger.info(f"  Total time: {results.get('total_time', 'N/A')}s")

    if results.get("asic_db_to_hardware_time"):
        logger.info(f"  ASIC DB → Hardware (syncd): {results['asic_db_to_hardware_time']}s")

    if results.get("fpmsyncd_timing") and len(results["fpmsyncd_timing"]) >= 3:
        logger.info(f"  FPMsyncd processing: {results['fpmsyncd_timing'][2]}s")

    if results.get("orchagent_timing") and len(results["orchagent_timing"]) >= 3:
        logger.info(f"  Orchagent processing: {results['orchagent_timing'][2]}s")

    # Publish metrics
    publish_metrics(dut_name, route_scale, results)

    logger.info(f"Route programming benchmark completed successfully for {route_scale} routes")
