#!/usr/bin/env python3
"""AirSat Export Processor v3.3: create GeoTIFF and polished branded outputs."""

from __future__ import annotations

import base64
import csv
import json
import math
import os
import re
import shutil
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import ee
import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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

POLLUTANT_FA = {
    "NO2": "دی‌اکسید نیتروژن",
    "SO2": "دی‌اکسید گوگرد",
    "CO": "مونوکسید کربن",
    "O3": "ازن",
    "HCHO": "فرمالدهید",
    "AER_AI": "شاخص جذب آئروسل",
    "CH4": "متان",
}

PERIOD_FA = {
    "latest_7d": "هفت روز اخیر",
    "latest_30d": "سی روز اخیر",
    "latest_90d": "نود روز اخیر",
    "latest_month": "ماه کامل قبلی",
    "current_year": "سال جاری",
}

ZIP_CONTENT_KEYS = [
    "geotiff",
    "map_png",
    "metadata_txt",
    "shortcut",
    "timeseries_png",
    "timeseries_csv",
    "timeseries_json",
]

ZIP_CONTENT_LABELS_FA = {
    "geotiff": "GeoTIFF",
    "map_png": "نقشه PNG",
    "metadata_txt": "شناسنامه خروجی",
    "shortcut": "میان‌بر AirSat",
    "timeseries_png": "نمودار سری زمانی",
    "timeseries_csv": "جدول CSV سری زمانی",
    "timeseries_json": "داده JSON سری زمانی",
}

OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_USER_AGENT = "AirSat/3.3 (https://airsat.ir; airsat.iran@gmail.com)"
REPORT_BLUE_TOP = "#0f4f91"
REPORT_BLUE_BOTTOM = "#1768ad"
REPORT_INK = "#18344f"
REPORT_MUTED = "#60788f"
REPORT_LINE = "#c9d6e3"
REPORT_OVERLAY_OPACITY = 0.80


