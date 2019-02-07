#!/usr/bin/env python

"""
Toy server using cherrypy and jinja2.

/ is the display pane, uses jinja2 to format whatever's in the toy database.

/mutate is the upload point, parses JSON and stuffs result into the toy database.
"""

import cherrypy, argparse, jinja2

class Root(object):

    def __init__(self, template_dir):
        self.env = jinja2.Environment(loader = jinja2.FileSystemLoader(template_dir))
        self.data = {}

    @cherrypy.expose
    def index(self):
        return self.env.get_template("index.html").render(self.data)

    @cherrypy.expose
    @cherrypy.tools.json_in()
    def mutate(self):
        self.data = cherrypy.request.json

HF = type("HF", (argparse.RawDescriptionHelpFormatter,
                 argparse.ArgumentDefaultsHelpFormatter), {})
ap = argparse.ArgumentParser(description = __doc__, formatter_class = HF)
ap.add_argument("--host", default = "127.0.0.1",        help = "listener address")
ap.add_argument("--port", default = 8080, type = int,   help = "listener port")
ap.add_argument("--templates", default = "templates",   help = "template directory")
args = ap.parse_args()

cherrypy.config.update({"server.socket_host" : args.host,
                        "server.socket_port" : args.port })

cherrypy.quickstart(Root(args.templates))
