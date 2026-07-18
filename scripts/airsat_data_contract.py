#!/usr/bin/env python3
"""AirSat's canonical public-data contract and lightweight health checks."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


POLLUTANTS = (
    "NO2",
    "SO2",
    "CO",
    "O3",
    "HCHO",
    "AER_AI",
    "CH4",
)

# Official Earth Engine collection availability start dates.
POLLUTANT_START_DATES: dict[str, date] = {
    "NO2": date(2018, 6, 28),
    "CO": date(2018, 6, 28),
    "AER_AI": date(2018, 7, 4),
    "O3": date(2018, 9, 8),
    "SO2": date(2018, 12, 5),
    "HCHO": date(2018, 12, 5),
    "CH4": date(2019, 2, 8),
}

DYNAMIC_KEYS = (
    "latest_7d",
    "latest_30d",
    "latest_90d",
    "latest_month",
    "current_year",
)

# Freshness is deliberately staggered so the daily repair run stays efficient.
DYNAMIC_TTL_HOURS: dict[str, int] = {
    "latest_7d": 36,
    "latest_30d": 72,
    "latest_90d": 168,
    "latest_month": 240,
    "current_year": 72,
}

EXPECTED_PROVINCES = 31


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def previous_year(today: date | None = None) -> int:
    today = today or utc_now().date()
    return today.year - 1


def annual_years(pollutant: str, today: date | None = None) -> list[int]:
    start_year = POLLUTANT_START_DATES[pollutant].year
    return list(range(start_year, previous_year(today) + 1))


def last_completed_month(today: date | None = None) -> date:
    today = today or utc_now().date()
    first_this_month = today.replace(day=1)
    return first_this_month - timedelta(days=1)


def first_supported_month(pollutant: str) -> date:
    """First month with enough product days for a monthly statistic."""
    start_date = POLLUTANT_START_DATES[pollutant]
    first = start_date.replace(day=1)
    if start_date.day > 15:
        if first.month == 12:
            return date(first.year + 1, 1, 1)
        return date(first.year, first.month + 1, 1)
    return first


def expected_months(pollutant: str, today: date | None = None) -> list[str]:
    start = first_supported_month(pollutant)
    stop = last_completed_month(today).replace(day=1)
    periods: list[str] = []
    current = start

    while current <= stop:
        periods.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return periods


def half_year_chunks(
    pollutant: str,
    today: date | None = None,
) -> list[dict[str, Any]]:
    periods = expected_months(pollutant, today)
    groups: dict[tuple[int, int], list[str]] = {}

    for period in periods:
        year, month = (int(part) for part in period.split("-"))
        half = 1 if month <= 6 else 2
        groups.setdefault((year, half), []).append(period)

    chunks = []
    for (year, half), selected in sorted(groups.items()):
        chunks.append(
            {
                "year": year,
                "half": half,
                "months": [int(item.split("-")[1]) for item in selected],
                "periods": selected,
                "period_key": f"timeseries_{year}_h{half}",
            }
        )
    return chunks


def catalog_index(layers: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    index: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []

    for layer in layers:
        layer_id = str(layer.get("id") or "")
        if not layer_id:
            continue
        if layer_id in index:
            duplicates.append(layer_id)
            old_time = parse_datetime(index[layer_id].get("generated_at_utc"))
            new_time = parse_datetime(layer.get("generated_at_utc"))
            if new_time and (not old_time or new_time >= old_time):
                index[layer_id] = layer
        else:
            index[layer_id] = layer

    return index, sorted(set(duplicates))


def layer_health(
    root: Path,
    pollutant: str,
    period_key: str,
    layer: dict[str, Any] | None,
    *,
    require_fresh: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    issues: list[str] = []

    if not layer:
        return {
            "healthy": False,
            "fresh": False,
            "issues": ["catalog_entry_missing"],
        }

    if layer.get("available") is not True:
        issues.append(
            "unavailable:"
            + str(
                layer.get("skip_reason")
                or (layer.get("validation") or {}).get("reason")
                or "unknown"
            )
        )

    public = root / "public"
    visual_path = str(layer.get("visual_path") or "")
    georef_path = str(layer.get("georef_path") or "")
    visual = public / visual_path.lstrip("/") if visual_path else None
    georef = public / georef_path.lstrip("/") if georef_path else None
    stats = public / "data" / "stats" / pollutant / f"{period_key}.json"

    if not visual_path:
        issues.append("visual_path_missing")
    elif not visual or not visual.exists():
        issues.append("visual_file_missing")

    if not georef_path:
        issues.append("georef_path_missing")
    elif not georef or not georef.exists():
        issues.append("georef_file_missing")

    if not stats.exists():
        issues.append("stats_file_missing")

    georef_payload = read_json(georef, {}) if georef and georef.exists() else {}
    stats_payload = read_json(stats, {}) if stats.exists() else {}

    if georef and georef.exists():
        if not georef_payload:
            issues.append("georef_json_invalid")
        elif georef_payload.get("validated") is not True:
            issues.append("georef_not_validated")

    if visual and visual.exists():
        actual_hash = sha256_file(visual)
        if georef_payload.get("webp_sha256") != actual_hash:
            issues.append("georef_hash_mismatch")
        if layer.get("visual_sha256") != actual_hash:
            issues.append("catalog_hash_mismatch")

    if stats.exists():
        if not stats_payload:
            issues.append("stats_json_invalid")
        else:
            rows = (
                stats_payload.get("province_stats")
                or stats_payload.get("provinces")
                or []
            )
            if len(rows) < EXPECTED_PROVINCES:
                issues.append(f"province_rows:{len(rows)}")
            numeric = [row for row in rows if finite(row.get("mean"))]
            if len(numeric) < EXPECTED_PROVINCES:
                issues.append(f"numeric_provinces:{len(numeric)}")

            summary = (stats_payload.get("layer") or layer).get("summary") or {}
            if not finite(summary.get("mean")):
                issues.append("national_mean_missing")

    fresh = True
    if require_fresh:
        generated = parse_datetime(layer.get("generated_at_utc"))
        ttl = DYNAMIC_TTL_HOURS.get(period_key)
        if not generated:
            fresh = False
            issues.append("generated_at_missing")
        elif ttl is not None and now - generated > timedelta(hours=ttl):
            fresh = False
            issues.append(f"stale_over_{ttl}h")

    return {
        "healthy": not issues,
        "fresh": fresh,
        "issues": issues,
        "generated_at_utc": layer.get("generated_at_utc"),
    }


def timeseries_coverage(
    root: Path,
    pollutant: str,
) -> dict[str, Any]:
    path = root / "public" / "data" / "timeseries" / f"{pollutant}.json"
    payload = read_json(path, {})
    issues: list[str] = []

    if not path.exists():
        return {
            "healthy": False,
            "issues": ["timeseries_file_missing"],
            "province_periods": {},
        }
    if not payload:
        return {
            "healthy": False,
            "issues": ["timeseries_json_invalid"],
            "province_periods": {},
        }

    provinces = payload.get("provinces") or []
    if len(provinces) < EXPECTED_PROVINCES:
        issues.append(f"timeseries_provinces:{len(provinces)}")

    province_periods: dict[str, set[str]] = {}
    duplicate_points: list[str] = []

    for province in provinces:
        province_id = str(
            province.get("id")
            or province.get("name_fa")
            or province.get("name_en")
            or ""
        )
        seen: set[str] = set()
        for point in province.get("series") or []:
            period = str(point.get("period") or "")
            if not period or not finite(point.get("value")):
                continue
            if period in seen:
                duplicate_points.append(f"{province_id}:{period}")
            seen.add(period)
        province_periods[province_id] = seen

    if duplicate_points:
        issues.append(f"duplicate_points:{len(duplicate_points)}")

    return {
        "healthy": not issues,
        "issues": issues,
        "province_periods": province_periods,
        "province_count": len(province_periods),
        "path": str(path),
    }


def timeseries_chunk_complete(
    coverage: dict[str, Any],
    required_periods: list[str],
) -> bool:
    if not required_periods:
        return True
    province_periods = coverage.get("province_periods") or {}
    if len(province_periods) < EXPECTED_PROVINCES:
        return False
    required = set(required_periods)
    return all(required.issubset(periods) for periods in province_periods.values())


def supported_layer(pollutant: str, period_key: str) -> bool:
    if period_key.startswith("annual_"):
        try:
            year = int(period_key.split("_")[1])
        except Exception:
            return True
        return year >= POLLUTANT_START_DATES[pollutant].year

    if period_key.startswith("range_"):
        try:
            start_year = int(period_key.split("_")[1])
        except Exception:
            return True
        return start_year >= POLLUTANT_START_DATES[pollutant].year

    return True