def period_fa(period_key: str, start: str, end: str) -> str:
    key = str(period_key or "")
    if key in PERIOD_FA:
        return PERIOD_FA[key]
    if key.startswith("annual_"):
        return f"سال {key.split('_')[-1]}"
    if key.startswith("range_"):
        parts = key.split("_")
        return f"از سال {parts[1]} تا {parts[2]}"
    return f"از {start} تا {end}"



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

    for key in ("message", "file_name", "notification_results"):
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
    province_name = str(row.get("province_name") or "").strip()
    if not province_name:
        raise RuntimeError(
            "Province selection is required; nationwide GeoTIFF export is disabled."
        )

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


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Use the Persian Vazir typeface installed by the GitHub workflow."""
    font_dir = Path(os.getenv("AIRSAT_FONT_DIR", "/usr/local/share/fonts/airsat"))
    preferred = font_dir / ("Vazir-Bold.ttf" if bold else "Vazir-Regular.ttf")

    candidates = [
        preferred,
        font_dir / "Vazir-Regular.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size, layout_engine=ImageFont.Layout.RAQM)

    raise RuntimeError(
        "Vazir font was not installed. Check the Install Vazir font workflow step."
    )



def shape_fa(value: Any) -> str:
    """Keep logical Unicode order; Pillow RAQM performs Persian shaping."""
    return str(value or "")


def display_text(value: Any) -> str:
    return str(value or "")


def has_persian(value: Any) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", str(value or "")))


def text_width(
    draw: ImageDraw.ImageDraw,
    value: Any,
    selected_font: ImageFont.ImageFont,
    *,
    rtl: bool | None = None,
) -> float:
    text = str(value or "")
    if rtl is None:
        rtl = has_persian(text)
    kwargs = {"direction": "rtl", "language": "fa"} if rtl else {"direction": "ltr"}
    return float(draw.textlength(text, font=selected_font, **kwargs))


def draw_rtl(
    draw: ImageDraw.ImageDraw,
    right_x: float,
    y: float,
    value: Any,
    *,
    selected_font: ImageFont.ImageFont,
    fill: str,
) -> float:
    text = str(value or "")
    draw.text(
        (right_x, y),
        text,
        font=selected_font,
        fill=fill,
        anchor="rt",
        direction="rtl",
        language="fa",
    )
    return text_width(draw, text, selected_font, rtl=True)


def draw_centered_rtl(
    draw: ImageDraw.ImageDraw,
    center_x: float,
    y: float,
    value: Any,
    *,
    selected_font: ImageFont.ImageFont,
    fill: str,
) -> None:
    draw.text(
        (center_x, y),
        str(value or ""),
        font=selected_font,
        fill=fill,
        anchor="mt",
        direction="rtl",
        language="fa",
    )


def draw_ltr(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    value: Any,
    *,
    selected_font: ImageFont.ImageFont,
    fill: str,
    anchor: str = "lt",
) -> float:
    text = str(value or "")
    draw.text(
        (x, y),
        text,
        font=selected_font,
        fill=fill,
        anchor=anchor,
        direction="ltr",
    )
    return text_width(draw, text, selected_font, rtl=False)


def draw_label_value_rtl(
    draw: ImageDraw.ImageDraw,
    right_x: float,
    y: float,
    label_fa: str,
    value_ltr: Any,
    *,
    label_font: ImageFont.ImageFont,
    value_font: ImageFont.ImageFont,
    fill: str = REPORT_INK,
    gap: int = 12,
) -> None:
    label_width = draw_rtl(
        draw,
        right_x,
        y,
        label_fa,
        selected_font=label_font,
        fill=fill,
    )
    value = str(value_ltr or "")
    value_right = right_x - label_width - gap
    if has_persian(value):
        draw.text(
            (value_right, y),
            value,
            font=value_font,
            fill=fill,
            anchor="rt",
            direction="rtl",
            language="fa",
        )
    else:
        draw.text(
            (value_right, y),
            value,
            font=value_font,
            fill=fill,
            anchor="rt",
            direction="ltr",
        )


def persian_digits(value: Any) -> str:
    return str(value).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_days = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + 365 * gy
        + (gy2 + 3) // 4
        - (gy2 + 99) // 100
        + (gy2 + 399) // 400
        + gd
        + g_days[gm - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + days // 31
        jd = 1 + days % 31
    else:
        jm = 7 + (days - 186) // 30
        jd = 1 + (days - 186) % 30
    return jy, jm, jd


def generated_time_fa() -> str:
    now = datetime.now(timezone(timedelta(hours=3, minutes=30)))
    jy, jm, jd = gregorian_to_jalali(now.year, now.month, now.day)
    return persian_digits(f"{jy}/{jm}/{jd}، {now:%H:%M}")


def draw_gradient_round_rect(
    image: Image.Image,
    box: tuple[int, int, int, int],
    radius: int,
    left_color: str,
    right_color: str,
) -> None:
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    left_rgb = tuple(int(left_color[i:i+2], 16) for i in (1, 3, 5))
    right_rgb = tuple(int(right_color[i:i+2], 16) for i in (1, 3, 5))
    gradient = Image.new("RGB", (width, height), left_color)
    pixels = gradient.load()
    for x in range(width):
        ratio = x / max(1, width - 1)
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(left_rgb, right_rgb))
        for y in range(height):
            pixels[x, y] = color
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.paste(gradient, (left, top), mask)


def draw_report_background(width: int = 1800, height: int = 1260) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), "#edf6fd")
    bg = Image.new("RGB", (width, height), "#edf6fd")
    pixels = bg.load()
    a = (237, 246, 253)
    b = (248, 251, 254)
    for x in range(width):
        ratio = x / max(1, width - 1)
        color = tuple(round(v1 + (v2 - v1) * ratio) for v1, v2 in zip(a, b))
        for y in range(height):
            pixels[x, y] = color
    image.paste(bg)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((72, 72, width - 72, height - 72), radius=28, fill="#ffffff", outline="#d6e5f1", width=2)
    return image, draw


def load_brand_logo(max_width: int = 330, max_height: int = 105) -> Image.Image | None:
    root = os.getenv("AIRSAT_AUTO_ROOT", "").strip()
    candidates = []
    if root:
        candidates.append(Path(root) / "public" / "assets" / "airsat-logo.png")
    candidates.extend([
        Path("target/public/assets/airsat-logo.png"),
        Path("public/assets/airsat-logo.png"),
    ])
    for candidate in candidates:
        if candidate.exists():
            logo = Image.open(candidate).convert("RGBA")
            logo.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            return logo
    return None

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



COUNTRY_LEVEL_REGION_NAMES = {
    "iran",
    "islamicrepublicofiran",
    "ایران",
    "کلایران",
    "سراسرایران",
    "all",
    "country",
    "national",
}


def validate_province_only_request(row: dict[str, Any]) -> None:
    """Defense in depth: processor must never export the whole country."""
    province_name = normalize_name(row.get("province_name"))
    roi_type = str(row.get("roi_type") or "").strip().lower()

    if (
        roi_type != "province"
        or not province_name
        or province_name in COUNTRY_LEVEL_REGION_NAMES
    ):
        raise RuntimeError(
            "GeoTIFF export is restricted to one selected province. "
            "Nationwide export is not permitted."
        )


def region_bbox(region: ee.Geometry, padding_ratio: float = 0.075) -> tuple[float, float, float, float]:
    coordinates = region.bounds(maxError=1000).coordinates().getInfo()[0]
    longitudes = [float(point[0]) for point in coordinates]
    latitudes = [float(point[1]) for point in coordinates]
    west, east = min(longitudes), max(longitudes)
    south, north = min(latitudes), max(latitudes)
    dx = max(0.05, (east - west) * padding_ratio)
    dy = max(0.05, (north - south) * padding_ratio)
    return west - dx, south - dy, east + dx, north + dy


def tile_xy(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    scale = 2 ** zoom
    x = (lon + 180.0) / 360.0 * scale
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * scale
    return x, y


def choose_osm_zoom(
    bbox: tuple[float, float, float, float],
    target_width: int,
    target_height: int,
) -> int:
    west, south, east, north = bbox
    for zoom in range(12, 4, -1):
        x1, y1 = tile_xy(west, north, zoom)
        x2, y2 = tile_xy(east, south, zoom)
        pixel_width = abs(x2 - x1) * 256
        pixel_height = abs(y2 - y1) * 256
        tile_count = (math.floor(x2) - math.floor(x1) + 1) * (math.floor(y2) - math.floor(y1) + 1)
        if pixel_width >= target_width * 0.9 and pixel_height >= target_height * 0.75 and tile_count <= 36:
            return zoom
    return 6


def osm_tile_path(cache_dir: Path, zoom: int, x: int, y: int) -> Path:
    return cache_dir / str(zoom) / str(x) / f"{y}.png"


def download_osm_tile(cache_dir: Path, zoom: int, x: int, y: int) -> Image.Image:
    path = osm_tile_path(cache_dir, zoom, x, y)
    if path.exists() and path.stat().st_size > 1000:
        return Image.open(path).convert("RGB")

    path.parent.mkdir(parents=True, exist_ok=True)
    url = OSM_TILE_URL.format(z=zoom, x=x, y=y)
    response = requests.get(
        url,
        headers={
            "User-Agent": OSM_USER_AGENT,
            "Referer": "https://airsat.ir/",
        },
        timeout=45,
    )
    response.raise_for_status()
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(response.content)
    temporary.replace(path)
    time.sleep(0.08)
    return Image.open(path).convert("RGB")


def build_osm_basemap(
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> Image.Image:
    cache_dir = Path(os.getenv("AIRSAT_OSM_CACHE_DIR", ".cache/airsat-osm"))
    west, south, east, north = bbox
    zoom = choose_osm_zoom(bbox, width, height)
    x1, y1 = tile_xy(west, north, zoom)
    x2, y2 = tile_xy(east, south, zoom)
    min_x, max_x = math.floor(x1), math.floor(x2)
    min_y, max_y = math.floor(y1), math.floor(y2)
    scale = 2 ** zoom

    mosaic = Image.new(
        "RGB",
        ((max_x - min_x + 1) * 256, (max_y - min_y + 1) * 256),
        "#e8edf2",
    )
    for tile_x in range(min_x, max_x + 1):
        for tile_y in range(min_y, max_y + 1):
            wrapped_x = tile_x % scale
            if tile_y < 0 or tile_y >= scale:
                continue
            tile = download_osm_tile(cache_dir, zoom, wrapped_x, tile_y)
            mosaic.paste(tile, ((tile_x - min_x) * 256, (tile_y - min_y) * 256))

    crop_box = (
        round((x1 - min_x) * 256),
        round((y1 - min_y) * 256),
        round((x2 - min_x) * 256),
        round((y2 - min_y) * 256),
    )
    cropped = mosaic.crop(crop_box)
    return cropped.resize((width, height), Image.Resampling.LANCZOS)


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    augmented = [list(row) + [vector[index]] for index, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * base
                for value, base in zip(augmented[row], augmented[column])
            ]
    return [row[-1] for row in augmented]


def add_months_label(label: str, count: int) -> str:
    match = re.search(r"(\d{4})-(\d{1,2})", str(label))
    if not match:
        return str(label)
    year, month = int(match.group(1)), int(match.group(2))
    month_index = year * 12 + month - 1 + count
    return f"{month_index // 12:04d}-{month_index % 12 + 1:02d}"


def lightweight_seasonal_forecast(
    series: list[dict[str, Any]],
    pollutant: str,
    horizon: int = 6,
) -> dict[str, Any] | None:
    values = [float(item["value"]) for item in series]
    count = len(values)
    if count < 3:
        return None
    non_negative = pollutant != "AER_AI"
    model: dict[str, Any] | None = None

    if count >= 24:
        size = 4
        xtx = [[0.0] * size for _ in range(size)]
        xty = [0.0] * size
        samples = []
        for index in range(12, count):
            features = [1.0, float(index), values[index - 1], values[index - 12]]
            target = values[index]
            samples.append((features, target))
            for i in range(size):
                xty[i] += features[i] * target
                for j in range(size):
                    xtx[i][j] += features[i] * features[j]
        for index in range(size):
            xtx[index][index] += 1e-9
        beta = solve_linear_system(xtx, xty)
        if beta:
            residuals = [
                target - sum(value * beta[i] for i, value in enumerate(features))
                for features, target in samples
            ]
            rss = sum(value * value for value in residuals)
            mean = sum(target for _, target in samples) / len(samples)
            tss = sum((target - mean) ** 2 for _, target in samples)
            model = {
                "type": "seasonal",
                "name_fa": "خودرگرسیون فصلی سبک AR(1,12)",
                "beta": beta,
                "sigma": math.sqrt(rss / max(1, len(samples) - size)),
                "r2": 1 - rss / tss if tss > 0 else 1.0,
            }

    if model is None:
        slope, intercept, r2 = compute_linear_trend(values)
        residuals = [value - (slope * index + intercept) for index, value in enumerate(values)]
        sigma = math.sqrt(sum(value * value for value in residuals) / max(1, count - 2))
        model = {
            "type": "linear",
            "name_fa": "روند خطی سبک",
            "slope": slope,
            "intercept": intercept,
            "sigma": sigma,
            "r2": r2,
        }

    work = list(values)
    forecast = []
    for step in range(1, horizon + 1):
        index = len(work)
        if model["type"] == "seasonal":
            b0, b1, phi1, phi12 = model["beta"]
            value = b0 + b1 * index + phi1 * work[index - 1] + phi12 * work[index - 12]
        else:
            value = model["slope"] * index + model["intercept"]
        if non_negative:
            value = max(0.0, value)
        work.append(value)
        margin = 1.96 * float(model.get("sigma") or 0.0) * math.sqrt(step)
        forecast.append({
            "period": add_months_label(series[-1]["period"], step),
            "value": value,
            "low": max(0.0, value - margin) if non_negative else value - margin,
            "high": value + margin,
        })
    model["forecast"] = forecast
    return model

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
    forecast_model: dict[str, Any] | None,
) -> None:
    with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "period", "value", "kind", "low", "high",
                "min", "max", "image_count", "interpolated",
            ],
        )
        writer.writeheader()
        for item in series:
            writer.writerow({**item, "kind": "observed", "low": None, "high": None})
        for item in (forecast_model or {}).get("forecast", []):
            writer.writerow({**item, "kind": "forecast"})

    json_path.write_text(
        json.dumps(
            {
                "pollutant": row["pollutant"],
                "province": row.get("province_name"),
                "unit": cfg["unit"],
                "source": source,
                "series": series,
                "forecast_model": forecast_model,
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
    forecast_model: dict[str, Any] | None,
) -> None:
    width, height = 1800, 1260
    image, draw = draw_report_background(width, height)
    pollutant_fa = POLLUTANT_FA[row["pollutant"]]
    province = row.get("province_name") or "کل کشور"
    title = f"سری زمانی ماهانه {pollutant_fa} در استان {province}"
    draw_header(image, draw, width, title, generated_time_fa())

    chart_x, chart_y, chart_w, chart_h = 106, 260, 1588, 710
    draw.rounded_rectangle((chart_x, chart_y, chart_x + chart_w, chart_y + chart_h), radius=22, fill="#fbfdff", outline="#d8e6f1", width=2)

    content_left, content_top = chart_x + 38, chart_y + 28
    content_right, content_bottom = chart_x + chart_w - 38, chart_y + chart_h - 28
    plot_left, plot_top = content_left + 110, content_top + 120
    plot_right, plot_bottom = content_right - 35, content_bottom - 86

    observed_values = [float(item["value"]) for item in series]
    forecast = (forecast_model or {}).get("forecast", [])
    y_candidates = list(observed_values)
    for item in forecast:
        y_candidates.extend([float(item["low"]), float(item["high"])])
    minimum, maximum = min(y_candidates), max(y_candidates)
    padding = (maximum - minimum) * 0.10 if not math.isclose(minimum, maximum) else abs(minimum) * 0.08 or 1.0
    y_min, y_max = minimum - padding, maximum + padding

    legend_y = content_top + 45
    legend_items = [
        ("#135fb3", "مشاهده‌شده"),
        ("#8ca9c8", "روند"),
        ("#ef8b2c", "پیش‌بینی"),
    ]
    legend_centers = [content_left + 360, content_left + 730, content_left + 1080]
    for (color, label), center in zip(legend_items, legend_centers):
        draw.line((center - 85, legend_y + 10, center - 30, legend_y + 10), fill=color, width=5)
        draw_rtl(draw, center + 70, legend_y - 4, label, selected_font=font(18), fill="#536a80")

    for step in range(6):
        y = plot_top + (plot_bottom - plot_top) * step / 5
        value = y_max - (y_max - y_min) * step / 5
        draw.line((plot_left, y, plot_right, y), fill="#d7e2ee", width=1)
        label = format_value(value)
        draw_ltr(draw, plot_left - 18, y - 10, label, selected_font=font(16), fill="#5d748c", anchor="rt")

    combined_count = len(series) + len(forecast)
    x_of = lambda index: plot_left + (plot_right - plot_left) * index / max(1, combined_count - 1)
    y_of = lambda value: plot_bottom - (float(value) - y_min) * (plot_bottom - plot_top) / max(1e-18, y_max - y_min)

    observed_points = [(x_of(index), y_of(item["value"])) for index, item in enumerate(series)]
    if len(observed_points) > 1:
        draw.line(observed_points, fill="#135fb3", width=6, joint="curve")
    for px, py_ in observed_points:
        draw.ellipse((px - 4, py_ - 4, px + 4, py_ + 4), fill="#135fb3")

    slope, intercept, r2 = compute_linear_trend(observed_values)
    trend_points = [(x_of(index), y_of(slope * index + intercept)) for index in range(len(series))]
    if len(trend_points) > 1:
        draw.line(trend_points, fill="#8ca9c8", width=3)

    if forecast:
        upper = [(x_of(len(series) + index), y_of(item["high"])) for index, item in enumerate(forecast)]
        lower = [(x_of(len(series) + index), y_of(item["low"])) for index, item in enumerate(forecast)]
        band_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(band_layer).polygon(upper + list(reversed(lower)), fill=(239, 139, 44, 42))
        image.paste(band_layer, (0, 0), band_layer)
        draw = ImageDraw.Draw(image)
        forecast_points = [(x_of(len(series) + index), y_of(item["value"])) for index, item in enumerate(forecast)]
        path = [observed_points[-1], *forecast_points]
        for index in range(len(path) - 1):
            x1, y1 = path[index]
            x2, y2 = path[index + 1]
            segments = 16
            for segment in range(0, segments, 2):
                a = segment / segments
                b = min(1.0, (segment + 1) / segments)
                draw.line((x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, x1 + (x2 - x1) * b, y1 + (y2 - y1) * b), fill="#ef8b2c", width=5)
        for px, py_ in forecast_points:
            draw.ellipse((px - 5, py_ - 5, px + 5, py_ + 5), fill="#ef8b2c")

    labels: list[tuple[int, str]] = []
    seen_years: set[str] = set()
    for index, item in enumerate(series):
        year = item["period"][:4]
        if year not in seen_years:
            labels.append((index, year))
            seen_years.add(year)
    if forecast:
        labels.append((combined_count - 1, forecast[-1]["period"]))
    for index, label in labels:
        px = x_of(index)
        draw.line((px, plot_bottom, px, plot_bottom + 8), fill="#6e8297", width=1)
        draw_ltr(draw, px, plot_bottom + 16, label, selected_font=font(15), fill="#5b7087", anchor="mt")

    draw_ltr(draw, content_right - 10, content_bottom - 25, "© AirSat", selected_font=font(18), fill="#93a8bd", anchor="rt")

    meta_y, meta_h = 994, 188
    draw.rounded_rectangle((chart_x, meta_y, chart_x + chart_w, meta_y + meta_h), radius=20, fill="#edf5fb", outline="#d7e6f1", width=2)

    mean_value = sum(observed_values) / len(observed_values)
    right_x, left_x = width - 130, 130
    draw_rtl(draw, right_x, meta_y + 28, f"آلاینده: {pollutant_fa}   |   استان: {province}", selected_font=font(20, bold=True), fill="#17324d")
    draw_label_value_rtl(draw, right_x, meta_y + 68, "واحد:", cfg["unit"], label_font=font(18), value_font=font(18), fill="#405a72")
    draw_label_value_rtl(draw, right_x - 300, meta_y + 68, "بازه:", period_fa(row.get("period_key"), series[0]["period"], series[-1]["period"]), label_font=font(18), value_font=font(18), fill="#405a72")
    draw_ltr(draw, right_x, meta_y + 108, f"Start: {series[0]['period']}   |   End: {series[-1]['period']}   |   n = {len(series)}", selected_font=font(17), fill="#405a72", anchor="rt")

    draw_rtl(draw, left_x + 600, meta_y + 28, "خلاصه آماری", selected_font=font(20, bold=True), fill="#17324d")
    draw_ltr(draw, left_x, meta_y + 68, f"Mean: {format_value(mean_value)}   |   Min: {format_value(min(observed_values))}   |   Max: {format_value(max(observed_values))}", selected_font=font(17), fill="#405a72")
    draw_ltr(draw, left_x, meta_y + 108, f"Source: {source}", selected_font=font(15), fill="#64748b")
    draw_ltr(draw, left_x, meta_y + 142, f"y = {slope:.3e}x + {intercept:.3e}   |   R² = {r2:.3f}", selected_font=font(16), fill="#36536d")
    model_name = (forecast_model or {}).get("name_fa") or "پیش‌بینی در دسترس نیست"
    draw_rtl(draw, right_x, meta_y + 142, model_name, selected_font=font(15), fill="#6b4b21")

    draw.rectangle((72, 1126, width - 72, 1188), fill="#f5f9fc")
    draw_ltr(draw, width / 2, 1147, "© AirSat  •  Satellite-powered air monitoring for Iran  •  Sentinel-5P / TROPOMI  •  airsat.ir", selected_font=font(17, bold=True), fill="#6b8195", anchor="mt")
    image.save(output, "PNG", optimize=True)


def write_shortcut(folder: Path) -> Path:
    windows_shortcut = folder / "AirSat.url"
    windows_shortcut.write_text(
        "[InternetShortcut]\nURL=https://airsat.ir\n",
        encoding="utf-8",
    )
    return windows_shortcut


def format_value(value: float) -> str:
    absolute = abs(float(value))
    if absolute == 0:
        return "0"
    if absolute >= 100:
        return f"{value:.1f}"
    if absolute >= 10:
        return f"{value:.2f}"
    if absolute >= 1:
        return f"{value:.3f}"
    return f"{value:.3e}".replace("e-0", "e-").replace("e+0", "e+")


def draw_header(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    width: int,
    title_fa: str,
    generated_at: str,
) -> None:
    pad = 72
    header_h = 155
    header = (pad, pad, width - pad, pad + header_h)
    draw_gradient_round_rect(image, header, 28, "#053d7b", "#2aa9e8")

    draw_ltr(
        draw,
        106,
        106,
        "Sentinel-5P / TROPOMI  •  Google Earth Engine  •  airsat.ir",
        selected_font=font(18),
        fill="#d9efff",
    )
    draw_ltr(draw, 106, 148, generated_at, selected_font=font(16), fill="#c1e2f8")

    draw_ltr(
        draw,
        width - 192,
        98,
        "AirSat",
        selected_font=font(44, bold=True),
        fill="white",
        anchor="rt",
    )
    draw_rtl(
        draw,
        width - 192,
        151,
        title_fa,
        selected_font=font(29, bold=True),
        fill="#edf8ff",
    )


def compute_linear_trend(values: list[float]) -> tuple[float, float, float]:
    n = len(values)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in values)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, values))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    return slope, intercept, r2


def draw_color_legend(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    cfg: dict[str, Any],
) -> None:
    left, top, right, bottom = box
    colors = [tuple(int(color[i:i+2], 16) for i in (0, 2, 4)) for color in cfg["palette"]]
    gradient = Image.new("RGB", (right - left, bottom - top), colors[0])
    pixels = gradient.load()
    for x in range(gradient.width):
        position = x / max(1, gradient.width - 1) * (len(colors) - 1)
        index = min(len(colors) - 2, int(position))
        ratio = position - index
        color = tuple(round(colors[index][c] + (colors[index + 1][c] - colors[index][c]) * ratio) for c in range(3))
        for y in range(gradient.height):
            pixels[x, y] = color
    image.paste(gradient, (left, top))
    draw.rectangle(box, outline="#72869a", width=1)
    draw.text((left, bottom + 8), format_value(cfg["min"]), fill="#4f6579", font=font(14))
    max_label = format_value(cfg["max"])
    max_width = text_width(draw, max_label, font(14))
    draw.text((right - max_width, bottom + 8), max_label, fill="#4f6579", font=font(14))


def compose_preview(
    raw_png: Path,
    output_png: Path,
    row: dict[str, Any],
    cfg: dict[str, Any],
    start: str,
    end: str,
    bbox: tuple[float, float, float, float],
) -> None:
    width, height = 1800, 1260
    image, draw = draw_report_background(width, height)
    pollutant_fa = POLLUTANT_FA[row["pollutant"]]
    province = row.get("province_name") or "کل کشور"
    title = f"نقشه مکانی {pollutant_fa} در استان {province}"
    draw_header(image, draw, width, title, generated_time_fa())

    map_x, map_y, map_w, map_h = 106, 260, 1588, 710
    draw.rounded_rectangle((map_x, map_y, map_x + map_w, map_y + map_h), radius=22, fill="#fbfdff", outline="#d8e6f1", width=2)
    content_box = (map_x + 38, map_y + 28, map_x + map_w - 38, map_y + map_h - 28)
    map_width, map_height = content_box[2] - content_box[0], content_box[3] - content_box[1]

    try:
        basemap = build_osm_basemap(bbox, map_width, map_height)
    except Exception as error:
        print("OSM basemap fallback:", repr(error))
        basemap = Image.new("RGB", (map_width, map_height), "#e8edf2")
        fallback_draw = ImageDraw.Draw(basemap)
        draw_centered_rtl(fallback_draw, map_width / 2, map_height / 2 - 14, "نقشه پایه موقتاً در دسترس نیست", selected_font=font(20), fill="#60788f")

    raw = Image.open(raw_png).convert("RGBA").resize((map_width, map_height), Image.Resampling.LANCZOS)
    original_alpha = raw.getchannel("A")
    alpha = original_alpha.point(lambda value: round(value * REPORT_OVERLAY_OPACITY))
    raw.putalpha(alpha)
    composed = basemap.convert("RGBA")
    composed.alpha_composite(raw)

    # Province boundary from the outer mask edge only.
    province_mask = original_alpha.point(lambda value: 255 if value > 4 else 0)
    edge = province_mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value > 32 else 0)
    boundary = Image.new("RGBA", raw.size, (15, 90, 150, 0))
    boundary.putalpha(edge)
    composed.alpha_composite(boundary)

    image.paste(composed.convert("RGB"), (content_box[0], content_box[1]))
    draw.rectangle(content_box, outline="#c5d5e2", width=2)

    # Legend, matching the browser export.
    legend_w = 430
    legend_x = content_box[0] + 34
    legend_y = content_box[1] + 38
    draw.rounded_rectangle((legend_x - 18, legend_y - 18, legend_x + legend_w + 18, legend_y + 82), radius=16, fill=(255, 255, 255), outline="#c8d8e5", width=2)
    draw_rtl(draw, legend_x + legend_w, legend_y - 8, f"{pollutant_fa} — {cfg['unit']}", selected_font=font(16, bold=True), fill="#29455f")
    draw_color_legend(image, draw, (legend_x, legend_y + 24, legend_x + legend_w, legend_y + 40), cfg)

    draw_ltr(draw, content_box[2] - 42, content_box[1] + 18, "N", selected_font=font(20, bold=True), fill="#153c60", anchor="mt")
    draw.polygon([(content_box[2] - 42, content_box[1] + 50), (content_box[2] - 54, content_box[1] + 84), (content_box[2] - 30, content_box[1] + 84)], fill="#153c60")
    draw_ltr(draw, content_box[0] + 14, content_box[3] - 28, "© OpenStreetMap contributors", selected_font=font(13), fill="#43596e")

    meta_y, meta_h = 994, 188
    draw.rounded_rectangle((map_x, meta_y, map_x + map_w, meta_y + meta_h), radius=20, fill="#edf5fb", outline="#d7e6f1", width=2)
    right_x, left_x = width - 130, 130
    draw_rtl(draw, right_x, meta_y + 28, f"آلاینده: {pollutant_fa}   |   استان: {province}", selected_font=font(20, bold=True), fill="#17324d")
    draw_label_value_rtl(draw, right_x, meta_y + 68, "واحد:", cfg["unit"], label_font=font(18), value_font=font(18), fill="#405a72")
    draw_label_value_rtl(draw, right_x - 300, meta_y + 68, "بازه:", period_fa(row.get("period_key"), start, end), label_font=font(18), value_font=font(18), fill="#405a72")
    draw_ltr(draw, right_x, meta_y + 108, f"Start: {start}   |   End: {end}", selected_font=font(17), fill="#405a72", anchor="rt")

    draw_rtl(draw, left_x + 600, meta_y + 28, "مشخصات تصویر", selected_font=font(20, bold=True), fill="#17324d")
    draw_rtl(draw, left_x + 600, meta_y + 68, "شفافیت لایه آلاینده: ۸۰٪", selected_font=font(17), fill="#405a72")
    draw_ltr(draw, left_x, meta_y + 108, "Source: Sentinel-5P / TROPOMI • Google Earth Engine • OpenStreetMap", selected_font=font(15), fill="#64748b")

    draw.rectangle((72, 1126, width - 72, 1188), fill="#f5f9fc")
    draw_ltr(draw, width / 2, 1147, "© AirSat  •  Satellite-powered air monitoring for Iran  •  airsat.ir", selected_font=font(17, bold=True), fill="#6b8195", anchor="mt")
    image.save(output_png, "PNG", optimize=True)


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



def get_site_settings(keys: list[str]) -> dict[str, Any]:
    if not keys:
        return {}
    url = required("SUPABASE_URL") + "/rest/v1/site_settings"
    response = requests.get(
        url,
        params={"key": f"in.({','.join(keys)})", "select": "key,value"},
        headers=service_headers(),
        timeout=30,
    )
    if not response.ok:
        print("Site settings unavailable:", supabase_error(response))
        return {}
    return {item["key"]: item.get("value") for item in response.json()}


def setting_bool(settings: dict[str, Any], key: str, default: bool = False) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and "enabled" in value:
        return bool(value["enabled"])
    return str(value).lower() in {"1", "true", "yes", "on"}


def get_zip_content_selection() -> list[str]:
    settings = get_site_settings(["exports.zip_contents"])
    value = settings.get("exports.zip_contents")
    if not isinstance(value, list):
        return list(ZIP_CONTENT_KEYS)
    selected = [str(item) for item in value if str(item) in ZIP_CONTENT_KEYS]
    return selected or ["metadata_txt"]


def get_profile(user_id: str) -> dict[str, Any]:
    url = required("SUPABASE_URL") + "/rest/v1/profiles"
    response = requests.get(
        url,
        params={"id": f"eq.{user_id}", "select": "*", "limit": "1"},
        headers=service_headers(),
        timeout=30,
    )
    return response.json()[0] if response.ok and response.json() else {}


def create_signed_download_url(object_path: str, expires_in: int = 86400) -> str | None:
    bucket = os.getenv("EXPORT_BUCKET", "airsat-exports")
    encoded = "/".join(quote(segment, safe="") for segment in object_path.split("/"))
    url = f"{required('SUPABASE_URL')}/storage/v1/object/sign/{bucket}/{encoded}"
    response = requests.post(
        url,
        headers=service_headers(),
        json={"expiresIn": expires_in},
        timeout=30,
    )
    if not response.ok:
        print("Signed URL failed:", supabase_error(response))
        return None
    signed = response.json().get("signedURL") or response.json().get("signedUrl")
    if not signed:
        return None
    if signed.startswith("http"):
        return signed
    return required("SUPABASE_URL").rstrip("/") + "/storage/v1" + signed


def ready_email_html(row: dict[str, Any], download_url: str, expiry_hours: int) -> str:
    province = row.get("province_name") or "استان انتخاب‌شده"
    pollutant = row.get("pollutant") or ""
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8"></head>
<body style="margin:0;background:#eef3f8;font-family:Tahoma,Arial,sans-serif;color:#18344f">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#eef3f8;padding:28px 12px"><tr><td align="center">
<table role="presentation" width="620" cellspacing="0" cellpadding="0" style="max-width:620px;background:#fff;border-radius:20px;overflow:hidden;border:1px solid #d4e0eb">
<tr><td style="background:linear-gradient(135deg,#0f4f91,#1768ad);padding:26px 32px;color:#fff">
<div style="font-size:34px;font-weight:700;direction:ltr;text-align:right">AirSat</div>
<div style="font-size:14px;color:#d9ecff;margin-top:4px">سامانه پایش ماهواره‌ای آلودگی هوای ایران</div>
</td></tr>
<tr><td style="padding:34px 34px 20px">
<h2 style="margin:0 0 18px;font-size:22px">فایل درخواست شما آماده است</h2>
<p style="line-height:2;margin:0 0 18px">خروجی <strong>{pollutant}</strong> برای <strong>{province}</strong> آماده شده است.</p>
<table width="100%" style="background:#f4f8fb;border-radius:12px;padding:14px;line-height:2">
<tr><td>آلاینده</td><td align="left" dir="ltr">{pollutant}</td></tr>
<tr><td>استان</td><td align="left">{province}</td></tr>
<tr><td>اعتبار لینک</td><td align="left">{expiry_hours} ساعت</td></tr>
</table>
<div style="text-align:center;margin:28px 0"><a href="{download_url}" style="display:inline-block;background:#0f5fae;color:#fff;text-decoration:none;padding:14px 28px;border-radius:11px;font-weight:700">دانلود بسته AirSat</a></div>
<p style="font-size:13px;color:#6b7f92;line-height:1.9">این ایمیل با قالب اختصاصی AirSat ارسال شده است. در صورت پیوست‌بودن فایل، نسخه پیوست و لینک دانلود هر دو در دسترس‌اند.</p>
</td></tr>
<tr><td style="padding:18px 32px;background:#e6eef5;color:#60788f;font-size:12px;text-align:center">© AirSat • Sentinel-5P / TROPOMI • airsat.ir</td></tr>
</table></td></tr></table></body></html>"""


