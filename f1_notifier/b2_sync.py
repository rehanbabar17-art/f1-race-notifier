#!/usr/bin/env python3
"""Download/upload the F1 notifier deduplication state in Backblaze B2."""

from __future__ import annotations

import os
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

STATE_FILE = Path(os.environ.get("F1_STATE_FILE", "/tmp/f1-state.json"))
B2_KEY = "f1-race-notifier/state.json"


def client():
    required = ["B2_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET"]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError(f"Missing B2 settings: {', '.join(missing)}")
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("B2_ENDPOINT", "https://s3.us-east-005.backblazeb2.com"),
        region_name="us-east-005",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        config=Config(signature_version="s3v4"),
    )


def download():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        body = client().get_object(Bucket=os.environ["B2_BUCKET"], Key=B2_KEY)["Body"].read()
        STATE_FILE.write_bytes(body)
        print(f"[B2] Restored {B2_KEY}.")
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            STATE_FILE.write_text('{"sent": {}}\n', encoding="utf-8")
            print(f"[B2] No {B2_KEY} yet; initialized empty state.")
            return
        raise


def upload():
    if not STATE_FILE.exists():
        raise RuntimeError(f"State file missing: {STATE_FILE}")
    client().put_object(
        Bucket=os.environ["B2_BUCKET"],
        Key=B2_KEY,
        Body=STATE_FILE.read_bytes(),
        ContentType="application/json",
    )
    print(f"[B2] Uploaded {B2_KEY}.")


command = os.environ.get("B2_COMMAND")
try:
    if command == "download":
        download()
    elif command == "upload":
        upload()
    else:
        raise RuntimeError("Set B2_COMMAND to download or upload")
except Exception as error:
    print(f"[B2] State sync failed: {error}")
    raise SystemExit(1)
