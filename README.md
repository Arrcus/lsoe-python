LSOE
====

Initial fumblings towards implementation of LSOE in Python.

Config
------

Initial code hard wires some constants, need to fix that.  Config
language either YAML or `.ini` file (using whatever `ConfigParser`
turned into in Python3).  Want comments in any case so not JSON.

Spec says "blah blah is configurable", do a sweep for that sort of
thing.  If go with YAML will probably want some `LazyDict` abstraction
that lets us use attributes instead of ten zillion `['foo']` instances.

Timers
------

Things that need timers (probably Tornado-based at least for now, but
I haven't yet figured out exactly how these slot into the data
structure architecture):

* HelloPDU beacon timer (global)
* KeepAlivePDU beacon timer (sending side, per session)
* Too long since last KeepAlivePDU (receiving side, per session)
* Retransmission timer(s?) (per session? per session*PDU_type?  multiplex?)

Callbacks or `@coroutine` loops in daemon pseudo-threads?  Think about
traceback context before jumping to conclusions here.

If pseudo-threads, are any of the usual thread synchronization
mechanisms useful here?  Queues?  Conditions?

Think about session close and teardown (separate things) too.

Still a bit confused about how this should work, but perhaps we could
use an `Event` with timeout.  Implemented slightly differently in
`asyncio` and Tornado but same basic mechanisms exist:
`tornado.locks.Event.wait(timeout = None)` vs 
`asyncio.wait_for(event.wait(), timeout)`.

General idea:

When we instantiate a Session, we create an `Event` object and start a
coroutine (call it `.sched()` for now).  `.sched()` runs an infinite
loop, running through a sequence of things it knows need timestamps
checked, performing actions (eg, sending packets), checking for things
which have timed out, and collecting information on when its next
wakeup should be.  When it's gone through its set of actions, it waits
on the `Event`, using the timeout it calculated, so it will wait until
kicked externally via the `Event` or until the timeout expires.
Either way, it wakes up and does it again.

`.sched()` should exit when `Session` is being shut down, or maybe
when closed (in which case we'd be starting `.sched()` when we start
trying to open or something).  `.sched()` would also need to be able
to trigger session close on various timeout events.

Remains the question: who awaits the `Future` returned by the
`.sched()` co-routine?  In Tornado, we could use the spawn mechanism,
haven't found equivalent in `asyncio`.  Well , maybe:

https://stackoverflow.com/questions/37278647/fire-and-forget-python-async-await

Simpler approach: handle this with a single looping coroutine in the
`Main` class, which iterates over sessions and calls a method in each
session to handle timer events in that session, etc.  Much simpler
control structure, doesn't require fire-and-forget voodoo.

Implementation environment
--------------------------

Probably needs to be Python 3 at this point.

Will probably use Tornado initially so don't have to learn entire
asyncio API in a hurry, but long term it may be possible to do
everything we need with standard libraries, since all we really need
is datagram sockets, coroutines, and supporting primatives like queues.

Er, on further analysis: "real" async coroutines (`await`, `async
def`, `asyncio`, etc) look fairly plausible, but were new in Python
3.5.  Our initial target is Debian Jessie, which is Python 3.4, so we
may be sticking with Tornado for a while.

PF_PACKET sockets
-----------------

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

Ethertypes
----------

IEEE considers EtherTypes to be a scarce resource, so they allocated
some playground space for development and private protocols:

Name                            | Value
--------------------------------|------
Local Experimental EtherType 1  | 88-B5
Local Experimental EtherType 2  | 88-B6
OUI Extended EtherType          | 88-B7

Decorator ordering
------------------

https://stackoverflow.com/questions/37119652/python-tornado-call-to-classmethod#37127703
says that `@classmethod` (or `@staticmethod`) must be the outermost
decorator, which sort of makes sense, so decorator ordering would be:

```python

    @classmethod
	@tornado.gen.coroutine
	def foo(cls, fee, fie, foe, fum):
	    yield do_something_interesting()
```

Interfaces and pyroute2
-----------------------

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

MPLS
----

Apparently there's some MPLS support in recent versions of pyroute2:
https://docs.pyroute2.org/mpls.html

But of course Jessie is too old.  Not like I have enough MPLS clue at
the moment to be messing with this in any case.
