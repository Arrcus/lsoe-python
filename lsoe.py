#!/usr/bin/env python3

"""Initial implementation of draft-ietf-lsvr-lsoe-01 (LSOE).

Be warned that the specification is in flux, we don't expect -02 to be
the final protocol.
"""

# Default configuration, here for ease of reference.

default_config = '''

# Default configuration values, expressed in the same syntax
# as the optional configuration file. Section name "[lsoe]" is mandatory.
# All times expressed in seconds, so 0.1 is 100 milliseconds, etc.

[lsoe]

# How long to wait before first retransmission
retransmit-initial-interval = 1.0

# Exponential backoff enabled?
retransmit-exponential-backoff = yes

# Maximum number of retransmissions before considering session dead
retransmit-max-drop = 3

# How frequently to send keepalives, in seconds
keepalive-send-interval = 1.0

# How long without receiving keepalive before considering connection dead? (0.0 = "never")
keepalive-receive-timeout = 60.0

# How frequently to send Hello PDUs
hello-interval = 60.0

# How long to wait before giving up on reassembly of a multi-frame PDU
reassembly-timeout = 1.0

# How long to wait before purging a stale MAC address from the cache
mac-address-cache-timeout = 300.0
'''

# Implementation notes:
#
# * Currently written using the third-party Tornado package, because I
#   know that API better than I know Python3's native asyncio API.  At
#   some point we'll probably rewrite this to use asyncio directly,
#   which may remove the need for Tornado, but Python didn't get support
#   for "proper" co-routines (`await`, `async def`, etc) until Python 3.5,
#   which is a bit new for some of the platforms we want to support,
#   so Tornado is probably a better bet for now in any case.
#
# * We don't have a real EtherType yet, because IEEE considers them a
#   scarce resource and won't allocate until the specification is
#   cooked.  So for now we use one of the "playground" EtherTypes IEEE
#   set aside for use for exactly this purpose.

import os
import sys
import copy
import socket
import struct
import logging
import argparse
import collections
import configparser

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

# MAC address to which we should send LSOE Hello PDUs.
# Some archived email discussing this, I think.

LSOE_HELLO_MACADDR = b"\xFF\xFF\xFF\xFF\xFF\xFF" # Figure out real value...

# Linux PF_PACKET API constants from linux/if_packet.h.

PACKET_HOST	 = 0
PACKET_BROADCAST = 1
PACKET_MULTICAST = 2
PACKET_OTHERHOST = 3
PACKET_OUTGOING	 = 4

# Order here must match the address tuples generated by the socket module for PF_PACKET
SockAddrLL = collections.namedtuple("Sockaddr_LL",
                                    ("ifname", "protocol", "pkttype", "arptype", "macaddr"))

# Logging setup
logger = logging.getLogger(os.path.splitext(os.path.basename(sys.argv[0]))[0])



#
# Low-level data types
#

class MACAddress(bytes):
    def __new__(cls, thing):
        if isinstance(thing, str):
            thing = bytes(int(i, 16) for i in thing.replace("-", ":").split(":"))
        assert isinstance(thing, bytes) and len(thing) == 6
        return bytes.__new__(cls, thing)

    def __str__(self):
        return ":".join("{:02x}".format(b) for b in self)

class IPAddress(bytes):
    def __new__(cls, thing):
        if isinstance(thing, str):
            thing = socket.inet_pton(socket.AF_INET6 if ":" in thing else socket.AF_INET, thing)
        assert isinstance(thing, bytes) and len(thing) in (4, 16)
        return bytes.__new__(cls, thing)

    def __str__(self):
        return socket.inet_ntop(self.af, self)

    @property
    def af(self):
        return socket.AF_INET if len(self) == 4 else socket.AF_INET6



#
# Transport layer
#

