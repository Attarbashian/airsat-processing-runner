#!/usr/bin/env python3
"""Process one lightweight AirSat GeoTIFF request.

Environment variables:
  REQUEST_ID
  EE_SERVICE_ACCOUNT_JSON
  EE_PROJECT
  EE_PROVINCES_ASSET
  EE_PROVINCE_NAME_FIELD (default: Ostan)
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  EXPORT_BUCKET (default: airsat-exports)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ee
import requests
from PIL import Image, ImageDraw, ImageFont

POLLUTANTS: dict[str, dict[str, Any]] = {
    "NO2": {"collection":"COPERNICUS/S5P/OFFL/L3_NO2","band":"tropospheric_NO2_column_number_density","qa":None,"min":0,"max":0.0002,"unit":"mol/m²","palette":["f4fbff","ccecff","79d2ef","27b3c8","4bc36b","f2d34f","f08a35","d63b32"]},
    "SO2": {"collection":"COPERNICUS/S5P/OFFL/L3_SO2","band":"SO2_column_number_density","qa":None,"min":0,"max":0.0005,"unit":"mol/m²","palette":["f8f5ff","ded5ff","b79cf2","8c66d1","b84eaa","e47886","c73d50"]},
    "CO": {"collection":"COPERNICUS/S5P/OFFL/L3_CO","band":"CO_column_number_density","qa":None,"min":0,"max":0.05,"unit":"mol/m²","palette":["f2fbf7","c6ead9","72c9a9","2aa38e","d6d956","f2a446","d6534f"]},
    "O3": {"collection":"COPERNICUS/S5P/OFFL/L3_O3","band":"O3_column_number_density","qa":None,"min":0.12,"max":0.15,"unit":"mol/m²","palette":["f1faff","c1e7f7","70c8dc","83cf7a","eedc62","ed9b59","a95eb8"]},
    "HCHO": {"collection":"COPERNICUS/S5P/OFFL/L3_HCHO","band":"tropospheric_HCHO_column_number_density","qa":None,"min":0,"max":0.0003,"unit":"mol/m²","palette":["f3fbfa","c4ebe1","6cccb8","50b68b","b8d85d","efb24d","db655c"]},
    "AER_AI": {"collection":"COPERNICUS/S5P/OFFL/L3_AER_AI","band":"absorbing_aerosol_index","qa":None,"min":-1,"max":3,"unit":"index","palette":["3f7fa3","87bdd2","d9eef1","f4edc3","f2cf72","e58b46","bb4b2d","7f2d20"]},
    "CH4": {"collection":"COPERNICUS/S5P/OFFL/L3_CH4","band":"CH4_column_volume_mixing_ratio_dry_air","qa":None,"min":1750,"max":1950,"unit":"ppb","palette":["f4fff8","c9f0d8","83d8aa","43bb80","c7d957","efa045","d65a4d"]},
}


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def headers() -> dict[str, str]:
    key = required("SUPABASE_SERVICE_ROLE_KEY")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def get_request(request_id: str) -> dict[str, Any]:
    url = required("SUPABASE_URL") + "/rest/v1/export_requests"
    r = requests.get(url, params={"id": f"eq.{request_id}", "select": "*"}, headers=headers(), timeout=60)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError("Request not found")
    return rows[0]


def update_request(request_id: str, payload: dict[str, Any]) -> None:
    url = required("SUPABASE_URL") + "/rest/v1/export_requests"
    h = headers(); h["Prefer"] = "return=minimal"
    r = requests.patch(url, params={"id": f"eq.{request_id}"}, headers=h, json=payload, timeout=60)
    r.raise_for_status()


def init_ee() -> None:
    raw = required("EE_SERVICE_ACCOUNT_JSON")
    info = json.loads(raw)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(info, f)
        key_path = f.name
    try:
        creds = ee.ServiceAccountCredentials(info["client_email"], key_path)
        ee.Initialize(credentials=creds, project=required("EE_PROJECT"))
    finally:
        Path(key_path).unlink(missing_ok=True)


def resolve_dates(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("start_date") and row.get("end_date"):
        return str(row["start_date"]), str(row["end_date"])
    key = str(row["period_key"])
    today = datetime.now(timezone.utc).date()
    if key == "latest_7d": start, end = today - timedelta(days=7), today
    elif key == "latest_30d": start, end = today - timedelta(days=30), today
    elif key == "latest_90d": start, end = today - timedelta(days=90), today
    elif key == "latest_month":
        first = today.replace(day=1); end = first; start = (first - timedelta(days=1)).replace(day=1)
    elif key == "current_year": start, end = date(today.year, 1, 1), today
    elif key.startswith("annual_"):
        y = int(key.split("_")[1]); start, end = date(y,1,1), date(y+1,1,1)
    elif key.startswith("range_"):
        _, a, b = key.split("_"); start, end = date(int(a),1,1), date(int(b)+1,1,1)
    else:
        raise RuntimeError(f"Unsupported period: {key}")
    return start.isoformat(), end.isoformat()


def safe_qa(img: ee.Image, threshold: float | None) -> ee.Image:
    if threshold is None:
        return img
    has_qa = img.bandNames().contains("qa_value")
    masked = img.updateMask(img.select("qa_value").gte(threshold))
    return ee.Image(ee.Algorithms.If(has_qa, masked, img))


def build_region(row: dict[str, Any]) -> ee.Geometry:
    provinces = ee.FeatureCollection(required("EE_PROVINCES_ASSET"))
    if row.get("province_name"):
        field = os.getenv("EE_PROVINCE_NAME_FIELD", "Ostan")
        selected = provinces.filter(ee.Filter.eq(field, row["province_name"]))
        if selected.size().getInfo() == 0:
            # fallback over common field names
            for f in ["name_fa", "NAME_FA", "Ostan", "ADM1_NAME", "NAME_1"]:
                selected = provinces.filter(ee.Filter.eq(f, row["province_name"]))
                if selected.size().getInfo() > 0:
                    break
        if selected.size().getInfo() == 0:
            raise RuntimeError(f"Province geometry not found: {row['province_name']}")
        return selected.geometry()
    return provinces.geometry()


def download_file(url: str, path: Path, timeout: int = 900) -> None:
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with path.open("wb") as f:
            shutil.copyfileobj(r.raw, f)


def normalize_tiff_download(downloaded: Path, target: Path) -> None:
    data = downloaded.read_bytes()[:4]
    if data[:2] == b"PK":
        with zipfile.ZipFile(downloaded) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".tif", ".tiff"))]
            if not names:
                raise RuntimeError("GeoTIFF was not found in Earth Engine download")
            target.write_bytes(z.read(names[0]))
    else:
        downloaded.replace(target)


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def gradient_image(width: int, height: int, palette: list[str]) -> Image.Image:
    colors = [hex_rgb(c) for c in palette]
    im = Image.new("RGB", (width, height), "white")
    px = im.load()
    for x in range(width):
        t = x / max(1, width - 1)
        pos = t * (len(colors) - 1)
        i = min(len(colors) - 2, int(pos)); f = pos - i
        c = tuple(round(colors[i][j] * (1 - f) + colors[i + 1][j] * f) for j in range(3))
        for y in range(height): px[x, y] = c
    return im


def compose_preview(raw_png: Path, output_png: Path, row: dict[str, Any], cfg: dict[str, Any], start: str, end: str) -> None:
    raw = Image.open(raw_png).convert("RGBA")
    width = max(raw.width, 1050)
    scale = width / raw.width
    map_h = round(raw.height * scale)
    canvas = Image.new("RGB", (width, map_h + 260), "white")
    canvas.paste(raw.resize((width, map_h), Image.Resampling.LANCZOS), (0, 70), raw.resize((width, map_h), Image.Resampling.LANCZOS))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    region = row.get("province_name") or "Iran"
    draw.text((24, 16), f"AirSat - {row['pollutant']} | {region}", fill="#10263d", font=font)
    draw.text((24, 38), f"{start} to {end} | Sentinel-5P / Google Earth Engine", fill="#64748b", font=font)
    legend_y = map_h + 95
    grad = gradient_image(min(500, width - 80), 16, cfg["palette"])
    gx = (width - grad.width) // 2
    canvas.paste(grad, (gx, legend_y))
    ticks = [cfg["min"] + (cfg["max"] - cfg["min"]) * i / 4 for i in range(5)]
    for i, value in enumerate(ticks):
        tx = gx + round(grad.width * i / 4)
        draw.text((max(4, tx - 26), legend_y + 23), f"{value:.2e}" if abs(value) < .001 else f"{value:g}", fill="#526477", font=font)
    card_y = legend_y + 55
    draw.rounded_rectangle((24, card_y, width - 24, card_y + 80), radius=12, fill="#eef6ff")
    draw.text((40, card_y + 14), f"Pollutant: {row['pollutant']} | Region: {region} | Unit: {cfg['unit']}", fill="#10263d", font=font)
    draw.text((40, card_y + 35), f"Period: {row['period_key']} | Display range: {cfg['min']} to {cfg['max']}", fill="#405166", font=font)
    draw.text((40, card_y + 56), "AirSat | airsat.ir", fill="#6b7d90", font=font)
    canvas.save(output_png, "PNG", optimize=True)


def upload_zip(path: Path, object_path: str) -> None:
    bucket = os.getenv("EXPORT_BUCKET", "airsat-exports")
    segments = "/".join(requests.utils.quote(s, safe="") for s in object_path.split("/"))
    url = f"{required('SUPABASE_URL')}/storage/v1/object/{bucket}/{segments}"
    key = required("SUPABASE_SERVICE_ROLE_KEY")
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/zip", "x-upsert": "true"}
    with path.open("rb") as f:
        r = requests.post(url, headers=h, data=f, timeout=1200)
    r.raise_for_status()


def main() -> None:
    request_id = required("REQUEST_ID")
    row = get_request(request_id)
    if row["status"] not in {"queued_auto", "processing"}:
        print(f"Request status is {row['status']}; nothing to do")
        return
    update_request(request_id, {"status": "processing", "message": "پردازش خودکار آغاز شد."})
    try:
        cfg = POLLUTANTS.get(row["pollutant"])
        if not cfg: raise RuntimeError("Unsupported pollutant")
        start, end = resolve_dates(row)
        init_ee()
        region = build_region(row)
        coll = ee.ImageCollection(cfg["collection"]).filterDate(start, end).filterBounds(region)
        count = int(coll.size().getInfo())
        if count == 0: raise RuntimeError("No satellite images were found for this period")
        # OFFL/L3 collections are already screened during ingestion. Keep the
        # scientific export masked as published; only public previews may use
        # separate display-only interpolation in the static pipeline.
        image = coll.select(cfg["band"]).mean().rename("value").clip(region)

        with tempfile.TemporaryDirectory(prefix="airsat_") as tmp:
            folder = Path(tmp)
            base = f"AirSat_{row['pollutant']}_{row['period_key']}_{row.get('province_name') or 'Iran'}".replace(" ", "_")
            raw = folder / "earthengine_download.bin"
            tif = folder / f"{base}.tif"
            png_raw = folder / "preview_raw.png"
            png = folder / f"{base}_preview.png"
            metadata = folder / "metadata.txt"
            shortcut = folder / "AirSat.url"
            out_zip = folder / f"{base}.zip"

            dl_url = image.getDownloadURL({"name": base, "scale": 7000, "crs": "EPSG:4326", "region": region, "format": "GEO_TIFF", "filePerBand": False})
            download_file(dl_url, raw)
            normalize_tiff_download(raw, tif)
            vis = image.visualize(min=cfg["min"], max=cfg["max"], palette=cfg["palette"])
            thumb_url = vis.getThumbURL({"region": region, "dimensions": 1400, "format": "png"})
            download_file(thumb_url, png_raw)
            compose_preview(png_raw, png, row, cfg, start, end)
            metadata.write_text(
                "\n".join([
                    "AirSat Output Metadata", "-" * 40,
                    f"Pollutant: {row['pollutant']}", f"Period: {row['period_key']}",
                    f"Start: {start}", f"End: {end}", f"Region: {row.get('province_name') or 'Iran'}",
                    f"Unit: {cfg['unit']}", f"Image count: {count}",
                    f"Collection: {cfg['collection']}", f"Band: {cfg['band']}",
                    f"Display min: {cfg['min']}", f"Display max: {cfg['max']}",
                    f"Palette: {', '.join(cfg['palette'])}", "Website: https://airsat.ir"
                ]), encoding="utf-8"
            )
            shortcut.write_text("[InternetShortcut]\nURL=https://airsat.ir\n", encoding="utf-8")
            with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
                for file in (tif, png, metadata, shortcut): z.write(file, file.name)
            object_path = f"{row['user_id']}/{request_id}/{out_zip.name}"
            upload_zip(out_zip, object_path)
            expires = datetime.now(timezone.utc) + timedelta(hours=24)
            update_request(request_id, {
                "status": "ready", "object_path": object_path, "file_name": out_zip.name,
                "expires_at": expires.isoformat(), "message": "فایل آماده است و تا ۲۴ ساعت در پنل قابل دانلود خواهد بود.",
                "error": None
            })
            print("Ready:", object_path)
    except Exception as exc:
        update_request(request_id, {"status": "failed", "error": str(exc), "message": "پردازش خودکار ناموفق بود و درخواست برای بررسی ثبت شد."})
        raise


if __name__ == "__main__":
    main()
