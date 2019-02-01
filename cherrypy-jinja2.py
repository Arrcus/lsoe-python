#!/usr/bin/env python

# Minimal demo hacked from
# https://stackoverflow.com/questions/16844182/getting-started-with-cherrypy-and-jinja2

# In theory, this allows POSTS as JSON data to /mutate, and renders /
# using Jinja2 templates.  Untested.

import cherrypy

data = {}

class Root(object):

    def __init__(self, dir = "templates"):
        from jinja2 import Environment, FileSystemLoader
        self.env = Environment(loader = FileSystemLoader(dir))

    @cherrypy.expose
    def index(self):
        return self.env.get_template("index.html").render(data)

    @cherrypy.expose
    @cherrypy.tools.json_in()
    def mutate(self):
        global data
        data = cherrypy.request.json

cherrypy.config.update({"server.socket_host" : "127.0.0.1",
                        "server.socket_port" : 8080 })

cherrypy.quickstart(Root())