class Datagram:
    """
    LSOE transport protocol datagram.
    """

    h = struct.Struct("!BBHL")
    LAST_FLAG  = 0x80

    # "F table" S-Box from Skipjack, used in the LSOE checksum
    _sbox = (0xa3,0xd7,0x09,0x83,0xf8,0x48,0xf6,0xf4,0xb3,0x21,0x15,0x78,0x99,0xb1,0xaf,0xf9,
             0xe7,0x2d,0x4d,0x8a,0xce,0x4c,0xca,0x2e,0x52,0x95,0xd9,0x1e,0x4e,0x38,0x44,0x28,
             0x0a,0xdf,0x02,0xa0,0x17,0xf1,0x60,0x68,0x12,0xb7,0x7a,0xc3,0xe9,0xfa,0x3d,0x53,
             0x96,0x84,0x6b,0xba,0xf2,0x63,0x9a,0x19,0x7c,0xae,0xe5,0xf5,0xf7,0x16,0x6a,0xa2,
             0x39,0xb6,0x7b,0x0f,0xc1,0x93,0x81,0x1b,0xee,0xb4,0x1a,0xea,0xd0,0x91,0x2f,0xb8,
             0x55,0xb9,0xda,0x85,0x3f,0x41,0xbf,0xe0,0x5a,0x58,0x80,0x5f,0x66,0x0b,0xd8,0x90,
             0x35,0xd5,0xc0,0xa7,0x33,0x06,0x65,0x69,0x45,0x00,0x94,0x56,0x6d,0x98,0x9b,0x76,
             0x97,0xfc,0xb2,0xc2,0xb0,0xfe,0xdb,0x20,0xe1,0xeb,0xd6,0xe4,0xdd,0x47,0x4a,0x1d,
             0x42,0xed,0x9e,0x6e,0x49,0x3c,0xcd,0x43,0x27,0xd2,0x07,0xd4,0xde,0xc7,0x67,0x18,
             0x89,0xcb,0x30,0x1f,0x8d,0xc6,0x8f,0xaa,0xc8,0x74,0xdc,0xc9,0x5d,0x5c,0x31,0xa4,
             0x70,0x88,0x61,0x2c,0x9f,0x0d,0x2b,0x87,0x50,0x82,0x54,0x64,0x26,0x7d,0x03,0x40,
             0x34,0x4b,0x1c,0x73,0xd1,0xc4,0xfd,0x3b,0xcc,0xfb,0x7f,0xab,0xe6,0x3e,0x5b,0xa5,
             0xad,0x04,0x23,0x9c,0x14,0x51,0x22,0xf0,0x29,0x79,0x71,0x7e,0xff,0x8c,0x0e,0xe2,
             0x0c,0xef,0xbc,0x72,0x75,0x6f,0x37,0xa1,0xec,0xd3,0x8e,0x62,0x8b,0x86,0x10,0xe8,
             0x08,0x77,0x11,0xbe,0x92,0x4f,0x24,0xc5,0x32,0x36,0x9d,0xcf,0xf3,0xa6,0xbb,0xac,
             0x5e,0x6c,0xa9,0x13,0x57,0x25,0xb5,0xe3,0xbd,0xa8,0x3a,0x01,0x05,0x59,0x2a,0x46)

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
        version, frag, length, checksum = cls.h.unpack_from(b, 0)
        if len(b) > length:
            b = b[:length]
        return cls(
            b         = b,
            sa_ll     = sa_ll,
            version   = version,
            frag      = frag,
            length    = length,
            checksum  = checksum,
            timestamp = tornado.ioloop.IOLoop.current().time())

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
        chunks = [b[i : i + n] for i in range(0, len(b), n)]
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
            sum[i & 3] += cls._sbox[b]
        for i in range(4):
            result = (result << 8) + sum[i]
        for i in range(2):
            result = (result >> 32) + (result & 0xFFFFFFFF)
        return result

    @property
    def payload(self):
        return self.bytes[self.h.size : self.h.size + self.length]

