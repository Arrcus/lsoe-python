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
