#!/usr/bin/env python3
"""Validate AirSat visuals, sidecars, provincial statistics and time series."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(os.environ.get("AIRSAT_REPOSITORY_ROOT", str(Path(__file__).resolve().parents[1]))).expanduser().resolve()
PUBLIC = ROOT / "public"
DATA = PUBLIC / "data"
CATALOG = DATA / "catalog" / "layers.json"
PROVINCES = DATA / "catalog" / "provinces.json"
STATS = DATA / "stats"
TIMESERIES = DATA / "timeseries"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def local_path(web_path: str) -> Path:
    return PUBLIC / web_path.lstrip("/")


def parse_pollutants(value: str):
    value = value.strip()
    return None if not value or value.lower() == "all" else {
        x.strip().upper() for x in value.split(",") if x.strip()
    }


def parse_groups(value: str):
    value = value.strip()
    return None if not value or value.lower() == "all" else {
        x.strip().lower() for x in value.split(",") if x.strip()
    }

def parse_periods(value: str):
    value = value.strip()
    return None if not value or value.lower() == "all" else {
        x.strip() for x in value.split(",") if x.strip()
    }


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def valid_bounds(bounds: Any) -> bool:
    if not (
        isinstance(bounds, list)
        and len(bounds) == 2
        and all(isinstance(x, list) and len(x) == 2 for x in bounds)
    ):
        return False
    south, west = bounds[0]
    north, east = bounds[1]
    return (
        -90 <= float(south) < float(north) <= 90
        and -180 <= float(west) < float(east) <= 180
    )


def expected_province_count() -> int:
    if not PROVINCES.exists():
        return 31
    return len(read_json(PROVINCES).get("provinces", [])) or 31


def validate_stats(layer: dict[str, Any], expected: int) -> list[str]:
    lid = str(layer.get("id", "unknown"))
    pollutant = str(layer.get("pollutant", ""))
    period = str(layer.get("period_key", ""))
    path = STATS / pollutant / f"{period}.json"
    if not path.exists():
        return [f"{lid}: statistics file missing: {path.relative_to(ROOT)}"]
    try:
        payload = read_json(path)
    except Exception as exc:
        return [f"{lid}: invalid statistics JSON: {exc}"]
    rows = payload.get("province_stats") or payload.get("provinces") or []
    errors: list[str] = []
    if len(rows) < expected:
        errors.append(f"{lid}: only {len(rows)} provincial rows; expected {expected}")
    numeric = [r for r in rows if finite(r.get("mean"))]
    if len(numeric) < expected:
        errors.append(f"{lid}: only {len(numeric)} provinces have a numeric mean; expected {expected}")
    names = [str(r.get("name_fa") or "").strip() for r in rows]
    if len({x for x in names if x}) < expected:
        errors.append(f"{lid}: province names are missing or duplicated")
    ranks = [int(r["rank"]) for r in numeric if r.get("rank") is not None]
    if len(ranks) != len(numeric) or sorted(ranks) != list(range(1, len(numeric) + 1)):
        errors.append(f"{lid}: provincial ranks are incomplete or non-contiguous")
    summary = (payload.get("layer") or layer).get("summary") or {}
    if not finite(summary.get("mean")):
        errors.append(f"{lid}: national mean is missing")
    return errors


def validate_layer(
    layer: dict[str, Any],
    expected: int,
    require_stats: bool,
    require_available: bool,
) -> list[str]:
    errors: list[str] = []
    lid = str(layer.get("id", "unknown"))
    if not layer.get("available"):
        if require_available:
            reason = (
                layer.get("skip_reason")
                or (layer.get("validation") or {}).get("reason")
                or "unknown"
            )
            return [f"{lid}: selected layer is unavailable; reason={reason}"]
        return errors
    visual_path = layer.get("visual_path")
    georef_path = layer.get("georef_path")
    if not visual_path:
        return [f"{lid}: missing visual_path"]
    if not georef_path:
        return [f"{lid}: missing georef_path"]
    visual = local_path(visual_path)
    georef = local_path(georef_path)
    if not visual.exists():
        return [f"{lid}: visual missing: {visual}"]
    if not georef.exists():
        return [f"{lid}: georef missing: {georef}"]
    meta = read_json(georef)
    if not meta.get("validated"):
        errors.append(f"{lid}: georef not validated")
    if str(meta.get("crs", "")).upper() not in {
        "EPSG:3857", "WGS 84 / PSEUDO-MERCATOR"
    }:
        errors.append(f"{lid}: unexpected CRS {meta.get('crs')}")
    if not valid_bounds(layer.get("bounds")):
        errors.append(f"{lid}: invalid layer bounds")
    if not valid_bounds(meta.get("web_bounds")):
        errors.append(f"{lid}: invalid georef web_bounds")
    with Image.open(visual) as im:
        w, h = im.size
        if w != int(meta.get("width", -1)):
            errors.append(f"{lid}: width mismatch")
        if h != int(meta.get("height", -1)):
            errors.append(f"{lid}: height mismatch")
        if im.mode not in {"RGB", "RGBA"}:
            errors.append(f"{lid}: unexpected image mode {im.mode}")
    actual = sha256_file(visual)
    if meta.get("webp_sha256") != actual:
        errors.append(f"{lid}: sidecar SHA-256 mismatch")
    if layer.get("visual_sha256") != actual:
        errors.append(f"{lid}: catalog SHA-256 mismatch")
    if not (layer.get("validation") or {}).get("validated"):
        errors.append(f"{lid}: catalog validation false")
    if not isinstance(layer.get("palette"), list) or len(layer.get("palette", [])) < 5:
        errors.append(f"{lid}: pollutant palette is missing or too short")
    if require_stats:
        errors.extend(validate_stats(layer, expected))
    return errors


def validate_timeseries(
    pollutant: str,
    expected: int,
    min_points: int,
    required_periods: set[str] | None,
) -> list[str]:
    path = TIMESERIES / f"{pollutant}.json"
    if not path.exists():
        return [f"{pollutant}: time-series file missing: {path.relative_to(ROOT)}"]
    try:
        payload = read_json(path)
    except Exception as exc:
        return [f"{pollutant}: invalid time-series JSON: {exc}"]
    provinces = payload.get("provinces") or []
    errors: list[str] = []
    if len(provinces) < expected:
        errors.append(f"{pollutant}: only {len(provinces)} time-series provinces; expected {expected}")
    names = [str(p.get("name_fa") or "").strip() for p in provinces]
    if len({x for x in names if x}) < expected:
        errors.append(f"{pollutant}: time-series province names are missing or duplicated")
    short = []
    missing_required = []
    duplicates = []

    for province in provinces:
        numeric = [
            point
            for point in (province.get("series") or [])
            if finite(point.get("value"))
        ]
        province_name = province.get("name_fa") or province.get("id")
        if len(numeric) < min_points:
            short.append(f"{province_name}={len(numeric)}")

        periods = [str(point.get("period") or "") for point in numeric]
        if len(periods) != len(set(periods)):
            duplicates.append(str(province_name))

        if required_periods is not None:
            missing = sorted(required_periods - set(periods))
            if missing:
                missing_required.append(
                    f"{province_name}:{','.join(missing)}"
                )

    if short:
        errors.append(
            f"{pollutant}: provinces with fewer than {min_points} numeric monthly points: "
            + ", ".join(short[:10])
            + (" ..." if len(short) > 10 else "")
        )
    if duplicates:
        errors.append(
            f"{pollutant}: duplicate monthly periods in provinces: "
            + ", ".join(duplicates[:10])
        )
    if missing_required:
        errors.append(
            f"{pollutant}: selected monthly periods are missing: "
            + " | ".join(missing_required[:10])
            + (" ..." if len(missing_required) > 10 else "")
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pollutants", default="all")
    parser.add_argument("--groups", default="all", help="dynamic,annual,range or all")
    parser.add_argument("--periods", default="all", help="Comma-separated period keys")
    parser.add_argument("--require-stats", action="store_true")
    parser.add_argument("--require-available", action="store_true")
    parser.add_argument("--require-timeseries", action="store_true")
    parser.add_argument("--skip-layers", action="store_true", help="Validate only time-series files")
    parser.add_argument("--min-series-points", type=int, default=12)
    parser.add_argument("--timeseries-periods", default="all")
    args = parser.parse_args()

    if not CATALOG.exists():
        print("ERROR: catalog not found", CATALOG)
        return 1
    selected = parse_pollutants(args.pollutants)
    groups = parse_groups(args.groups)
    periods = parse_periods(args.periods)
    required_timeseries_periods = parse_periods(args.timeseries_periods)
    layers = read_json(CATALOG).get("layers", [])
    if selected is not None:
        layers = [l for l in layers if str(l.get("pollutant", "")).upper() in selected]
    if groups is not None:
        layers = [l for l in layers if str(l.get("period_group", "")).lower() in groups]
    if periods is not None:
        layers = [l for l in layers if str(l.get("period_key", "")) in periods]

    expected = expected_province_count()
    errors: list[str] = []
    count = 0
    if not args.skip_layers:
        if periods is not None:
            found_periods = {
                str(layer.get("period_key", ""))
                for layer in layers
            }
            for missing_period in sorted(periods - found_periods):
                errors.append(
                    f"catalog: selected period is missing: {missing_period}"
                )

        for layer in layers:
            errs = validate_layer(
                layer,
                expected,
                args.require_stats,
                args.require_available,
            )
            errors.extend(errs)
            if layer.get("available") and not errs:
                count += 1

    if args.require_timeseries:
        pollutants = selected or {
            str(l.get("pollutant", "")).upper() for l in layers if l.get("pollutant")
        }
        for pollutant in sorted(pollutants):
            errors.extend(
                validate_timeseries(
                    pollutant,
                    expected,
                    args.min_series_points,
                    required_timeseries_periods,
                )
            )

    if errors:
        print("VALIDATION FAILED")
        for error in errors:
            print(" -", error)
        return 1
    print(
        f"VALIDATION PASSED: {count} available layers; "
        f"expected provinces={expected}; groups={args.groups}; periods={args.periods}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