class EtherIO:
    """
    LSOE transport protocol implementation.  Uses Tornado to read and
    write from a PF_PACKET datagram socket.  Handles fragmentation,
    reassembly, checksum, and transport layer sanity checks.

    User interface to upper layer is the .read(), .write(), and
    .close() methods, everything else is internal to the engine.
    """

    class MACDB:
        def __init__(self, macaddr, ifname):
            self.macaddr = macaddr
            self.ifname = ifname
            self.timestamp = None

    def __init__(self, cfg):
        # Do we need to do anything with multicast setup?
        self.cfg = cfg
        self.macdb = {}
        self.dgrams = {}
        self.q = tornado.queues.Queue()
        self.s = socket.socket(socket.PF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_LSOE))
        self.ioloop = tornado.ioloop.IOLoop.current()
        self.ioloop.add_handler(self.s, self._handle_read,  tornado.ioloop.IOLoop.READ)
        #self.ioloop.add_handler(self.s, self._handle_error, tornado.ioloop.IOLoop.ERROR)
        tornado.ioloop.PeriodicCallback(self._gc, self.cfg.getfloat("reassembly-timeout") * 500)
        # Might need one or more self.ioloop.spawn_callback() calls somewhere

    # Returns a Future, awaiting which returns a (bytes, macaddr, ifname) tuple
    def read(self):
        return self.q.get()

    # Put a PDU back in the read queue, to simplify session restart
    def unread(self, msg, macaddr, ifname):
        self.q.put_nowait((bytes(msg), macaddr, ifname))

    # Convert PDU to bytes, breaks into datagrams, and sends them
    def write(self, pdu, macaddr, ifname = None):
        if ifname is None:
            ifname = self.macdb[macaddr].ifname
        for d in Datagram.split_message(bytes(pdu), macaddr, ifname):
            self.s.sendto(d.bytes, d.sa_ll)

    # Tears down I/O
    def close(self):
        self.ioloop.remove_handler(self.s)

    # Internal handler for READ events
    def _handle_read(self, fd, events):
        assert fd == self.s
        pkt, sa_ll = self.s.recvfrom(ETH_DATA_LEN)
        sa_ll = SockAddrLL(*sa_ll)
        assert sa_ll.protocol == ETH_P_LSOE
        macaddr = MACAddress(sa_ll.macaddr)
        logger.debug("Received frame from MAC address %s, interface %s", macaddr, sa_ll.ifname)
        if len(pkt) < Datagram.h.size:
            logger.debug("Frame length %s too short to contain transport header, dropping", len(pkt))
            return
        if sa_ll.pkttype == PACKET_OUTGOING:
            logger.debug("Frame type flagged as our own output, dropping")
            return
        if macaddr not in self.macdb:
            logger.debug("Frame from new MAC address %s", macaddr)
            self.macdb[macaddr] = self.MACDB(macaddr, sa_ll.ifname)
        elif self.macdb[macaddr].ifname != sa_ll.ifname:
            logger.warn("MAC address %s moved from interface %s to interface %s",
                        macaddr, self.macdb[macaddr].ifname, sa_ll.ifname)
            return
        self.macdb[macaddr].timestamp = self.ioloop.time()
        d = Datagram.incoming(pkt, sa_ll)
        if not d.verify():
            logger.debug("Frame failed verification, dropping: version %s length %s (%s) checksum %s",
                         d.version, d.length, len(d.bytes), d.checksum)
            return
        try:
            rq = self.dgrams[macaddr]
        except KeyError:
            rq = self.dgrams[macaddr] = []
        rq.append(d)
        rq.sort(key = lambda d: (d.dgram_number, -d.timestamp))
        if not rq[-1].is_final:
            return None
        rq[:] = [d for i, d in enumerate(rq) if d.dgram_number >= i]
        for i, d in enumerate(rq):
            if d.dgram_number != i or d.is_final != (d is rq[-1]):
                logger.debug("Reassembly failed, waiting for more frames")
                return
        del self.dgrams[macaddr]
        logger.debug("Queuing PDU for upper layer")
        self.q.put_nowait((b"".join(d.payload for d in rq), macaddr, sa_ll.ifname))

    # Garbage collect incomplete messages and stale MAC addresses
    def _gc(self):
        logger.debug("EtherIO GC")
        now = self.ioloop.time()
        threshold = now - self.cfg.getfloat("reassembly-timeout")
        for macaddr, rq in self.dgrams.items():
            rq.sort(key = lambda d: d.timestamp)
            while rq[0].timestamp < threshold:
                del rq[0]
            if not rq:
                del self.dgrams[macaddr]
        threshold = now - self.cfg.getfloat("mac-address-cache-timeout")
        for macaddr, m in self.macdb.items():
            if m.timestamp < threshold:
                del self.macdb[macaddr]



#
# Presentation layer: Encapsulations
#

class Encapsulation:

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

    def _kwset(self, b, offset, kwargs):
        assert (b is None and offset is None) or not kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

class IPEncapsulation(Encapsulation):

    def __init__(self, b = None, offset = None, **kwargs):
        self._kwset(b, offset, kwargs)
        if b is not None:
            self.flags, self.ipaddr, self.prefixlen = self.h1.unpack_from(b, offset)

    def __len__(self):
        return self.h1.size

    def __bytes__(self):
        return self.h1.pack(self.flags, self.ipaddr, self.prefixlen)

    def __repr__(self):
        return "<{}: <{}{}> {} {}>".format(
            self.__class__.__name__,
            "P" if self.primary else "",
            "L" if self.loopback else "",
            socket.inet_ntop(socket.AF_INET if len(self.ipaddr) == 4 else socket.AF_INET6, self.ipaddr),
            self.prefixlen)

class MPLSIPEncapsulation(Encapsulation):

    # Pretend for now that we can treat an MPLS label as an opaque
    # three-octet string rather than needing get/set properties.

    h1 = struct.Struct("BB")
    h2 = struct.Struct("3s")

    def __init__(self, b = None, offset = None, **kwargs):
        self.labels = []
        self._kwset(b, offset, kwargs)
        if b is not None:
            self.flags, label_count = self.h1.unpack_from(b, offset)
            offset += self.h1.size
            for i in range(label_count):
                labels.append(self.h2.unpack_from(b, offset)[0])
                offset += self.h2.size
            self.ipaddr, self.prefixlen = self.h3.unpack_from(b, offset)

    def __len__(self):
        return self.h1.size + self.h2.size * len(self.labels) + self.h3.size

    def __bytes__(self):
        return self.h1.pack(self.flags, len(self.labels)) \
            + b"".join(self.h2.pack(label) for label in self.labels) \
            + self.h3.pack(self.ipaddr, self.prefixlen)

    def __repr__(self):
        return "<{}: <{}{}> {!r} {} {}>".format(
            self.__class__.__name__,
            "P" if self.primary else "",
            "L" if self.loopback else "",
            ["".join("{:02x}".format(l) for l in label) for label in self.labels],
            socket.inet_ntop(socket.AF_INET if len(self.ipaddr) == 4 else socket.AF_INET6, self.ipaddr),
            self.prefixlen)

