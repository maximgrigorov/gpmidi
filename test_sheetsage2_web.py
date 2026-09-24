from __future__ import annotations

from io import BytesIO

import app as web


def test_audio_limit_does_not_raise_the_guitar_pro_upload_limit():
    assert not web._gp_upload_too_large(128 * 1024 * 1024)
    assert web._gp_upload_too_large(128 * 1024 * 1024 + 1)


class FakeResponse:
    def __init__(self, payload=None, status=200, content=b""):
        self._payload = payload or {}
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": "application/octet-stream", "Content-Disposition": "attachment; filename=result.mid"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        pass


def test_audio_tool_page_and_streaming_upload(monkeypatch):
    captured = {}

    def get(url, **kwargs):
        assert url.endswith("/v1/jobs")
        return FakeResponse({"jobs": []})

    def put(url, data, headers, timeout):
        captured["url"] = url
        captured["body"] = data.read()
        captured["headers"] = headers
        return FakeResponse({"id": "abc123def456", "status": "queued"}, status=202)

    monkeypatch.setattr(web.requests, "get", get)
    monkeypatch.setattr(web.requests, "put", put)
    client = web.app.test_client()
    page = client.get("/audio-to-midi")
    assert page.status_code == 200
    assert "Audio → MIDI · SheetSage2" in page.get_data(as_text=True)

    response = client.post(
        "/audio-to-midi/upload",
        data={"file": (BytesIO(b"RIFFxxxxWAVEdata"), "song.wav")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert "job=abc123def456" in response.headers["Location"]
    assert captured["body"] == b"RIFFxxxxWAVEdata"
    assert captured["headers"]["X-Filename"] == "song.wav"


def test_audio_status_preserves_actionable_vram_message(monkeypatch):
    message = "Недостаточно свободной видеопамяти. Остановите нагрузку, очистите VRAM и повторите запрос."
    monkeypatch.setattr(
        web.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"id": "abc123def456", "status": "failed", "message": message}),
    )
    response = web.app.test_client().get("/audio-to-midi/jobs/abc123def456/status")
    assert response.status_code == 200
    assert response.get_json()["message"] == message


def test_audio_download_is_streamed(monkeypatch):
    monkeypatch.setattr(
        web.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(content=b"MThd-result"),
    )
    response = web.app.test_client().get("/audio-to-midi/jobs/abc123def456/download/midi")
    assert response.status_code == 200
    assert response.data == b"MThd-result"
    assert "result.mid" in response.headers["Content-Disposition"]
