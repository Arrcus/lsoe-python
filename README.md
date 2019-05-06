lsoe-demo
=========

Containerized LSoE demo and test environment.

This spins up a bunch of containerized `lsoed` instances in a
spine/leaf Clos topology, along with one instance of `kriek` (a
monitoring tool based on CherryPy and Jinja2), with the `lsoed`
instances configured to report RFC 7752 data to `kriek`.

lsoed - an lsoe state machine
=============================

This is an implementation of the base LSoE protocol as described in
https://tools.ietf.org/html/draft-ietf-lsvr-lsoe-01

It is meant to augment the internet draft, not provide a functional
product.  lsoed is essentially a python3 state machine for the lsor
protocol.  It does not have the northbound interface to a BGP-LS API;
but rather stubs to run the cherrypy lsoe-demo.

It is not current with the L3DL internet drafts.

Assumptions
-----------

Assumes Linux with Python3, GNU make, and Docker installed.

Usage
-----

In theory, doing

```
make demo
```

will build the Docker images, start the herd of containers, plumb all
of the `lsoed` containers together with `veth` interfaces, and start
up the `kriek` container listening for plain HTTP on port `8080`.

Caveats
-------

* The HTML is ugly.  Feel free to write better Jinja2 templates.

* Plain HTTP sucks, but so do stuffing private X.509 certificates
  into your browser for a demo or negotiating Let's Encrypt
  certificates in a container for a demo.  Sorry.

* All mechanisms for determining a useful local IP address on Linux
  without installing a third-party library like netifaces seem to be
  kludges.  The classic `gethostbyname(gethostname())` usually just
  returns the loopback address.  Using `ip route get` to ask what
  local address would be picked for talking to some remote address
  seems to work as well as anything, and better than making
  assumptions about interface names.
