#!/usr/bin/env python3

"""Initial implementation of draft-ymbk-lsvr-lsoe-02 (LSOE).

Be warned that the specification is in flux, we don't expect -02 to be
the final protocol.
"""

# Implementation notes:
#
# * Currently written using the third-party Tornado package, because I
#   know that API better than I know Python3's native asyncio API.  At
#   some point we'll probably rewrite this to use asyncio directly,
#   which may remove the need for Tornado.
#
# * We don't have a real EtherType yet, because IEEE considers them a
#   scarce resource and won't allocate until the specification is
#   cooked.  So for now we use one of the "playground" EtherTypes IEEE
#   set aside for use for exactly this purpose.

import time
import socket
import struct
import logging
import collections

import tornado.gen
import tornado.locks
import tornado.ioloop
import tornado.queues

import pyroute2
import pyroute2.netlink.rtnl
import pyroute2.netlink.rtnl.ifinfmsg

# This is LSOE protocol version zero

LSOE_VERSION = 0

# Ethernet physical layer contstants from linux/if_ether.h, with additions.

ETH_DATA_LEN    = 1500          # Max. octets in payload
ETH_FRAME_LEN   = 1514          # Max. octets in frame sans FCS

ETH_P_ALL       = 0x0003        # All packets
ETH_P_IP        = 0x0800        # IPv4
ETH_P_IPV6      = 0x86DD        # IPv6
ETH_P_IEEE_EXP1 = 0x8885        # "Local Experimental EtherType 1"
ETH_P_IEEE_EXP2 = 0x8886        # "Local Experimental EtherType 2"

ETH_P_LSOE      = ETH_P_IEEE_EXP1

# Linux PF_PACKET API constants from linux/if_packet.h.

PACKET_HOST	 = 0
PACKET_BROADCAST = 1
PACKET_MULTICAST = 2
PACKET_OTHERHOST = 3
PACKET_OUTGOING	 = 4

# Order here must match the address tuples generated by the socket module for PF_PACKET
SockAddrLL = collections.namedtuple("Sockaddr_LL",
                                    ("ifname", "protocol", "pkttype", "arptype", "macaddr"))

# Magic parameters which ought to come from a configuration file
lsoe_msg_reassembly_timeout = 1   # seconds
lsoe_macaddr_cache_timeout  = 300 # Seconds, number pulled out of a hat

#
# Transport layer
#

