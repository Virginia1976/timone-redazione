from __future__ import print_function
import subprocess
import sys

try:
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from urllib.parse import unquote, urlparse, parse_qs
except ImportError:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
    from urllib import unquote
    from urlparse import urlparse, parse_qs

PORT = 5020


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == '/apri' and 'path' in params:
            file_path = unquote(params['path'][0])
            subprocess.call(['open', file_path])
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print('Timone server in ascolto su porta', PORT)
    server.serve_forever()
