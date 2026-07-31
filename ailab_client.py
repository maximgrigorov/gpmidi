"""HTTP client for AILab Asset API.

Encapsulates all communication with the remote asset-api service
so Flask views stay free of transport details.
"""

from __future__ import annotations

import os
from typing import Any, BinaryIO

import requests

ASSET_API_BASE = os.environ.get("ASSET_API_BASE", "https://192.168.30.2/asset-api")
ASSET_API_TIMEOUT = int(os.environ.get("ASSET_API_TIMEOUT", "30"))


def _parse_bool(val: str) -> bool:
    return val.lower() in ("1", "true", "yes", "on")


ASSET_API_VERIFY_TLS = _parse_bool(os.environ.get("ASSET_API_VERIFY_TLS", "false"))


class AssetAPIError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"[{status_code}] {code}: {message}")


class AssetAPIClient:
    def __init__(self, base_url: str | None = None):
        self.base = (base_url or ASSET_API_BASE).rstrip("/")
        self.verify = ASSET_API_VERIFY_TLS
        self.timeout = ASSET_API_TIMEOUT

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _handle(self, resp: requests.Response) -> dict:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", {})
                if isinstance(detail, dict):
                    code = detail.get("code", "unknown")
                    msg = detail.get("message", resp.text)
                else:
                    code = "error"
                    msg = str(detail)
            except Exception:
                code = "error"
                msg = resp.text[:500]
            raise AssetAPIError(resp.status_code, code, msg)
        if resp.status_code == 204:
            return {}
        return resp.json()

    def health(self) -> dict:
        try:
            r = requests.get(self._url("/healthz"), verify=self.verify, timeout=5)
            return r.json()
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    # --- Projects ---

    def create_project(self, name: str, description: str | None = None) -> dict:
        body: dict[str, Any] = {"name": name}
        if description:
            body["description"] = description
        r = requests.post(self._url("/v1/projects"), json=body,
                          verify=self.verify, timeout=self.timeout)
        return self._handle(r)

    def list_projects(self) -> list[dict]:
        r = requests.get(self._url("/v1/projects"),
                         verify=self.verify, timeout=self.timeout)
        return self._handle(r).get("projects", [])

    def get_project(self, project_id: str) -> dict:
        r = requests.get(self._url(f"/v1/projects/{project_id}"),
                         verify=self.verify, timeout=self.timeout)
        return self._handle(r)

    def update_project(self, project_id: str, revision: int,
                       name: str | None = None, description: str | None = None) -> dict:
        body: dict[str, Any] = {"revision": revision}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        r = requests.patch(self._url(f"/v1/projects/{project_id}"), json=body,
                           verify=self.verify, timeout=self.timeout)
        return self._handle(r)

    def delete_project(self, project_id: str) -> None:
        r = requests.delete(self._url(f"/v1/projects/{project_id}"),
                            verify=self.verify, timeout=self.timeout)
        self._handle(r)

    # --- Upload ---

    def create_upload_ticket(self, project_id: str, role: str, filename: str) -> dict:
        r = requests.post(
            self._url(f"/v1/projects/{project_id}/upload-tickets"),
            json={"role": role, "original_filename": filename},
            verify=self.verify, timeout=self.timeout,
        )
        return self._handle(r)

    def upload_via_ticket(self, ticket: str, data: BinaryIO | bytes) -> dict:
        """Stream upload using a ticket. Returns upload result."""
        r = requests.put(
            self._url(f"/v1/uploads/{ticket}"),
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            verify=self.verify, timeout=max(self.timeout, 300),
        )
        return self._handle(r)

    def stream_proxy_upload(self, ticket: str, stream) -> dict:
        """Proxy upload from Flask request stream to AILab without storing full file."""
        headers: dict[str, str] = {"Content-Type": "application/octet-stream"}
        r = requests.put(
            self._url(f"/v1/uploads/{ticket}"),
            data=stream,
            headers=headers,
            verify=self.verify,
            timeout=max(self.timeout, 600),
        )
        return self._handle(r)

    # --- Assets ---

    def list_assets(self, project_id: str) -> list[dict]:
        r = requests.get(self._url(f"/v1/projects/{project_id}/assets"),
                         verify=self.verify, timeout=self.timeout)
        return self._handle(r).get("assets", [])

    def get_manifest(self, project_id: str) -> dict:
        r = requests.get(self._url(f"/v1/projects/{project_id}/manifest"),
                         verify=self.verify, timeout=self.timeout)
        return self._handle(r)

    def download_asset(self, project_id: str, link_id: str) -> requests.Response:
        """Returns raw response for streaming to client."""
        r = requests.get(
            self._url(f"/v1/projects/{project_id}/assets/{link_id}/download"),
            verify=self.verify, timeout=max(self.timeout, 300),
            stream=True,
        )
        if r.status_code >= 400:
            self._handle(r)
        return r

    def delete_asset_link(self, project_id: str, link_id: str) -> None:
        r = requests.delete(
            self._url(f"/v1/projects/{project_id}/assets/{link_id}"),
            verify=self.verify, timeout=self.timeout,
        )
        self._handle(r)

    # --- Reference-Time Analysis ---

    def _rt_url(self, path: str) -> str:
        base = self.base.replace("/asset-api", "/reference-time")
        return f"{base}{path}"

    def create_analysis(
        self, project_id: str, gp_link_id: str, gp_sha: str,
        midi_link_ids: list[str],
        audio_link_ids: list[str] | None = None,
        structure_link_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "gp_revision_sha256": gp_sha,
            "gp_asset_link_id": gp_link_id,
            "source_midi_link_ids": midi_link_ids,
            "audio_link_ids": audio_link_ids or [],
        }
        if structure_link_id:
            body["structure_link_id"] = structure_link_id
        r = requests.post(
            self._rt_url(f"/v1/projects/{project_id}/analyses"),
            json=body, verify=self.verify, timeout=self.timeout,
        )
        return self._handle(r)

    def list_analyses(self, project_id: str) -> dict:
        r = requests.get(
            self._rt_url(f"/v1/projects/{project_id}/analyses"),
            verify=self.verify, timeout=self.timeout,
        )
        return self._handle(r)

    def get_analysis_job(self, job_id: str) -> dict:
        r = requests.get(
            self._rt_url(f"/v1/jobs/{job_id}"),
            verify=self.verify, timeout=self.timeout,
        )
        return self._handle(r)

    def get_analysis_report_json(self, project_id: str, analysis_id: str) -> dict:
        r = requests.get(
            self._rt_url(f"/v1/projects/{project_id}/analyses/{analysis_id}/report.json"),
            verify=self.verify, timeout=self.timeout,
        )
        return self._handle(r)

    def get_analysis_report_html(self, project_id: str, analysis_id: str) -> str:
        r = requests.get(
            self._rt_url(f"/v1/projects/{project_id}/analyses/{analysis_id}/report.html"),
            verify=self.verify, timeout=self.timeout,
        )
        if r.status_code >= 400:
            self._handle(r)
        return r.text


_client: AssetAPIClient | None = None


def get_client() -> AssetAPIClient:
    global _client
    if _client is None:
        _client = AssetAPIClient()
    return _client