def send_ready_email(
    recipient: str,
    row: dict[str, Any],
    zip_path: Path,
    download_url: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    sender = os.getenv("AIRSAT_EMAIL_FROM", "").strip()
    if not api_key or not sender:
        return {"status": "skipped", "reason": "email-secrets-missing"}
    expiry_hours = int(settings.get("notifications.download_expiry_hours") or 24)
    subject_value = settings.get("notifications.email_subject_fa")
    subject = str(subject_value if isinstance(subject_value, str) else "فایل AirSat شما آماده است")
    payload: dict[str, Any] = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "html": ready_email_html(row, download_url, expiry_hours),
    }
    reply_to = os.getenv("AIRSAT_EMAIL_REPLY_TO", "").strip()
    if reply_to:
        payload["reply_to"] = reply_to

    attach = setting_bool(settings, "notifications.email_attach_zip", True)
    # Resend's total attachment limit is after Base64 encoding. Stay safely below it.
    if attach and zip_path.stat().st_size <= 28 * 1024 * 1024:
        payload["attachments"] = [{
            "filename": zip_path.name,
            "content": base64.b64encode(zip_path.read_bytes()).decode("ascii"),
        }]

    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if not response.ok:
        return {"status": "failed", "error": response.text[:800]}
    return {"status": "sent", "provider": "resend", "id": response.json().get("id"), "attached": "attachments" in payload}


