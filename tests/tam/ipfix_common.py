"""
IPFIX packet structures for TAM Mirror on Drop testing.

This module provides basic IPFIX packet parsing capabilities for validating
TAM Mirror on Drop IPFIX reports.
"""

from scapy.all import Packet, ShortField, IntField, bind_layers, Ether
from scapy.layers.inet import UDP

class IPFIXHeader(Packet):
    """
    IPFIX Message Header as defined in RFC 7011.
    
    The IPFIX Message Header format:
     0                   1                   2                   3
     0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |       Version Number          |            Length             |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                           Export Time                         |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                       Sequence Number                        |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    |                    Observation Domain ID                      |
    +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
    """
    name = "IPFIXHeader"
    fields_desc = [
        ShortField("version", 10),          # IPFIX version (10)
        ShortField("length", 0),            # Total length of IPFIX message
        IntField("export_time", 0),         # Time when message was exported
        IntField("sequence_number", 0),     # Sequence number
        IntField("observation_domain_id", 0) # Observation domain ID
    ]


# Bind IPFIX layers
bind_layers(UDP, IPFIXHeader, dport=4739)  # Standard IPFIX port