class IPv4Encapsulation(IPEncapsulation):
    h1 = struct.Struct("B4sB")

class IPv6Encapsulation(IPEncapsulation):
    h1 = struct.Struct("B16sB")

class MPLSIPv4Encapsulation(MPLSIPEncapsulation):
    h3 = struct.Struct("4sB")

class MPLSIPv6Encapsulation(MPLSIPEncapsulation):
    h3 = struct.Struct("16sB")



#
# Presentation layer: PDUs
#

def register_pdu(cls):
    """
    Decorator to add a PDU class to the PDU dispatch table.
    """

    assert cls.pdu_type is not None
    assert cls.pdu_type not in cls.pdu_type_map
    cls.pdu_type_map[cls.pdu_type] = cls
    return cls

class PDUParseError(Exception):
    "Error parsing LSOE PDU."

class PDU:
    """
    Abstract base class for PDUs.
    """

    pdu_type = None
    pdu_type_map = {}

    h0 = struct.Struct("!BH")

    def __cmp__(self, other):
        return cmp(bytes(self), bytes(other))

    @classmethod
    def parse(cls, b):
        pdu_type, pdu_length = cls.h0.unpack_from(b, 0)
        if len(b) != pdu_length:
            raise PDUParseError("Unexpected PDU length: len(b) {}, pdu_length {}".format(len(b), pdu_length))
        pdu_class = cls.pdu_type_map[pdu_type]
        logger.debug("PDU class %s", pdu_class.__name__)
        self = pdu_class(b)
        #self.pdu_bytes = b
        return self

    def _b(self, b):
        return self.h0.pack(self.pdu_type, self.h0.size + len(b)) + b

    def _kwset(self, b, kwargs):
        assert b is None or not kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

@register_pdu
class HelloPDU(PDU):

    pdu_type = 0

    h1 = struct.Struct("6s")

    def __init__(self, b = None, **kwargs):
        self._kwset(b, kwargs)
        if b is not None:
            my_macaddr, = self.h1.unpack_from(b, 0)
            self.my_macaddr = MACAddress(my_macaddr)

    def __bytes__(self):
        return self._b(self.h1.pack(self.my_macaddr))

    def __repr__(self):
        return "<HelloPDU: {}>".format(self.my_macaddr)

@register_pdu
class OpenPDU(PDU):

    pdu_type = 1

    h1 = struct.Struct("4s10spH")

    def __init__(self, b = None, **kwargs):
        self._kwset(b, kwargs)
        if b is not None:
            self.nonce, self.local_id, self.attributes, self.auth_length = self.h1.unpack_from(b, 0)
            if self.auth_length != 0:
                # Implementation restriction until LSOE signature spec written
                raise PDUParserError

    def __bytes__(self):
        return self._b(self.h1.pack(self.nonce, self.local_id, self.attributes, 0))

    def __repr__(self):
        return "<OpenPDU: {} {} {}>".format(
            "".join("{:02x}".format(b) for b in self.nonce),
            ":".join("{:02x}".format(b) for b in self.local_id),
            ",".join("{:02x}".format(b) for b in self.attributes))

    @property
    def nonce(self):
        try:
            return self._nonce
        except AttributeError:
            self._nonce = os.urandom(4)
            return self._nonce

    @nonce.setter
    def nonce(self, value):
        self._nonce = value

@register_pdu
class KeepAlivePDU(PDU):

    pdu_type = 2

    def __init__(self, b = None, **kwargs):
        assert not kwargs
        if b not in (None, b""):
            raise PDUParseError("KeepAlivePDU content payload must be empty")

    def __bytes__(self):
        return self._b(b"")

    def __repr__(self):
        return "<KeepAlivePDU>"

@register_pdu
class ACKPDU(PDU):

    pdu_type = 3

    h1 = struct.Struct("B")

    def __init__(self, b = None, **kwargs):
        self._kwset(b, kwargs)
        if b is not None:
            acked_type, = self.h1.unpack_from(b, 0)
            try:
                self.acked_type = self.pdu_type_map[acked_type]
            except:
                raise PDUParseError("ACK of unknown PDU type {}".format(acked_type))
            if not issubclass(self.acked_type, (OpenPDU, EncapsulationPDU)):
                raise PDUParseError("ACK of un-ACKed PDU type {}".format(acked_type))

    def __bytes__(self):
        assert issubclass(self.acked_type, (OpenPDU, EncapsulationPDU))
        return self._b(self.h1.pack(self.acked_type.pdu_type))

    def __repr__(self):
        return "<ACKPDU: {} ({})>".format(self.acked_type.__name__, self.acked_type.pdu_type)

