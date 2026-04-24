import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    get_status: Callable[[], dict]

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/status":
            self._respond(200, self.__class__.get_status())
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("health: " + fmt, *args)


class HealthServer:
    def __init__(self, port: int, get_status: Callable[[], dict]):
        handler = type("Handler", (_Handler,), {"get_status": staticmethod(get_status)})
        self._server = HTTPServer(("", port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("Health server listening on port %d", self._server.server_address[1])

    def stop(self) -> None:
        self._server.shutdown()
