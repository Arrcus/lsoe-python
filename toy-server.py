#!/usr/bin/env python

# Toy server using cherrypy and jinja2.
#
# / is the display pane, uses jinja2 to format whatever's in the toy database.
#
# /mutate is the upload point, parses JSON and stuffs result into the toy database.

import cherrypy

class Root(object):

    def __init__(self, dir = "templates"):
        from jinja2 import Environment, FileSystemLoader
        self.env = Environment(loader = FileSystemLoader(dir))
        self.data = {}

    @cherrypy.expose
    def index(self):
        return self.env.get_template("index.html").render(self.data)

    @cherrypy.expose
    @cherrypy.tools.json_in()
    def mutate(self):
        self.data = cherrypy.request.json

cherrypy.config.update({"server.socket_host" : "127.0.0.1",
                        "server.socket_port" : 8080 })

cherrypy.quickstart(Root())
