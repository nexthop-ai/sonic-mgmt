#!/usr/bin/env python

"""
Script to generate PFC packets.

"""
import sys
import optparse
import logging
import logging.handlers
import time
import multiprocessing

from socket import socket, AF_PACKET, SOCK_RAW

# Import common PFC utilities (will be bundled at deployment time)
from pfc_common import (
    build_pfc_packet,
    distribute_interfaces,
    validate_options
)

logger = logging.getLogger('MyLogger')
logger.setLevel(logging.DEBUG)


class PacketSender():
    """
    A class to send PFC pause frames
    """
    def __init__(self, interfaces, packet, num, interval):
        # Create RAW socket to send PFC pause frames
        self.sockets = []
        try:
            for interface in interfaces:
                s = socket(AF_PACKET, SOCK_RAW)
                s.bind((interface, 0))
                self.sockets.append(s)
        except Exception as e:
            print("Unable to create socket. Check your permissions: %s" % e)
            sys.exit(1)
        self.packet_num = num
        self.packet_interval = interval
        self.process = None
        self.packet = packet

    def send_packets(self):
        iteration = self.packet_num
        while iteration > 0:
            for s in self.sockets:
                s.send(self.packet)
                if self.packet_interval > 0:
                    time.sleep(self.packet_interval)
            iteration -= 1

    def start(self):
        self.process = multiprocessing.Process(target=self.send_packets)
        self.process.start()

    def stop(self, timeout=None):
        if self.process:
            self.process.join(timeout)
        for s in self.sockets:
            s.close()


def main():
    usage = "usage: %prog [options] arg1 arg2"
    parser = optparse.OptionParser(usage=usage)
    parser.add_option("-i", "--interface", type="string", dest="interface",
                      help="Interface list to send packets, separated by ','", metavar="Interface")
    parser.add_option('-p', "--priority", type="int", dest="priority",
                      help="PFC class enable bitmap.", metavar="Priority", default=-1)
    parser.add_option("-t", "--time", type="int", dest="time",
                      help="Pause time in quanta for global pause or enabled class", metavar="time")
    parser.add_option("-n", "--num", type="int", dest="num",
                      help="Number of packets to be sent", metavar="number", default=1)
    parser.add_option("-r", "--rsyslog-server", type="string", dest="rsyslog_server",
                      default="127.0.0.1", help="Rsyslog server IPv4 address", metavar="IPAddress")
    parser.add_option('-g', "--global", action="store_true", dest="global_pf",
                      help="Send global pause frames (not PFC)", default=False)
    parser.add_option("-s", "--send_pfc_frame_interval", type="float", dest="send_pfc_frame_interval",
                      help="Interval sending pfc frame", metavar="send_pfc_frame_interval", default=0)
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

    # Configure logging
    handler = logging.handlers.SysLogHandler(address=(options.rsyslog_server, 514))
    handler.ident = 'pfc_gen: '
    logger.addHandler(handler)

    # Build PFC packet using common function
    packet = build_pfc_packet(options.priority, options.time, options.global_pf)

    pre_str = 'GLOBAL_PF' if options.global_pf else 'PFC'
    logger.debug(pre_str + '_STORM_DEBUG')

    # Start sending PFC pause frames
    if options.multiprocess:
        senders = []
        # Distribute interfaces across optimal number of processes
        interface_slices, num_processes = distribute_interfaces(interfaces)
        num_interfaces = len(interfaces)

        logger.debug("Multiprocess mode: {} interfaces across {} processes (~{} interfaces/process)".format(
            num_interfaces, num_processes, (num_interfaces + num_processes - 1) // num_processes))

        for interface_slice in interface_slices:
            s = PacketSender(interface_slice, packet, options.num, options.send_pfc_frame_interval)
            s.start()
            senders.append(s)

        logger.debug(pre_str + '_STORM_START')
        # Wait PFC packets to be sent
        for sender in senders:
            sender.stop()
    else:
        sender = PacketSender(interfaces, packet, options.num, options.send_pfc_frame_interval)
        logger.debug(pre_str + '_STORM_START')
        sender.send_packets()

    logger.debug(pre_str + '_STORM_END')


if __name__ == "__main__":
    main()
