Fun with CherryPy and Jinja2
============================

Toy client and server using CherryPy and Jinja2.  This may turn into
something more useful later, right now it's just a proof-of-concept
demonstrating the basic techniques we might want for the display
screen of an LSOE demo.  Basic idea is a bare-bones web server which
displays its top-level page using Jinja2 templates and supports an
upload URL to which a client can stuff JSON data uploads.

In theory, the rest is a small matter of template programming.  In
practice, we might add some other action URLs to control the demo.
