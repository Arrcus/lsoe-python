Link State Over Ethernet (LSOE)
===============================

This is a Python3 implementation of the base LSoE protocol as
described in https://tools.ietf.org/html/draft-ietf-lsvr-lsoe-01

It is meant to augment the Internet-Draft, not provide a functional
product.  In particular, `lsoed` includes a Python3 state machine for
the LSoE protocol.

`lsoed` doesn't include the northbound interface to a BGP-LS API;
instead, it has an HTTP stub client which drives a CherryPy-based demo.

`lsoed` is not current with the L3DL Internet-Drafts.

Implementation environment and dependencies
-------------------------------------------

Currently we're avoiding any features not available as packaged
software on Debian Jessie, which means:

* Avoiding Python features requiring a version more recent than Python 3.4;
* Using Tornado's I/O and coroutine environment rather than Python `asyncio`;
* Using `yield`-based coroutines rather than `async def`, `await`, et cetera.

We're currently using two external packages:

* Tornado (`python3-tornado`), version 4.x.
* PyRoute2 (`python3-pyroute2`), version 0.3.16.

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

Apparently there's some MPLS support in recent versions of PyRoute2,
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

Demo using Docker and CherryPy
------------------------------

The `demo/` subdirectory contains a Docker-based demo of `lsoed`.

The demo spins up a bunch of containerized `lsoed` instances in a
spine/leaf Clos topology, along with one instance of `kriek` (a
monitoring tool based on CherryPy and Jinja2), with the `lsoed`
instances configured to report RFC 7752 data to `kriek`.

### Demo requirements

In addition to the requirements for `lsoed` itself, the demo assumes a
Linux environment (it configures a bunch of `veth` interfaces to set
up the topology) with GNU make and Docker installed.  Building the
demo pulls down Docker images and PyPi packages as needed.

Because most of the demo code is running under Docker, it's relatively
insensitive to the version of Linux running on the host machine: we've
tested it on Debian Jessie and Debian Stretch, but it would almost
certainly work in many other Linux environments.

### Demo Usage

Running

```
make test
```

in the `demo/` directory should build the Docker images, start the
herd of containers, plumb all of the `lsoed` containers together with
`veth` interfaces, and start up the `kriek` container listening for
plain HTTP on port `8080`.  To tear down the demo, do:

```
make clean
```

If you want to experiment, you can supply your own `topology.json`
file to replace the default one generated by `make test`, and there
are a few other `Makefile` targets which might be useful if you want
to tweak something.

### Demo Caveats

* The HTML is ugly.  Feel free to write a better Jinja2 template.

* Plain HTTP sucks, but so does stuffing private X.509 certificates
  into your browser for a demo or negotiating Let's Encrypt
  certificates in containers for a demo.  Sorry.

* All mechanisms for determining a useful local IP address on Linux
  without installing a third-party library like netifaces seem to be
  kludges.  The classic `gethostbyname(gethostname())` usually just
  returns the loopback address.  Using `ip route get` to ask what
  local address would be picked for talking to some remote address
  seems to work as well as anything, and works better than wiring in
  assumptions about interface names.