class Datagram(object):
    """
    LSOE transport protocol datagram.
    """

    h = struct.Struct("!BBHL")
    LAST_FLAG  = 0x80

    _sbox = (0xa3,0xd7,0x09,0x83,0xf8,0x48,0xf6,0xf4,0xb3,0x21,0x15,0x78,
             0x99,0xb1,0xaf,0xf9,0xe7,0x2d,0x4d,0x8a,0xce,0x4c,0xca,0x2e,
             0x52,0x95,0xd9,0x1e,0x4e,0x38,0x44,0x28,0x0a,0xdf,0x02,0xa0,
             0x17,0xf1,0x60,0x68,0x12,0xb7,0x7a,0xc3,0xe9,0xfa,0x3d,0x53,
             0x96,0x84,0x6b,0xba,0xf2,0x63,0x9a,0x19,0x7c,0xae,0xe5,0xf5,
             0xf7,0x16,0x6a,0xa2,0x39,0xb6,0x7b,0x0f,0xc1,0x93,0x81,0x1b,
             0xee,0xb4,0x1a,0xea,0xd0,0x91,0x2f,0xb8,0x55,0xb9,0xda,0x85,
             0x3f,0x41,0xbf,0xe0,0x5a,0x58,0x80,0x5f,0x66,0x0b,0xd8,0x90,
             0x35,0xd5,0xc0,0xa7,0x33,0x06,0x65,0x69,0x45,0x00,0x94,0x56,
             0x6d,0x98,0x9b,0x76,0x97,0xfc,0xb2,0xc2,0xb0,0xfe,0xdb,0x20,
             0xe1,0xeb,0xd6,0xe4,0xdd,0x47,0x4a,0x1d,0x42,0xed,0x9e,0x6e,
             0x49,0x3c,0xcd,0x43,0x27,0xd2,0x07,0xd4,0xde,0xc7,0x67,0x18,
             0x89,0xcb,0x30,0x1f,0x8d,0xc6,0x8f,0xaa,0xc8,0x74,0xdc,0xc9,
             0x5d,0x5c,0x31,0xa4,0x70,0x88,0x61,0x2c,0x9f,0x0d,0x2b,0x87,
             0x50,0x82,0x54,0x64,0x26,0x7d,0x03,0x40,0x34,0x4b,0x1c,0x73,
             0xd1,0xc4,0xfd,0x3b,0xcc,0xfb,0x7f,0xab,0xe6,0x3e,0x5b,0xa5,
             0xad,0x04,0x23,0x9c,0x14,0x51,0x22,0xf0,0x29,0x79,0x71,0x7e,
             0xff,0x8c,0x0e,0xe2,0x0c,0xef,0xbc,0x72,0x75,0x6f,0x37,0xa1,
             0xec,0xd3,0x8e,0x62,0x8b,0x86,0x10,0xe8,0x08,0x77,0x11,0xbe,
             0x92,0x4f,0x24,0xc5,0x32,0x36,0x9d,0xcf,0xf3,0xa6,0xbb,0xac,
             0x5e,0x6c,0xa9,0x13,0x57,0x25,0xb5,0xe3,0xbd,0xa8,0x3a,0x01,
             0x05,0x59,0x2a,0x46)

    def __init__(self, b, sa_ll, version, frag, length, checksum, timestamp = None):
        self.bytes     = b
        self.sa_ll     = sa_ll
        self.version   = version
        self.frag      = frag
        self.length    = length
        self.checksum  = checksum
        self.timestamp = timestamp

    @classmethod
    def incoming(cls, b, sa_ll):
        version, frag, length, checksum = cls.h.unpack(b)
        if length > len(b):
            b = b[:length]
        return cls(
            b         = b,
            sa_ll     = sa_ll,
            version   = version,
            frag      = frag,
            length    = length,
            checksum  = checksum,
            timestamp = time.time())

    def verify(self):
        return self.version == LSOE_VERSION and \
            len(self.bytes) == self.length and \
            self.checksum == self._sbox_checksum(
                self.bytes[self.h.size:], self.frag, self.length)

    @classmethod
    def outgoing(cls, b, sa_ll, frag, last):
        if last:
            frag |= Datagram.LAST_FLAG
        length = cls.h.size + len(b)
        cksum  = cls._sbox_checksum(b, frag, length)
        hdr    = cls.h.pack(LSOE_VERSION, frag, length, cksum)
        return cls(
            b         = hdr + b,
            sa_ll     = sa_ll,
            version   = LSOE_VERSION,
            frag      = frag,
            length    = length,
            checksum  = cksum)

    @classmethod
    def split_message(cls, b, macaddr, ifname):
        sa_ll = SockAddrLL(macaddr  = macaddr,
                           ifname   = ifname,
                           protocol = ETH_P_LSOE,
                           pkttype  = 0,
                           arptype  = 0)
        n = ETH_DATA_LEN - cls.h.size
        chunks = [b[i : i + n] for i in xrange(0, len(b), n)]
        for i, chunk in enumerate(chunks):
            yield cls.outgoing(chunk, sa_ll, i, chunk is chunks[-1])

    @property
    def is_final(self):
        return self.frag & self.LAST_FLAG != 0

    @property
    def dgram_number(self):
        return self.frag & ~self.LAST_FLAG

    @classmethod
    def _sbox_checksum(cls, b, frag, length, version = LSOE_VERSION):
        pkt = cls.h.pack(version, frag, length, 0) + b
        sum, result = [0, 0, 0, 0], 0
        for i, b in enumerate(pkt):
            sum[i & 3] += self._sbox[b]
        for i in xrange(4):
            result = (result << 8) + sum[i]
        for i in xrange(2):
            result = (result >> 32) + (result & 0xFFFFFFFF)
        return result

    @property
    def payload(self):
        return self.bytes[self.h.size : self.h.size + self.length]

