"""Bounded, streaming access to Asset API blobs.

Downloads are streamed to a temporary file inside the pod's own bounded storage
and never materialized whole in memory. Two independent limits are enforced
*while* reading, so an oversized or slow asset is abandoned as soon as it
crosses a bound rather than after it has already been buffered:

* a per-role hard byte ceiling;
* a per-role wall-clock deadline covering the whole transfer.

Temporary files are removed on success, decode failure, timeout and
cancellation. A `DownloadScope` owns a directory whose entire subtree is removed
when the scope exits, so a process restart mid-analysis leaves at most the
directory of that one run behind, which `purge_stale_scopes` reclaims on startup.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

SCOPE_PREFIX = "rt-scope-"


class AssetFetchError(Exception):
    """Bounded-download failure with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DownloadLimits:
    """Per-role transfer bounds."""

    max_bytes: int
    max_seconds: float


@dataclass(frozen=True)
class DownloadedAsset:
    path: str
    size_bytes: int
    truncated: bool = False


class DownloadScope:
    """A per-analysis temporary directory removed in full when the scope ends."""

    def __init__(self, root: str, name: str):
        os.makedirs(root, exist_ok=True)
        self.path = tempfile.mkdtemp(prefix=f"{SCOPE_PREFIX}{name}-", dir=root)

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


@contextmanager
def download_scope(root: str, name: str):
    scope = DownloadScope(root, name)
    try:
        yield scope
    finally:
        scope.cleanup()


def purge_stale_scopes(root: str) -> int:
    """Remove scope directories left behind by a previous process.

    Called at startup: any scope directory that still exists belongs to a run
    that no longer has an owner, because a live scope is always removed by its
    own context manager.
    """
    removed = 0
    if not os.path.isdir(root):
        return 0
    for entry in sorted(os.listdir(root)):
        if not entry.startswith(SCOPE_PREFIX):
            continue
        shutil.rmtree(os.path.join(root, entry), ignore_errors=True)
        removed += 1
    if removed:
        logger.info("Purged %d stale download scope(s) under %s", removed, root)
    return removed


class AssetClient:
    """Narrow, bounded interface to the Asset API.

    The analyzer only ever learns asset *identity* and *content*; it never sees
    or constructs a physical blob path.
    """

    def __init__(
        self,
        base_url: str,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
        pool_timeout: float,
        metadata_timeout: float,
        chunk_size: int = 65536,
        verify_tls: bool = False,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=write_timeout,
            pool=pool_timeout,
        )
        self._metadata_timeout = metadata_timeout
        self._chunk_size = chunk_size
        self._verify_tls = verify_tls

    # -- metadata ---------------------------------------------------------

    def _get_json(self, path: str, timeout: float) -> dict:
        url = f"{self._base_url}{path}"
        try:
            with httpx.Client(timeout=timeout, verify=self._verify_tls) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            raise AssetFetchError(
                "asset_api_status_error",
                f"Asset API returned HTTP {e.response.status_code} for {path}",
            ) from e
        except httpx.HTTPError as e:
            raise AssetFetchError(
                "asset_api_unreachable", f"Asset API request to {path} failed: {e}"
            ) from e
        except ValueError as e:
            raise AssetFetchError(
                "asset_api_bad_response", f"Asset API returned invalid JSON for {path}: {e}"
            ) from e

    def get_project(self, project_id: str) -> dict:
        """Project record including asset links and GP revisions."""
        return self._get_json(
            f"/asset-api/v1/projects/{project_id}", self._metadata_timeout
        )

    def list_assets(self, project_id: str) -> list[dict]:
        payload = self._get_json(
            f"/asset-api/v1/projects/{project_id}/assets", self._metadata_timeout
        )
        assets = payload.get("assets")
        if not isinstance(assets, list):
            raise AssetFetchError(
                "asset_api_bad_response",
                f"Asset API asset listing for project {project_id} is malformed",
            )
        return assets

    def list_gp_revisions(self, project_id: str) -> list[dict]:
        payload = self.get_project(project_id)
        revisions = payload.get("gp_revisions")
        if revisions is None:
            return []
        if not isinstance(revisions, list):
            raise AssetFetchError(
                "asset_api_bad_response",
                f"Asset API GP revisions for project {project_id} are malformed",
            )
        return revisions

    # -- content ----------------------------------------------------------

    def download(
        self,
        project_id: str,
        link_id: str,
        dest_path: str,
        limits: DownloadLimits,
    ) -> DownloadedAsset:
        """Stream one asset to `dest_path`, enforcing byte and time bounds.

        The partial file is removed before raising so no oversized or truncated
        payload is left on disk.
        """
        url = (
            f"{self._base_url}/asset-api/v1/projects/{project_id}"
            f"/assets/{link_id}/download"
        )
        started = time.monotonic()
        written = 0
        try:
            with httpx.Client(timeout=self._timeout, verify=self._verify_tls) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()

                    # A declared Content-Length over the ceiling is rejected before
                    # a single byte is stored.
                    declared = resp.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > limits.max_bytes:
                        raise AssetFetchError(
                            "asset_too_large",
                            f"Asset {link_id} declares {declared} bytes, over the "
                            f"{limits.max_bytes} byte limit",
                        )

                    with open(dest_path, "wb") as fh:
                        for chunk in resp.iter_bytes(self._chunk_size):
                            written += len(chunk)
                            if written > limits.max_bytes:
                                raise AssetFetchError(
                                    "asset_too_large",
                                    f"Asset {link_id} exceeded the {limits.max_bytes} "
                                    f"byte limit while streaming",
                                )
                            elapsed = time.monotonic() - started
                            if elapsed > limits.max_seconds:
                                raise AssetFetchError(
                                    "asset_download_timeout",
                                    f"Asset {link_id} exceeded the "
                                    f"{limits.max_seconds}s transfer deadline after "
                                    f"{written} bytes",
                                )
                            fh.write(chunk)
        except AssetFetchError:
            _unlink(dest_path)
            raise
        except httpx.HTTPStatusError as e:
            _unlink(dest_path)
            raise AssetFetchError(
                "asset_download_status_error",
                f"Asset API returned HTTP {e.response.status_code} downloading {link_id}",
            ) from e
        except httpx.TimeoutException as e:
            _unlink(dest_path)
            raise AssetFetchError(
                "asset_download_timeout", f"Timed out downloading asset {link_id}: {e}"
            ) from e
        except httpx.HTTPError as e:
            _unlink(dest_path)
            raise AssetFetchError(
                "asset_download_failed", f"Failed to download asset {link_id}: {e}"
            ) from e
        except OSError as e:
            _unlink(dest_path)
            raise AssetFetchError(
                "asset_write_failed", f"Failed to store asset {link_id}: {e}"
            ) from e

        if written == 0:
            _unlink(dest_path)
            raise AssetFetchError(
                "asset_empty", f"Asset {link_id} download produced no bytes"
            )

        return DownloadedAsset(path=dest_path, size_bytes=written)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
