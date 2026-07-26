from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "verify_outbox_artifact.py"
)
SPEC = importlib.util.spec_from_file_location(
    "verify_outbox_artifact",
    SCRIPT_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load verify_outbox_artifact.py")
VERIFIER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = VERIFIER
SPEC.loader.exec_module(VERIFIER)


ORIGINAL = """\
def legacy():
    return "original"
"""

GOOD_PATCHED = """\
import fcntl
import os

def save_state(state):
    return None

def log_trade(entry):
    fd = os.open("log", os.O_CREAT)
    os.fchmod(fd, 0o600)

def build_outbox_event():
    return now.isoformat().replace("+00:00", "Z")

def drain_outbox(evt):
    return ["--created-at", evt["created_at"]]

def _enqueue_and_save(state, event):
    save_state(state)

def execute_legacy_sell():
    return None

def execute_legacy_partial_sell():
    return None

def execute_swing_buy(state, trade, event):
    state["trade_history"].append(trade)
    state["total_trades"] += 1
    event = build_outbox_event()
    _enqueue_and_save(state, event)
    log_trade(trade)
    drain_outbox(event)

def _run_locked():
    return None

def run():
    fd = os.open("lock", os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    _run_locked()
    fcntl.flock(fd, fcntl.LOCK_UN)
"""


class OutboxArtifactVerifierTests(unittest.TestCase):
    def facts(self, path: Path) -> dict[str, object]:
        payload = path.read_bytes()
        return {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "lines": len(payload.decode("utf-8").splitlines()),
            "mode": f"{stat.S_IMODE(path.stat().st_mode):03o}",
        }

    def write_bundle(
        self,
        directory: str,
        *,
        patched: str = GOOD_PATCHED,
        patch_override: str | None = None,
    ) -> tuple[object, Path]:
        root = Path(directory)
        names = {
            "original": "swing_trader.py.original",
            "patched": "swing_trader.py.patched.v4",
            "patch": "outbox.v4.patch",
            "tests": "test_outbox_artifact_v4.py",
            "manifest": "manifest.v4.json",
        }
        original_path = root / names["original"]
        patched_path = root / names["patched"]
        patch_path = root / names["patch"]
        tests_path = root / names["tests"]
        manifest_path = root / names["manifest"]
        original_path.write_text(ORIGINAL, encoding="utf-8", newline="\n")
        patched_path.write_text(patched, encoding="utf-8", newline="\n")
        patch_text = "".join(
            difflib.unified_diff(
                ORIGINAL.splitlines(keepends=True),
                patched.splitlines(keepends=True),
                fromfile=names["original"],
                tofile=names["patched"],
            )
        )
        patch_path.write_text(
            patch_override if patch_override is not None else patch_text,
            encoding="utf-8",
            newline="\n",
        )
        tests_path.write_text(
            "import importlib.machinery\n"
            "import importlib.util\n"
            "import os\n"
            "import sys\n"
            "import types\n"
            "from pathlib import Path\n"
            "if os.name == 'nt':\n"
            "    fcntl = types.ModuleType('fcntl')\n"
            "    fcntl.LOCK_EX = 1\n"
            "    fcntl.LOCK_NB = 2\n"
            "    fcntl.LOCK_UN = 8\n"
            "    fcntl.flock = lambda *args: None\n"
            "    sys.modules['fcntl'] = fcntl\n"
            "path = Path(os.environ['OUTBOX_ARTIFACT'])\n"
            "loader = importlib.machinery.SourceFileLoader('artifact', str(path))\n"
            "spec = importlib.util.spec_from_loader('artifact', loader)\n"
            "assert spec is not None and spec.loader is not None\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "assert callable(module.run)\n",
            encoding="utf-8",
            newline="\n",
        )
        for path in (original_path, patched_path, patch_path, tests_path):
            os.chmod(path, 0o600)
        manifest_path.write_text(
            json.dumps(
                {
                    path.name: self.facts(path)
                    for path in (
                        original_path,
                        patched_path,
                        patch_path,
                        tests_path,
                    )
                }
            ),
            encoding="utf-8",
            newline="\n",
        )
        os.chmod(manifest_path, 0o600)
        args = VERIFIER.parser().parse_args(
            [
                "--bundle-dir",
                str(root),
                "--patched",
                names["patched"],
                "--patch",
                names["patch"],
                "--tests",
                names["tests"],
                "--manifest",
                names["manifest"],
                "--expected-original-sha256",
                hashlib.sha256(ORIGINAL.encode()).hexdigest(),
                "--skip-mode-check",
            ]
        )
        return args, manifest_path

    def test_valid_bundle_passes_every_static_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(directory)
            result = VERIFIER.verify(args)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            result["patch"]["rebuilt_sha256"],
            result["patch"]["patched_sha256"],
        )
        self.assertEqual(result["source"]["run_lock"], "verified")
        self.assertEqual(result["source"]["buy_atomic_order"], "verified")

    def test_real_artifact_test_receives_exact_patched_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(directory)
            args.run_artifact_tests = True
            result = VERIFIER.verify(args)
        self.assertEqual(result["artifact_tests"]["status"], "passed")
        self.assertTrue(result["artifact_test_hashes_stable"])

    def test_test_file_must_dynamically_load_declared_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, manifest_path = self.write_bundle(directory)
            tests_path = Path(directory) / args.tests
            tests_path.write_text(
                "print('green without loading artifact')\n",
                encoding="utf-8",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest[args.tests] = self.facts(tests_path)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "OUTBOX_ARTIFACT",
            ):
                VERIFIER.verify(args)

    def test_manifest_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, manifest_path = self.write_bundle(directory)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest[args.patched]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "manifest mismatch",
            ):
                VERIFIER.verify(args)

    def test_patch_context_mismatch_fails_zero_fuzz(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patch_override=(
                    "--- swing_trader.py.original\n"
                    "+++ swing_trader.py.patched.v4\n"
                    "@@ -1,2 +1,2 @@\n"
                    "-def not_legacy():\n"
                    "+def replacement():\n"
                    '     return "original"\n'
                ),
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "context mismatch",
            ):
                VERIFIER.verify(args)

    def test_duplicate_run_definition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patched=GOOD_PATCHED + "\ndef run():\n    return None\n",
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "define run exactly once",
            ):
                VERIFIER.verify(args)

    def test_missing_run_locked_definition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patched=GOOD_PATCHED.replace(
                    "def _run_locked():",
                    "def another_name():",
                ),
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "define _run_locked exactly once",
            ):
                VERIFIER.verify(args)

    def test_invalid_offset_plus_z_builder_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patched=GOOD_PATCHED.replace(
                    'return now.isoformat().replace("+00:00", "Z")',
                    'now_utc = now.isoformat()\n'
                    '    now_utc += "Z"\n'
                    "    return now_utc",
                ),
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "produce UTC Z",
            ):
                VERIFIER.verify(args)

    def test_missing_created_at_forwarding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patched=GOOD_PATCHED.replace(
                    'return ["--created-at", evt["created_at"]]',
                    'return ["--occurred-at", evt["occurred_at"]]',
                ),
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "persisted --created-at",
            ):
                VERIFIER.verify(args)

    def test_existing_log_without_fchmod_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patched=GOOD_PATCHED.replace(
                    "    os.fchmod(fd, 0o600)\n",
                    "",
                ),
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "os.fchmod",
            ):
                VERIFIER.verify(args)

    def test_buy_history_after_save_is_rejected(self) -> None:
        bad_buy = """\
def execute_swing_buy(state, trade, event):
    _enqueue_and_save(state, event)
    state["trade_history"].append(trade)
    state["total_trades"] += 1
    event = build_outbox_event()
    log_trade(trade)
    drain_outbox(event)
"""
        good_buy = """\
def execute_swing_buy(state, trade, event):
    state["trade_history"].append(trade)
    state["total_trades"] += 1
    event = build_outbox_event()
    _enqueue_and_save(state, event)
    log_trade(trade)
    drain_outbox(event)
"""
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(
                directory,
                patched=GOOD_PATCHED.replace(good_buy, bad_buy),
            )
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "append trade_history before",
            ):
                VERIFIER.verify(args)

    def test_bundle_filename_cannot_escape_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args, _ = self.write_bundle(directory)
            args.patched = "../outside.py"
            with self.assertRaisesRegex(
                VERIFIER.ArtifactVerificationError,
                "direct bundle filename",
            ):
                VERIFIER.verify(args)


if __name__ == "__main__":
    unittest.main()
