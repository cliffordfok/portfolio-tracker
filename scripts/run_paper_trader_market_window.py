#!/usr/bin/env python3
"""Run the Paper trader only at the approved New York market time."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Callable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.market_time import (
    NEW_YORK_NAME,
    is_nyse_early_close,
    new_york_local,
)
from portfolio_tracker.snapshot import is_nyse_session


TARGET_TIME = time(15, 45)
SECRET_ENVIRONMENT_NAMES = {
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "PORTFOLIO_GITHUB_TOKEN",
}


class PaperWindowError(ValueError):
    """Raised when the guarded Paper trader cannot run safely."""


@dataclass(frozen=True)
class WindowDecision:
    eligible: bool
    reason: str
    local_time: datetime


Runner = Callable[
    [Sequence[str], Mapping[str, str]],
    subprocess.CompletedProcess[str],
]


def decide(
    now_utc: datetime,
    *,
    target_time: time = TARGET_TIME,
) -> WindowDecision:
    """Return whether this invocation is the one daily permitted execution."""

    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise PaperWindowError("current time must include a timezone")
    local = new_york_local(now_utc)
    if not is_nyse_session(local.date()):
        return WindowDecision(False, "not_nyse_session", local)
    if is_nyse_early_close(local.date()):
        return WindowDecision(False, "early_close_session", local)
    if (local.hour, local.minute) != (
        target_time.hour,
        target_time.minute,
    ):
        return WindowDecision(False, "outside_market_window", local)
    return WindowDecision(True, "market_window", local)


def scrubbed_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    for name in SECRET_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def default_runner(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        env=dict(environment),
        check=False,
        timeout=3600,
        text=True,
    )


def execute(
    command: Sequence[str],
    *,
    now_utc: datetime | None = None,
    check_only: bool = False,
    runner: Runner = default_runner,
    source_environment: Mapping[str, str] | None = None,
) -> tuple[dict[str, object], int]:
    current = (now_utc or datetime.now(UTC)).astimezone(UTC)
    decision = decide(current)
    result: dict[str, object] = {
        "status": "eligible" if decision.eligible else "skipped",
        "reason": decision.reason,
        "timezone": NEW_YORK_NAME,
        "local_date": decision.local_time.date().isoformat(),
        "local_time": decision.local_time.strftime("%H:%M:%S%z"),
        "target_time": TARGET_TIME.strftime("%H:%M"),
        "check_only": check_only,
    }
    if check_only or not decision.eligible:
        return result, 0
    if not command:
        raise PaperWindowError("an executable command is required")
    try:
        completed = runner(
            list(command),
            scrubbed_environment(source_environment),
        )
    except subprocess.TimeoutExpired as exc:
        raise PaperWindowError("Paper trader timed out") from exc
    except OSError as exc:
        raise PaperWindowError(f"Paper trader could not start: {exc}") from exc
    result["status"] = "completed" if completed.returncode == 0 else "failed"
    result["returncode"] = completed.returncode
    return result, completed.returncode


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="run-paper-trader-market-window",
        description=(
            "Permit the Paper trader only at 15:45 America/New_York on "
            "NYSE sessions. Schedule this guard at both 19:45 and 20:45 UTC "
            "to cover daylight and standard time."
        ),
    )
    command.add_argument(
        "--check-only",
        action="store_true",
        help="report eligibility without running the child command",
    )
    command.add_argument(
        "child_command",
        nargs=argparse.REMAINDER,
        help="child command after --",
    )
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    child = list(args.child_command)
    if child[:1] == ["--"]:
        child = child[1:]
    try:
        result, returncode = execute(
            child,
            check_only=args.check_only,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return returncode
    except PaperWindowError as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
