#!/usr/bin/env python3
"""Run one AirSat task with a clean workspace, strict validation, and retry."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        timeout=timeout,
    )


def clean_target(root: Path) -> None:
    subprocess.run(
        ["git", "reset", "--hard", "HEAD"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "clean",
            "-fd",
            "--",
            "public/data",
            "public/visual_real",
        ],
        cwd=root,
        check=True,
    )


def task_environment(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env["AIRSAT_REPOSITORY_ROOT"] = str(args.root)
    env["AIRSAT_POLLUTANTS"] = args.pollutant
    # A planned task must actually rebuild; stale files must never be skipped.
    env["AIRSAT_SKIP_EXISTING"] = "false"

    if args.kind == "dynamic":
        env["AIRSAT_BUILD_MODE"] = "daily"
        env["AIRSAT_PERIOD_KEYS"] = args.period_key
    elif args.kind == "annual":
        env["AIRSAT_BUILD_MODE"] = "bootstrap"
        env["AIRSAT_BUILD_TIMESERIES"] = "false"
        env["AIRSAT_PERIOD_KEYS"] = args.period_key
    elif args.kind == "timeseries":
        env["AIRSAT_BUILD_MODE"] = "timeseries"
        env["AIRSAT_TIMESERIES_YEAR"] = args.year
        env["AIRSAT_TIMESERIES_MONTHS"] = args.months
    elif args.kind == "range":
        env["AIRSAT_BUILD_MODE"] = "ranges"
        env["AIRSAT_PERIOD_KEYS"] = args.period_key
    else:
        raise RuntimeError(f"Unknown task kind: {args.kind}")

    return env


def validation_command(args: argparse.Namespace) -> list[str]:
    validator = str(args.runner_root / "scripts/validate_airsat_outputs.py")
    command = [
        sys.executable,
        validator,
        "--pollutants",
        args.pollutant,
    ]

    if args.kind == "timeseries":
        command.extend(
            [
                "--require-timeseries",
                "--skip-layers",
                "--timeseries-periods",
                args.timeseries_periods,
            ]
        )
    else:
        command.extend(
            [
                "--periods",
                args.period_key,
                "--require-stats",
                "--require-available",
            ]
        )

    return command


def write_success_record(
    args: argparse.Namespace,
    attempt: int,
    duration_seconds: float,
) -> None:
    health_dir = args.root / "public" / "data" / "health" / "tasks"
    health_dir.mkdir(parents=True, exist_ok=True)
    path = health_dir / f"{args.pollutant}_{args.period_key}.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "status": "success",
                "pollutant": args.pollutant,
                "kind": args.kind,
                "period_key": args.period_key,
                "year": args.year or None,
                "months": args.months or None,
                "attempt": attempt,
                "duration_seconds": round(duration_seconds, 2),
                "completed_at_utc": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runner-root", type=Path, required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--pollutant", required=True)
    parser.add_argument("--period-key", required=True)
    parser.add_argument("--year", default="")
    parser.add_argument("--months", default="")
    parser.add_argument("--timeseries-periods", default="")
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument(
        "--attempt-timeout-seconds",
        type=int,
        default=7800,
    )
    args = parser.parse_args()

    args.root = args.root.resolve()
    args.runner_root = args.runner_root.resolve()
    build_script = str(args.runner_root / "scripts/build_airsat_static.py")
    validate = validation_command(args)
    last_error: Exception | None = None

    for attempt in range(1, args.attempts + 1):
        started = time.monotonic()
        try:
            print(
                f"AirSat task attempt {attempt}/{args.attempts}: "
                f"{args.pollutant} {args.period_key}",
                flush=True,
            )
            clean_target(args.root)
            env = task_environment(args)

            run(
                [sys.executable, build_script],
                cwd=args.root.parent,
                env=env,
                timeout=args.attempt_timeout_seconds,
            )
            run(
                validate,
                cwd=args.root.parent,
                env=env,
                timeout=900,
            )

            duration = time.monotonic() - started
            write_success_record(args, attempt, duration)
            print(
                f"Task validated successfully in {duration:.1f} seconds.",
                flush=True,
            )
            return

        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            RuntimeError,
        ) as error:
            last_error = error
            print(
                f"Attempt {attempt} failed: {error!r}",
                file=sys.stderr,
                flush=True,
            )
            if attempt < args.attempts:
                delay = 45 * attempt
                print(f"Retrying after {delay} seconds.", flush=True)
                time.sleep(delay)

    raise RuntimeError(
        f"AirSat task failed after {args.attempts} attempts: {last_error!r}"
    )


if __name__ == "__main__":
    main()
