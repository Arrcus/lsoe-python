# See /usr/include/linux/if_ether.h for various numeric codes

# See https://stackoverflow.com/questions/42821309/how-to-interpret-result-of-recvfrom-raw-socket
# for purported explanation of socket address values returned by raw packet sockets:
#
# [0]: interface name (eg 'eth0')
# [1]: protocol at the physical level (defined in linux/if_ether.h)
# [2]: packet type (defined in linux/if_packet.h)
# [3]: ARPHRD (defined in linux/if_arp.h)
# [4]: physical address

import socket, textwrap

ETH_DATA_LEN    = 1500          # Max. octets in payload
ETH_FRAME_LEN   = 1514          # Max. octets in frame sans FCS

ETH_P_ALL       = 0x0003        # Everything
ETH_P_IP        = 0x0800        # IPv4
ETH_P_IPV6      = 0x86DD        # IPv6
# ETH_P_LSOE    = 0x????        # LSOE type TBD, see draft

def hexify(bytes, delimiter):
    return delimiter.join("{:02x}".format(ord(b)) for b in bytes)

s = socket.socket(socket.PF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))

# Might use packet filter here if listening for particular ether type
# is too painful, but once we have an LSOE ethertype that should just
# work, sez here.

for i in xrange(200):
    p, a = s.recvfrom(ETH_FRAME_LEN)
    print "Packet on interface {0[0]} from address {1} protocol {0[1]:04x} type {0[2]:04x} arphrd {0[3]:04x}".format(a, hexify(a[4], ":"))
#   print "\n".join(textwrap.wrap(hexify(p, " ")))
