#!/usr/bin/env python3
"""Process one AirSat GeoTIFF request and upload a temporary ZIP to Supabase."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ee
import requests
from PIL import Image, ImageDraw, ImageFont


POLLUTANTS: dict[str, dict[str, Any]] = {
    "NO2": {
        "collection": "COPERNICUS/S5P/OFFL/L3_NO2",
        "band": "tropospheric_NO2_column_number_density",
        "qa": 0.75,
        "min": 0,
        "max": 0.0002,
        "unit": "mol/m²",
        "palette": ["38bdf8", "22c55e", "fde047", "fb923c", "dc2626"],
    },
    "SO2": {
        "collection": "COPERNICUS/S5P/OFFL/L3_SO2",
        "band": "SO2_column_number_density",
        "qa": 0.5,
        "min": 0,
        "max": 0.0005,
        "unit": "mol/m²",
        "palette": ["c4b5fd", "a855f7", "f472b6", "fb923c", "b91c1c"],
    },
    "CO": {
        "collection": "COPERNICUS/S5P/OFFL/L3_CO",
        "band": "CO_column_number_density",
        "qa": None,
        "min": 0,
        "max": 0.05,
        "unit": "mol/m²",
        "palette": ["67e8f9", "34d399", "fde047", "fb923c", "b91c1c"],
    },
    "O3": {
        "collection": "COPERNICUS/S5P/OFFL/L3_O3",
        "band": "O3_column_number_density",
        "qa": 0.7,
        "min": 0.0001,
        "max": 0.0005,
        "unit": "mol/m²",
        "palette": ["7dd3fc", "67e8f9", "fde68a", "fb923c", "c084fc"],
    },
    "HCHO": {
        "collection": "COPERNICUS/S5P/OFFL/L3_HCHO",
        "band": "tropospheric_HCHO_column_number_density",
        "qa": 0.8,
        "min": 0,
        "max": 0.000002,
        "unit": "mol/m²",
        "palette": ["67e8f9", "86efac", "fde047", "fb923c", "dc2626"],
    },
    "AER_AI": {
        "collection": "COPERNICUS/S5P/OFFL/L3_AER_AI",
        "band": "absorbing_aerosol_index",
        "qa": None,
        "min": -1,
        "max": 3,
        "unit": "index",
        "palette": ["d6d3d1", "fde68a", "f59e0b", "c2410c", "7f1d1d"],
    },
    "CH4": {
        "collection": "COPERNICUS/S5P/OFFL/L3_CH4",
        "band": "CH4_column_volume_mixing_ratio_dry_air",
        "qa": 0.7,
        "min": 1750,
        "max": 1950,
        "unit": "ppb",
        "palette": ["a7f3d0", "bef264", "fde047", "fb923c", "c2410c"],
    },
}


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def service_headers(content_type: str = "application/json") -> dict[str, str]:
    """Support legacy service_role JWTs and new sb_secret_* API keys."""
    key = required("SUPABASE_SERVICE_ROLE_KEY")
    headers = {"apikey": key, "Content-Type": content_type}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def supabase_error(response: requests.Response) -> str:
    try:
        body = response.json()
        return " | ".join(
            str(body.get(k))
            for k in ("message", "details", "hint", "code", "error")
            if body.get(k)
        ) or response.text
    except Exception:
        return response.text or f"HTTP {response.status_code}"


def get_request(request_id: str) -> dict[str, Any]:
    url = required("SUPABASE_URL") + "/rest/v1/download_requests"
    response = requests.get(
        url,
        params={"id": f"eq.{request_id}", "select": "*"},
        headers=service_headers(),
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Could not read request: {supabase_error(response)}")
    rows = response.json()
    if not rows:
        raise RuntimeError("Request not found")

    row = rows[0]
    metadata = row.get("metadata") or {}
    row["period_key"] = metadata.get("period_key", "")
    row["province_id"] = metadata.get("province_id")
    return row


def update_request(request_id: str, payload: dict[str, Any]) -> None:
    current = get_request(request_id)
    metadata = dict(current.get("metadata") or {})
    outgoing = dict(payload)

    for key in ("message", "file_name"):
        if key in outgoing:
            metadata[key] = outgoing.pop(key)

    if "object_path" in outgoing:
        outgoing["object_key"] = outgoing.pop("object_path")
    if "error" in outgoing:
        outgoing["error_message"] = outgoing.pop("error")

    outgoing["metadata"] = metadata

    url = required("SUPABASE_URL") + "/rest/v1/download_requests"
    headers = service_headers()
    headers["Prefer"] = "return=minimal"
    response = requests.patch(
        url,
        params={"id": f"eq.{request_id}"},
        headers=headers,
        json=outgoing,
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(f"Could not update request: {supabase_error(response)}")


def init_earth_engine() -> None:
    info = json.loads(required("EE_SERVICE_ACCOUNT_JSON"))
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as file:
        json.dump(info, file)
        key_path = file.name

    try:
        credentials = ee.ServiceAccountCredentials(info["client_email"], key_path)
        ee.Initialize(credentials=credentials, project=required("EE_PROJECT"))
    finally:
        Path(key_path).unlink(missing_ok=True)


def parse_iso(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def resolve_dates(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return display start, display end and EE-exclusive end."""
    if row.get("date_start") and row.get("date_end"):
        start = parse_iso(row["date_start"])
        end = parse_iso(row["date_end"])
    else:
        key = str(row.get("period_key") or "")
        today = datetime.now(timezone.utc).date()

        if key == "latest_7d":
            start, end = today - timedelta(days=6), today
        elif key == "latest_30d":
            start, end = today - timedelta(days=29), today
        elif key == "latest_90d":
            start, end = today - timedelta(days=89), today
        elif key == "latest_month":
            first = today.replace(day=1)
            end = first - timedelta(days=1)
            start = end.replace(day=1)
        elif key == "current_year":
            start, end = date(today.year, 1, 1), today
        elif key.startswith("annual_"):
            year = int(key.split("_")[1])
            start, end = date(year, 1, 1), date(year, 12, 31)
        elif key.startswith("range_"):
            _, first_year, last_year = key.split("_")
            start = date(int(first_year), 1, 1)
            end = date(int(last_year), 12, 31)
        else:
            raise RuntimeError(f"Unsupported period: {key}")

    return start.isoformat(), end.isoformat(), (end + timedelta(days=1)).isoformat()


