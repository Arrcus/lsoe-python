#!/usr/bin/env python3

import sys
import json
import pyroute2
import pyroute2.netlink.rtnl
import tornado.ioloop

def handle_read(*args, **kwargs):
    print("args: {!r}, kwargs {!r}".format(args, kwargs))
    json.dump(ip.get(), sys.stdout, indent = 4, sort_keys = True)
    sys.stdout.flush()

ip = pyroute2.RawIPRoute()
ip.bind(pyroute2.netlink.rtnl.RTNLGRP_LINK)

ioloop = tornado.ioloop.IOLoop.current()
ioloop.add_handler(ip.fileno(), handle_read, tornado.ioloop.IOLoop.READ)
ioloop.start()
