#!/usr/bin/env python

"""
Script to generate PFC packets.

"""
import sys
import optparse
import logging
import logging.handlers
from socket import socket, AF_PACKET, SOCK_RAW
import time
import multiprocessing
from ctypes import (
    CDLL,
    POINTER,
    Structure,
    c_int,
    c_size_t,
    c_uint,
    c_uint32,
    c_void_p,
    cast,
    get_errno,
    pointer,
)

# Import common PFC utilities (will be bundled at deployment time)
from pfc_common import (
    build_pfc_packet,
    distribute_interfaces,
    validate_options,
    setup_logging
)


class struct_iovec(Structure):
    _fields_ = [
        ("iov_base", c_void_p),
        ("iov_len", c_size_t),
    ]


class struct_msghdr(Structure):
    _fields_ = [
        ("msg_name", c_void_p),
        ("msg_namelen", c_uint32),
        ("msg_iov", POINTER(struct_iovec)),
        ("msg_iovlen", c_size_t),
        ("msg_control", c_void_p),
        ("msg_controllen", c_size_t),
        ("msg_flags", c_int),
    ]


class struct_mmsghdr(Structure):
    _fields_ = [
        ("msg_hdr", struct_msghdr),
        ("msg_len", c_uint)
    ]


# cdll.LoadLibrary("libc.so.6")
libc = CDLL("libc.so.6")
_sendmmsg = libc.sendmmsg
_sendmmsg.argtypes = [c_int, POINTER(struct_mmsghdr), c_uint, c_int]
_sendmmsg.restype = c_int

my_logger = logging.getLogger('MyLogger')
my_logger.setLevel(logging.DEBUG)

fo_logger = logging.getLogger('MyLogger')
fo_logger.setLevel(logging.DEBUG)


class PacketSender():
    """
    A class to send PFC pause frames using sendmmsg
    """
    def __init__(self, interfaces, packet, num, sendtime, rsyslog_server):
        self.interfaces = interfaces
        self.packet = packet
        self.packet_num = num
        self.sendtime = sendtime
        self.rsyslog_server = rsyslog_server
        self.process = None

    def send_packets(self):
        """
        Send PFC packets on assigned interfaces
        """
        # Setup sockets for this process
        sockets = []
        fo_logger = logging.getLogger('MyLogger')
        fo_logger.setLevel(logging.DEBUG)
        fo_handler = logging.handlers.SysLogHandler()
        fo_logger.addHandler(fo_handler)

        try:
            for interface in self.interfaces:
                mysocket = socket(AF_PACKET, SOCK_RAW)
                mysocket.bind((interface, 0))
                mysocket.setsockopt(263, 20, 1)  # QDISC_BYPASS
                mysocket.setblocking(False)
                sockets.append(mysocket)
                fo_logger.debug("Socket bound : {}".format(mysocket.getsockname()))
        except Exception as e:
            print("Unable to create socket. Check your permissions: %s" % e)
            return

        # construct mmsg header to send in bulk for minimal latency
        m_msghdr = (struct_mmsghdr * 1000)()
        iov = struct_iovec(cast(self.packet, c_void_p), len(self.packet))
        msg_iov = pointer(iov)

        for i in range(0, 1000):
            msghdr = struct_msghdr(
                        cast(None, c_void_p), 0, msg_iov, 1,
                        0, 0, 0)
            m_msghdr[i] = struct_mmsghdr(msghdr)

        length_of_list = len(sockets)
        start_time = time.monotonic()

        # Time-based sending
        if self.sendtime > 0:
            num_to_send = 1 if length_of_list > 1 else 1000
            unable_to_send = 0
            total_num_sent = 0
            iters = 0

            while True:
                for s in sockets:
                    num_sent = _sendmmsg(s.fileno(), m_msghdr[0], num_to_send, 0)
                    if num_sent < 0:
                        unable_to_send += 1
                        if unable_to_send > 30:
                            break
                    else:
                        unable_to_send = 0
                        total_num_sent += num_sent
                iters += 1
                elapsed_time = time.monotonic() - start_time
                if elapsed_time >= self.sendtime:
                    break

        # Count-based sending
        elif self.packet_num:
            num_to_send_max = 1 if length_of_list > 1 else 1000
            num_sockets = len(sockets)
            total_pkts_sent = [0] * num_sockets
            total_pkts_remaining = [self.packet_num] * num_sockets
            keep_sending = True
            unable_to_send = 0

            while keep_sending:
                for s in sockets:
                    index = sockets.index(s)
                    if total_pkts_remaining[index] <= 0:
                        continue
                    num_to_send = min(num_to_send_max, total_pkts_remaining[index])
                    num_sent = _sendmmsg(s.fileno(), m_msghdr[0], num_to_send, 0)

                    if num_sent < 0:
                        unable_to_send += 1
                        if unable_to_send > 30:
                            break
                    else:
                        unable_to_send = 0
                        if num_sent > 0:
                            total_pkts_remaining[index] -= num_sent
                            total_pkts_sent[index] += num_sent

                keep_sending = any(pkts > 0 for pkts in total_pkts_remaining)

        # Close sockets
        for s in sockets:
            s.close()
            s.detach()

    def start(self):
        self.process = multiprocessing.Process(target=self.send_packets)
        self.process.start()

    def stop(self, timeout=None):
        if self.process:
            self.process.join(timeout)


