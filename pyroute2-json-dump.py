#!/usr/bin/env python3

# https://docs.pyroute2.org/usage.html

from pyroute2 import IPRoute
from sys import stdout
from json import dump

with IPRoute() as ipr:
    dump(dict(links = ipr.get_links(),
              addrs = ipr.get_addr()),
         stdout, indent = 4, sort_keys = True)