class EtherIO(object):
    """
    LSOE transport protocol implementation.  Uses Tornado to read and
    write from a PF_PACKET datagram socket.  Handles fragmentation,
    reassembly, checksum, and transport layer sanity checks.

    User interface to upper layer is the .read(), .write(), and
    .close() methods, everything else is internal to the engine.
    """

    class MACAddr(object):
        def __init__(self, macaddr, ifname):
            self.macaddr = macaddr
            self.ifname = ifname
            self.timestamp = None

    def __init__(self):
        # Do we need to do anything with multicast setup?
        self.macaddrs = {}
        self.dgrams = {}
        self.q = tornado.queues.Queue()
        self.s = socket.socket(socket.PF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_LSOE))
        self.ioloop = tornado.ioloop.IOLoop.current()
        self.ioloop.add_handler(self.s, self._handle_read,  tornado.ioloop.READ)
        #self.ioloop.add_handler(self.s, self._handle_error, tornado.ioloop.ERROR)
        self.ioloop.PeriodicCallback(self._gc, lsoe_msg_reassembly_timeout * 500)
        # Might need one or more self.ioloop.spawn_callback() calls somewhere

    # Returns a Future, awaiting which returns a (bytes, macaddr) tuple
    def read(self):
        return self.q.get()

    # Breaks bytes into datagrams and sends them
    def write(self, b, macaddr):
        for d in Datagram.split_message(b, macaddr, self.macaddrs[macaddr].ifname):
            self.s.sendto(d.bytes, d.sa_ll)

    # Tears down I/O
    def close(self):
        self.ioloop.remove_handler(self.s)

    # Internal handler for READ events
    def _handle_read(self, events):
        pkt, sa_ll = s.recvfrom(ETH_DATA_LEN)
        if len(pkt) < Datagram.h.size:
            return
        sa_ll = SockAddrLL(*sa_ll)
        assert sa_ll.protocol == ETH_P_LSOE
        if sa_ll.pkttype = PACKET_OUTGOING:
            return
        if sa_ll.macaddr not in self.macaddrs:
            self.macaddrs[macaddr] = self.MACAddr(sa_ll.macaddr, sa_ll.ifname)
        elif self.macaddrs[macaddr].ifname != sa_ll.ifname:
            # Should yell about MAC address appearing on wrong interface here
            return
        self.macaddrs[sa_ll.macaddr].timestamp = time.time()
        d = Datagram.incoming(pkt, sa_ll)
        if not d.verify():
            return
        try:
            rq = self.dgrams[sa_ll.macaddr]
        except KeyError:
            rq = self.dgrams[sa_ll.macaddr] = []
        rq.append(d)
        rq.sort(key = lambda d: (d.dgram_number, -d.timestamp))
        if not rq[-1].is_final:
            return None
        rq[:] = [d for i, d in enumerate(rq) if d.dgram_number >= i]
        for i, d in enumerate(rq):
            if d.dgram_number != i or d.is_final != (d is rq[-1]):
                return
        del self.dgrams[sa_ll.macaddr]
        self.q.put_nowait((b"".join(d.payload for d in rq), sa_ll.macaddr))

    # Internal handler to garbage collect incomplete messages and stale MAC addresses
    def _gc(self):
        threshold = time.time() - lsoe_msg_reassembly_timeout
        for macaddr, rq in self.dgrams.items():
            rq.sort(key = lambda d: d.timestamp)
            while rq[0].timestamp < threshold:
                del rq[0]
            if not rq:
                del self.dgrams[macaddr]
        threshold = time.time() - lsoe_macaddr_cache_timeout
        for macaddr, m in self.macaddrs.items():
            if m.timestamp < threshold:
                del self.macaddrs[macaddr]

#
# Presentation layer
#

def register_pdu(cls):
    """
    Decorator to add a PDU class to the PDU dispatch table.
    """

    assert cls.pdu_type is not None
    assert cls.pdu_type not in cls.pdu_dispatch
    cls.pdu_dispatch[cls.pdu_type] = cls

class PDUParseError(Exception):
    "Error parsing LSOE PDU."

# Not sure whether we want to be passing macaddr to .from_wire(), this
# is just PDU parsing, received macaddr is connection/session.  Omit for now.

class PDU(object):
    """
    Abstract base class for PDUs.
    """

    pdu_type = None
    pdu_dispatch = {}

    h = struct.Struct("!BH")

    def __cmp__(self, other):
        return cmp(self.to_wire(), other.to_wire())

    @classmethod
    def from_wire(cls, b):
        pdu_type, pdu_length = cls.h.unpack(b)
        self = cls.pdu_dispatch[pdu_type]()
        if len(b) != pdu_length:
            raise PDUParseError
        self.pdu_length = pdu_length
        self.from_wire(b[cls.h.size:])
        return self

    def to_wire(self, b):
        return self.h.pack(self.pdu_type, self.h.size + len(b)) + b


@register_pdu
class HelloPDU(PDU):

    pdu_type = 0

    h = struct.Struct("6s")

    def from_wire(self, b):
        self.my_macaddr, = self.h.unpack(b)

    def to_wire(self):
        return super(HelloPDU, self).to_wire(self.h.pack(self.my_macaddr))
    
@register_pdu
class OpenPDU(PDU):

    # Implementation restriction: For now, we assume and require Authentiation Data to be empty.

    pdu_type = 1

    h = struct.Struct("10s10spH")

    def from_wire(self, b):
        self.local_id, self.remote_id, self.attributes, self.auth_length = self.h.unpack(b)
        if self.auth_length != 0:
            raise PDUParserError

    def to_wire(self):
        return super(OpenPDU, self).to_wire(self.h.pack(
            self.local_id, self.remote_id, self.attributes, 0))