class EncapsulationPDU(PDU):

    h1 = struct.Struct("H")

    encap_type = None

    def __init__(self, b = None, **kwargs):
        self.encaps = []
        self._kwset(b, kwargs)
        if b is not None:
            count, = self.h1.unpack_from(b, 0)
            offset = self.h1.size
            for i in range(count):
                encaps.append(self.encap_type(b, offset))
                offset += len(encaps[-1])

    def __bytes__(self):
        return self._b(self.h1.pack(len(self.encaps)) + b"".join(bytes(encap) for encap in self.encaps))

    def __repr__(self):
        return "<{}: {!r}>".format(self.__class__.__name__, self.encaps)

@register_pdu
class IPv4EncapsulationPDU(EncapsulationPDU):
    pdu_type = 4
    encap_type = IPv4Encapsulation

@register_pdu
class IPv6EncapsulationPDU(EncapsulationPDU):
    pdu_type = 5
    encap_type = IPv6Encapsulation

@register_pdu
class MPLSIPv4EncapsulationPDU(EncapsulationPDU):
    pdu_type = 6
    encap_type = MPLSIPv4Encapsulation

@register_pdu
class MPLSIPv6EncapsulationPDU(EncapsulationPDU):
    pdu_type = 7
    encap_type = MPLSIPv6Encapsulation



#
# Network interface status and monitoring.
#

# Do we send the same encapsulation PDU to each neighbor?  If
# we're storing .send_pdu() timeouts and counters in the PDU
# object we're going to need separate copies for each session.
#
# So we need a current set of encap PDUs (all encapsulations we
# support) when we start a new session, and we need copies of a
# changed encapsulation PDU for each live session when something
# changes.  Probably best to leave copying in latter case for
# Main/Session layer since we have no idea how many sesions here,
# but only we know when something changed so we have to initiate.
# Only Main/Session knows when we have new or restart session, so
# it has to initiate.  So I guess ._handle_event() has to push,
# and Main/Session has to pull.

# pyroute2 interface is all text representations of addresess, need to
# convert to binary, just a question of where's the best place.

class Interface:

    def __init__(self, index, name, macaddr, flags):
        self.index   = index
        self.name    = name
        self.macaddr = macaddr
        self.flags   = flags
        self.ipaddrs = {}
        logger.debug("Interface %s [%s] macaddr %s flags %s",
                     self.name, self.index, self.macaddr, self.flags)

    def add_ipaddr(self, family, ipaddr, prefixlen):
        if family not in self.ipaddrs:
            self.ipaddrs[family] = []
        self.ipaddrs[family].append((ipaddr, prefixlen))
        logger.debug("Interface %s [%s] add family %s %s/%s",
                     self.name, self.index, family, ipaddr, prefixlen)

    def del_ipaddr(self, family, ipaddr, prefixlen):
        self.ipaddrs[family].remove((ipaddr, prefixlen))
        logger.debug("Interface %s [%s] del family %s %s/%s",
                     self.name, self.index, family, ipaddr, prefixlen)

    def update_flags(self, flags):
        self.flags = flags
        logger.debug("Interface %s [%s] flags %s",
                     self.name, self.index, flags)

    @property
    def is_up(self):
        return self.flags & pyroute2.netlink.rtnl.ifinfmsg.IFF_UP != 0

    @property
    def is_loopback(self):
        return self.flags & pyroute2.netlink.rtnl.ifinfmsg.IFF_LOOPBACK != 0

