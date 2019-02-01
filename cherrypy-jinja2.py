# Minimal demo from
# https://stackoverflow.com/questions/16844182/getting-started-with-cherrypy-and-jinja2

import cherrypy
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('templates'))

class Root:
    @cherrypy.expose
    def index(self):
        tmpl = env.get_template('index.html')
        return tmpl.render(salutation='Hello', target='World')

cherrypy.config.update({'server.socket_host': '127.0.0.1',
                         'server.socket_port': 8080,
                        })

cherrypy.quickstart(Root())

