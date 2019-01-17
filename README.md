Link State Over Ethernet (LSOE)
===============================

Initial reference implementation of LSOE in Python3.

Implementation environment and dependencies
-------------------------------------------

Currently we're avoiding any features not available as packaged
software on Debian Jessie, which means:

* No requiring Python features more recent than Python 3.4;
* Tornado's I/O and coroutine environment rather than Python `asyncio`;
* `yield`-based coroutines rather than `async def`, `await`, et cetera.

We're currently using two external packages:

* Tornado (`python3-tornado`), version 4.x (`4.4.3-1~bpo8+1`)
* PyRoute2 (`python3-pyroute2`), version 0.3.16 (`0.3.16-1~bpo8+1`)

Both of these are from the `jessie-backports` collection.

Newer versions will probably work, but haven't been tested.
Older versions probably won't work.

Ethertypes
----------

IEEE considers EtherTypes to be a scarce resource, so they allocated
some playground space for development and private protocols:

Name                            | Value
--------------------------------|------
Local Experimental EtherType 1  | 88-B5
Local Experimental EtherType 2  | 88-B6
OUI Extended EtherType          | 88-B7

MPLS
----

Apparently there's some MPLS support in recent versions of pyroute2,
see <https://docs.pyroute2.org/mpls.html>, but the version of PyRoute2
available on Jessie is too old to support this.  We'll get to it
eventually, but for the moment the MPLS support is is a placeholder,
which reports nothing but empty encapsulation lists.

Vendor extensions
-----------------

The implementation has minimal support for externally supplied
vendor-specific PDUs.  Specifically, there are hooks in the VENDOR,
ACK, and ERROR PDU handlers for vendor-supplied code.  This has not
been tested, and we don't recommend using this mechanism unless you're
comfortable reading the code to see what it does.  The intent is just
to make it possible for you to add vendor extensions without having to
modify the core protocol engine.

Example:

```
import sys
import lsoe
import logger
import tornado.ioloop

my_enterprise_number = 12

def my_vendor_pdu_handler(session, pdu):
    # Do something interesting here
	
def my_ack_pdu_handler(session, pdu):
    # Do something interesting here

def my_error_pdu_handler(session, pdu):
    # Do something interesting here

lsoe.VendorPDU.vendor_dispatch[my_enterprise_number] = my_vendor_pdu_handler
lsoe.ACKPDU.vendor_hook = my_ack_pdu_handler
lsoe.ErrorDU.vendor_hook = my_error_pdu_handler

if __name__ == "__main__":
    try:
        tornado.ioloop.IOLoop.current().run_sync(lsoe.Main().main)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(0)
    except:
        logger.exception("Unhandled exception")
        sys.exit(1)
```

You don't need to use all the hooks, only set the one(s) you need.

Multicast addresses
-------------------

The current test code uses the ethernet broadcast address rather than
multicast, because we needed to do something for testing and didn't
want to wait until we figured out which of the half dozen kinds of
Ethernet multicast addresses we were supposed to use here.

We'll fix this.  For now, don't use this outside a lab.

Some helpful advice from a colleague, kept here until we sort this out:

	Date: Wed, 7 Nov 2018 21:25:58 -0800
	From: Paul Congdon
	Subject: Addressing and prototype protocol development for LSOE

	This may or may not be useful, but IEEE 802 Overview and
	Architecture specifies an Ethertype and header encapsulation
	specific for prototype and vendor-specific protocol development.
	The motivation was to develop protocols without using your final
	assigned Ethertype in order to avoid interoperability issues if
	these somehow get out into the wild.  It is specified in Clause
	9.2.2 of 802-2014 which you can freely download from the Get802
	website:
	https://ieeexplore.ieee.org/browse/standards/get-program/page/series?id=68

	You mentioned that you will ultimately want 2 different multi-cast
	addresses for LSOE to allow different 'reach' of the addresses; one
	that will stop at a switch/bridge and another that will pass through
	a switch.

	For the one that will stop at a bridge, I would suggest that you use
	one of the specified LLDP addresses.  Just make sure you use a
	different Etherrtype (which could be the prototype development one
	while you are under development).  The current LLDP addresses are
	specified in Table 7-1 of LLDP (802.1AB) and are:

	01-80-C2-00-00-0E   Nearest Bridge = Propagation constrained to a
						  single physical link; stopped by all types of
						  bridges (including TMPRs (media converters)).
						  This is the default LLDP address

	01-80-C2-00-00-03   Nearest non-TPMR Bridge = Propagation
						  constrained by all bridges other than TPMRs;
						  intended for use within provider bridged
						  networks

	01-80-C2-00-00-00   Nearest Customer Bridge = Propagation
						  constrained by customer bridges, but passes
						  through TPMRs and S-VLAN provider bridges

	Seems like the 01-80-C2-00-00-00 address might be the best choice
	because it should pass through other stuff on the wire between your
	routers, but I think there are some issues with legacy
	implementations that didn't look at the Ethertype and just assumed
	this was a Spanning Tree packet.  Those legacy environments are
	probably not an issue in your deployments and it also is the reach
	of the secure L2 (MacSec) protocols if those were to be of use for
	you.  You could also go with 01-80-C2-00-00-0E which should be
	pretty safe, but assumes you just have point-to-point Ethernet wires
	between your routers..

	As for the other multicast address, you probably just want to
	allocate another EUI-48 Assignments under the IANA OUI as specified
	in 2.1.1 of RFC 7042.  The current list of multicast addresses
	already allocated can be seen here:
	https://www.iana.org/assignments/ethernet-numbers/ethernet-numbers.xhtml#ethernet-numbers-1.
	There should be no problem allocating another for your protocol.
