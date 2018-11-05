#!/usr/bin/env python3

# https://docs.pyroute2.org/

import sys
import json
import socket
import pyroute2
import pyroute2.netlink.rtnl
import tornado.ioloop

from pyroute2.netlink.rtnl.ifinfmsg import IFF_UP

afname = dict((getattr(socket, i), i) for i in ("AF_INET", "AF_INET6"))

def handle_read(*ignored):
    msgs = ip.get()
    for msg in msgs:
        print("Interface index {} state {}".format(msg["index"],
                                                   "up" if (msg["flags"] & IFF_UP) else "down"))
        sys.stdout.flush()

class Interface(object):

    def __init__(self, index, name, macaddr):
        self.index   = index
        self.name    = name
        self.macaddr = macaddr
        self.ipaddrs = {}

    def add_ipaddr(self, family, ipaddr):
        if family not in self.ipaddrs:
            self.ipaddrs[family] = []
        self.ipaddrs[family].append(ipaddr)

    def show(self):
        print("Interface {0.name} (#{0.index}) link {0.macaddr}".format(self))
        for family in sorted(self.ipaddrs):
            print("  Address family {} (#{}):".format(afname.get(family, "???"), family))
            for ipaddr in self.ipaddrs[family]:
                print("    {}".format(ipaddr))

ifnames = {}
ifindex = {}

with pyroute2.IPRoute() as ipr:
    for x in ipr.get_links():
        iface = Interface(x["index"], x.get_attr("IFLA_IFNAME"), x.get_attr("IFLA_ADDRESS"))
        ifindex[iface.index] = iface
        ifnames[iface.name]  = iface
    for x in ipr.get_addr():
        ifindex[x["index"]].add_ipaddr(x["family"], x.get_attr("IFA_ADDRESS"))

for i in sorted(ifindex):
    ifindex[i].show()
    print()

if False:
    ip = pyroute2.RawIPRoute()
    ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK)

    ioloop = tornado.ioloop.IOLoop.current()
    ioloop.add_handler(ip.fileno(), handle_read, tornado.ioloop.IOLoop.READ)
    ioloop.start()