class PrimLoopFlagsMixin(object):

    _primary_flag  = 0x80
    _loopback_flag = 0x40
    flags = 0

    @property
    def primary(self):
        return self.flags & self._primary_flag != 0

    @primary.setter
    def primary(self, newval):
        if newval:
            self.flags |= self._primary_flag
        else:
            self.flags &= ~self._primary_flag

    @property
    def loopback(self):
        return self.flags & self._loopback_flag != 0

    @loopback.setter
    def loopback(self, newval):
        if newval:
            self.flags |= self._loopback_flag
        else:
            self.flags &= ~self._loopback_flag

class IPEncapsulation(PrimLoopFlagsMixin):

    @classmethod
    def from_wire(cls, b, offset):
        self = cls()
        self.flags, self.ipaddr, self.prefixlen = self.h.unpack_from(b, offset)
        return self, self.h.size

    def to_wire(self):
        return self.h.pack(self.flags, self.ipaddr, self.prefixlen)

# Pretend for now that we can treat an MPLS label as an opaque
# three-octet string rather than needing get/set properties.

class MPLSIPEncapsulation(PrimLoopFlagsMixin):

    h1 = struct.Struct("BB")
    h2 = struct.Struct("3s")

    @classmethod
    def from_wire(cls, b, offset):
        self = cls()
        self.flags, label_count = self.h1.unpack_from(b, offset)
        self.labels = []
        offset += self.h1.size
        for i in xrange(label_count):
            labels.append(self.h2.unpack_from(b, offset)[0])
            offset += self.h2.size
        self.ipaddr, self.prefixlen = self.h3.unpack_from(b, offset)
        return self, self.h1.size + self.h2.size * len(self.labels) + self.h3.size

    def to_wire(self):
        return self.h1.pack(self.flags, len(labels)) \
            + b"".join(self.h2.pack(l) for l in self.labels) \
            + self.h13.pack(self.ipaddr, self.prefixlen)

class EncapsulationPDU(PDU):

    h = struct.Struct("H")

    encap_type = None

    def from_wire(self, b):
        count, = self.h.unpack(b)
        offset = self.h.size
        self.encaps = []
        for i in xrange(count):
            e, n = self.encap_type.from_wire(b, offset)
            encaps.append(e)
            offset += n

    def to_wire(self):
        return super(EncapsulationPDU, self).to_wire(
            self.h.pack(len(self.encaps)) +
            b"".join(e.to_wire() for e in self.encaps))

class IPv4Encapsulation(IPEncapsulation):
    h = struct.Struct("B4sB")

class IPv6Encapsulation(IPEncapsulation):
    h = struct.Struct("B16sB")

class MPLSIPv4Encapsulation(MPLSIPEncapsulation):
    h3 = struct.Struct("4sB")

class MPLSIPv6Encapsulation(MPLSIPEncapsulation):
    h3 = struct.Struct("16sB")

@register_pdu
class IPv4EncapsulationPDU(PDU):
    pdu_type = 4
    encap_type = IPv4Encapsulation

@register_pdu
class IPv6EncapsulationPDU(PDU):
    pdu_type = 5
    encap_type = IPv6Encapsulation

@register_pdu
class MPLSIPv4EncapsulationPDU(PDU):
    pdu_type = 6
    encap_type = MPLSIPv4Encapsulation

@register_pdu
class MPLSIPv6EncapsulationPDU(PDU):
    pdu_type = 7
    encap_type = MPLSIPv6Encapsulation

@register_pdu
class EncapsulationACKPDU(PDU):

    pdu_type = 3

    h = struct.Struct("B")

    def from_wire(self, b):
        self.encap_type, = self.h.unpack(b)

    def to_wire(self):
        return super(EncapsulationACKPDU, self).to_wire(self.h.pack(self.encap_type))

@register_pdu
class KeepAlivePDU(PDU):

    pdu_type = 2

    def from_wire(self, b):
        pass

    def to_wire(self):
        return super(KeepAlivePDU, self).to_wire("")

#
# Network interface status and monitoring.
#

class Interface(object):

    def __init__(self, index, name, macaddr, flags):
        self.index   = index
        self.name    = name
        self.macaddr = macaddr
        self.flags   = flags
        self.ipaddrs = {}

    def add_ipaddr(self, family, ipaddr):
        if family not in self.ipaddrs:
            self.ipaddrs[family] = []
        self.ipaddrs[family].append(ipaddr)

    def del_ipaddr(self, family, ipaddr):
        self.ipaddrs[family].remove(ipaddr)

    def update_flags(self, flags):
        self.flags = flags

    @property
    def is_up(self):
        # Add other flags as needed, eg, IFF_LOWER_UP
        return self.flags & pyroute2.netlink.rtnl.ifinfmsg.IFF_UP != 0

