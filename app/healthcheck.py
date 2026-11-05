import http.client
import json
import os
import socket

connection = http.client.HTTPConnection("localhost", timeout=3)
connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.sock.settimeout(3)
connection.sock.connect(os.environ.get("PANEL_SOCKET", "/run/panel/panel.sock"))
try:
    connection.request("GET", "/health")
    response = connection.getresponse()
    assert response.status == 200 and json.loads(response.read())["healthy"]
finally:
    connection.close()
