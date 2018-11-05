#!/usr/bin/env python3

# https://docs.pyroute2.org/usage.html

from pyroute2 import IPRoute
with IPRoute() as ipr:
    print([x.get_attr('IFLA_IFNAME')
           for x in ipr.get_links()])