class Interfaces(dict):

    def __init__(self):
        logger.debug("Initializing interfaces")
        self.q = tornado.queues.Queue()
        # Race condition: open event monitor socket before doing initial scans.
        self.ip = pyroute2.RawIPRoute()
        self.ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK|
                     pyroute2.netlink.rtnl.RTNLGRP_IPV4_IFADDR|
                     pyroute2.netlink.rtnl.RTNLGRP_IPV6_IFADDR)
        with pyroute2.IPRoute() as ipr:
            for msg in ipr.get_links():
                iface = Interface(
                    index   = msg["index"],
                    flags   = msg["flags"],
                    name    = msg.get_attr("IFLA_IFNAME"),
                    macaddr = MACAddress(msg.get_attr("IFLA_ADDRESS")))
                self[iface.index] = iface
            for msg in ipr.get_addr():
                self[msg["index"]].add_ipaddr(
                    family = msg["family"],
                    ipaddr = IPAddress(msg.get_attr("IFA_ADDRESS")),
                    prefixlen = int(msg["prefixlen"]))
        tornado.ioloop.IOLoop.current().add_handler(
            self.ip.fileno(), self._handle_event, tornado.ioloop.IOLoop.READ)
        logger.debug("Done initializing interfaces")

    # Returns a Future, which returns an EncapsulationPDU
    def read_updates(self):
        return self.q.get()

    # Doc sketchy on RTM_DELLINK, may need to experiment

    def _handle_event(self, *ignored):
        logger.debug("Interface updates")
        changed = set()
        for msg in self.ip.get():
            if msg["event"] == "RTM_NEWLINK" or msg["event"] == "RTM_DELLINK":
                self[msg["index"]].update_flags(msg["flags"])
                changed.add(True)
            elif msg["event"] == "RTM_NEWADDR":
                self[msg["index"]].add_ipaddr(
                    family = msg["family"],
                    ipaddr = IPAddress(msg.get_attr("IFA_ADDRESS")),
                    prefixlen = int(msg["prefixlen"]))
                changed.add(msg["family"])
            elif msg["event"] == "RTM_DELADDR":
                self[msg["index"]].del_ipaddr(
                    family = msg["family"],
                    ipaddr = IPAddress(msg.get_attr("IFA_ADDRESS")),
                    prefixlen = int(msg["prefixlen"]))
                changed.add(msg["family"])
            else:
                logger.debug("pyroute2 WTF: %s event", msg["event"])
        if changed & {True, socket.AF_INET}:
            self.q.put_nowait(self._get_IPv4EncapsulationPDU())
        if changed & {True, socket.AF_INET6}:
            self.q.put_nowait(self._get_IPv6EncapsulationPDU())
        logger.debug("Done interface updates")

    def get_encapsulations(self):
        return (self._get_IPv4EncapsulationPDU(),
                self._get_IPv6EncapsulationPDU(),
                self._get_MPLSIPv4EncapsulationPDU(),
                self._get_MPLSIPv6EncapsulationPDU())

    def _get_IPEncapsulationPDU(self, af, cls):
        pdu = cls()
        for iface in self.values():
            for addr, prefixlen in iface.ipaddrs[af]:
                # "primary" and "loopback" fields need work
                pdu.encaps.append(cls.encap_type(
                    primary = False,
                    loopback = iface.is_loopback,
                    ipaddr = addr, prefixlen = prefixlen))
        return pdu

    def _get_IPv4EncapsulationPDU(self):
        return self._get_IPEncapsulationPDU(socket.AF_INET,  IPv4EncapsulationPDU)

    def _get_IPv6EncapsulationPDU(self):
        return self._get_IPEncapsulationPDU(socket.AF_INET6, IPv6EncapsulationPDU)

    # Implementation restriction: we don't support MPLS yet, so only empty MPLS encapsulations

    def _get_MPLSIPv4EncapsulationPDU(self):
        return MPLSIPv4EncapsulationPDU()

    def _get_MPLSIPv6EncapsulationPDU(self):
        return MPLSIPv6EncapsulationPDU()



#
# Session layer
#

class Timer:

    def __init__(self, event):
        self.now   = tornado.ioloop.IOLoop.current().time()
        self.wake  = None
        self.event = event

    def wake_after(self, delay):
        when = self.now + delay
        if self.wake is None or when < self.wake:
            self.wake = when
        return when

    def expired(self, when):
        return when <= self.now

    @tornado.gen.coroutine
    def wait(self):
        try:
            yield self.event.wait(timeout = self.wake)
        except Tornado.gen.TimeoutError:
            return False
        else:
            return True