def apply_qa_if_available(image: ee.Image, threshold: float | None) -> ee.Image:
    if threshold is None:
        return image
    return ee.Image(
        ee.Algorithms.If(
            image.bandNames().contains("qa_value"),
            image.updateMask(image.select("qa_value").gte(threshold)),
            image,
        )
    )


def build_region(row: dict[str, Any]) -> ee.Geometry:
    provinces = ee.FeatureCollection(required("EE_PROVINCES_ASSET"))
    province_name = row.get("province_name")
    if not province_name:
        return provinces.geometry()

    fields = [
        os.getenv("EE_PROVINCE_NAME_FIELD", "Ostan"),
        "name_fa",
        "NAME_FA",
        "Ostan",
        "ADM1_NAME",
        "NAME_1",
        "province",
    ]

    for field in dict.fromkeys(filter(None, fields)):
        selected = provinces.filter(ee.Filter.eq(field, province_name))
        if int(selected.size().getInfo()) > 0:
            return selected.geometry()

    raise RuntimeError(f"Province geometry not found: {province_name}")


def download_file(url: str, target: Path, timeout: int = 1200) -> None:
    with requests.get(url, stream=True, timeout=timeout) as response:
        response.raise_for_status()
        with target.open("wb") as file:
            shutil.copyfileobj(response.raw, file)


def normalize_tiff_download(downloaded: Path, target: Path) -> None:
    if downloaded.read_bytes()[:2] == b"PK":
        with zipfile.ZipFile(downloaded) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".tif", ".tiff"))
            ]
            if not names:
                raise RuntimeError("GeoTIFF was not found in Earth Engine download")
            target.write_bytes(archive.read(names[0]))
    else:
        downloaded.replace(target)


def font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def compose_preview(
    raw_png: Path,
    output_png: Path,
    row: dict[str, Any],
    cfg: dict[str, Any],
    start: str,
    end: str,
) -> None:
    raw = Image.open(raw_png).convert("RGBA")
    width = 1400
    map_height = round(raw.height * width / raw.width)
    header_height = 130
    footer_height = 230

    canvas = Image.new("RGB", (width, header_height + map_height + footer_height), "white")
    resized = raw.resize((width, map_height), Image.Resampling.LANCZOS)
    canvas.paste(resized, (0, header_height), resized)

    draw = ImageDraw.Draw(canvas)
    region = row.get("province_name") or "Iran"
    draw.text((36, 24), f"AirSat | {row['pollutant']} | {region}", fill="#10263d", font=font(28))
    draw.text(
        (36, 70),
        f"{start} – {end}   |   Sentinel-5P / TROPOMI   |   Google Earth Engine",
        fill="#64748b",
        font=font(18),
    )

    footer_y = header_height + map_height
    draw.rectangle((0, footer_y, width, canvas.height), fill="#f8fafc")
    draw.text(
        (36, footer_y + 28),
        f"Unit: {cfg['unit']}   |   Display range: {cfg['min']} to {cfg['max']}",
        fill="#334155",
        font=font(18),
    )
    draw.text(
        (36, footer_y + 72),
        f"Period: {row.get('period_key') or start + ' to ' + end}",
        fill="#475569",
        font=font(17),
    )
    draw.text(
        (36, footer_y + 122),
        "AirSat – Satellite-Based Air Pollution Monitoring System for Iran",
        fill="#0f766e",
        font=font(19),
    )
    draw.text(
        (36, footer_y + 166),
        "airsat.ir",
        fill="#64748b",
        font=font(16),
    )

    canvas.save(output_png, "PNG", optimize=True)


def safe_filename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("_") or "AirSat_export"


