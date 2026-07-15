#!/usr/bin/env python3
"""Plan only missing, stale, or corrupted AirSat data tasks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from airsat_data_contract import (
    DYNAMIC_KEYS,
    POLLUTANTS,
    annual_years,
    catalog_index,
    half_year_chunks,
    layer_health,
    read_json,
    timeseries_chunk_complete,
    timeseries_coverage,
    utc_now,
)


def selected_pollutants(value: str) -> list[str]:
    normalized = value.strip().upper()
    if not normalized or normalized == "ALL":
        return list(POLLUTANTS)
    selected = [item.strip() for item in normalized.split(",") if item.strip()]
    unknown = sorted(set(selected) - set(POLLUTANTS))
    if unknown:
        raise RuntimeError(f"Unknown pollutants: {unknown}")
    return selected


def task(
    kind: str,
    pollutant: str,
    period_key: str,
    *,
    year: str = "",
    months: str = "",
    timeseries_periods: str = "",
    start_year: str = "",
    end_year: str = "",
    reason: str = "",
) -> dict[str, str]:
    return {
        "kind": kind,
        "pollutant": pollutant,
        "period_key": period_key,
        "year": year,
        "months": months,
        "timeseries_periods": timeseries_periods,
        "start_year": start_year,
        "end_year": end_year,
        "reason": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--pollutant", default="ALL")
    parser.add_argument("--scope", choices=("core", "complete"), default="core")
    parser.add_argument("--force-rebuild", action="store_true")
    parser.add_argument("--max-tasks", type=int, default=240)
    parser.add_argument("--github-output")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    selected = selected_pollutants(args.pollutant)

    if args.scope == "complete" and len(selected) > 1:
        raise RuntimeError(
            "scope=complete is intentionally limited to one pollutant per run "
            "because GitHub matrices are capped at 256 jobs. Use ALL with core."
        )

    catalog_path = root / "public" / "data" / "catalog" / "layers.json"
    layers = (read_json(catalog_path, {}) or {}).get("layers") or []
    index, duplicates = catalog_index(layers)

    if duplicates:
        print("Catalog duplicate IDs detected:", ", ".join(duplicates))

    now = utc_now()
    tasks: list[dict[str, str]] = []

    for pollutant in selected:
        # Missing historical maps get first priority.
        for year in annual_years(pollutant):
            period_key = f"annual_{year}"
            health = layer_health(
                root,
                pollutant,
                period_key,
                index.get(f"{pollutant}_{period_key}"),
                require_fresh=False,
                now=now,
            )
            if args.force_rebuild or not health["healthy"]:
                tasks.append(
                    task(
                        "annual",
                        pollutant,
                        period_key,
                        year=str(year),
                        reason="force_rebuild"
                        if args.force_rebuild
                        else ",".join(health["issues"]),
                    )
                )

        # Dynamic layers are refreshed according to their own TTL.
        for period_key in DYNAMIC_KEYS:
            health = layer_health(
                root,
                pollutant,
                period_key,
                index.get(f"{pollutant}_{period_key}"),
                require_fresh=True,
                now=now,
            )
            if args.force_rebuild or not health["healthy"]:
                tasks.append(
                    task(
                        "dynamic",
                        pollutant,
                        period_key,
                        reason="force_rebuild"
                        if args.force_rebuild
                        else ",".join(health["issues"]),
                    )
                )

        # Every half-year must exist for every province, not merely somewhere
        # in the merged file.
        coverage = timeseries_coverage(root, pollutant)
        for chunk in half_year_chunks(pollutant):
            complete = timeseries_chunk_complete(
                coverage,
                chunk["periods"],
            )
            if args.force_rebuild or not complete:
                tasks.append(
                    task(
                        "timeseries",
                        pollutant,
                        chunk["period_key"],
                        year=str(chunk["year"]),
                        months=",".join(str(month) for month in chunk["months"]),
                        timeseries_periods=",".join(chunk["periods"]),
                        reason="force_rebuild"
                        if args.force_rebuild
                        else "missing_for_one_or_more_provinces",
                    )
                )

        if args.scope == "complete":
            years = annual_years(pollutant)
            for start_index, start_year in enumerate(years):
                for end_year in years[start_index + 1 :]:
                    period_key = f"range_{start_year}_{end_year}"
                    health = layer_health(
                        root,
                        pollutant,
                        period_key,
                        index.get(f"{pollutant}_{period_key}"),
                        require_fresh=False,
                        now=now,
                    )
                    if args.force_rebuild or not health["healthy"]:
                        tasks.append(
                            task(
                                "range",
                                pollutant,
                                period_key,
                                start_year=str(start_year),
                                end_year=str(end_year),
                                reason="force_rebuild"
                                if args.force_rebuild
                                else ",".join(health["issues"]),
                            )
                        )

    if len(tasks) > args.max_tasks:
        raise RuntimeError(
            f"Planner generated {len(tasks)} tasks, exceeding the safe "
            f"limit of {args.max_tasks}. Run core for ALL or complete for "
            "one pollutant."
        )

    matrix = {"include": tasks}
    output_values = {
        "matrix": json.dumps(matrix, separators=(",", ":")),
        "has_tasks": "true" if tasks else "false",
        "task_count": str(len(tasks)),
        "selected_pollutants": ",".join(selected),
        "scope": args.scope,
    }

    output_path = args.github_output or os.getenv("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            for key, value in output_values.items():
                output.write(f"{key}={value}\n")

    print(f"Selected pollutants: {', '.join(selected)}")
    print(f"Scope: {args.scope}")
    print(f"Planned tasks: {len(tasks)}")
    for index_number, item in enumerate(tasks, 1):
        print(
            f"{index_number:03d}. "
            f"{item['pollutant']} · {item['kind']} · "
            f"{item['period_key']} · {item['reason']}"
        )


if __name__ == "__main__":
    main()
