#!/usr/bin/env python3
"""Recover stale AirSat requests that were registered but not dispatched."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def headers() -> dict[str, str]:
    key = required("SUPABASE_SERVICE_ROLE_KEY")
    result = {
        "apikey": key,
        "Content-Type": "application/json",
    }
    if key.startswith("eyJ"):
        result["Authorization"] = f"Bearer {key}"
    return result


def error_text(response: requests.Response) -> str:
    try:
        return json.dumps(response.json(), ensure_ascii=False)
    except Exception:
        return response.text


def stale_pending_requests() -> list[dict[str, Any]]:
    minimum_age = int(os.getenv("PENDING_MIN_AGE_MINUTES", "10"))
    limit = int(os.getenv("MAX_PENDING_REQUESTS", "2"))
    threshold = (
        datetime.now(timezone.utc) - timedelta(minutes=minimum_age)
    ).isoformat()

    query = urlencode(
        {
            "status": "eq.pending",
            "created_at": f"lt.{threshold}",
            "select": "id,status,created_at,metadata",
            "order": "created_at.asc",
            "limit": str(limit),
        }
    )
    url = (
        required("SUPABASE_URL")
        + "/rest/v1/download_requests?"
        + query
    )
    response = requests.get(url, headers=headers(), timeout=60)
    if not response.ok:
        raise RuntimeError(
            f"Could not read pending requests: {error_text(response)}"
        )
    return response.json()


def claim_request(row: dict[str, Any]) -> bool:
    request_id = row["id"]
    url = (
        required("SUPABASE_URL")
        + "/rest/v1/download_requests?"
        + urlencode(
            {
                "id": f"eq.{request_id}",
                "status": "eq.pending",
            }
        )
    )

    metadata = dict(row.get("metadata") or {})
    metadata.update(
        {
            "dispatch_status": "recovered",
            "recovered_at": datetime.now(timezone.utc).isoformat(),
            "message": "درخواست جامانده به‌صورت خودکار از صف بازیابی شد.",
        }
    )

    claim_headers = headers()
    claim_headers["Prefer"] = "return=representation"
    response = requests.patch(
        url,
        headers=claim_headers,
        json={"status": "queued", "metadata": metadata},
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"Could not claim request {request_id}: {error_text(response)}"
        )
    return bool(response.json())


def process_request(request_id: str) -> bool:
    env = dict(os.environ)
    env["REQUEST_ID"] = request_id
    env["FORCE_REBUILD_REQUEST"] = "false"

    result = subprocess.run(
        [sys.executable, "scripts/process_geotiff_request.py"],
        env=env,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    rows = stale_pending_requests()
    if not rows:
        print("No stale pending AirSat requests.")
        return

    failures = []
    for row in rows:
        request_id = row["id"]
        if not claim_request(row):
            print(f"Skipped already-claimed request: {request_id}")
            continue

        print(f"Recovering request: {request_id}")
        if not process_request(request_id):
            failures.append(request_id)

    if failures:
        raise RuntimeError(
            "Recovered requests failed: " + ", ".join(failures)
        )


if __name__ == "__main__":
    main()