def checksum(msg):
    s = 0

    # loop taking 2 characters at a time
    for i in range(0, len(msg), 2):
        w = ord(msg[i]) + (ord(msg[i+1]) << 8)
        s = s + w

    s = (s >> 16) + (s & 0xffff)
    s = s + (s >> 16)

    # complement and mask to 4 byte short
    s = ~s & 0xffff

    return s


def main():
    usage = "usage: %prog [options] arg1 arg2"
    parser = optparse.OptionParser(usage=usage)
    parser.add_option("-i", "--interface", type="string", dest="interface",
                      help="Interface list to send packets, seperated by ','", metavar="Interface")
    parser.add_option('-p', "--priority", type="int", dest="priority",
                      help="PFC class enable bitmap.", metavar="Priority", default=-1)
    parser.add_option("-t", "--time", type="int", dest="time",
                      help="Pause time in quanta for global pause or enabled class", metavar="time")
    parser.add_option("-s", "--sendtime", type="int", dest="sendtime",
                      help="Total amount of time to send pkts. -n option is ignored if this is set",
                      metavar="sendtime", default=0)
    parser.add_option("-n", "--num", type="int", dest="num",
                      help="Number of packets to be sent", metavar="number", default=0)
    parser.add_option("-r", "--rsyslog-server", type="string", dest="rsyslog_server",
                      default="127.0.0.1", help="Rsyslog server IPv4 address", metavar="IPAddress")
    parser.add_option('-g', "--global", action="store_true", dest="global_pf",
                      help="Send global pause frames (not PFC)", default=False)
    parser.add_option("-m", "--multiprocess", action="store_true", dest="multiprocess",
                      help="Use multiple processes to send packets", default=False)
    (options, args) = parser.parse_args()

    if options.interface is None:
        print("Need to specify the interface to send PFC/global pause frame packets.")
        parser.print_help()
        sys.exit(1)

    # Validate options using common validation function
    try:
        validate_options(options)
    except ValueError as e:
        print(str(e))
        parser.print_help()
        sys.exit(1)

    interfaces = options.interface.split(',')

    # Build PFC packet using common function
    packet = build_pfc_packet(options.priority, options.time, options.global_pf)

    pre_str = 'GLOBAL_PF' if options.global_pf else 'PFC'

    # Configure logging using common function
    my_logger = setup_logging(options.rsyslog_server)

    # Use multiprocessing if enabled
    if options.multiprocess:
        senders = []
        # Distribute interfaces across optimal number of processes
        interface_slices, num_processes = distribute_interfaces(interfaces)
        num_interfaces = len(interfaces)

        my_logger.debug("Multiprocess mode: {} interfaces across {} processes (~{} interfaces/process)".format(
            num_interfaces, num_processes, (num_interfaces + num_processes - 1) // num_processes))

        for interface_slice in interface_slices:
            s = PacketSender(interface_slice, packet, options.num, options.sendtime, options.rsyslog_server)
            s.start()
            senders.append(s)

        my_logger.debug(pre_str + '_STORM_START')
        # Wait for PFC packets to be sent
        for sender in senders:
            sender.stop()
        my_logger.debug(pre_str + '_STORM_END')
        return

    # Single-process mode (original code)
    length_of_list = len(interfaces)
    sockets = []
    fo_str = "Fanout"

    # Configure fanout logging
    fo_logger = logging.getLogger('MyLogger')
    fo_logger.setLevel(logging.DEBUG)
    fo_handler = logging.handlers.SysLogHandler()
    fo_logger.addHandler(fo_handler)

    # Create sockets
    try:
        for i in range(0, length_of_list):
            mysocket = socket(AF_PACKET, SOCK_RAW)
            sockets.append(mysocket)
            fo_logger.debug("Socket number : {} {}".format(i, mysocket.getsockname()))
    except Exception:
        print("Unable to create socket for i %i. Check your permissions" % i)
        sys.exit(1)

    for s, interface in zip(sockets, interfaces):
        s.bind((interface, 0))
        s.setsockopt(263, 20, 1)  # QDISC_BYPASS
        s.setblocking(False)
        fo_logger.debug("Socket bound : {}".format(s.getsockname()))

    # construct mmsg header to send in bulk for minimal latency
    # Prepare 1000 buffers in advance.
    m_msghdr = (struct_mmsghdr * 1000)()

    iov = struct_iovec(cast(packet, c_void_p), len(packet))

    msg_iov = pointer(iov)
    msg_iovlen = 1
    msg_control = 0
    msg_controllen = 0

    msg_namelen = 0
    msg_name = cast(None, c_void_p)

    # construct the vector
    for i in range(0, 1000):
        msghdr = struct_msghdr(
                    msg_name, msg_namelen, msg_iov, msg_iovlen,
                    msg_control, msg_controllen, 0)
        m_msghdr[i] = struct_mmsghdr(msghdr)
        i += 1

    total_num_remaining = options.num
    total_num_sent = 0
    iters = 0
    start_time = time.monotonic()

    if options.sendtime > 0:       # send according to requested period of send time
        if length_of_list > 1:
            num_to_send = 1
        else:
            num_to_send = 1000
        print("Generating Packet(s) over period of %f seconds" % options.sendtime)
        my_logger.debug(pre_str + '_STORM_START')
        unable_to_send = 0
        while True:
            for s in sockets:
                num_sent = _sendmmsg(s.fileno(), m_msghdr[0], num_to_send, 0)   # direct to c library api
                if num_sent < 0:
                    errno = get_errno()
                    fo_logger.debug(fo_str + ' sendmmsg got errno ' + str(errno) + ' for socket ' +
                                    str(s.getsockname()))
                    unable_to_send += 1
                    if unable_to_send > 30:
                        break
                else:
                    unable_to_send = 0
                    if num_sent != num_to_send:
                        fo_logger.debug(fo_str + ' sendmmsg iteration ' + str(iters) + ' only sent ' +
                                        str(num_sent) + ' out of requested ' + str(num_to_send) +
                                        ' for socket ' + str(s.getsockname()))
                # Count across all sockets
                total_num_sent += num_sent
            iters += 1
            done_time = time.monotonic()
            elapsed_time = done_time - start_time
            if elapsed_time >= options.sendtime:
                break

        my_logger.debug(pre_str + '_STORM_END')
        fo_logger.debug(fo_str + '_STORM_END_AFTER_RSYSLOG_CALL : sent ' + str(total_num_sent) + ' pkts in ' + str(
            iters) + ' iterations and elapsed time of ' + str(elapsed_time) + ' secs')
    # send according to requested number of pkts
    elif options.num:
        if length_of_list > 1:
            num_to_send_max = 1
        else:
            num_to_send_max = 1000
        print("Generating %s Packet(s)" % options.num)
        my_logger.debug(pre_str + '_STORM_START')
        num_sockets = len(sockets)
        total_pkts_sent = [0] * num_sockets
        total_pkts_remaining = [total_num_remaining] * num_sockets
        keep_sending = True
        unable_to_send = 0
        while keep_sending is True:
            for s in sockets:
                index = sockets.index(s)
                if total_pkts_remaining[index] <= 0:
                    continue
                num_to_send = min(num_to_send_max, total_pkts_remaining[index])
                num_sent = _sendmmsg(s.fileno(), m_msghdr[0], num_to_send, 0)
                if num_sent < 0:
                    errno = get_errno()
                    fo_logger.debug(fo_str + ' sendmmsg got errno ' + str(errno) + ' for socket ' +
                                    str(s.getsockname()))
                    unable_to_send += 1
                    if unable_to_send > 30:
                        break

                else:
                    unable_to_send = 0
                    if num_sent != num_to_send:
                        fo_logger.debug(fo_str + ' sendmmsg iteration ' + str(iters) +
                                        ' only sent ' + str(num_sent) +
                                        ' out of requested ' + str(num_to_send) + ' for socket ' +
                                        str(s.getsockname()))
            if num_sent > 0:
                total_pkts_remaining[index] -= num_sent
                total_pkts_sent[index] += num_sent
            iters += 1
            keep_sending = False
            for i in range(0, num_sockets):
                if total_pkts_remaining[i] > 0:
                    keep_sending = True

        done_time = time.monotonic()
        elapsed_time = done_time - start_time

        my_logger.debug(pre_str + '_STORM_END')
        for i in range(0, num_sockets):
            fo_logger.debug(fo_str + '_STORM_END : socket ' + str(i) + ' sent ' + str(total_pkts_sent[i]) + ' pkts')
        fo_logger.debug(fo_str + '_STORM_END_AFTER_RSYSLOG_CALL : ' + str(iters) +
                        ' iterations and elapsed time of ' + str(elapsed_time) + ' secs')
    else:
        print("Pls provide atleast one -s or -n option.", file=sys.stderr)

    for s in sockets:
        s.close()
        s.detach()


if __name__ == "__main__":
    main()