class Interfaces(object):
    
    def __init__(self):
        # Race condition: open event monitor socket before doing initial scans.
        self.ip = pyroute2.RawIPRoute()
        self.ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK|
                     pyroute2.netlink.rtnl.RTNLGRP_IPV4_IFADDR|
                     pyroute2.netlink.rtnl.RTNLGRP_IPV4_IFADDR)
        self.ifnames = {}
        self.ifindex = {}
        with pyroute2.IPRoute() as ipr:
            for msg in ipr.get_links():
                iface = Interface(
                    index   = msg["index"],
                    flags   = msg["flags"],
                    name    = msg.get_attr("IFLA_IFNAME"),
                    macaddr = msg.get_attr("IFLA_ADDRESS"))
                self.ifindex[iface.index] = iface
                self.ifnames[iface.name]  = iface
            for msg in ipr.get_addr():
                self.ifindex[msg["index"]].add_ipaddr(
                    family = msg["family"],
                    ipaddr = msg.get_attr("IFA_ADDRESS"))
            tornado.ioloop.IOLoop.current().add_handler(
                self.ip.fileno(), self._handle_event, tornado.ioloop.IOLoop.READ)

        def _handle_event(self, *ignored):
            for msg in ip.get():
                if msg["event"] == "RTM_NEWLINK":
                    ifindex[msg["index"]].update_flags(msg["flags"])
                elif msg["event"] == "RTM_NEWADDR":
                    ifindex[msg["index"]].add_ipaddr(msg["family"], msg.get_attr("IFA_ADDRESS"))
                elif msg["event"] == "RTM_DELADDR":
                    ifindex[msg["index"]].del_ipaddr(msg["family"], msg.get_attr("IFA_ADDRESS"))
                else:
                    print("WTF: {} event".format(msg["event"]))

#
# Protocol engine
#

# This is not even close to stable yet

@tornado.gen.coroutine
def main():

    # Probably ought to be reading config file before doing anything else

    sessions = {}
    ifs = Interfaces()
    io  = EtherIO()
    while True:
        msg, macaddr = yield io.read()
        if macaddr not in sessions:
            sessions[macaddr] = Session(io, ifs, macaddr)
        sessions[macaddr].recv(msg)

        # Need to do something (here or in a timer callback) to send
        # HELLO on every interface, or maybe all configured
        # interfaces, or intersection of configured and detected
        # interfaces, or ... but in any case we need to send some.
        #
        # Might want to do that in a separate pseudo-thread.
        #
        # Might want main() to just initialise shared data structures
        # and task pseudo-threads then wait them to exit.
        #
        # main() might want to be a class to simplify shared data.
        #
        # Need something here to gc dead sessions?

class Session(object):

    # Unclear what (if anything) needs to be a coroutine here.
    # Depends on how much fun we want to have with dispatch mechanisms
    # here vs pseudo threads vs ... for state.

    def __init__(self, io, ifs, macaddr):
        self.io = io
        self.ifs = ifs
        self.macaddr = macaddr
        self.have_sent_open = False
        self.have_seen_open = False

    def recv(self, msg):
        pdu = PDU.from_wire(msg)        

        # If this is a new MAC address, the only allowed PDUs are
        # Hello and Open.  I think current spec says each side sends
        # the other an Open, so either we're sending an Open in
        # response to a Hello or we're sending it in response to an
        # Open.  Once we've sent an Open we have local state for the
        # peer so simultaneous Opens should not be a problem.
        #
        # If this is not a new MAC address, we should already have
        # local state for the peer.
        # 
        # Need to think about appropriate structure here.  Encode
        # state machine as pseudo-thread state (dict of queues)?
        # Explicit state machine data structure?  PDU dispatch
        # methods?  Don't try to use all the toys in the box, question
        # is which ones help.
        #
        # State machine is pretty simple:
        #
        # * Each side sends an OPEN
        # * Each side acks the other side's open
        # * Each side should send encaps
        # * each side acks the other's encaps
        # * After which point we're just doing keepalives forever
        #
        # May be able to encode state machine(s) as instance variables
        # containing bound methods, eg:
        #
        #   self.next_state = self.blarg_state
        #
        # then we just dispatch to self.next_state() at the
        # appropriate point, or something like that.