class Session:

    def __repr__(self):
        return "<Session {} {}>".format(
            "+" if self.is_open else "",
            ":".join("{:02x}".format(b) for b in self.macaddr))

    def __init__(self, main, macaddr, ifname):
        self.main     = main
        self.macaddr  = macaddr
        self.ifname   = ifname
        self.is_open  = False
        self.dispatch = {}
        self.rxq      = {}
        self.deferred = {}
        self.dispatch = dict((k, getattr(self, "handle_" + v.__name__))
                             for k, v in PDU.pdu_type_map.items())
        logger.debug("%r init", self)

    def close(self):
        logger.debug("%r closing", self)
        if self.is_open:
            self.cleanup_rfc7752()
        self.is_open = False
        self.rxq.clear()
        self.deferred.clear()

    @property
    def is_open(self):
        return self.our_open_acked and self.peer_open_nonce is not None

    @is_open.setter
    def is_open(self, value):
        assert not value
        self.our_open_acked = False
        self.peer_open_nonce = None
        self.saw_last_keepalive = None
        self.send_next_keepalive = None

    def recv(self, msg):
        try:
            logger.debug("%r parsing received PDU", self)
            pdu = PDU.parse(msg)
        except PDUParseError as e:
            logger.warn("%r couldn't parse PDU: %s", self, e)
        else:
            logger.debug("%r received PDU %r", self, pdu)
            self.dispatch[pdu.pdu_type](pdu)

    def handle_HelloPDU(self, pdu):
        self.send_open_maybe()

    def handle_OpenPDU(self, pdu):
        assert pdu.nonce is not None
        if pdu.nonce == self.peer_open_nonce:
            logger.info("%r discarding duplicate OpenPDU: %r", self, pdu)
            return
        if self.peer_open_nonce is not None:
            # If we change .close() to destroy the session and remove
            # it from main.sessions[], we might want to salvage the
            # PDU that triggered this first, to speed up the re-open:
            #
            #self.main.io.unread(pdu, self.macaddr, self.ifname)
            #
            self.close()
        self.peer_open_nonce = pdu.nonce
        self.send_ack(pdu)
        self.send_open_maybe()

    def handle_KeepAlivePDU(self, pdu):
        if not self.is_open:
            logger.info("%r received keepalive but connection not open: %r", self, pdu)
            return
        self.saw_last_keepalive = tornado.ioloop.IOLoop.current().time()

    def handle_ACKPDU(self, pdu):
        if pdu.pdu_type not in self.rxq:
            logger.info("%r received ACK for unexpected PDU type: %r", self, pdu)
            return
        if pdu.pdu_type not in self.rxq:
            logger.info("%r received ACK with no relevant outgoing PDU: %r", self, pdu)
            return
        logger.debug("%r received ACK %r for PDU %r", self, pdu, self.rxq[pdu.pdu_type])
        del self.rxq[pdu.pdu_type]
        next_pdu = self.deferred.pop(pdu.pdu_type, None)
        if isinstance(pdu, OpenPDU):
            assert next_pdu is None
            self.our_open_acked = True
        elif next_pdu is not None:
            self.send_pdu(next_pdu)

    def handle_encapsulation(self, pdu):
        if not self.is_open:
            logger.info("%r received encapsulation but connection not open: %r", self, pdu)
            return
        self.send_ACK(pdu)
        self.report_rfc7752(pdu)

    def handle_IPv4EncapsulationPDU(self, pdu):
        self.handle_encapsulation(pdu)

    def handle_IPv6EncapsulationPDU(self, pdu):
        self.handle_encapsulation(pdu)

    def handle_MPLSIPv4EncapsulationPDU(self, pdu):
        self.handle_encapsulation(pdu)

    def handle_MPLSIPv6EncapsulationPDU(self, pdu):
        self.handle_encapsulation(pdu)

    def send_open_maybe(self, attributes = b""):
        logger.debug("%s considering whether to send OpenPDU", self)
        if self.our_open_acked or OpenPDU.pdu_type in self.rxq:
            logger.debug("%r not sending OpenPDU: our_open_acked %s, self.rxq[OpenPDU] %r",
                         self, self.our_open_acked, self.rxq.get(OpenPDU.pdu_type))
            return
        pdu = OpenPDU(local_id = self.main.local_id, attributes = attributes)
        logger.debug("%r sending %r", self, pdu)
        self.send_pdu(pdu)

    def send_ack(self, pdu):
        ack = ACKPDU(acked_type = type(pdu))
        logger.debug("%r ACKing %r with %r", self, pdu, ack)
        self.send_pdu(ack)

    def send_pdu(self, pdu):
        if isinstance(pdu, EncapsulationPDU) and pdu.pdu_type in self.rxq:
            logger.debug("%r deferring %r", self, pdu)
            self.deferred[pdu.pdu_type] = pdu
            return
        assert pdu.pdu_type not in self.rxq
        logger.debug("%r sending %r", self, pdu)
        if isinstance(pdu, (OpenPDU, EncapsulationPDU)):
            self.rxq[pdu.pdu_type] = pdu
        self.main.io.write(pdu, self.macaddr)
        if pdu.pdu_type in self.rxq:
            pdu.rxmit_interval  = self.main.cfg.getfloat("retransmit-initial-interval")
            pdu.rxmit_dropsleft = self.main.cfg.getint("retransmit-max-drop")
            pdu.rxmit_timeout   = tornado.ioloop.IOLoop.current().time() + pdu.rxmit_interval
            self.main.wake.set()
        logger.debug("%s done sending %r", self, pdu)

    def check_timeouts(self, timer):
        logger.debug("%r checking timers", self)
        for pdu in self.rxq.values():
            if not timer.expired(pdu.rxmit_timeout):
                timer.wake_after(pdu.rxmit_timeout)
                continue
            pdu.rxmit_dropsleft -= 1
            if pdu.rxmit_dropsleft <= 0:
                self.close()
                return
            if self.main.cfg.getboolean("retransmit-exponential-backoff"):
                pdu.rxmit_interval *= 2
            pdu.rxmit_timeout = timer.wake_after(pdu.rxmit_interval)
            logger.debug("%r retransmitting %r", self, pdu)
            self.main.io.write(pdu, self.macaddr)
        if self.is_open and (self.send_next_keepalive is None or timer.expired(self.send_next_keepalive)):
            self.send_pdu(KeepAlivePDU())
            self.send_next_keepalive = timer.wake_after(self.main.cfg.getfloat("keepalive-send-interval"))

    def report_rfc7752(self, pdu):
        # No real RFC 7752 code yet, so just blat to log for now
        logger.info("RFC-7752 data: %r", pdu)