def upload_zip(path: Path, object_path: str) -> None:
    bucket = os.getenv("EXPORT_BUCKET", "airsat-exports")
    encoded_path = "/".join(quote(segment, safe="") for segment in object_path.split("/"))
    url = f"{required('SUPABASE_URL')}/storage/v1/object/{bucket}/{encoded_path}"

    headers = service_headers("application/zip")
    headers["x-upsert"] = "true"

    with path.open("rb") as file:
        response = requests.post(url, headers=headers, data=file, timeout=1200)

    if not response.ok:
        raise RuntimeError(f"Storage upload failed: {supabase_error(response)}")


def main() -> None:
    request_id = required("REQUEST_ID")
    row = get_request(request_id)

    if row["status"] == "ready":
        print("Request is already ready; nothing to do.")
        return
    if row["status"] in {"cancelled", "expired"}:
        print(f"Request status is {row['status']}; nothing to do.")
        return

    update_request(
        request_id,
        {
            "status": "processing",
            "message": "پردازش خودکار GeoTIFF آغاز شد.",
            "error": None,
        },
    )

    try:
        cfg = POLLUTANTS.get(row["pollutant"])
        if not cfg:
            raise RuntimeError(f"Unsupported pollutant: {row['pollutant']}")

        start, end, ee_end = resolve_dates(row)
        init_earth_engine()
        region = build_region(row)

        collection = (
            ee.ImageCollection(cfg["collection"])
            .filterDate(start, ee_end)
            .filterBounds(region)
        )
        count = int(collection.size().getInfo())
        if count == 0:
            raise RuntimeError("No Sentinel-5P images were found for this period")

        if cfg["qa"] is not None:
            collection = collection.map(
                lambda image: apply_qa_if_available(image, cfg["qa"])
            )

        image = collection.select(cfg["band"]).mean().rename("value").clip(region)

        with tempfile.TemporaryDirectory(prefix="airsat_export_") as temp:
            folder = Path(temp)
            base = safe_filename(
                f"AirSat_{row['pollutant']}_{row.get('period_key') or 'custom'}_{request_id[:8]}"
            )
            downloaded = folder / "earthengine_download.bin"
            geotiff = folder / f"{base}.tif"
            raw_preview = folder / "preview_raw.png"
            preview = folder / f"{base}_preview.png"
            metadata = folder / "metadata.txt"
            output_zip = folder / f"{base}.zip"

            download_url = image.getDownloadURL(
                {
                    "name": base,
                    "scale": 7000,
                    "crs": "EPSG:4326",
                    "region": region,
                    "format": "GEO_TIFF",
                    "filePerBand": False,
                }
            )
            download_file(download_url, downloaded)
            normalize_tiff_download(downloaded, geotiff)

            visual = image.visualize(
                min=cfg["min"],
                max=cfg["max"],
                palette=cfg["palette"],
            )
            thumbnail_url = visual.getThumbURL(
                {"region": region, "dimensions": 1400, "format": "png"}
            )
            download_file(thumbnail_url, raw_preview)
            compose_preview(raw_preview, preview, row, cfg, start, end)

            metadata.write_text(
                "\n".join(
                    [
                        "AirSat Output Metadata",
                        "-" * 48,
                        f"Request ID: {request_id}",
                        f"Pollutant: {row['pollutant']}",
                        f"Period key: {row.get('period_key') or 'custom'}",
                        f"Start date: {start}",
                        f"End date: {end}",
                        f"Region: {row.get('province_name') or 'Iran'}",
                        f"Unit: {cfg['unit']}",
                        f"Sentinel-5P image count: {count}",
                        f"Collection: {cfg['collection']}",
                        f"Band: {cfg['band']}",
                        f"Nominal export scale: 7000 m",
                        "CRS: EPSG:4326",
                        "Website: https://airsat.ir",
                    ]
                ),
                encoding="utf-8",
            )

            with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for file in (geotiff, preview, metadata):
                    archive.write(file, file.name)

            # Free Supabase projects currently cap an individual file at 50 MB.
            max_bytes = 50 * 1024 * 1024
            if output_zip.stat().st_size > max_bytes:
                raise RuntimeError(
                    "The generated ZIP is larger than the 50 MB Supabase Free limit"
                )

            object_path = f"{row['user_id']}/{request_id}/{output_zip.name}"
            upload_zip(output_zip, object_path)

            expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            update_request(
                request_id,
                {
                    "status": "ready",
                    "object_path": object_path,
                    "file_name": output_zip.name,
                    "expires_at": expires_at.isoformat(),
                    "message": "فایل آماده است و تا ۲۴ ساعت قابل دانلود خواهد بود.",
                    "error": None,
                },
            )
            print(f"READY: {object_path}")

    except Exception as error:
        update_request(
            request_id,
            {
                "status": "failed",
                "error": str(error),
                "message": "پردازش ناموفق بود؛ جزئیات برای بررسی ثبت شد.",
            },
        )
        raise


if __name__ == "__main__":
    main()
