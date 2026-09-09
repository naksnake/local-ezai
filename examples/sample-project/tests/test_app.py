import http.client
import json
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


class AppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    def test_root_greets(self) -> None:
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), app.GREETING)

    def test_unknown_path_is_404(self) -> None:
        status, _ = self.get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
