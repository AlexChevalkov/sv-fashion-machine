"""
Helper for uploading generated assets to Cloudflare R2 and getting a
PERMANENT public URL back (instead of an expiring Krea/Airtable URL).

This module is additive and safe:
- It never fails to import even if boto3 is not installed yet (boto3 is
  imported lazily inside the upload function).
- Callers should guard uploads with `r2_is_configured()` and fall back to
  the original URL on any error, so the pipeline keeps working even before
  the R2 secrets are wired up.

Required environment variables (set as GitHub Actions secrets):
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_ENDPOINT          e.g. https://<account-id>.r2.cloudflarestorage.com
    R2_BUCKET            e.g. sv-fashion-assets
    R2_PUBLIC_BASE       e.g. https://pub-xxxxxxxx.r2.dev
"""

import os
import mimetypes


R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT")
R2_BUCKET = os.environ.get("R2_BUCKET", "sv-fashion-assets")
R2_PUBLIC_BASE = (os.environ.get("R2_PUBLIC_BASE") or "").rstrip("/")


def r2_is_configured() -> bool:
    """True only when every R2 setting is present."""
    return bool(
        R2_ACCESS_KEY_ID
        and R2_SECRET_ACCESS_KEY
        and R2_ENDPOINT
        and R2_PUBLIC_BASE
    )


def _build_client():
    # Imported lazily so importing this module never crashes if boto3
    # is not installed in the environment yet.
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def upload_file_to_r2(local_path: str, key: str) -> str:
    """
    Upload a local file to R2 under `key` and return its permanent public URL.

    Raises if R2 is not configured — callers should guard with
    `r2_is_configured()` and handle exceptions with a fallback.
    """
    if not r2_is_configured():
        raise RuntimeError("R2 is not configured (missing R2_* environment variables).")

    key = str(key).lstrip("/")
    content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"

    client = _build_client()
    client.upload_file(
        str(local_path),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type},
    )

    public_url = f"{R2_PUBLIC_BASE}/{key}"
    print("Uploaded to R2:", public_url)
    return public_url
