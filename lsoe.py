#!/usr/bin/env python3

# Copyright (c) 2019 Arrcus, Inc.
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Initial implementation of Link State Over Ethernet (LSOE).
https://datatracker.ietf.org/doc/draft-ietf-lsvr-lsoe/

This is a work in progress, neither the specification nor the code are
really stable yet.
"""

# Default configuration, here for ease of reference.

default_config = '''

# Default configuration values, expressed in the same syntax
# as the optional configuration file. Section name "[lsoe]" is mandatory.
# All times are expressed in seconds, so 0.1 is 100 milliseconds, et cetera.

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
import enum
import time
import socket
import struct
import logging
import argparse
import textwrap
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

class MaybeEnum(enum.Enum):
    @classmethod
    def maybe(cls, value):
        "Cast to enum class if we can."
        for member in cls:
            if value == member.value:
                return member
        return value

@enum.unique
class LSOEErrorType(MaybeEnum):
    NO_ERROR = 0                # No error occurred, code and hint MUST be zero
    WARNING  = 1                # Something bad happened but we can continue
    RESTART  = 2                # Something bad happened and required session restart
    HOPELESS = 3                # Something bad happened and restart won't help, call an operator

@enum.unique
class LSOEErrorCode(MaybeEnum):
    LINK_ADDRESSING_CONFLICT            = 1
    AUTHORIZATION_FAILURE_IN_OPEN       = 2

# Ethernet physical layer contstants from linux/if_ether.h, with additions.

ETH_DATA_LEN    = 1500          # Max. octets in payload
ETH_FRAME_LEN   = 1514          # Max. octets in frame sans FCS

ETH_P_IEEE_EXP1 = 0x8885        # "Local Experimental EtherType 1"
ETH_P_IEEE_EXP2 = 0x8886        # "Local Experimental EtherType 2"
ETH_P_LSOE      = ETH_P_IEEE_EXP1

# MAC address to which we should send LSOE Hello PDUs.
# Some archived email discussing this, I think.

LSOE_HELLO_MACADDR = b"\xFF\xFF\xFF\xFF\xFF\xFF" # Figure out real value...

# Linux PF_PACKET API constants from linux/if_packet.h.

class PFPacketType(enum.IntEnum):
    PACKET_HOST      = 0
    PACKET_BROADCAST = 1
    PACKET_MULTICAST = 2
    PACKET_OTHERHOST = 3
    PACKET_OUTGOING  = 4

# Order here must match the address tuples generated by the socket module for PF_PACKET
SockAddrLL = collections.namedtuple("Sockaddr_LL",
                                    ("ifname", "protocol", "pkttype", "arptype", "macaddr"))

# Logging setup
logger = logging.getLogger(os.path.splitext(os.path.basename(sys.argv[0]))[0])



#
# Low-level data types
#

class MACAddress(bytes):
    "MAC address -- 6 bytes, with pretty formatting."

    def __new__(cls, thing):
        if isinstance(thing, str):
            thing = bytes(int(i, 16) for i in thing.replace("-", ":").split(":"))
        assert isinstance(thing, bytes) and len(thing) == 6
        return bytes.__new__(cls, thing)

    def __str__(self):
        return ":".join("{:02x}".format(b) for b in self)

class IPAddress(bytes):
    "IP address -- 4 or 16 bytes, with pretty formatting."

    def __new__(cls, thing):
        if isinstance(thing, str):
            thing = socket.inet_pton(socket.AF_INET6 if ":" in thing else socket.AF_INET, thing)
        assert isinstance(thing, bytes) and len(thing) in (4, 16)
        return bytes.__new__(cls, thing)

    def __str__(self):
        return socket.inet_ntop(self.af, self)

    @property
    def af(self):
        "Address Family"
        return socket.AF_INET if len(self) == 4 else socket.AF_INET6

# We represent time as Python's time.time() function does: a Python
# float representing time in seconds, so .1 is 100 milliseconds, et
# cetera.  Per the Tornado documentation, we use Tornado's .time()
# function rather than using Python's directly.

def current_time():
    return tornado.ioloop.IOLoop.current().time()



#
# Transport layer
#

class Datagram:
    """
    LSOE transport protocol datagram.
    """

    h = struct.Struct("!BBHL")
    LAST_FLAG = 0x80

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
        "Construct datagram for incoming data."
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
            timestamp = current_time())

    def verify(self):
        "Verify content of an incoming datagram."
        if self.version != LSOE_VERSION:
            logger.debug("Datagram verification failed: bad version: expected %s, got %s",  LSOE_VERSION, self.version)
            return False
        if len(self.bytes) != self.length:
            logger.debug("Datagram verification failed: bad length: expected %s, got %s", len(self.bytes), self.length)
            return False
        if self.checksum != self._sbox_checksum(self.bytes[self.h.size:], self.frag, self.length):
            logger.debug("Datagram verification failed: bad checksum")
            return False
        return True

    @classmethod
    def outgoing(cls, b, sa_ll, frag, last):
        "Construct datagram for outgoing data."
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
        "Split bytes of a PDU into datagrams."
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
        "Is this the last datragram in a PDU?"
        return self.frag & self.LAST_FLAG != 0

    @property
    def dgram_number(self):
        "Datagram number (zero-based) within a PDU."
        return self.frag & ~self.LAST_FLAG

    @classmethod
    def _sbox_checksum(cls, b, frag, length, version = LSOE_VERSION):
        "Compute the LSOE S-box checksum."
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
        "Payload (upper level content) from a datagram."
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
        "Entry in EtherIO's internal map of MAC addresses to interfaces."
        def __init__(self, macaddr, ifname):
            self.macaddr = macaddr
            self.ifname = ifname
            self.timestamp = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.macdb = {}
        self.dgrams = {}
        self.q = tornado.queues.Queue()
        self.s = socket.socket(socket.PF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_LSOE))
        self.ioloop = tornado.ioloop.IOLoop.current()
        self.ioloop.add_handler(self.s, self._handle_read,  tornado.ioloop.IOLoop.READ)
        tornado.ioloop.PeriodicCallback(self._gc, self.cfg.getfloat("reassembly-timeout") * 500)

    # Not really a coroutine, just plays one on TV
    def read(self):
        "Coroutine returning (bytes, macaddr, ifname) tuple."
        return self.q.get()

    def unread(self, msg, macaddr, ifname):
        "Stuff a PDU back into the read queue, to simplify session restart."
        self.q.put_nowait((bytes(msg), macaddr, ifname))

    def write(self, pdu, macaddr, ifname = None):
        "Convert a PDU to bytes, breaks into datagrams, and send them."
        if ifname is None:
            ifname = self.macdb[macaddr].ifname
        for d in Datagram.split_message(bytes(pdu), macaddr, ifname):
            self.s.sendto(d.bytes, d.sa_ll)

    def close(self):
        "Tear down this EtherIO instance."
        self.ioloop.remove_handler(self.s)

    def _handle_read(self, fd, events):
        "Internal handler for READ events."
        assert fd == self.s
        pkt, sa_ll = self.s.recvfrom(ETH_DATA_LEN)
        sa_ll = SockAddrLL(*sa_ll)
        assert sa_ll.protocol == ETH_P_LSOE
        macaddr = MACAddress(sa_ll.macaddr)
        logger.debug("Received frame from MAC address %s, interface %s, length %d", macaddr, sa_ll.ifname, len(pkt))
        if len(pkt) < Datagram.h.size or sa_ll.pkttype == PFPacketType.PACKET_OUTGOING:
            logger.debug("Frame too short or flagged as our own output, dropping")
            return
        if macaddr not in self.macdb:
            logger.debug("Frame from new MAC address %s", macaddr)
            self.macdb[macaddr] = self.MACDB(macaddr, sa_ll.ifname)
        elif self.macdb[macaddr].ifname != sa_ll.ifname:
            logger.warn("MAC address %s moved from interface %s to interface %s, dropping",
                        macaddr, self.macdb[macaddr].ifname, sa_ll.ifname)
            return
        self.macdb[macaddr].timestamp = current_time()
        d = Datagram.incoming(pkt, sa_ll)
        if not d.verify():
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
                logger.debug("PDU reassembly failed, waiting for more frames")
                return
        del self.dgrams[macaddr]
        self.q.put_nowait((b"".join(d.payload for d in rq), macaddr, sa_ll.ifname))

    def _gc(self):
        "Internal garbage collector for incomplete messages and stale MAC addresses."
        logger.debug("EtherIO GC")
        now = current_time()
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
# Presentation layer: Encapsulations (payload of the encapsulation PDU classes)
#

class Encapsulation:
    """
    Abstract base for encapsulation classes.
    """

    _primary_flag  = 0x80       # Bit mask for the "primary" flag
    _loopback_flag = 0x40       # Bit mask for the "loopback" flag
    flags = 0                   # Default flags byte to zero

    # Property methods to make the flags act like Python booleans

    def _flag_getter(self, flag):
        return self.flags & flag != 0

    def _flag_setter(self, flag, value):
        if value:
            self.flags |= flag
        else:
            self.flags &= ~flag

    primary = property(
        lambda self:    self._flag_getter(self._primary_flag),
        lambda self, v: self._flag_setter(self._primary_flag, v))

    loopback = property(
        lambda self:    self._flag_getter(self._loopback_flag),
        lambda self, v: self._flag_setter(self._loopback_flag, v))

    def _kwset(self, b, offset, kwargs):
        "Keyword-based initialization."
        assert (b is None and offset is None) or not kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

class IPEncapsulation(Encapsulation):
    """
    Base for IP encapsulation classes.
    """

    def __init__(self, b = None, offset = None, **kwargs):
        self._kwset(b, offset, kwargs)
        if b is not None:
            self.flags, self.ipaddr, self.prefixlen = self.h1.unpack_from(b, offset)

    def __len__(self):
        return self.h1.size

    def __bytes__(self):
        return self.h1.pack(self.flags, self.ipaddr, self.prefixlen)

    def __repr__(self):
        return "<{}: {}{}{}/{}>".format(
            self.__class__.__name__,
            "<P> " if self.primary else "",
            "<L> " if self.loopback else "",
            socket.inet_ntop(socket.AF_INET if len(self.ipaddr) == 4 else socket.AF_INET6, self.ipaddr),
            self.prefixlen)

class MPLSIPEncapsulation(Encapsulation):
    """
    Base for MPLS encapsulation classes.

    For now we pretend that we can treat an MPLS label as an opaque
    three-octet string rather than needing yet another class with
    get/set properties.
    """

    h1 = struct.Struct("!BB")
    h2 = struct.Struct("!3s")

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
    "IPv4 encapsulation."
    h1 = struct.Struct("!B4sB")

class IPv6Encapsulation(IPEncapsulation):
    "IPv6 encapsulation."
    h1 = struct.Struct("!B16sB")

class MPLSIPv4Encapsulation(MPLSIPEncapsulation):
    "MPLS IPv4 encapsulation."
    h3 = struct.Struct("!4sB")

class MPLSIPv6Encapsulation(MPLSIPEncapsulation):
    "MPLS IPv6 encapsulation."
    h3 = struct.Struct("!16sB")



#
# Presentation layer: PDUs
#

def register_unacked_pdu(cls):
    """
    Decorator to register a PDU class in the PDU dispatch table.
    """

    assert cls.pdu_type is not None
    assert cls.pdu_type not in cls.pdu_type_map
    cls.pdu_type_map[cls.pdu_type] = cls
    return cls

def register_acked_pdu(cls):
    """
    Decorator to register a PDU class in the PDU dispatch table
    and mark it as a class for which we expect to see ACKs.
    """

    cls = register_unacked_pdu(cls)
    PDU.acked_pdu_classes = PDU.acked_pdu_classes + (cls,)
    return cls

class PDUParseError(Exception):
    "Error parsing LSOE PDU."

class PDU:
    """
    Abstract base class for PDUs.
    """

    pdu_type = None             # Each subclass must override this
    pdu_type_map = {}           # Class data: map pdu_number -> PDU subclass, built up by @register_*_pdu decorators
    acked_pdu_classes = ()      # Class data: tuple of ACKed types, built up by @register_acked_pdu decorator

    h0 = struct.Struct("!BH")

    def __eq__(self, other): return bytes(self) == bytes(other)
    def __ne__(self, other): return bytes(self) != bytes(other)
    def __lt__(self, other): return bytes(self) <  bytes(other)
    def __gt__(self, other): return bytes(self) >  bytes(other)
    def __le__(self, other): return bytes(self) <= bytes(other)
    def __ge__(self, other): return bytes(self) >= bytes(other)

    @classmethod
    def parse(cls, b):
        "Parse an incoming PDU."
        pdu_type, pdu_length = cls.h0.unpack_from(b, 0)
        if len(b) != pdu_length:
            raise PDUParseError("Unexpected PDU length: len(b) {}, pdu_length {}".format(len(b), pdu_length))
        return cls.pdu_type_map[pdu_type](b)

    def _b(self, b):
        "Construct outermost TLV wrapper of a PDU."
        return self.h0.pack(self.pdu_type, self.h0.size + len(b)) + b

    def _kwset(self, b, kwargs):
        "Keyword-based initialization."
        assert b is None or not kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

@register_unacked_pdu
class HelloPDU(PDU):
    "HELLO PDU."

    pdu_type = 0

    h1 = struct.Struct("!6s")

    def __init__(self, b = None, **kwargs):
        self._kwset(b, kwargs)
        if b is not None:
            my_macaddr, = self.h1.unpack_from(b, self.h0.size)
            self.my_macaddr = MACAddress(my_macaddr)

    def __bytes__(self):
        return self._b(self.h1.pack(self.my_macaddr))

    def __repr__(self):
        return "<HelloPDU: {}>".format(self.my_macaddr)

@register_acked_pdu
class OpenPDU(PDU):
    "OPEN PDU."

    pdu_type = 1

    h1 = struct.Struct("!4s10sB")
    h2 = struct.Struct("!H")

    def __init__(self, b = None, **kwargs):
        self._kwset(b, kwargs)
        if b is not None:
            self.nonce, self.local_id, attribute_length = self.h1.unpack_from(b, self.h0.size)
            self.attributes = b[self.h0.size + self.h1.size : self.h0.size + self.h1.size + attribute_length]
            self.auth_length, = self.h2.unpack_from(b, self.h0.size + self.h1.size + attribute_length)
            if self.auth_length != 0:
                # Implementation restriction until LSOE signature spec written
                raise PDUParseError("Received OpenPDU has non-zero auth_length {}".format(self.auth_length))

    def __bytes__(self):
        return self._b(self.h1.pack(self.nonce, self.local_id, len(self.attributes)) +
                       self.attributes +
                       self.h2.pack(0))

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

@register_unacked_pdu
class KeepAlivePDU(PDU):
    "KEEPALIVE PDU."

    pdu_type = 2

    def __init__(self, b = None, **kwargs):
        assert not kwargs
        if b is not None and len(b) != self.h0.size:
            raise PDUParseError("KeepAlivePDU content payload must be empty")

    def __bytes__(self):
        return self._b(b"")

    def __repr__(self):
        return "<KeepAlivePDU>"

@register_unacked_pdu
class ACKPDU(PDU):
    "ACK PDU."

    pdu_type = 4

    h1 = struct.Struct("!BHH")

    _type_mask = 0xF000
    _code_mask = 0x0FFF

    _type_shift = (_type_mask & (~_type_mask + 1)).bit_length() - 1
    _code_shift = (_code_mask & (~_code_mask + 1)).bit_length() - 1

    _error_type_code = LSOEErrorType.NO_ERROR.value << _type_shift

    vendor_hook = None

    def __init__(self, b = None, **kwargs):
        self._kwset(b, kwargs)
        if b is not None:
            self.ack_type, self._error_type_code, self.error_hint = self.h1.unpack_from(b, self.h0.size)
            if self.ack_type not in self.pdu_type_map:
                raise PDUParseError("ACK of unknown PDU type {}".format(self.ack_type))
            if not issubclass(self.pdu_type_map[self.ack_type], PDU.acked_pdu_classes):
                raise PDUParseError("ACK of un-ACKed PDU type {}".format(self.pdu_type_map[self.ack_type]))
            if not isinstance(self.error_type, LSOEErrorType):
                raise PDUParseError("ACK with unknown error type: {!r}".format(self))
            elif self.error_type is LSOEErrorType.NO_ERROR and (self.error_code != 0 or self.error_hint != 0):
                raise PDUParseError("ACK with non-zero value in must-be-zero field: {!r}".format(self))
            elif self.error_type is not LSOEErrorType.NO_ERROR and not isinstance(self.error_code, LSOEErrorCode):
                raise PDUParseError("ACK with unknown error code: {!r}".format(self))

    def __bytes__(self):
        assert issubclass(self.pdu_type_map[self.ack_type], PDU.acked_pdu_classes)
        if self.error_type is LSOEErrorType.NO_ERROR:
            assert self.error_code == 0 and self.error_hint == 0
        else:
            assert isinstance(self.error_type, LSOEErrorType) and isinstance(self.error_code, LSOEErrorCode)
        return self._b(self.h1.pack(self.ack_type, self._error_type_code, self.error_hint))

    def __repr__(self):
        return "<ACKPDU: {name} ({self.ack_type}) {self.error_type!r} {self.error_code!r} {self.error_hint}>".format(
            self = self,
            name = self.pdu_type_map[self.ack_type].__name__)

    def _error_getter(self, cls, mask, shift):
        return cls.maybe((self._error_type_code & mask) >> shift)

    def _error_setter(self, cls, mask, shift, value):
        assert isinstance(value, cls)
        value = value.value << shift
        assert value & ~mask == 0
        self._error_type_code &= ~mask
        self._error_type_code |= value

    error_type = property(
        lambda self:    self._error_getter(LSOEErrorType, self._type_mask, self._type_shift),
        lambda self, v: self._error_setter(LSOEErrorType, self._type_mask, self._type_shift, v))

    error_code = property(
        lambda self:    self._error_getter(LSOEErrorCode, self._code_mask, self._code_shift),
        lambda self, v: self._error_setter(LSOEErrorCode, self._code_mask, self._code_shift, v))

class EncapsulationPDU(PDU):
    """"
    Base for encapsulation PDU classes.

    All of this are basically just a list of zero or more instances
    of the corresponding encapsulation class.
    """

    h1 = struct.Struct("!H")

    encap_type = None

    def __init__(self, b = None, **kwargs):
        self.encaps = []
        self._kwset(b, kwargs)
        if b is not None:
            count, = self.h1.unpack_from(b, self.h0.size)
            offset = self.h0.size + self.h1.size
            for i in range(count):
                self.encaps.append(self.encap_type(b, offset))
                offset += len(self.encaps[-1])

    def __bytes__(self):
        return self._b(self.h1.pack(len(self.encaps)) + b"".join(bytes(encap) for encap in self.encaps))

    def __repr__(self):
        return "<{}: {!r}>".format(self.__class__.__name__, self.encaps)

@register_acked_pdu
class IPv4EncapsulationPDU(EncapsulationPDU):
    "IPv4 encapsulation PDU."
    pdu_type = 5
    encap_type = IPv4Encapsulation

@register_acked_pdu
class IPv6EncapsulationPDU(EncapsulationPDU):
    "IPv6 encapsulation PDU."
    pdu_type = 6
    encap_type = IPv6Encapsulation

@register_acked_pdu
class MPLSIPv4EncapsulationPDU(EncapsulationPDU):
    "MPLS IPv4 encapsulation PDU."
    pdu_type = 7
    encap_type = MPLSIPv4Encapsulation

@register_acked_pdu
class MPLSIPv6EncapsulationPDU(EncapsulationPDU):
    "MPLS IPv6 encapsulation PDU."
    pdu_type = 8
    encap_type = MPLSIPv6Encapsulation

@register_acked_pdu
class VendorPDU(PDU):
    """
    VENDOR PDU.

    You can hook into the receive process for VENDOR PDUs carrying
    particular enterprise numbers by creating entries in
    VendorPDU.vendor_dispatch[]: the key should be a Python int
    (the enterprise number), while the value should be a Python
    callable which expects to receive the session instance and
    the PDU instance as arguments.
    """

    pdu_type = 255

    h1 = struct.Struct("!L")

    vendor_dispatch = {}

    def __init__(self, b = None, **kwargs):
        self.enterprise_data = b""
        self._kwset(b, kwargs)
        if b is not None:
            self.enterprise_number, = self.h1.unpack_from(b, self.h0.size)
            self.enterprise_data = b[self.h0.size + self.h1.size :]

    def __bytes__(self):
        return self._b(self.h1.pack(self.enterprise_number) + self.enterprise_data)

    def __repr__(self):
        return "<VendorPDU: {}>".format(self.enterprise_number)



#
# Network interface status and monitoring.
#

class Interface:
    "A network interface as reported by PyRoute2."

    def __init__(self, index, name, macaddr, flags):
        self.index   = index
        self.name    = name
        self.macaddr = macaddr
        self.flags   = flags
        self.ipaddrs = {}
        logger.debug("Interface %s [%s] macaddr %s flags %s",
                     self.name, self.index, self.macaddr, self.flags)

    def add_ipaddr(self, af, ipaddr, prefixlen):
        "Record an IP address for this interface."
        if af not in self.ipaddrs:
            self.ipaddrs[af] = []
        self.ipaddrs[af].append((ipaddr, prefixlen))
        logger.debug("Interface %s [%s] add af %s %s/%s",
                     self.name, self.index, af, ipaddr, prefixlen)

    def del_ipaddr(self, af, ipaddr, prefixlen):
        "Forget an IP address for this interface."
        self.ipaddrs[af].remove((ipaddr, prefixlen))
        logger.debug("Interface %s [%s] del af %s %s/%s",
                     self.name, self.index, af, ipaddr, prefixlen)

    def update_flags(self, flags):
        "Update this interface's flags."
        self.flags = flags
        logger.debug("Interface %s [%s] flags %s",
                     self.name, self.index, flags)

    @property
    def is_up(self):
        "Is this interface up?"
        return self.flags & pyroute2.netlink.rtnl.ifinfmsg.IFF_UP != 0

    @property
    def is_loopback(self):
        "Is this a loopback interface?"
        return self.flags & pyroute2.netlink.rtnl.ifinfmsg.IFF_LOOPBACK != 0

class Interfaces(dict):
    """
    Interface database.  This hooks into PyRoute2 with a callback function
    so that we get update messages when interfaces or their addresses are
    added or deleted.
    """

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
                    af        = msg["family"],
                    ipaddr    = IPAddress(msg.get_attr("IFA_ADDRESS")),
                    prefixlen = int(msg["prefixlen"]))
        tornado.ioloop.IOLoop.current().add_handler(
            self.ip.fileno(), self._handle_event, tornado.ioloop.IOLoop.READ)
        logger.debug("Done initializing interfaces")

    # Not really a coroutine, just plays one on TV
    def read_updates(self):
        "Coroutine returning an EncapsulationPDU to be sent to all sessions."
        return self.q.get()

    # Documentation on RTM_DELLINK is sketchy, we may need to
    # experiment to figure out how that really works.

    def _handle_event(self, *ignored):
        "Internal callback handler to receive change events from PyRoute2."
        logger.debug("Interface updates")
        changed = set()
        for msg in self.ip.get():
            if msg["event"] == "RTM_NEWLINK" or msg["event"] == "RTM_DELLINK":
                self[msg["index"]].update_flags(msg["flags"])
                changed.add(True)
            elif msg["event"] == "RTM_NEWADDR":
                self[msg["index"]].add_ipaddr(
                    af        = msg["family"],
                    ipaddr    = IPAddress(msg.get_attr("IFA_ADDRESS")),
                    prefixlen = int(msg["prefixlen"]))
                changed.add(msg["family"])
            elif msg["event"] == "RTM_DELADDR":
                self[msg["index"]].del_ipaddr(
                    af        = msg["family"],
                    ipaddr    = IPAddress(msg.get_attr("IFA_ADDRESS")),
                    prefixlen = int(msg["prefixlen"]))
                changed.add(msg["family"])
            else:
                logger.debug("pyroute2 WTF: %r", msg)
        if changed & {True, socket.AF_INET}:
            self.q.put_nowait(self._get_IPv4EncapsulationPDU())
        if changed & {True, socket.AF_INET6}:
            self.q.put_nowait(self._get_IPv6EncapsulationPDU())
        logger.debug("Done interface updates")

    def get_encapsulations(self):
        "Get a current set of encapsulation PDUs to send on session open."
        return (self._get_IPv4EncapsulationPDU(),
                self._get_IPv6EncapsulationPDU(),
                self._get_MPLSIPv4EncapsulationPDU(),
                self._get_MPLSIPv6EncapsulationPDU())

    def _get_IPEncapsulationPDU(self, af, cls):
        """
        Common code to construct encapsulation PDUs from this database.

        Figuring out what to put in the "primary" and "loopback" fields
        needs work.  "Primary" probably needs to come from configuration.
        Loopback is currently set based on the IFF_LOOPBACK flag, which
        may be good enough or may require a configuration override.

        We may also need configuration to suppress interfaces we don't
        really want to expose (eg, docker0).
        """

        pdu = cls()
        for iface in self.values():
            if af in iface.ipaddrs:
                for addr, prefixlen in iface.ipaddrs[af]:
                    # "primary" and "loopback" fields need work
                    pdu.encaps.append(cls.encap_type(
                        primary = False,
                        loopback = iface.is_loopback,
                        ipaddr = addr, prefixlen = prefixlen))
        return pdu

    def _get_IPv4EncapsulationPDU(self):
        "Generate an IPv4 encapsulation from the database."
        return self._get_IPEncapsulationPDU(socket.AF_INET,  IPv4EncapsulationPDU)

    def _get_IPv6EncapsulationPDU(self):
        "Generate an IPv6 encapsulation from the database."
        return self._get_IPEncapsulationPDU(socket.AF_INET6, IPv6EncapsulationPDU)

    # Implementation restriction: we don't support MPLS yet, so we
    # only generate empty MPLS encapsulation PDUs.

    def _get_MPLSIPv4EncapsulationPDU(self):
        "Generate an (empty) MPLS IPv4 encapsulation from the database."
        return MPLSIPv4EncapsulationPDU()

    def _get_MPLSIPv6EncapsulationPDU(self):
        "Generate an (empty) MPLS IPv6 encapsulation from the database."
        return MPLSIPv6EncapsulationPDU()



#
# Session layer
#

class Timer:
    """
    Simplistic timer object to multiplex an arbitrary number of
    timeout events by waiting on a single Event object with a timeout.

    Basic model here is that one creates a new Timer() at the start of
    each pass through the event loop, uses it to schedule and check
    all the individual timeout events while simultaneously determining
    what the earliest wakeup time is for the entire set of timeouts,
    then go to sleep for that long.  If no timers are set at all, we
    just wait for an external signal to the Event (which occurs when,
    eg, a Session stuffs a new PDU into a retransmission queue.

    This is probably not the most efficient way to do this, but it's
    simple, and it's not all that expensive, since all the checks
    involved are simple numeric comparisons.  If this turns out to be
    a bottleneck, we'll write something better, but for now the
    simplicity of this approach wins over premature optimization.
    """


    def __init__(self, event):
        self.now   = current_time()
        self.wake  = None
        self.event = event
        logger.debug("%r initialized", self)

    def __repr__(self):
        return "<Timer now {} ({}) wake {} ({})>".format(
            self.now,
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.now)),
            self.wake,
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.wake)) if self.wake else "")

    # The assertions in .wake_after() and .check_expired() are to
    # catch accidental mis-use of one of these methods in place of the
    # other.  They should be OK as long as Tornado's underlying time
    # source is time.time(), but might break with time.monotonoic().
    # So shake the bugs out of this code before trying that.

    def wake_after(self, delay):
        "Schedule wakeup after specified delay (must be a relative time)."
        assert delay >= 0 and delay < self.now
        when = self.now + delay
        if self.wake is None or when < self.wake:
            self.wake = when
        logger.debug("%r wake_after(%s) %s", self, delay, when)
        return when

    def check_expired(self, when):
        "Check whether an absolute time has passed, schedule wakeup if not."
        assert when * 2 > self.now
        expired = when <= self.now
        if not expired and (self.wake is None or when < self.wake):
            self.wake = when
        logger.debug("%r check_expired(%s) %s", self, when, expired)
        return expired

    @tornado.gen.coroutine
    def wait(self):
        "Wait for event or first timeout."
        try:
            logger.debug("%r sleeping", self)
            yield self.event.wait(timeout = self.wake)
        except tornado.gen.TimeoutError:
            logger.debug("%r timer wakeup", self)
            return False
        else:
            logger.debug("%r event wakeup", self)
            return True


class Session:
    """
    LSOE session, between us and one neighbor.
    Encapsulates state machine and most PDU processing.

    At present we have no RFC 7752 implementation available,
    so we just log the data we would otherwise be sending
    northbound via RFC 7752.
    """

    def __repr__(self):
        return "<Session {} {} {}>".format(
            "+" if self.is_open else "-",
            self.ifname,
            ":".join("{:02x}".format(b) for b in self.macaddr))

    def __init__(self, main, macaddr, ifname):
        self.main                = main
        self.macaddr             = macaddr
        self.ifname              = ifname
        self.dispatch            = {}
        self.rxq                 = {}
        self.deferred            = {}
        self.our_open_acked      = False
        self.peer_open_nonce     = None
        self.saw_last_keepalive  = None
        self.send_next_keepalive = None
        self.dispatch = dict((k, getattr(self, "handle_" + v.__name__))
                             for k, v in PDU.pdu_type_map.items())
        logger.debug("%r init", self)

    def close(self):
        "Close and delete this session."
        logger.debug("%r closing", self)
        if self.is_open:
            self.cleanup_rfc7752()
        self.our_open_acked  = False
        self.peer_open_nonce = None
        del self.main.sessions[self.macaddr]

    @property
    def is_open(self):
        "Has this session reached the Open state?"
        return self.our_open_acked and self.peer_open_nonce is not None

    def recv(self, msg):
        "Receive and process one PDU."
        try:
            pdu = PDU.parse(msg)
        except PDUParseError as e:
            logger.warn("%r couldn't parse PDU: %s", self, e)
        else:
            logger.debug("%r received PDU %r", self, pdu)
            self.dispatch[pdu.pdu_type](pdu)

    def handle_HelloPDU(self, pdu):
        "Process a HELLO PDU -- triggers start of Open dance if haven't already."
        self.send_open_maybe()

    def handle_OpenPDU(self, pdu):
        """
        Process an OPEN PDU.

        This can reset an existing open session if the nonce doesn't match,
        because changed nonce is assumed to mean peer has restarted.

        This may trigger us to send an OPEN PDU back to the peer, if
        we haven't already done that.

        This may cause us to reach the Open state, if peer has already
        ACKed an OPEN PDU we sent.
        """

        assert pdu.nonce is not None
        if pdu.nonce == self.peer_open_nonce:
            logger.info("%r discarding duplicate OpenPDU: %r", self, pdu)
            return
        if self.peer_open_nonce is not None:
            self.main.io.unread(pdu, self.macaddr, self.ifname)
            return self.close()
        self.peer_open_nonce = pdu.nonce
        self.send_ack(pdu)
        self.send_open_maybe()
        self.saw_keepalive()

    def handle_KeepAlivePDU(self, pdu):
        "Process a KEEPALIVE PDU -- update timer or trigger OPEN, as appropriate."
        if self.is_open:
            self.saw_keepalive()
        else:
            self.send_open_maybe()

    def handle_ACKPDU(self, pdu):
        "Process an ACK PDU, which may cause the session to reach the Open state."
        if pdu.ack_type not in self.rxq:
            logger.info("%r received ACK with no relevant outgoing PDU: %r", self, pdu)
            return
        logger.debug("%r received ACK %r for PDU %r", self, pdu, self.rxq[pdu.ack_type])
        del self.rxq[pdu.ack_type]
        next_pdu = self.deferred.pop(pdu.ack_type, None)
        if pdu.ack_type == OpenPDU.pdu_type:
            assert next_pdu is None
            self.our_open_acked = True
            self.saw_keepalive()
        elif next_pdu is not None:
            self.send_pdu(next_pdu)
            if pdu.vendor_hook is not None:
                try:
                    pdu.vendor_hook(self, pdu)
                except:
                    logger.exception("%r unhandled exception running %r on %r", self, pdu.vendor_hook, pdu)

    def handle_encapsulation(self, pdu):
        "Common code for processing encapsulation PDUs."
        if not self.is_open:
            logger.info("%r received encapsulation but connection not open: %r", self, pdu)
            return
        self.send_ack(pdu)
        self.report_rfc7752(pdu)

    def handle_IPv4EncapsulationPDU(self, pdu):
        "Handle an IPv4 encapsulation PDU."
        self.handle_encapsulation(pdu)

    def handle_IPv6EncapsulationPDU(self, pdu):
        "Handle an IPv6 encapsulation PDU."
        self.handle_encapsulation(pdu)

    def handle_MPLSIPv4EncapsulationPDU(self, pdu):
        "Handle an MPLS IPv4 encapsulation PDU."
        self.handle_encapsulation(pdu)

    def handle_MPLSIPv6EncapsulationPDU(self, pdu):
        "Handle an MPLS IPv6 encapsulation PDU."
        self.handle_encapsulation(pdu)

    def handle_VendorPDU(self, pdu):
        "Handle a VENDOR PDU -- may dispatch to vendor-supplied hook."
        if not self.is_open:
            logger.info("%r received VendorPDU but connection not open: %r", self, pdu)
            return
        self.send_ack(pdu)
        if self.enterprise_number in self.vendor_dispatch:
            try:
                self.vendor_dispatch[self.enterprise_number](self, pdu)
            except:
                logger.exception("%r unhandled exception from %r processing %r",
                                 self, self.vendor_dispatch[self.enterprise_number], pdu)

    def saw_keepalive(self):
        "Record keepalive timestamp, if and only if session is open."
        if self.is_open:
            self.saw_last_keepalive = current_time()

    def send_open_maybe(self, attributes = b""):
        "Send an OPEN PDU if appropriate in our session current state."
        if self.our_open_acked:
            logger.debug("%r not sending OpenPDU because our Open has already been ACKed", self)
        elif OpenPDU.pdu_type in self.rxq:
            logger.debug("%r not sending OpenPDU because we're already sending %r", self, self.rxq[OpenPDU.pdu_type])
        else:
            self.send_pdu(OpenPDU(local_id = self.main.local_id, attributes = attributes))

    def send_ack(self, pdu):
        "Send an ACK PDU."
        self.send_pdu(ACKPDU(ack_type = pdu.pdu_type, error_hint = 0))

    def send_error(self, pdu, error_type, error_code, error_hint = 0):
        "Send an ACK PDU with an error code."
        self.send_pdu(ACKPDU(ack_type = pdu.pdu_type, error_type = error_type, error_code = error_code, error_hint = error_hint))

    def send_pdu(self, pdu):
        "Send a PDU, deferring it or setting up for retransmission if appropriate."
        if pdu.pdu_type != OpenPDU.pdu_type and isinstance(pdu, PDU.acked_pdu_classes) and pdu.pdu_type in self.rxq:
            logger.debug("%r deferring %r", self, pdu)
            self.deferred[pdu.pdu_type] = pdu
            return
        assert pdu.pdu_type not in self.rxq
        if isinstance(pdu, PDU.acked_pdu_classes):
            logger.debug("%r adding %r to rxq", self, pdu)
            self.rxq[pdu.pdu_type] = pdu
        logger.debug("%r sending %r", self, pdu)
        self.main.io.write(pdu, self.macaddr)
        if pdu.pdu_type in self.rxq:
            pdu.rxmit_interval  = self.main.cfg.getfloat("retransmit-initial-interval")
            pdu.rxmit_dropsleft = self.main.cfg.getint("retransmit-max-drop")
            pdu.rxmit_timeout   = current_time() + pdu.rxmit_interval
            logger.debug("%r setting initial rxmit_timeout %s rxmit_interval %s rxmit_dropsleft %s for %r",
                         self, pdu.rxmit_timeout, pdu.rxmit_interval, pdu.rxmit_dropsleft, pdu)
            self.main.wake.set()

    def check_timeouts(self, timer):
        "Check all the little timeout values in this session."
        logger.debug("%r checking timers", self)

        if self.is_open and timer.now > self.saw_last_keepalive + self.main.cfg.getfloat("keepalive-receive-timeout"):
            logger.info("%r too long since last KeepAlive, closing session", self)
            return self.close()

        for pdu in self.rxq.values():
            if not timer.check_expired(pdu.rxmit_timeout):
                logger.debug("%r scheduled wakeup %r for unchanged PDU %r rxmit_timeout %s (%s)",
                             self, timer, pdu, pdu.rxmit_timeout,
                             time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(pdu.rxmit_timeout)))
                continue

            pdu.rxmit_dropsleft -= 1
            if pdu.rxmit_dropsleft <= 0:
                logger.info("%r too many drops for PDU %r, closing session", self, pdu)
                return self.close()

            if self.main.cfg.getboolean("retransmit-exponential-backoff"):
                pdu.rxmit_interval *= 2

            pdu.rxmit_timeout = timer.wake_after(pdu.rxmit_interval)
            logger.debug("%r retransmitting %r rxmit_timeout %s (%s)",
                         self, pdu, pdu.rxmit_timeout,
                         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(pdu.rxmit_timeout)))

            self.main.io.write(pdu, self.macaddr)

        if self.is_open and (self.send_next_keepalive is None or timer.check_expired(self.send_next_keepalive)):
            self.send_next_keepalive = timer.wake_after(self.main.cfg.getfloat("keepalive-send-interval"))
            logger.debug("%r sending keep-alive, next one scheduled for %s (%s)",
                         self, self.send_next_keepalive,
                         time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.send_next_keepalive)))
            self.send_pdu(KeepAlivePDU())

    def report_rfc7752(self, pdu):
        "Toss RFC 7752 data at log, since we have no real RFC 7752 code (yet)."
        logger.info("%r RFC-7752 data: %r", self, pdu)

    def cleanup_rfc7752(self):
        "Clear all RFC 7752 data, by synthesizing empty encapsulation PDUs."
        for cls in PDU.pdu_type_map.values():
            if issubclass(cls, EncapsulationPDU):
                self.report_rfc7752(cls())



#
# Main program
#

class Main:
    """
    LSOE main program.  This is intended to be run under Tornado's .run_sync() method.
    Calling sequence is important: run_sync(Main().main), because there are certain things
    we need to do before the Tornado I/O loop starts, and we need to supply a coroutine
    to .run_sync().
    """

    # __init__() in this class handles things that need to be done *before* Tornado's
    # I/O loop starts.  Mostly this means configuring the logging module, along with
    # everything that depends upon.

    def __init__(self):
        os.environ.update(TZ = "UTC")
        time.tzset()

        ap = argparse.ArgumentParser(description = __doc__,
                                     formatter_class = type("HF", (argparse.ArgumentDefaultsHelpFormatter,
                                                                   argparse.RawDescriptionHelpFormatter), {}),
                                     epilog = default_config)
        ap.add_argument("-c", "--config",
                        help = "configuration file",
                        type = argparse.FileType("r"),
                        default = os.getenv("LSOE_CONFIG", None))
        ap.add_argument("-d", "--debug",
                        help = "bark more",
                        action = "count",
                        default = 0)
        args = ap.parse_args()

        cfg = configparser.ConfigParser()
        cfg.read_string(default_config)
        if args.config is not None:
            cfg.read_file(args.config)
        self.cfg = cfg["lsoe"]

        self.debug = args.debug

        logging.basicConfig(level  = logging.DEBUG if self.debug else logging.INFO,
                            format = "%(asctime)s %(name)s[%(process)d] %(levelname)s %(message)s")

        self.configure_id()

    def configure_id(self):
        """
        Configure the "Local ID" of this LSOE instance.

        This is a separate method because set of text formats we might
        have to parse is a bit open-ended.  For now we only support a
        hex string (with optional ":", "-", or whitespace separation
        between bytes).

        For convenience during initial testing, we also support a
        default based on the "product_uuid" value off in Linux's /sys
        tree.  This may go away, at which point configuration of
        "local ID" would become mandatory, as would the configuration
        file.  Dunno yet.
        """

        try:
            text = self.cfg["local-id"]

        except KeyError:
            import hashlib
            self.local_id = hashlib.md5(open("/sys/class/dmi/id/product_uuid").read().encode("ascii")).digest()[:10]

        else:
            self.local_id = bytes.fromhex(text.replace("-", ":").replace(":", " "))

    @tornado.gen.coroutine
    def main(self):
        "Main coroutine, initializes a few things then waits for other coroutines."
        logger.debug("Starting")
        self.sessions = {}
        self.ifs  = Interfaces()
        self.io   = EtherIO(self.cfg)
        self.wake = tornado.locks.Event()

        wait_iterator = tornado.gen.WaitIterator(
            self.pdu_receiver(), self.hello_beacon(), self.session_timers(), self.interface_tracker())

        while not wait_iterator.done():
            yield wait_iterator.next()

    def log_raw_pdu(self, msg, macaddr, ifname):
        "More than you ever wanted to know about bytes received from the wire."
        logger.debug("Received PDU from EtherIO layer, MAC address %s, interface %s", macaddr, ifname)
        for i, line in enumerate(textwrap.wrap(" ".join("{:02x}".format(b) for b in msg))):
            logger.debug("[%3d] %s", i, line)

    @tornado.gen.coroutine
    def pdu_receiver(self):
        "Coroutine PDU receiver loop."
        logger.debug("Starting pdu_receiver task")
        while True:
            msg, macaddr, ifname = yield self.io.read()
            if self.debug > 1:
                self.log_raw_pdu(msg, macaddr, ifname)
            try:
                session = self.sessions[macaddr]
            except KeyError:
                session = self.sessions[macaddr] = Session(self, macaddr, ifname)
                logger.debug("Created new session for MAC address %s, interface %s", macaddr, ifname)
            was_open = session.is_open
            self.sessions[macaddr].recv(msg)
            if session.is_open and not was_open:
                logger.debug("Session %r just opened, stuffing initial encapsulations", session)
                for pdu in self.ifs.get_encapsulations():
                    session.send_pdu(pdu)

    @tornado.gen.coroutine
    def hello_beacon(self):
        "Coroutine HELLO transmission loop."
        logger.debug("Starting hello_beacon task")
        while True:
            logger.debug("Running hello_beacon task")
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
            logger.debug("Sleeping hello_beacon task")
            yield tornado.gen.sleep(self.cfg.getfloat("hello-interval"))

    @tornado.gen.coroutine
    def session_timers(self):
        "Coroutine handling all the session timers."
        logger.debug("Starting timers task")
        while True:
            timer = Timer(self.wake)
            for session in tuple(self.sessions.values()):
                session.check_timeouts(timer)
            yield timer.wait()
            self.wake.clear()

    @tornado.gen.coroutine
    def interface_tracker(self):
        "Couroutine sending interface changes to existing sessions."
        logger.debug("Starting interface_tracker task")
        while True:
            pdu = yield self.ifs.read_updates()
            for session in self.sessions.values():
                if session.is_open:
                    session.send_pdu(copy.copy(pdu))

# Python voodoo to call Main().main().  See calling sequence notes in
# Main class.  Be careful about exceptions which should cause quiet
# exits.  In theory we might want to do some kind of final cleanup
# here (perhaps in a "finally:" clause to clear out RFC 7752 data if
# we can, but since this process is exiting by that point there's not
# a lot of other cleanup we can or should do.

if __name__ == "__main__":
    try:
        tornado.ioloop.IOLoop.current().run_sync(Main().main)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(0)
    except:
        logger.exception("Unhandled exception")
        sys.exit(1)
