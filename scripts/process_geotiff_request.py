#!/usr/bin/env python3
"""Process one AirSat GeoTIFF request and upload a temporary ZIP to Supabase."""

from __future__ import annotations

import csv
import json
import math
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

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:  # Optional visual improvement only.
    arabic_reshaper = None
    get_display = None


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



def display_text(value: Any) -> str:
    """Shape Persian/Arabic text correctly when optional packages are installed."""
    text = str(value or "")
    if arabic_reshaper is not None and get_display is not None and re.search(r"[\u0600-\u06FF]", text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def normalize_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    replacements = {
        "ي": "ی",
        "ك": "ک",
        "\u200c": "",
        " ": "",
        "-": "",
        "_": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def load_precomputed_timeseries(row: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Load province time series already generated in the private airsat-auto repo."""
    if not row.get("province_name"):
        return [], "not-applicable"

    root_raw = os.getenv("AIRSAT_AUTO_ROOT", "").strip()
    if not root_raw:
        return [], "private-repository-not-mounted"

    path = Path(root_raw) / "public" / "data" / "timeseries" / f"{row['pollutant']}.json"
    if not path.exists():
        return [], "precomputed-file-not-found"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], "precomputed-file-invalid"

    metadata = row.get("metadata") or {}
    wanted_id = normalize_name(metadata.get("province_id") or row.get("province_id"))
    wanted_name = normalize_name(row.get("province_name"))

    best = None
    for province in payload.get("provinces", []) or []:
        candidates = {
            normalize_name(province.get("id")),
            normalize_name(province.get("name_fa")),
            normalize_name(province.get("name_en")),
        }
        if wanted_id and wanted_id in candidates:
            best = province
            break
        if wanted_name and wanted_name in candidates:
            best = province
            break

    if not best:
        return [], "province-not-found-in-precomputed-file"

    series = []
    for point in best.get("series", []) or []:
        value = point.get("value")
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        series.append(
            {
                "period": str(point.get("period") or ""),
                "value": value,
                "min": point.get("min"),
                "max": point.get("max"),
                "image_count": point.get("image_count"),
                "interpolated": bool(point.get("interpolated", False)),
            }
        )

    series.sort(key=lambda item: item["period"])
    return series, "airsat-auto-precomputed"


def month_ranges(start_year: int = 2018):
    today = datetime.now(timezone.utc).date()
    stop = today.replace(day=1)
    current = date(start_year, 1, 1)

    while current < stop:
        if current.month == 12:
            following = date(current.year + 1, 1, 1)
        else:
            following = date(current.year, current.month + 1, 1)
        yield current.strftime("%Y-%m"), current.isoformat(), following.isoformat()
        current = following


def compute_region_timeseries(
    cfg: dict[str, Any],
    region: ee.Geometry,
) -> tuple[list[dict[str, Any]], str]:
    """Fallback: calculate completed monthly means for the selected province."""
    if os.getenv("AIRSAT_TIMESERIES_FALLBACK", "true").lower() not in {
        "1", "true", "yes", "on"
    }:
        return [], "fallback-disabled"

    base = ee.ImageCollection(cfg["collection"]).filterBounds(region)
    if cfg["qa"] is not None:
        base = base.map(lambda image: apply_qa_if_available(image, cfg["qa"]))

    features = []
    for label, start, end in month_ranges(2018):
        monthly = base.filterDate(start, end)
        monthly_mean = monthly.select(cfg["band"]).mean()
        reduced = monthly_mean.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=7000,
            bestEffort=True,
            maxPixels=1e13,
            tileScale=4,
        )
        features.append(
            ee.Feature(
                None,
                {
                    "period": label,
                    "value": reduced.get(cfg["band"]),
                    "image_count": monthly.size(),
                },
            )
        )

    payload = ee.FeatureCollection(features).getInfo()
    series = []
    for feature in payload.get("features", []):
        properties = feature.get("properties", {})
        value = properties.get("value")
        count = properties.get("image_count") or 0
        if value is None or count <= 0:
            continue
        series.append(
            {
                "period": str(properties.get("period") or ""),
                "value": float(value),
                "min": None,
                "max": None,
                "image_count": int(count),
                "interpolated": False,
            }
        )

    series.sort(key=lambda item: item["period"])
    return series, "earth-engine-monthly-fallback"


def write_timeseries_files(
    series: list[dict[str, Any]],
    csv_path: Path,
    json_path: Path,
    row: dict[str, Any],
    cfg: dict[str, Any],
    source: str,
) -> None:
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["period", "value", "min", "max", "image_count", "interpolated"],
        )
        writer.writeheader()
        writer.writerows(series)

    json_path.write_text(
        json.dumps(
            {
                "pollutant": row["pollutant"],
                "province": row.get("province_name"),
                "unit": cfg["unit"],
                "source": source,
                "series": series,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def draw_timeseries_chart(
    series: list[dict[str, Any]],
    output: Path,
    row: dict[str, Any],
    cfg: dict[str, Any],
    source: str,
) -> None:
    width, height = 1400, 820
    image = Image.new("RGB", (width, height), "#f1f5f9")
    draw = ImageDraw.Draw(image)

    # Main report card.
    draw.rounded_rectangle((28, 28, width - 28, height - 28), radius=28, fill="white")
    draw.text(
        (70, 62),
        display_text(f"AirSat | {row['pollutant']} | {row.get('province_name') or 'Iran'}"),
        fill="#10263d",
        font=font(30),
    )
    draw.text(
        (70, 112),
        f"Monthly time series | 2018–present | Unit: {cfg['unit']}",
        fill="#64748b",
        font=font(18),
    )

    left, top, right, bottom = 115, 190, width - 80, height - 135
    draw.rounded_rectangle((left, top, right, bottom), radius=14, fill="#f8fafc", outline="#cbd5e1", width=2)

    values = [float(item["value"]) for item in series]
    minimum, maximum = min(values), max(values)
    if math.isclose(minimum, maximum):
        padding = abs(minimum) * 0.08 or 1.0
    else:
        padding = (maximum - minimum) * 0.08
    y_min, y_max = minimum - padding, maximum + padding

    # Grid and y labels.
    for step in range(6):
        y = top + (bottom - top) * step / 5
        value = y_max - (y_max - y_min) * step / 5
        draw.line((left, y, right, y), fill="#e2e8f0", width=1)
        draw.text((38, y - 10), f"{value:.3g}", fill="#64748b", font=font(15))

    count = len(series)
    points = []
    for index, item in enumerate(series):
        x = left if count == 1 else left + (right - left) * index / (count - 1)
        y = bottom - (float(item["value"]) - y_min) * (bottom - top) / (y_max - y_min)
        points.append((x, y))

    if len(points) > 1:
        draw.line(points, fill="#0284c7", width=4, joint="curve")
    for x, y in points[::max(1, len(points)//24)]:
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#0f766e")

    # One label per year where possible.
    last_year = None
    for index, item in enumerate(series):
        year = item["period"][:4]
        if year == last_year:
            continue
        last_year = year
        x = left if count == 1 else left + (right - left) * index / (count - 1)
        draw.line((x, bottom, x, bottom + 7), fill="#64748b", width=1)
        draw.text((x - 19, bottom + 14), year, fill="#475569", font=font(14))

    latest = series[-1]
    draw.text(
        (70, height - 91),
        f"Latest: {latest['period']} = {latest['value']:.6g} {cfg['unit']}",
        fill="#0f766e",
        font=font(18),
    )
    draw.text(
        (width - 500, height - 91),
        f"Source: {source} | airsat.ir",
        fill="#64748b",
        font=font(15),
    )
    image.save(output, "PNG", optimize=True)


def write_shortcuts(folder: Path) -> tuple[Path, Path]:
    windows_shortcut = folder / "AirSat.url"
    windows_shortcut.write_text(
        "[InternetShortcut]\nURL=https://airsat.ir\n",
        encoding="utf-8",
    )

    html_shortcut = folder / "Open_AirSat.html"
    html_shortcut.write_text(
        """<!doctype html>
<html lang="fa" dir="rtl">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=https://airsat.ir">
<title>Open AirSat</title>
</head>
<body>
<p><a href="https://airsat.ir">ورود به سامانه AirSat</a></p>
</body>
</html>
""",
        encoding="utf-8",
    )
    return windows_shortcut, html_shortcut


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
    draw.text((36, 24), display_text(f"AirSat | {row['pollutant']} | {region}"), fill="#10263d", font=font(28))
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

    force_rebuild = os.getenv("FORCE_REBUILD_REQUEST", "false").lower() in {
        "1", "true", "yes", "on"
    }
    if row["status"] == "ready" and not force_rebuild:
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
            preview = folder / f"{base}_map.png"
            timeseries_chart = folder / f"{base}_timeseries.png"
            timeseries_csv = folder / f"{base}_timeseries.csv"
            timeseries_json = folder / f"{base}_timeseries.json"
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

            series = []
            series_source = "not-applicable"
            if row.get("province_name"):
                series, series_source = load_precomputed_timeseries(row)
                if not series:
                    print(f"Precomputed time series unavailable: {series_source}")
                    series, series_source = compute_region_timeseries(cfg, region)

                if series:
                    write_timeseries_files(
                        series,
                        timeseries_csv,
                        timeseries_json,
                        row,
                        cfg,
                        series_source,
                    )
                    draw_timeseries_chart(
                        series,
                        timeseries_chart,
                        row,
                        cfg,
                        series_source,
                    )

            windows_shortcut, html_shortcut = write_shortcuts(folder)

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
                        f"Province time-series points: {len(series)}",
                        f"Province time-series source: {series_source}",
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
                files = [
                    geotiff,
                    preview,
                    metadata,
                    windows_shortcut,
                    html_shortcut,
                ]
                if series:
                    files.extend([timeseries_chart, timeseries_csv, timeseries_json])
                for file in files:
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
                    "message": "بسته شامل GeoTIFF، نقشه قاب‌دار و در صورت انتخاب استان، سری زمانی است؛ تا ۲۴ ساعت قابل دانلود خواهد بود.",
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
