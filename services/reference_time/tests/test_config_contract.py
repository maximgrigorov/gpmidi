"""Cross-service limits that must stay compatible with Asset API."""

from reference_time.config import MAX_GP_BYTES


def test_default_gp_download_limit_matches_asset_api_upload_contract():
    """A GP accepted by Asset API (up to 100 MiB) must be analyzable."""
    assert MAX_GP_BYTES >= 100 * 1024 * 1024
