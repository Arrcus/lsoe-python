LSOE
====

Initial fumblings towards implementation of LSOE in Python.

Probably needs to be Python 3 at this point.

Will probably use Tornado initially so don't have to learn entire
asyncio API in a hurry, but long term it may be possible to do
everything we need with standard libraries, since all we really need
is datagram sockets, coroutines, and supporting primatives like queues.

Raw ethernet frame socket I/O is not well documented, see Linux
packet(7) for what doc there is.

Half-assed Example of sending an ethernet frame, although we probably
want to use .sendto() which may require extra fields:

  https://stackoverflow.com/questions/12229155/how-do-i-send-an-raw-ethernet-frame-in-python

Purported explanation of the fields as seen by .recvfrom():

https://stackoverflow.com/questions/42821309/how-to-interpret-result-of-recvfrom-raw-socket

* [0]: interface name (eg 'eth0')
* [1]: protocol at the physical level (defined in linux/if_ether.h)
* [2]: packet type (defined in linux/if_packet.h)
* [3]: ARPHRD (defined in linux/if_arp.h)
* [4]: physical address

This does seem to match up with `sockaddr_ll` as described in `packet(7)`.

See `packet(7)` for description of sending packets.  In particular
note that certain fields in the `sockaddr_ll` should be zero on send.
If I'm reading this correctly, the zeroed fields are [2] and [3] in
the Python interpretation.  Whether the Python code wants those as
zero or just omits them...probably the former, but can read the
_socket source code if necessary.  OK, I read the source code, and it
looks like the sockaddr_ll component ordering is consistant between
read and write; when writing, the last three are optional as far as
the `PyArg_ParseTuple()` format is concerned, but since we do need to
specify the MAC address, which is the last element of the tuple, we
also need to specify the two zeroed fields.

IEEE considers EtherTypes to be a scarce resource, so they allocated
some playground space for development and private protocols:

Name                            | Value
--------------------------------|------
Local Experimental EtherType 1  | 88-B5
Local Experimental EtherType 2  | 88-B6
OUI Extended EtherType          | 88-B7


Other fun implementation stuff
------------------------------

The similarity between the Python 3 `byte` and `bytearray` types may
allow us to do something cut with the Frame and Message classes: use
`byte` for received messages, `bytearray` for messages we're
composing, and we automatically get the check for the writable
operations only applying to stuff we're composing.

Per discussion with Randy just now: checksums are over the frame
payload not over the message (ie, text in 5.2.1 is wrong here).  In
SLSOE the signature will be over the message with all the checksum
fields zeroed, so we're still checksuming the frames rather than
signing the checksums or leaving the checksum blank on the last frame
or ....

In -02 the frames within a message do have sequence numbers.  Messages
do not have sequence numbers in -02, protocol pretty much assumes
lock-step for everything where that might matter.

At this point I may have sold Randy on separating the transport
framing from the application message.  What I'm looking for is very
UDP-like, except that I also want the fragmentation mangement at this
layer.  So basically a combination of IPv4 fragments and UDP.  Payload
inside this transport layer is just bytes as far as the transport is
concerned; application messages look very BGP-like.

For some reason I'm thinking of NETBLT and VMTP, but probably way over
the top.

Possible transport header:

    0                   1                   2                   3
    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |    Version    |L| PDU Number  |           PDU Length          |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
   |                            Checksum                           |
   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

Where L flags that this is the last PDU (frame) in a message.  So a
single-frame message would have L = 1, PDU Number = 0.

Decorator ordering:
https://stackoverflow.com/questions/37119652/python-tornado-call-to-classmethod#37127703
says that `@classmethod` (or `@staticmethod`) must be the outermost
decorator, which sort of makes sense, so decorator ordering would be:

```python

    @classmethod
	@tornado.gen.coroutine
	def foo(cls, fee, fie, foe, fum):
	    yield do_something_interesting()
```

So it looks like we really do need to use pyroute2, not just because
screen scraping output from `ip addr list` is nasty, but because we
need to receive notifications when interfaces go up / down / south.

Using pyroute2 to list interfaces is pretty easy, although there's
this annoying split between listing interfaces and listing addresses,
one may have to do both to get useful associations between interface
names and addresses at layers 2 and three.

The fun part, though, is how one does the `RTNLGRP_LINK` monitoring to
receive events.  Nothing obvious in pyroute2 code proper, but there's
an example in `pyroute2.config.test_platform.TestCapsRtnl.monitor()`.
Looks like one can bind a `RawIPRoute()` to `RTNLGRP_LINK`, treat the
`RawIPRoute` object as a socket, then call its `.get()` method on
wakeup to pull messages.  At least, the test code implies that it
works with `poll()`, maybe it'll work with Tornado too.

But of course then we need to figure out which tiny fraction of these
messages we actually want.  Maybe pyroute2 makes this easier than
libnl did?  We'll see.  Probably start with an event dumper and go
from there.

There's something seriously weird going on with the PyRoute2
monitoring code.  It uses the old bit-mask API rather than the newer
`.add_membership()` / `.drop_membership()` API (see
<http://www.infradead.org/~tgr/libnl/doc/core.html#core_sk_multicast>),
which is a bit crufty but probably OK.  Obvious mask is:

```RTNLGRP_IPV4_IFADDR | RTNLGRP_IPV6_IFADDR | RTNLGRP_LINK```

which works for everything we want -- except IPv6 `RTM_NEWADDR`, which
we don't seem to get.  We can see route additions and deletions if we add

```RTNLGRP_IPV4_ROUTE | RTNLGRP_IPV6_ROUTE```

to the mix, but there's no interface index on route events so even if
we could tell that these are zeroth hop routes, this wouldn't help.

We get this behavior with both stock Jessie kernel and 4.14.

Only thing I can think of at the moment is to treat the routing events
as a triger to scan all interface addresses again.  Nasty, but would
probably work.

Question is whether this is kernel weridness or PyRoute2 weirdness.
Not obvious how PyRoute2 would be breaking this, but could test
against Python libnl for sanity check.  Slightly more likely is that
the old bitmap API is starting to turn into cottage cheese and we'd
get better results with the add/drop API, but while PyRoute2 does
define methods for that, they're not on the socket-like objects we
use.  So all is chaos.

Well, the base pyroute2 socket-ish objects we're using do support
`.setsockopt()`, so in theory we could do

```.setsockopt(SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, ...)```

if only we knew what to do for `...` -- which looks like the same
constants we're currently using in the bitmask, which suggests that
this may be pointless.

* * * 

Current state and next steps
----------------------------

So we have:

* Ethernet I/O code
* Interface/address monitoring code
* Presentation layer

Still needed:

* Protocol itself (state machine, ...)
* Upwards API to BGP*
