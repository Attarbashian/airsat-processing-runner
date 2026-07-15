#!/usr/bin/env python3
"""Audit the entire AirSat public-data contract and publish a health manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from airsat_data_contract import (
    DYNAMIC_KEYS,
    POLLUTANTS,
    annual_years,
    catalog_index,
    expected_months,
    layer_health,
    read_json,
    supported_layer,
    timeseries_coverage,
    utc_now,
    write_json_atomic,
)


def normalize_catalog(
    catalog_payload: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    layers = catalog_payload.get("layers") or []
    index, duplicates = catalog_index(layers)
    normalized = []

    for layer in index.values():
        pollutant = str(layer.get("pollutant") or "").upper()
        period_key = str(layer.get("period_key") or "")
        if pollutant in POLLUTANTS and not supported_layer(
            pollutant,
            period_key,
        ):
            continue
        normalized.append(layer)

    group_order = {"dynamic": 0, "annual": 1, "range": 2}
    normalized.sort(
        key=lambda layer: (
            str(layer.get("pollutant") or ""),
            group_order.get(str(layer.get("period_group") or ""), 9),
            str(layer.get("period_key") or ""),
        )
    )

    updated = dict(catalog_payload)
    updated["layers"] = normalized
    updated["normalized_at_utc"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    return updated, duplicates


def missing_timeseries_points(
    coverage: dict[str, Any],
    required: list[str],
) -> dict[str, list[str]]:
    province_periods = coverage.get("province_periods") or {}
    missing: dict[str, list[str]] = {}

    for province, periods in province_periods.items():
        absent = sorted(set(required) - set(periods))
        if absent:
            missing[province] = absent

    if len(province_periods) < 31:
        missing["__missing_province_records__"] = [
            str(31 - len(province_periods))
        ]

    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--write-health", action="store_true")
    parser.add_argument("--repair-catalog", action="store_true")
    parser.add_argument("--strict-pollutants", default="ALL")
    parser.add_argument("--no-fail", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    catalog_path = root / "public" / "data" / "catalog" / "layers.json"
    catalog_payload = read_json(catalog_path, {}) or {}
    normalized_catalog, duplicates = normalize_catalog(catalog_payload)

    if args.repair_catalog and normalized_catalog != catalog_payload:
        write_json_atomic(catalog_path, normalized_catalog)
        catalog_payload = normalized_catalog

    layers = catalog_payload.get("layers") or []
    index, remaining_duplicates = catalog_index(layers)
    now = utc_now()
    issues: list[dict[str, Any]] = []
    report_pollutants: dict[str, Any] = {}

    for pollutant in POLLUTANTS:
        pollutant_issues: list[dict[str, Any]] = []
        layer_results: dict[str, Any] = {}

        expected_layer_keys = [
            *(f"annual_{year}" for year in annual_years(pollutant)),
            *DYNAMIC_KEYS,
        ]

        for period_key in expected_layer_keys:
            dynamic = period_key in DYNAMIC_KEYS
            health = layer_health(
                root,
                pollutant,
                period_key,
                index.get(f"{pollutant}_{period_key}"),
                require_fresh=dynamic,
                now=now,
            )
            layer_results[period_key] = health
            if not health["healthy"]:
                issue = {
                    "pollutant": pollutant,
                    "type": "layer",
                    "period_key": period_key,
                    "details": health["issues"],
                }
                issues.append(issue)
                pollutant_issues.append(issue)

        coverage = timeseries_coverage(root, pollutant)
        required_months = expected_months(pollutant)
        missing = missing_timeseries_points(coverage, required_months)

        if coverage.get("issues"):
            issue = {
                "pollutant": pollutant,
                "type": "timeseries_structure",
                "details": coverage["issues"],
            }
            issues.append(issue)
            pollutant_issues.append(issue)

        if missing:
            issue = {
                "pollutant": pollutant,
                "type": "timeseries_coverage",
                "missing_provinces": len(missing),
                "sample": dict(list(missing.items())[:5]),
            }
            issues.append(issue)
            pollutant_issues.append(issue)

        healthy_layers = sum(
            1 for result in layer_results.values() if result["healthy"]
        )
        report_pollutants[pollutant] = {
            "status": "healthy" if not pollutant_issues else "degraded",
            "expected_core_layers": len(expected_layer_keys),
            "healthy_core_layers": healthy_layers,
            "timeseries_expected_months": len(required_months),
            "timeseries_province_count": coverage.get("province_count", 0),
            "timeseries_missing_provinces": len(missing),
            "layers": layer_results,
            "issues": pollutant_issues,
        }

    if duplicates or remaining_duplicates:
        issues.append(
            {
                "type": "catalog_duplicates",
                "details": sorted(
                    set(duplicates) | set(remaining_duplicates)
                ),
            }
        )

    healthy_pollutants = sum(
        1
        for payload in report_pollutants.values()
        if payload["status"] == "healthy"
    )

    health_payload = {
        "status": "healthy" if not issues else "degraded",
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "contract_version": "airsat-data-contract-v7.0",
        "summary": {
            "pollutants_total": len(POLLUTANTS),
            "pollutants_healthy": healthy_pollutants,
            "issues_total": len(issues),
        },
        "pollutants": report_pollutants,
        "issues": issues,
    }

    if args.write_health:
        health_path = (
            root
            / "public"
            / "data"
            / "health"
            / "data-health.json"
        )
        write_json_atomic(health_path, health_payload)
        print("Health manifest:", health_path)

    strict_value = args.strict_pollutants.strip().upper()
    strict = (
        set(POLLUTANTS)
        if strict_value in {"", "ALL"}
        else {
            item.strip()
            for item in strict_value.split(",")
            if item.strip()
        }
    )
    strict_issues = [
        issue
        for issue in issues
        if issue.get("pollutant") in strict
        or "pollutant" not in issue
    ]

    print(
        f"AirSat health: {health_payload['status']} | "
        f"healthy pollutants: {healthy_pollutants}/{len(POLLUTANTS)} | "
        f"strict issues: {len(strict_issues)}"
    )
    for issue in strict_issues[:50]:
        print(" -", issue)

    if strict_issues and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
