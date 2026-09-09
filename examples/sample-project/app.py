"""Local-EZAI sample project — a tiny HTTP service without a /health endpoint.

The first run plans "add a GET /health endpoint" against this file; it is the
safe place for your first autonomous engineering task.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

GREETING = {"message": "hello from the Local-EZAI sample project"}


def routes() -> dict[str, dict]:
    """Path → JSON body served with 200; anything else is 404."""
    return {"/": GREETING}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — http.server's naming
        body = routes().get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # quiet in tests
        return None


def serve(port: int = 8080) -> None:
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
