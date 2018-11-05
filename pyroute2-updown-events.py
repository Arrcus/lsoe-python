#!/usr/bin/env python3

import sys
import json
import pyroute2
import pyroute2.netlink.rtnl
import tornado.ioloop

from pyroute2.netlink.rtnl.ifinfmsg import IFF_UP

def handle_read(*ignored):
    msgs = ip.get()
    for msg in msgs:
        print("Interface index {} state {}".format(msg["index"],
                                                   "up" if (msg["flags"] & IFF_UP) else "down"))
        sys.stdout.flush()

ip = pyroute2.RawIPRoute()
ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK)

ioloop = tornado.ioloop.IOLoop.current()
ioloop.add_handler(ip.fileno(), handle_read, tornado.ioloop.IOLoop.READ)
ioloop.start()
