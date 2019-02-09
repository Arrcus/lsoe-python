lsoe-demo
=========

Containerized LSoE demo and test environment.

This spins up a bunch of containerized `lsoed` instances in a
spine/leaf Clos topology, along with one instance of `kriek` (a
monitoring tool based on CherryPy and Jinja2), with the `lsoed`
instances configured to report RFC 7752 data to `kriek`.

Assumptions
-----------

Assumes you have Python3, GNU make, and Docker installed, and that
you're running on a Linux box whose primary interface is `eth0`.

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
