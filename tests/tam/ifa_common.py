from scapy.all import Packet, BitField, ByteField, ShortField, IntField, bind_layers, IP, UDP, TCP


class IFA2Header(Packet):
    name = "IFA2Header"
    fields_desc = [
        BitField("version", 2, 4),
        BitField("gns", 1, 4),
        ByteField("ip_nxthdr", 0),
        BitField("flags", 0, 8),
        ByteField("max_length", 255),
    ]


class IFA2MetadataHeader(Packet):
    name = "IFA2MetadataHeader"
    fields_desc = [
        ByteField("request_vector", 0),
        ByteField("action_vector", 0),
        ByteField("hop_limit", 0),
        ByteField("length", 0),
    ]


class IFAMetadata(Packet):
    name = "IFA2Metadata"
    fields_desc = [
        BitField("lns", 0, 4),
        BitField("device_id", 0, 20),
        BitField("ttl", 0, 8),
        BitField("speed", 0, 4),
        BitField("cng", 0, 2),
        BitField("queue_id", 0, 6),
        BitField("rx_seconds", 0, 20),
        ShortField("egress_port", 0),
        ShortField("ingress_port", 0),
        IntField("rx_nanoseonds", 0),
        IntField("rsd_time", 0),
        IntField("egress_queue_bytes", 0),
        ShortField("reserved", 0),
        ShortField("egress_queue_depth", 0),
        IntField("mmu_stats", 0),
    ]


# Bind the custom layers once so tests importing these classes don't need to re-bind
bind_layers(IP, IFA2Header, proto=253)
bind_layers(IFA2Header, UDP, ip_nxthdr=17)
bind_layers(UDP, IFA2MetadataHeader)
bind_layers(IFA2MetadataHeader, IFAMetadata)
# Also support TCP as next header when ip_nxthdr=6
bind_layers(IFA2Header, TCP, ip_nxthdr=6)
bind_layers(TCP, IFA2MetadataHeader)
