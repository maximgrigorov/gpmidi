"""Integration test: Flask upload proxy sends correct Content-Length.

Uses a real local HTTP server as upstream, verifying the actual body
size matches what was sent, not the multipart request Content-Length.
"""

from __future__ import annotations

import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO

import pytest


class _CapturingHandler(BaseHTTPRequestHandler):
    """Records received body and Content-Length header."""

    received_bodies: list[bytes] = []
    received_content_lengths: list[str | None] = []

    def do_PUT(self):
        cl = self.headers.get("Content-Length")
        self.__class__.received_content_lengths.append(cl)
        if cl:
            body = self.rfile.read(int(cl))
        else:
            body = self.rfile.read()
        self.__class__.received_bodies.append(body)
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        import json
        resp = json.dumps({"link_id": "test", "sha256": "abc", "size_bytes": len(body),
                           "role": "mix", "deduplicated": False, "request_id": "r1"})
        self.wfile.write(resp.encode())

    def log_message(self, format, *args):
        pass


@pytest.fixture
def upstream_server():
    _CapturingHandler.received_bodies = []
    _CapturingHandler.received_content_lengths = []
    server = HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def test_proxy_upload_content_length(upstream_server, monkeypatch):
    """Flask proxy must not send multipart total as Content-Length."""
    monkeypatch.setenv("ASSET_API_BASE", upstream_server)
    monkeypatch.setenv("ASSET_API_VERIFY_TLS", "false")

    import importlib
    import ailab_client
    importlib.reload(ailab_client)

    client = ailab_client.AssetAPIClient(base_url=upstream_server)
    file_content = b"RIFF" + b"\x00" * 40 + b"WAVEfmt " + b"\x00" * 200
    stream = BytesIO(file_content)

    result = client.stream_proxy_upload("test-ticket", stream)
    assert result["link_id"] == "test"

    # The upstream should have received exactly len(file_content) bytes
    assert len(_CapturingHandler.received_bodies) == 1
    assert len(_CapturingHandler.received_bodies[0]) == len(file_content)
