# Monitor Link Group Tests

Automated tests for the SONiC Monitor Link Group feature.

## Overview

Monitor Link Group allows configuring groups of uplinks and downlinks where the downlink state follows the uplink state. When uplinks go down (below `min_uplinks` threshold), downlinks are automatically brought down.

## Class Diagram

```mermaid
classDiagram
    class TestHandler {
        -DutHandler duthandler
        -GroupHandler groups
        -InterfaceManager intf_mgr
        -Verifier verifier
        +create_group(name, num_uplinks, num_downlinks)
        +remove_group(name)
        +add_uplink(name, num_uplinks)
        +remove_uplink(name, index)
        +add_downlink(name, num_downlinks)
        +remove_downlink(name, index) str
        +set_uplink_up(name, index)
        +set_uplink_down(name, index)
        +get_uplinks(name) List~str~
        +get_downlinks(name) List~str~
        +verify(name, expected_params)
        +verify_not_exists(name)
        +verify_link_up(interface)
    }

    class DutHandler {
        -duthost
        +run_cmd(cmd) str
        +apply_patch(patch)
        +get_port_status(interface) str
        +shutdown_port(interface)
        +startup_port(interface)
        +get_available_ports() List~str~
    }

    class GroupHandler {
        -groups Dict
        +create(name, uplinks, downlinks, config)
        +get(name) Dict
        +remove(name)
        +add_uplink(name, interface)
        +remove_uplink(name, interface)
        +add_downlink(name, interface)
        +remove_downlink(name, interface)
    }

    class InterfaceManager {
        -available List~str~
        -allocated Set~str~
        +allocate(count) List~str~
        +release(interface)
    }

    class Verifier {
        -ConfigDbVerifier configdb_verifier
        -StateDbVerifier statedb_verifier
        -MonitorLinkVerifier cli_verifier
        -LinkStateVerifier link_state_verifier
        +verify(name, expected_params)
        +verify_not_exists(name, timeout)
        +verify_link_state(interface, state)
    }

    class ConfigDbVerifier {
        +verify(name)
    }

    class StateDbVerifier {
        +verify(name, expected_params)
        +verify_not_exists(name)
    }

    class MonitorLinkVerifier {
        +verify(name, expected_params)
    }

    class LinkStateVerifier {
        +verify(name, expected_params)
        +verify_single_link(interface, state, timeout)
    }

    TestHandler --> DutHandler
    TestHandler --> GroupHandler
    TestHandler --> InterfaceManager
    TestHandler --> Verifier
    Verifier --> ConfigDbVerifier
    Verifier --> StateDbVerifier
    Verifier --> MonitorLinkVerifier
    Verifier --> LinkStateVerifier
    ConfigDbVerifier --> DutHandler
    StateDbVerifier --> DutHandler
    MonitorLinkVerifier --> DutHandler
    LinkStateVerifier --> DutHandler
```

## Fixtures

Defined in `conftest.py`:

### `handler`
Provides a `TestHandler` instance for interacting with the DUT.

```python
def test_example(handler):
    handler.create_group('my-group', num_uplinks=2, num_downlinks=1)
```

## File Structure

```
tests/monitor-link-group/
├── README.md                      # This file
├── conftest.py                    # Fixtures and pytest config
├── test_monitor_link.py    # Monitor Link tests
└── lib/
    ├── __init__.py
    ├── test_handler.py            # High-level test API
    ├── dut_handler.py             # DUT interaction
    └── verifiers/
        ├── __init__.py
        ├── cli.py                 # CLI output verification
        ├── statedb.py             # STATE_DB verification
        ├── configdb.py            # CONFIG_DB verification
        └── link_state.py          # Link operational state verification
```
