#!/usr/bin/env python

"""
Common utilities for PFC packet generation.
Shared between pfc_gen.py and pfc_gen_t2.py.
"""
import binascii
import logging
import logging.handlers


# Maximum number of processes to be created (upper limit)
MAX_PROCESS_NUM = 32


def build_pfc_packet(priority, pause_time, is_global=False):
    """
    Build PFC or Global Pause packet.

    Args:
        priority (int): PFC class enable bitmap (0-255) or -1 for global pause
        pause_time (int): Pause time in quanta (0-65535)
        is_global (bool): True for global pause frame, False for PFC frame

    Returns:
        bytes: The constructed packet

    The Ethernet Frame format for PFC packets:

    Destination MAC |   01:80:C2:00:00:01   |
                    -------------------------
    Source MAC      |      Station MAC      |
                    -------------------------
    Ethertype       |         0x8808        |
                    -------------------------
    OpCode          |         0x0101        |
                    -------------------------
    Class Enable V  | 0x00 E7...E0          |
                    -------------------------
    Time Class 0    |       0x0000          |
                    -------------------------
    Time Class 1    |       0x0000          |
                    -------------------------
    ...
                    -------------------------
    Time Class 7    |       0x0000          |
                    -------------------------

    The Ethernet Frame format for pause frames:

    Destination MAC |   01:80:C2:00:00:01   |
                    -------------------------
    Source MAC      |      Station MAC      |
                    -------------------------
    Ethertype       |        0x8808         |
                    -------------------------
    OpCode          |        0x0001         |
                    -------------------------
    pause time      |        0x0000         |
                    -------------------------
    """
    src_addr = b"\x00\x01\x02\x03\x04\x05"
    dst_addr = b"\x01\x80\xc2\x00\x00\x01"
    if is_global:
        opcode = b"\x00\x01"
    else:
        opcode = b"\x01\x01"
    ethertype = b"\x88\x08"

    packet = dst_addr + src_addr + ethertype + opcode
    if is_global:
        packet = packet + binascii.unhexlify(format(pause_time, '04x'))
    else:
        class_enable = priority
        class_enable_field = binascii.unhexlify(format(class_enable, '04x'))

        packet = packet + class_enable_field
        for p in range(0, 8):
            if (class_enable & (1 << p)):
                packet = packet + binascii.unhexlify(format(pause_time, '04x'))
            else:
                packet = packet + b"\x00\x00"

    return packet


def setup_logging(rsyslog_server, ident='pfc_gen: '):
    """
    Setup syslog handler for logging.

    Args:
        rsyslog_server (str): IP address of rsyslog server
        ident (str): Syslog identifier prefix

    Returns:
        logging.Logger: Configured logger instance
    """
    logger = logging.getLogger('MyLogger')
    logger.setLevel(logging.DEBUG)
    handler = logging.handlers.SysLogHandler(address=(rsyslog_server, 514))
    handler.ident = ident
    logger.addHandler(handler)
    return logger


def distribute_interfaces(interfaces):
    """
    Calculate optimal process count and distribute interfaces across processes.

    Maximizes parallelism by creating one process per interface (up to MAX_PROCESS_NUM).
    Distributes interfaces using round-robin allocation.

    Args:
        interfaces (list): List of interface names

    Returns:
        tuple: (interface_slices, num_processes)
            - interface_slices: List of interface slices (each slice is a list of interfaces)
            - num_processes: Number of processes created
    """
    num_interfaces = len(interfaces)
    num_processes = min(MAX_PROCESS_NUM, max(1, num_interfaces))

    interface_slices = [[] for i in range(num_processes)]
    for i in range(0, num_interfaces):
        interface_slices[i % num_processes].append(interfaces[i])

    # Filter out empty slices
    return [slice for slice in interface_slices if slice], num_processes


def validate_options(options):
    """
    Validate command-line options.

    Args:
        options: Parsed options object

    Raises:
        ValueError: If validation fails
    """
    if options.time > 65535 or options.time < 0:
        raise ValueError("Quanta is not valid. Need to be in range 0-65535.")

    if options.global_pf:
        if options.priority != -1:
            raise ValueError("'-p' option is not valid when sending global pause frames ('--global' / '-g')")
    elif options.priority > 255 or options.priority < 0:
        raise ValueError("Enable class bitmap is not valid. Need to be in range 0-255.")
