# SNMPv3 Test Plan

# SNMPv3 Test Plan

## Overview

This document outlines the test plan for validating Simple Network Management Protocol version 3 (SNMPv3) functionality. The primary focus is to ensure the correct operation of basic SNMPv3 GET requests and to verify the system's ability to appropriately reject SNMPv3 requests with invalid credentials.

## Scope

The testing will cover the following aspects:

* Tests basic SNMPv3 GET operations
    * Verifies different security levels (authPriv, authNoPriv, noAuthNoPriv)
    * Checks basic system information retrieval
    * Validates expected successes and failures
* This test verifies that SNMP operations fail appropriately when:
    * Using an incorrect username
    * Using an incorrect authentication password
    * Using an incorrect privacy password
    * Using an incorrect authentication protocol
    * Using an incorrect privacy protocol

## Testbed

The test will run on any testbeds.

## Setup Configuration

This test requires no specific setup.

## Test Cases

1.  **test\_snmpv3\_basic\_get**
    * Tests basic SNMPv3 GET operations: This part ensures that when a valid SNMPv3 GET request is sent to the SONiC device, it responds with the requested information. It's checking the basic communication flow and the device's ability to process these requests.
    * Verifies different security levels (authPriv, authNoPriv, noAuthNoPriv): SNMPv3 offers different levels of security. This aspect of the test makes sure that the GET operation functions as expected across all the key security levels:
        * authPriv (Authentication and Privacy)
        * authNoPriv (Authentication only, no Privacy)
        * noAuthNoPriv (No Authentication, no Privacy)
    * Checks basic system information retrieval: This specifies what kind of information the tests will try to retrieve. It will likely focus on standard MIB (Management Information Base) objects that provide fundamental details about the system, such as:
        * System description (e.g., the device's model and software version).
        * System uptime (how long the device has been running).
        * System name (the configured hostname of the device). The tests will verify that the values returned for these standard OIDs (Object Identifiers) are as expected.
    * Validates expected successes and failures
2.  **test\_snmpv3\_invalid\_credentials**
    * This test suite ensures the SONiC device correctly rejects SNMPv3 requests when presented with incorrect or incompatible credentials.
    * Using an incorrect username
    * Using an incorrect authentication password
    * Using an incorrect privacy password
    * Using an incorrect authentication protocol
    * Using an incorrect privacy protocol

## Expected Results

All test cases should:

* Complete successfully without errors
* Handle error conditions gracefully
* Clean up configurations after completion

Test failures should provide:

* Clear error messages
* Relevant log information
