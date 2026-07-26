from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "portfolio_cron.py"
)
SPEC = importlib.util.spec_from_file_location("portfolio_cron", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load portfolio_cron.py")
CRON = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CRON
SPEC.loader.exec_module(CRON)


class RecordingRunner:
    def __init__(self, *, fail_action: str | None = None) -> None:
        self.fail_action = fail_action
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        command: list[str],
        cwd: Path,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        action = next(
            name
            for name in ("rebuild", "publish", "backup", "doctor")
            if name in command
        )
        self.calls.append(
            {
                "action": action,
                "command": list(command),
                "cwd": cwd,
                "environment": dict(environment),
            }
        )
        if action == self.fail_action:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr=json.dumps({"status": "error", "error": "failed"}),
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "current", "action": action}),
            stderr="",
        )


class PortfolioCronTests(unittest.TestCase):
    def config(self, directory: str) -> object:
        root = Path(directory)
        project = root / "project"
        backend = project / "backend" / "portfolio_tracker"
        backend.mkdir(parents=True)
        (backend / "cli.py").write_text("# test\n", encoding="utf-8")
        runtime = root / "runtime"
        runtime.mkdir()
        secret = root / "secret"
        secret.write_text("test-token-value\n", encoding="ascii")
        os.chmod(secret, 0o600)
        return CRON.CronConfig(
            project_root=project,
            runtime_root=runtime,
            token_file=secret,
            repository="cliffordfok/portfolio-tracker",
            branch="portfolio-data",
            snapshot_path="portfolio-snapshot.json",
            python="/usr/local/bin/python3",
        )

    def test_rebuild_scrubs_all_github_token_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            runner = RecordingRunner()
            result = CRON.execute(
                config,
                "rebuild",
                runner=runner,
                source_environment={
                    "PATH": "/usr/bin",
                    "GITHUB_TOKEN": "ambient",
                    "GH_TOKEN": "ambient-gh",
                    "PORTFOLIO_GITHUB_TOKEN": "ambient-portfolio",
                },
            )
        self.assertEqual(result["status"], "ok")
        environment = runner.calls[0]["environment"]
        self.assertTrue(
            CRON.SECRET_ENVIRONMENT_NAMES.isdisjoint(environment)
        )

    def test_publish_receives_only_scoped_portfolio_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            runner = RecordingRunner()
            result = CRON.execute(
                config,
                "publish",
                runner=runner,
                source_environment={"GITHUB_TOKEN": "ambient"},
            )
        environment = runner.calls[0]["environment"]
        self.assertEqual(
            environment["PORTFOLIO_GITHUB_TOKEN"],
            "test-token-value",
        )
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("test-token-value", json.dumps(result))
        self.assertNotIn("--bootstrap", runner.calls[0]["command"])

    def test_success_output_cannot_echo_publisher_token(self) -> None:
        result = subprocess.CompletedProcess(
            ["publisher"],
            0,
            stdout=json.dumps(
                {
                    "status": "published",
                    "unexpected": "test-token-value",
                }
            ),
            stderr="",
        )
        parsed = CRON.parse_child_result(
            "publish",
            result,
            redaction="test-token-value",
        )
        self.assertEqual(parsed["unexpected"], "[REDACTED]")
        self.assertNotIn(
            "test-token-value",
            json.dumps(parsed),
        )

    def test_maintain_rebuild_process_never_receives_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            runner = RecordingRunner()
            CRON.execute(
                config,
                "maintain",
                runner=runner,
                source_environment={
                    "PORTFOLIO_GITHUB_TOKEN": "ambient-portfolio",
                },
            )
        self.assertEqual(
            [call["action"] for call in runner.calls],
            ["rebuild", "publish"],
        )
        rebuild_env = runner.calls[0]["environment"]
        publish_env = runner.calls[1]["environment"]
        self.assertTrue(
            CRON.SECRET_ENVIRONMENT_NAMES.isdisjoint(rebuild_env)
        )
        self.assertEqual(
            publish_env["PORTFOLIO_GITHUB_TOKEN"],
            "test-token-value",
        )

    def test_failed_rebuild_stops_before_token_read_or_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            config.token_file.unlink()
            runner = RecordingRunner(fail_action="rebuild")
            with self.assertRaisesRegex(CRON.CronError, "rebuild failed"):
                CRON.execute(config, "maintain", runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["action"], "rebuild")

    def test_token_file_must_not_be_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            with mock.patch.object(
                type(config.token_file),
                "is_symlink",
                return_value=True,
            ):
                with self.assertRaisesRegex(
                    CRON.CronError,
                    "must not be a symlink",
                ):
                    CRON.read_publisher_token(config.token_file)

    def test_token_file_requires_one_trimmed_ascii_line(self) -> None:
        invalid_values = (
            "",
            " token\n",
            "token value\n",
            "first\nsecond\n",
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with tempfile.TemporaryDirectory() as directory:
                    config = self.config(directory)
                    config.token_file.write_text(value, encoding="ascii")
                    with self.assertRaises(CRON.CronError):
                        CRON.execute(
                            config,
                            "publish",
                            runner=RecordingRunner(),
                        )

    def test_token_file_requires_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            metadata = SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_size=config.token_file.stat().st_size,
                st_uid=1234,
            )
            with (
                mock.patch.object(CRON.os, "name", "posix"),
                mock.patch.object(
                    CRON.os,
                    "geteuid",
                    return_value=1234,
                    create=True,
                ),
                mock.patch.object(
                    type(config.token_file),
                    "stat",
                    return_value=metadata,
                ),
            ):
                with self.assertRaisesRegex(
                    CRON.CronError,
                    "mode must be 600",
                ):
                    CRON.read_publisher_token(config.token_file)

    def test_doctor_active_passes_every_acceptance_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            runner = RecordingRunner()
            CRON.execute(config, "doctor-active", runner=runner)
        command = runner.calls[0]["command"]
        for flag in (
            "--require-initialized",
            "--require-current",
            "--require-published",
            "--require-backup",
        ):
            self.assertIn(flag, command)

    def test_snapshot_path_cannot_escape_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            unsafe = CRON.CronConfig(
                **{
                    **config.__dict__,
                    "snapshot_path": "../private.json",
                }
            )
            with self.assertRaisesRegex(CRON.CronError, "safe repository-relative"):
                CRON.execute(
                    unsafe,
                    "publish",
                    runner=RecordingRunner(),
                )


if __name__ == "__main__":
    unittest.main()
