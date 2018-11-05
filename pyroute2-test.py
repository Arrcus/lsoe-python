#!/usr/bin/env python3

# https://docs.pyroute2.org/usage.html

from pyroute2 import IPRoute
from sys import stdout
from json import dump

with IPRoute() as ipr:
    for x in ipr.get_links():
        print(x.get_attr("IFLA_IFNAME"),
              x.get_attr("IFLA_ADDRESS"),
              x.get_attr("IFLA_BROADCAST"))
