#!/usr/bin/env python3

# https://docs.pyroute2.org/

import sys
import json
import socket

import pyroute2
import pyroute2.netlink.rtnl
import pyroute2.netlink.rtnl.ifinfmsg

import tornado.ioloop

afname = dict((getattr(socket, i), i) for i in ("AF_INET", "AF_INET6"))

iface_flags = dict((name, getattr(pyroute2.netlink.rtnl.ifinfmsg, name))
                   for name in dir(pyroute2.netlink.rtnl.ifinfmsg)
                   if  name.startswith("IFF_")
                   and isinstance(getattr(pyroute2.netlink.rtnl.ifinfmsg, name), int))

globals().update(iface_flags)

class Interface(object):

    def __init__(self, index, name, macaddr, flags):
        self.index   = index
        self.name    = name
        self.macaddr = macaddr
        self.flags   = flags
        self.ipaddrs = {}

    def add_ipaddr(self, family, ipaddr, prefixlen):
        if family not in self.ipaddrs:
            self.ipaddrs[family] = []
        self.ipaddrs[family].append((ipaddr, prefixlen))
        print("Interface {} add {}/{}".format(self.name, ipaddr, prefixlen))

    def del_ipaddr(self, family, ipaddr, prefixlen):
        self.ipaddrs[family].remove((ipaddr, prefixlen))
        print("Interface {} del {}/{}".format(self.name, ipaddr, prefixlen))

    @property
    def flagtext(self):
        return  "<{}>".format(",".join(
            sorted(n for (n, f) in iface_flags.items() if f & self.flags != 0)))

    def update_flags(self, flags):
        old_flags = self.flagtext
        self.flags = flags
        print("Interface {} flags {} => {}".format(self.name, old_flags, self.flagtext))

    def show(self):
        print("Interface {0.name} (#{0.index}) link {0.macaddr} flags {0.flagtext}".format(self))
        for family in sorted(self.ipaddrs):
            print("  Address family {} (#{}):".format(afname.get(family, "???"), family))
            for ipaddr, prefixlen in self.ipaddrs[family]:
                print("    {}/{}".format(ipaddr, prefixlen))

# Race condition: open event monitor socket before doing initial scans.
# Unlikely to matter in toy demo but on busy switch it might

ip = pyroute2.RawIPRoute()
ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK|
        pyroute2.netlink.rtnl.RTNLGRP_IPV4_IFADDR|
        pyroute2.netlink.rtnl.RTNLGRP_IPV6_IFADDR)

ifnames = {}
ifindex = {}

with pyroute2.IPRoute() as ipr:
    for x in ipr.get_links():
        iface = Interface(
            index   = x["index"],
            flags   = x["flags"],
            name    = x.get_attr("IFLA_IFNAME"),
            macaddr = x.get_attr("IFLA_ADDRESS"))
        ifindex[iface.index] = iface
        ifnames[iface.name]  = iface
    for x in ipr.get_addr():
        ifindex[x["index"]].add_ipaddr(
            family = x["family"],
            ipaddr = x.get_attr("IFA_ADDRESS"),
            prefixlen = x["prefixlen"])

for i in sorted(ifindex):
    ifindex[i].show()
    print()

def handle_event(*ignored):
    for msg in ip.get():
        #print("+ {}".format(json.dumps(msg, indent = 4, sort_keys = True)))
        if msg["event"] == "RTM_NEWLINK":
            ifindex[msg["index"]].update_flags(msg["flags"])
        elif msg["event"] == "RTM_NEWADDR":
            ifindex[msg["index"]].add_ipaddr(msg["family"], msg.get_attr("IFA_ADDRESS"), msg["prefixlen"])
        elif msg["event"] == "RTM_DELADDR":
            ifindex[msg["index"]].del_ipaddr(msg["family"], msg.get_attr("IFA_ADDRESS"), msg["prefixlen"])
        else:
            print("WTF: {} event".format(msg["event"]))

ioloop = tornado.ioloop.IOLoop.current()
ioloop.add_handler(ip.fileno(), handle_event, tornado.ioloop.IOLoop.READ)
ioloop.start()
