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

    def __eq__(self, other):
        return  self.index   == other.index   \
            and self.name    == other.name    \
            and self.macaddr == other.macaddr \
            and self.flags   == other.flags   \
            and self.ipaddrs == other.ipaddrs

    def __str__(self):
        s = "Interface {0.name} (#{0.index}) link {0.macaddr} flags {0.flagtext}\n".format(self)
        for family in sorted(self.ipaddrs):
            s += "  Address family {} (#{}):\n".format(afname.get(family, "???"), family)
            for ipaddr in self.ipaddrs[family]:
                s += "    {}\n".format(ipaddr)
        return s

    def add_ipaddr(self, family, ipaddr):
        if family not in self.ipaddrs:
            self.ipaddrs[family] = []
        self.ipaddrs[family].append(ipaddr)

    def del_ipaddr(self, family, ipaddr):
        self.ipaddrs[family].remove(ipaddr)

    @property
    def flagtext(self):
        return  "<{}>".format(",".join(
            sorted(n for (n, f) in iface_flags.items() if f & self.flags != 0)))

    def update_flags(self, flags):
        self.flags = flags

class Interfaces(dict):

    def __init__(self):
        with pyroute2.IPRoute() as ipr:
            for x in ipr.get_links():
                iface = Interface(
                    index   = x["index"],
                    flags   = x["flags"],
                    name    = x.get_attr("IFLA_IFNAME"),
                    macaddr = x.get_attr("IFLA_ADDRESS"))
                self[iface.index] = iface
            for x in ipr.get_addr():
                self[x["index"]].add_ipaddr(
                    family = x["family"],
                    ipaddr = x.get_attr("IFA_ADDRESS"))

    def __str__(self):
        return "\n".join(str(self[i]) for i in sorted(self)) + "\n\n"

# Race condition: open event monitor socket before doing initial scans.
# Unlikely to matter in toy demo but on busy switch it might

ip = pyroute2.RawIPRoute()
ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK|
        pyroute2.netlink.rtnl.RTNLGRP_IPV4_IFADDR|
        pyroute2.netlink.rtnl.RTNLGRP_IPV6_IFADDR|
        pyroute2.netlink.rtnl.RTNLGRP_IPV4_ROUTE|
        pyroute2.netlink.rtnl.RTNLGRP_IPV6_ROUTE)

ifs = Interfaces()

print(ifs)

def handle_event(*ignored):
    for msg in ip.get():
        pass
    global ifs
    new = Interfaces()
    if ifs != new:
        ifs = new
        print(ifs)

ioloop = tornado.ioloop.IOLoop.current()
ioloop.add_handler(ip.fileno(), handle_event, tornado.ioloop.IOLoop.READ)
ioloop.start()