def normalize_iran_mobile(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"9\d{9}", digits) else None


def send_ready_sms(phone: str, row: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("SMSIR_API_KEY", "").strip()
    template_id = os.getenv("SMSIR_READY_TEMPLATE_ID", "").strip()
    mobile = normalize_iran_mobile(phone)
    if not api_key or not template_id or not mobile:
        return {"status": "skipped", "reason": "sms-config-or-phone-missing"}
    response = requests.post(
        "https://api.sms.ir/v1/send/verify",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        json={
            "mobile": mobile,
            "templateId": int(template_id),
            "parameters": [
                {"name": "POLLUTANT", "value": str(row.get("pollutant") or "")},
                {"name": "REGION", "value": str(row.get("province_name") or "")},
            ],
        },
        timeout=45,
    )
    if not response.ok:
        return {"status": "failed", "error": response.text[:800]}
    return {"status": "sent", "provider": "sms.ir", "response": response.json()}


def send_ready_notifications(
    row: dict[str, Any],
    zip_path: Path,
    object_path: str,
) -> dict[str, Any]:
    keys = [
        "features.email_notifications_enabled",
        "features.sms_notifications_enabled",
        "notifications.email_attach_zip",
        "notifications.download_expiry_hours",
        "notifications.email_subject_fa",
    ]
    settings = get_site_settings(keys)
    profile = get_profile(str(row["user_id"]))
    metadata = row.get("metadata") or {}
    results: dict[str, Any] = {}
    expiry_hours = int(settings.get("notifications.download_expiry_hours") or 24)
    signed_url = create_signed_download_url(object_path, expiry_hours * 3600)

    email = profile.get("email") or metadata.get("user_email")
    email_allowed = profile.get("email_notifications", True)
    if setting_bool(settings, "features.email_notifications_enabled", False) and email and email_allowed and signed_url:
        results["email"] = send_ready_email(email, row, zip_path, signed_url, settings)
    else:
        results["email"] = {"status": "skipped"}

    phone = profile.get("phone") or metadata.get("user_phone")
    sms_allowed = profile.get("sms_notifications", True)
    if setting_bool(settings, "features.sms_notifications_enabled", False) and phone and sms_allowed:
        results["sms"] = send_ready_sms(phone, row)
    else:
        results["sms"] = {"status": "skipped"}
    return results

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

    try:
        validate_province_only_request(row)
    except Exception as error:
        update_request(
            request_id,
            {
                "status": "failed",
                "error": str(error),
                "message": "درخواست رد شد؛ دریافت GeoTIFF فقط برای یک استان مجاز است.",
            },
        )
        raise

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
        render_bbox = region_bbox(region)
        render_region = ee.Geometry.Rectangle(list(render_bbox), proj=None, geodesic=False)

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
                {
                    "region": render_region,
                    "dimensions": "1512x654",
                    "crs": "EPSG:3857",
                    "format": "png",
                }
            )
            download_file(thumbnail_url, raw_preview)
            compose_preview(raw_preview, preview, row, cfg, start, end, render_bbox)

            series = []
            series_source = "not-applicable"
            if row.get("province_name"):
                series, series_source = load_precomputed_timeseries(row)
                if not series:
                    print(f"Precomputed time series unavailable: {series_source}")
                    series, series_source = compute_region_timeseries(cfg, region)

                if series:
                    forecast_model = lightweight_seasonal_forecast(
                        series,
                        row["pollutant"],
                        horizon=6,
                    )
                    write_timeseries_files(
                        series,
                        timeseries_csv,
                        timeseries_json,
                        row,
                        cfg,
                        series_source,
                        forecast_model,
                    )
                    draw_timeseries_chart(
                        series,
                        timeseries_chart,
                        row,
                        cfg,
                        series_source,
                        forecast_model,
                    )

            windows_shortcut = write_shortcut(folder)
            selected_zip_contents = get_zip_content_selection()

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
                        f"Region: {row.get('province_name')}",
                        f"Unit: {cfg['unit']}",
                        f"Sentinel-5P image count: {count}",
                        f"Province time-series points: {len(series)}",
                        f"Province time-series source: {series_source}",
                        f"Collection: {cfg['collection']}",
                        f"Band: {cfg['band']}",
                        f"Nominal export scale: 7000 m",
                        "CRS: EPSG:4326",
                        "ZIP configuration: " + ", ".join(selected_zip_contents),
                        "Website: https://airsat.ir",
                    ]
                ),
                encoding="utf-8",
            )

            available_zip_files: dict[str, Path] = {
                "geotiff": geotiff,
                "map_png": preview,
                "metadata_txt": metadata,
                "shortcut": windows_shortcut,
            }
            if series:
                available_zip_files.update({
                    "timeseries_png": timeseries_chart,
                    "timeseries_csv": timeseries_csv,
                    "timeseries_json": timeseries_json,
                })

            included_zip_keys = [
                key
                for key in selected_zip_contents
                if key in available_zip_files and available_zip_files[key].exists()
            ]
            # A configuration containing only time-series files may produce no files
            # when the series is unavailable. Keep the package valid and informative.
            if not included_zip_keys:
                included_zip_keys = ["metadata_txt"]

            with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for key in included_zip_keys:
                    file = available_zip_files[key]
                    archive.write(file, file.name)

            # Free Supabase projects currently cap an individual file at 50 MB.
            max_bytes = 50 * 1024 * 1024
            if output_zip.stat().st_size > max_bytes:
                raise RuntimeError(
                    "The generated ZIP is larger than the 50 MB Supabase Free limit"
                )

            object_path = f"{row['user_id']}/{request_id}/{output_zip.name}"
            upload_zip(output_zip, object_path)

            notification_settings = get_site_settings(["notifications.download_expiry_hours"])
            expiry_hours = int(notification_settings.get("notifications.download_expiry_hours") or 24)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
            notification_results = send_ready_notifications(row, output_zip, object_path)
            update_request(
                request_id,
                {
                    "status": "ready",
                    "object_path": object_path,
                    "file_name": output_zip.name,
                    "expires_at": expires_at.isoformat(),
                    "notification_results": notification_results,
                    "message": (
                        "بسته آماده است و شامل "
                        + "، ".join(ZIP_CONTENT_LABELS_FA[key] for key in included_zip_keys)
                        + f" است؛ تا {expiry_hours} ساعت قابل دانلود خواهد بود."
                    ),
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