#
# Main program
#

# Need something here to gc dead sessions?

class Main:

    def __init__(self):
        ap = argparse.ArgumentParser()
        ap.add_argument("-c", "--config",
                        help = "configuration file",
                        type = argparse.FileType("r"),
                        default = os.getenv("LSOE_CONFIG", None))
        ap.add_argument("-d", "--debug",
                        help = "bark more",
                        action = "store_true")
        args = ap.parse_args()

        cfg = configparser.ConfigParser()
        cfg.read_string(default_config)
        if args.config is not None:
            cfg.read_file(args.config)
        self.cfg = cfg["lsoe"]

        logging.basicConfig(level = logging.DEBUG if args.debug else logging.INFO)

        self.configure_id()

    def configure_id(self):
        # Separate method because set of text formats we might have to
        # parse is a bit open-ended.  For now we only support a hex
        # string (with optional ":", "-", or whitespace separation
        # between bytes).
        #
        # For convenience during initial testing we also support a
        # default, which may go away, leaving this as mandatory
        # configuration, thus requiring a config file.  Dunno yet.

        try:
            text = self.cfg["local-id"]

        except KeyError:
            import hashlib
            self.local_id = hashlib.md5(open("/sys/class/dmi/id/product_uuid").read().encode("ascii")).digest()[:10]

        else:
            self.local_id = bytes.fromhex(text.replace("-", ":").replace(":", " "))

    @tornado.gen.coroutine
    def main(self):
        logger.debug("Starting")
        self.sessions = {}
        self.ifs  = Interfaces()
        self.io   = EtherIO(self.cfg)
        self.wake = tornado.locks.Event()

        wait_iterator = tornado.gen.WaitIterator(
            self.receiver(), self.beacon(), self.timers(), self.interface_tracker())

        while not wait_iterator.done():
            yield wait_iterator.next()

    @tornado.gen.coroutine
    def receiver(self):
        logger.debug("Starting receiver task")
        while True:
            msg, macaddr, ifname = yield self.io.read()
            logger.debug("Received message from EtherIO layer, MAC address %s, interface %s", macaddr, ifname)
            if macaddr not in self.sessions:
                logger.debug("Creating new session for MAC address %s, interface %s", macaddr, ifname)
                self.sessions[macaddr] = Session(self, macaddr, ifname)
            logger.debug("Dispatching to session %r for MAC address %s, interface %s", self.sessions[macaddr], macaddr, ifname)
            self.sessions[macaddr].recv(msg)

    @tornado.gen.coroutine
    def beacon(self):
        logger.debug("Starting beacon task")
        while True:
            logger.debug("Running beacon task")
            for iface in self.ifs.values():
                if iface.is_loopback:
                    logger.debug("Skipping Hello on loopback interface %s", iface.name)
                    continue
                if not iface.is_up:
                    logger.debug("Skipping Hello on down interface %s", iface.name)
                    continue
                pdu = HelloPDU(my_macaddr = iface.macaddr)
                logger.debug("Multicasting %r to %s", pdu, iface.name)
                self.io.write(pdu, LSOE_HELLO_MACADDR, iface.name)
            logger.debug("Sleeping beacon task")
            yield tornado.gen.sleep(self.cfg.getfloat("hello-interval"))

    @tornado.gen.coroutine
    def timers(self):
        logger.debug("Starting timers task")
        while True:
            timer = Timer(self.wake)
            for session in self.sessions.values():
                session.check_timeouts(timer)
            yield timer.wait()
            self.wake.clear()

    @tornado.gen.coroutine
    def interface_tracker(self):
        logger.debug("Starting interface_tracker task")
        while True:
            pdu = yield self.ifs.read_updates()
            for session in self.sessions.values():
                if session.is_open:
                    session.send_pdu(copy.copy(pdu))

if __name__ == "__main__":
    try:
        tornado.ioloop.IOLoop.current().run_sync(Main().main)
    except SystemExit:
        raise
    except:
        logger.exception("Unhandled exception")
        sys.exit(1)
