#!/usr/bin/env python3
"""Docker/cron orchestration with publisher-only GitHub token injection."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_PROJECT_ROOT = Path("/data/portfolio-tracker")
DEFAULT_RUNTIME_ROOT = Path("/data/portfolio")
DEFAULT_TOKEN_FILE = Path("/data/portfolio/secrets/github-token")
DEFAULT_REPOSITORY = "cliffordfok/portfolio-tracker"
DEFAULT_DATA_BRANCH = "portfolio-data"
SECRET_ENVIRONMENT_NAMES = {
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "PORTFOLIO_GITHUB_TOKEN",
}


class CronError(ValueError):
    """Raised when a cron action cannot run safely."""


@dataclass(frozen=True)
class CronConfig:
    project_root: Path
    runtime_root: Path
    token_file: Path
    repository: str
    branch: str
    snapshot_path: str
    python: str


Runner = Callable[
    [Sequence[str], Path, Mapping[str, str]],
    subprocess.CompletedProcess[str],
]


def scrubbed_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(source if source is not None else os.environ)
    for name in SECRET_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def validate_config(config: CronConfig) -> None:
    backend = config.project_root / "backend"
    cli = backend / "portfolio_tracker" / "cli.py"
    if not cli.is_file():
        raise CronError(f"portfolio tracker backend not found: {backend}")
    if not config.runtime_root.is_dir():
        raise CronError(f"runtime root not found: {config.runtime_root}")
    if "/" not in config.repository or config.repository.startswith("/"):
        raise CronError("repository must use owner/name")
    if not config.branch or any(char.isspace() for char in config.branch):
        raise CronError("data branch must be non-empty without whitespace")
    snapshot = Path(config.snapshot_path)
    if (
        snapshot.is_absolute()
        or ".." in snapshot.parts
        or config.snapshot_path.strip() != config.snapshot_path
    ):
        raise CronError("snapshot path must be a safe repository-relative path")


def read_publisher_token(path: Path) -> str:
    if path.is_symlink():
        raise CronError("publisher token file must not be a symlink")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise CronError(f"publisher token file not found: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CronError("publisher token path must be a regular file")
    if metadata.st_size < 1 or metadata.st_size > 1024:
        raise CronError("publisher token file has an invalid size")
    if os.name == "posix":
        mode = stat.S_IMODE(metadata.st_mode)
        if mode != 0o600:
            raise CronError(
                f"publisher token file mode must be 600, got {mode:03o}"
            )
        if metadata.st_uid != os.geteuid():
            raise CronError("publisher token file must be owned by the cron user")
    try:
        content = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise CronError("publisher token file must contain one ASCII line") from exc
    lines = content.splitlines()
    if len(lines) != 1:
        raise CronError("publisher token file must contain exactly one line")
    token = lines[0]
    if (
        not token
        or token != token.strip()
        or any(char.isspace() for char in token)
    ):
        raise CronError("publisher token must be non-empty without whitespace")
    return token


def default_runner(
    command: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def portfolio_command(config: CronConfig, action: str) -> list[str]:
    command = [
        config.python,
        "-B",
        "-m",
        "portfolio_tracker.cli",
        "--root",
        str(config.runtime_root),
    ]
    if action == "rebuild":
        return [*command, "rebuild", "--if-needed"]
    if action in {"publish", "bootstrap-publish"}:
        publish = [
            *command,
            "publish",
            "--repository",
            config.repository,
            "--branch",
            config.branch,
            "--path",
            config.snapshot_path,
        ]
        if action == "bootstrap-publish":
            publish.append("--bootstrap")
        return publish
    if action == "backup":
        return [*command, "backup"]
    if action == "doctor":
        return [*command, "doctor"]
    if action == "doctor-active":
        return [
            *command,
            "doctor",
            "--require-initialized",
            "--require-current",
            "--require-published",
            "--require-backup",
        ]
    if action == "doctor-paper-active":
        return [
            *command,
            "doctor",
            "--require-paper-initialized",
            "--require-live-uninitialized",
            "--require-current",
            "--require-published",
            "--require-backup",
        ]
    raise CronError(f"unsupported cron action: {action}")


def parse_child_result(
    action: str,
    result: subprocess.CompletedProcess[str],
    *,
    redaction: str | None,
) -> dict[str, Any]:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        if redaction:
            detail = detail.replace(redaction, "[REDACTED]")
        raise CronError(
            f"{action} failed with exit {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    safe_stdout = result.stdout
    if redaction:
        safe_stdout = safe_stdout.replace(redaction, "[REDACTED]")
    try:
        payload = json.loads(safe_stdout)
    except json.JSONDecodeError as exc:
        raise CronError(f"{action} returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise CronError(f"{action} returned an invalid result object")
    return payload


def run_action(
    config: CronConfig,
    action: str,
    *,
    runner: Runner = default_runner,
    source_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    validate_config(config)
    environment = scrubbed_environment(source_environment)
    environment["PYTHONPATH"] = str(config.project_root / "backend")
    token: str | None = None
    if action in {"publish", "bootstrap-publish"}:
        token = read_publisher_token(config.token_file)
        environment["PORTFOLIO_GITHUB_TOKEN"] = token
    command = portfolio_command(config, action)
    try:
        result = runner(
            command,
            config.project_root / "backend",
            environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CronError(f"{action} process failed: {exc}") from exc
    finally:
        if token is not None:
            token = None
    return parse_child_result(
        action,
        result,
        redaction=environment.get("PORTFOLIO_GITHUB_TOKEN"),
    )


def execute(
    config: CronConfig,
    action: str,
    *,
    runner: Runner = default_runner,
    source_environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if action == "maintain":
        steps = ["rebuild", "publish"]
    elif action == "bootstrap-publish":
        steps = ["rebuild", "bootstrap-publish"]
    else:
        steps = [action]
    results = [
        {
            "action": step,
            "result": run_action(
                config,
                step,
                runner=runner,
                source_environment=source_environment,
            ),
        }
        for step in steps
    ]
    return {
        "status": "ok",
        "action": action,
        "steps": results,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Run Portfolio Tracker maintenance from Docker cron"
    )
    command.add_argument(
        "action",
        choices=(
            "maintain",
            "rebuild",
            "publish",
            "bootstrap-publish",
            "backup",
            "doctor",
            "doctor-active",
            "doctor-paper-active",
        ),
    )
    command.add_argument(
        "--project-root",
        default=os.environ.get(
            "PORTFOLIO_PROJECT_ROOT",
            str(DEFAULT_PROJECT_ROOT),
        ),
    )
    command.add_argument(
        "--runtime-root",
        default=os.environ.get(
            "PORTFOLIO_RUNTIME_ROOT",
            str(DEFAULT_RUNTIME_ROOT),
        ),
    )
    command.add_argument(
        "--token-file",
        default=os.environ.get(
            "PORTFOLIO_TOKEN_FILE",
            str(DEFAULT_TOKEN_FILE),
        ),
    )
    command.add_argument(
        "--repository",
        default=os.environ.get(
            "PORTFOLIO_REPOSITORY",
            DEFAULT_REPOSITORY,
        ),
    )
    command.add_argument(
        "--branch",
        default=os.environ.get(
            "PORTFOLIO_DATA_BRANCH",
            DEFAULT_DATA_BRANCH,
        ),
    )
    command.add_argument("--snapshot-path", default="portfolio-snapshot.json")
    command.add_argument("--python", default=sys.executable)
    return command


def config_from_args(args: argparse.Namespace) -> CronConfig:
    return CronConfig(
        project_root=Path(args.project_root).resolve(),
        runtime_root=Path(args.runtime_root).resolve(),
        token_file=Path(args.token_file).resolve(),
        repository=args.repository,
        branch=args.branch,
        snapshot_path=args.snapshot_path,
        python=args.python,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(config_from_args(args), args.action)
    except CronError as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
