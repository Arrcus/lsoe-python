Link State Over Ethernet (LSOE)
===============================

Initial reference implementation of LSOE in Python3.

Implementation environment and dependencies
-------------------------------------------

Currently we're avoiding any features not available as packaged
software on Debian Jessie, which means:

* Avoiding Python features requiring a version more recent than Python 3.4;
* Using Tornado's I/O and coroutine environment rather than Python `asyncio`;
* Using `yield`-based coroutines rather than `async def`, `await`, et cetera.

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
vendor-specific PDUs.  Specifically, there are hooks in the VENDOR and
ACK PDU handlers for vendor-supplied code.  This has not been tested,
and we don't recommend using this mechanism unless you're comfortable
reading the code to see what it does.  The intent is just to make it
possible for you to add vendor extensions without having to modify the
core protocol engine.

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

lsoe.VendorPDU.vendor_dispatch[my_enterprise_number] = my_vendor_pdu_handler
lsoe.ACKPDU.vendor_hook = my_ack_pdu_handler

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

You don't need to use both hooks, only set the one(s) you need.

The ACK hook doesn't use the vendor's enterprise number because it
intercepts *all* ACKs, not just ACKs for VENDOR PDUs.  This is subject
to change if and when we figure out a better way to do this.

Multicast addresses
-------------------

The Ethernet multicast address LSOE uses for HELLO PDUs is
configurable.  The default is `01-80-C2-00-00-0E`, which constrains
multicast propegation to a single physical link.  Other values might
make sense in particular operational environments, eg
`01-80-C2-00-00-03` on a provider-bridged network.
