#!/usr/bin/env python3
"""Verify a staged swing-trader outbox artifact before production deployment.

The verifier is intentionally independent of the production runtime. It checks
the frozen bundle's hashes, applies the unified diff without fuzz, inspects the
actual patched source, and can execute the bundle's own real-artifact tests in
an isolated subprocess.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


EXPECTED_ORIGINAL_SHA256 = (
    "b3c23c9347950dd8ae8edfa141d70b942b94ce0b5872fdf72582fe23144e6e63"
)
REQUIRED_FUNCTIONS = {
    "_enqueue_and_save",
    "_run_locked",
    "build_outbox_event",
    "drain_outbox",
    "execute_legacy_partial_sell",
    "execute_legacy_sell",
    "execute_swing_buy",
    "log_trade",
    "run",
    "save_state",
}
HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


class ArtifactVerificationError(ValueError):
    """Raised when a frozen review bundle fails acceptance."""


@dataclass(frozen=True)
class BundlePaths:
    root: Path
    original: Path
    patched: Path
    patch: Path
    tests: Path
    manifest: Path


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_facts(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactVerificationError(f"{path.name} must be UTF-8") from exc
    return {
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
        "lines": len(text.splitlines()),
        "mode": f"{stat.S_IMODE(path.stat().st_mode):03o}",
    }


def safe_child(root: Path, name: str, *, field: str) -> Path:
    candidate_name = Path(name)
    if (
        candidate_name.is_absolute()
        or len(candidate_name.parts) != 1
        or candidate_name.name in {"", ".", ".."}
    ):
        raise ArtifactVerificationError(f"{field} must be a direct bundle filename")
    candidate = (root / candidate_name.name).resolve()
    if candidate.parent != root:
        raise ArtifactVerificationError(f"{field} escapes the bundle directory")
    return candidate


def bundle_paths(args: argparse.Namespace) -> BundlePaths:
    root = Path(args.bundle_dir).resolve()
    if not root.is_dir():
        raise ArtifactVerificationError(f"bundle directory not found: {root}")
    return BundlePaths(
        root=root,
        original=safe_child(root, args.original, field="original"),
        patched=safe_child(root, args.patched, field="patched"),
        patch=safe_child(root, args.patch, field="patch"),
        tests=safe_child(root, args.tests, field="tests"),
        manifest=safe_child(root, args.manifest, field="manifest"),
    )


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationError("manifest must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ArtifactVerificationError("manifest must be a JSON object")
    parsed: dict[str, dict[str, Any]] = {}
    for name, facts in value.items():
        if not isinstance(name, str) or not isinstance(facts, dict):
            raise ArtifactVerificationError("manifest entries must be filename objects")
        parsed[name] = facts
    return parsed


def verify_manifest(
    paths: BundlePaths,
    *,
    expected_original_sha256: str,
    check_modes: bool,
) -> dict[str, dict[str, Any]]:
    files = (paths.original, paths.patched, paths.patch, paths.tests)
    for path in (*files, paths.manifest):
        if not path.is_file():
            raise ArtifactVerificationError(f"missing artifact: {path.name}")

    manifest = load_manifest(paths.manifest)
    expected_names = {path.name for path in files}
    if set(manifest) != expected_names:
        missing = sorted(expected_names - set(manifest))
        extra = sorted(set(manifest) - expected_names)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise ArtifactVerificationError(
            f"manifest file set mismatch: {'; '.join(details)}"
        )

    actual: dict[str, dict[str, Any]] = {}
    for path in files:
        facts = file_facts(path)
        actual[path.name] = facts
        declared = manifest[path.name]
        for field in ("sha256", "bytes", "lines", "mode"):
            if field not in declared:
                raise ArtifactVerificationError(
                    f"manifest {path.name} missing {field}"
                )
            declared_value = declared[field]
            actual_value: Any = facts[field]
            if field in {"bytes", "lines"}:
                if isinstance(declared_value, bool) or not isinstance(
                    declared_value, int
                ):
                    raise ArtifactVerificationError(
                        f"manifest {path.name}.{field} must be an integer"
                    )
            else:
                declared_value = str(declared_value).lower()
                actual_value = str(actual_value).lower()
            if field == "mode" and not check_modes:
                if re.fullmatch(r"[0-7]{3,4}", str(declared_value)) is None:
                    raise ArtifactVerificationError(
                        f"manifest {path.name}.mode must be an octal mode"
                    )
                continue
            if declared_value != actual_value:
                raise ArtifactVerificationError(
                    f"manifest mismatch for {path.name}.{field}: "
                    f"declared {declared_value}, actual {actual_value}"
                )
        if check_modes and facts["mode"] != "600":
            raise ArtifactVerificationError(
                f"{path.name} mode must be 600, got {facts['mode']}"
            )

    if actual[paths.original.name]["sha256"] != expected_original_sha256:
        raise ArtifactVerificationError(
            "original SHA-256 does not match the approved production baseline"
        )
    if check_modes:
        manifest_mode = f"{stat.S_IMODE(paths.manifest.stat().st_mode):03o}"
        if manifest_mode != "600":
            raise ArtifactVerificationError(
                f"{paths.manifest.name} mode must be 600, got {manifest_mode}"
            )
    return actual


def parse_range_count(value: str | None) -> int:
    return 1 if value is None else int(value)


def apply_unified_patch(original: bytes, patch_payload: bytes) -> bytes:
    try:
        old_text = original.decode("utf-8")
        patch_text = patch_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactVerificationError("source and patch must be UTF-8") from exc
    if "\r" in old_text or "\r" in patch_text:
        raise ArtifactVerificationError("source and patch must use LF line endings")
    if old_text.startswith("\ufeff") or patch_text.startswith("\ufeff"):
        raise ArtifactVerificationError("source and patch must not contain a BOM")

    patch_lines = patch_text.splitlines()
    if len(patch_lines) < 3:
        raise ArtifactVerificationError("patch is incomplete")
    if not patch_lines[0].startswith("--- ") or not patch_lines[1].startswith("+++ "):
        raise ArtifactVerificationError("patch must start with --- and +++ headers")

    old_lines = old_text.splitlines()
    output: list[str] = []
    old_index = 0
    index = 2
    saw_hunk = False

    while index < len(patch_lines):
        header = HUNK_RE.match(patch_lines[index])
        if header is None:
            raise ArtifactVerificationError(
                f"unexpected patch line {index + 1}: {patch_lines[index][:40]}"
            )
        saw_hunk = True
        old_start = int(header.group("old_start"))
        old_count = parse_range_count(header.group("old_count"))
        new_count = parse_range_count(header.group("new_count"))
        target_index = max(old_start - 1, 0)
        if target_index < old_index or target_index > len(old_lines):
            raise ArtifactVerificationError("patch hunk has an invalid old range")
        output.extend(old_lines[old_index:target_index])
        old_index = target_index
        index += 1
        consumed_old = 0
        produced_new = 0

        while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
            line = patch_lines[index]
            if line == r"\ No newline at end of file":
                raise ArtifactVerificationError(
                    "no-final-newline patches are not supported"
                )
            if not line or line[0] not in {" ", "+", "-"}:
                raise ArtifactVerificationError(
                    f"invalid hunk line {index + 1}"
                )
            marker, content = line[0], line[1:]
            if marker in {" ", "-"}:
                if old_index >= len(old_lines) or old_lines[old_index] != content:
                    raise ArtifactVerificationError(
                        f"zero-fuzz context mismatch at patch line {index + 1}"
                    )
                old_index += 1
                consumed_old += 1
            if marker in {" ", "+"}:
                output.append(content)
                produced_new += 1
            index += 1

        if consumed_old != old_count or produced_new != new_count:
            raise ArtifactVerificationError(
                "patch hunk line counts do not match its header"
            )

    if not saw_hunk:
        raise ArtifactVerificationError("patch contains no hunks")
    output.extend(old_lines[old_index:])
    final_text = "\n".join(output)
    if old_text.endswith("\n"):
        final_text += "\n"
    return final_text.encode("utf-8")


def call_name(node: ast.Call) -> str | None:
    value: ast.AST = node.func
    parts: list[str] = []
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
        return ".".join(reversed(parts))
    return None


def function_calls(function: ast.FunctionDef) -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name is not None:
                calls.append((name, node.lineno))
    return calls


def function_map(tree: ast.Module) -> dict[str, list[ast.FunctionDef]]:
    functions: dict[str, list[ast.FunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)
    return functions


def has_utc_z_replace(function: ast.FunctionDef) -> bool:
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "+00:00"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "Z"
        ):
            return True
    return False


def has_total_trades_update(function: ast.FunctionDef) -> int | None:
    for node in ast.walk(function):
        if not isinstance(node, ast.AugAssign):
            continue
        target = node.target
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "state"
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "total_trades"
        ):
            return node.lineno
    return None


def verify_source(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ArtifactVerificationError(
            f"patched source is not valid Python: {exc}"
        ) from exc
    functions = function_map(tree)
    for name in sorted(REQUIRED_FUNCTIONS):
        count = len(functions.get(name, []))
        if count != 1:
            raise ArtifactVerificationError(
                f"patched source must define {name} exactly once, found {count}"
            )

    run_call_list = function_calls(functions["run"][0])
    run_calls = {name for name, _ in run_call_list}
    for required_call in ("_run_locked", "fcntl.flock", "os.open"):
        if required_call not in run_calls:
            raise ArtifactVerificationError(
                f"run must call {required_call}"
            )
    lock_lines = [line for name, line in run_call_list if name == "fcntl.flock"]
    locked_body_lines = [
        line for name, line in run_call_list if name == "_run_locked"
    ]
    if (
        len(lock_lines) < 2
        or len(locked_body_lines) != 1
        or min(lock_lines) >= locked_body_lines[0]
        or max(lock_lines) <= locked_body_lines[0]
    ):
        raise ArtifactVerificationError(
            "run must hold flock around exactly one _run_locked call"
        )

    outbox_source = ast.get_source_segment(
        source, functions["build_outbox_event"][0]
    ) or ""
    if not has_utc_z_replace(functions["build_outbox_event"][0]):
        raise ArtifactVerificationError(
            "build_outbox_event must produce UTC Z using replace('+00:00', 'Z')"
        )
    if re.search(r"\bnow_utc\s*\+=\s*[\"']Z[\"']", outbox_source):
        raise ArtifactVerificationError(
            "build_outbox_event appends Z to an existing timezone offset"
        )

    drain_source = ast.get_source_segment(source, functions["drain_outbox"][0]) or ""
    if '"--created-at"' not in drain_source and "'--created-at'" not in drain_source:
        raise ArtifactVerificationError(
            "drain_outbox must pass the persisted --created-at value"
        )
    if '["created_at"]' not in drain_source and "['created_at']" not in drain_source:
        raise ArtifactVerificationError(
            "drain_outbox must read created_at from the persisted event"
        )

    log_calls = {name for name, _ in function_calls(functions["log_trade"][0])}
    if "os.fchmod" not in log_calls:
        raise ArtifactVerificationError(
            "log_trade must harden an existing audit log with os.fchmod"
        )

    buy = functions["execute_swing_buy"][0]
    buy_calls = function_calls(buy)
    enqueue_lines = [
        line for name, line in buy_calls if name == "_enqueue_and_save"
    ]
    history_lines = [
        node.lineno
        for node in ast.walk(buy)
        if isinstance(node, ast.Call)
        and "trade_history" in (ast.get_source_segment(source, node) or "")
        and ".append(" in (ast.get_source_segment(source, node) or "")
    ]
    total_line = has_total_trades_update(buy)
    build_lines = [
        line for name, line in buy_calls if name == "build_outbox_event"
    ]
    log_lines = [line for name, line in buy_calls if name == "log_trade"]
    drain_lines = [line for name, line in buy_calls if name == "drain_outbox"]
    if len(enqueue_lines) != 1:
        raise ArtifactVerificationError(
            "execute_swing_buy must call _enqueue_and_save exactly once"
        )
    enqueue_line = enqueue_lines[0]
    if not history_lines or min(history_lines) >= enqueue_line:
        raise ArtifactVerificationError(
            "execute_swing_buy must append trade_history before its durable save"
        )
    if total_line is None or total_line >= enqueue_line:
        raise ArtifactVerificationError(
            "execute_swing_buy must update total_trades before its durable save"
        )
    if len(build_lines) != 1 or build_lines[0] >= enqueue_line:
        raise ArtifactVerificationError(
            "execute_swing_buy must build one immutable event before its durable save"
        )
    if (
        len(log_lines) != 1
        or len(drain_lines) != 1
        or enqueue_line >= log_lines[0]
        or log_lines[0] >= drain_lines[0]
    ):
        raise ArtifactVerificationError(
            "execute_swing_buy must save, then audit, then drain"
        )

    return {
        "functions": {name: functions[name][0].lineno for name in sorted(REQUIRED_FUNCTIONS)},
        "run_lock": "verified",
        "utc_z": "verified",
        "created_at_forwarding": "verified",
        "log_mode_hardening": "verified",
        "buy_atomic_order": "verified",
    }


def verify_patch(paths: BundlePaths) -> dict[str, str]:
    rebuilt = apply_unified_patch(paths.original.read_bytes(), paths.patch.read_bytes())
    actual = paths.patched.read_bytes()
    if rebuilt != actual:
        raise ArtifactVerificationError(
            "zero-fuzz patch result is not byte-identical to the patched artifact"
        )
    return {
        "rebuilt_sha256": sha256_bytes(rebuilt),
        "patched_sha256": sha256_bytes(actual),
    }


def verify_test_contract(path: Path) -> dict[str, str]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ArtifactVerificationError(
            f"artifact test file is not valid Python: {exc}"
        ) from exc
    if "OUTBOX_ARTIFACT" not in source:
        raise ArtifactVerificationError(
            "artifact tests must load the path from OUTBOX_ARTIFACT"
        )
    calls = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for name in [call_name(node)]
        if name is not None
    }
    supported_loaders = {
        "importlib.machinery.SourceFileLoader",
        "importlib.util.spec_from_file_location",
        "runpy.run_path",
    }
    if calls.isdisjoint(supported_loaders):
        raise ArtifactVerificationError(
            "artifact tests must dynamically execute OUTBOX_ARTIFACT"
        )
    return {
        "path_environment": "OUTBOX_ARTIFACT",
        "dynamic_loader": sorted(calls & supported_loaders)[0],
    }


def run_artifact_tests(paths: BundlePaths, *, timeout: int) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["OUTBOX_ARTIFACT"] = str(paths.patched)
    result = subprocess.run(
        [sys.executable, "-B", str(paths.tests)],
        cwd=paths.root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()[-2000:]
        stdout = result.stdout.strip()[-2000:]
        raise ArtifactVerificationError(
            "artifact tests failed"
            + (f"; stdout: {stdout}" if stdout else "")
            + (f"; stderr: {stderr}" if stderr else "")
        )
    return {
        "status": "passed",
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def verify(args: argparse.Namespace) -> dict[str, Any]:
    paths = bundle_paths(args)
    check_modes = os.name == "posix" and not args.skip_mode_check
    facts = verify_manifest(
        paths,
        expected_original_sha256=args.expected_original_sha256.lower(),
        check_modes=check_modes,
    )
    patch = verify_patch(paths)
    source = verify_source(paths.patched)
    test_contract = verify_test_contract(paths.tests)
    manifest_facts = file_facts(paths.manifest)
    result: dict[str, Any] = {
        "status": "valid",
        "bundle": str(paths.root),
        "mode_check": "enforced" if check_modes else "skipped",
        "artifacts": facts,
        "manifest": manifest_facts,
        "patch": patch,
        "source": source,
        "test_contract": test_contract,
    }
    if args.run_artifact_tests:
        frozen_paths = (
            paths.original,
            paths.patched,
            paths.patch,
            paths.tests,
            paths.manifest,
        )
        before = {
            path.name: file_facts(path)["sha256"]
            for path in frozen_paths
        }
        result["artifact_tests"] = run_artifact_tests(
            paths,
            timeout=args.test_timeout,
        )
        after = {
            path.name: file_facts(path)["sha256"]
            for path in frozen_paths
        }
        if before != after:
            raise ArtifactVerificationError(
                "a frozen artifact changed while its tests were running"
            )
        result["artifact_test_hashes_stable"] = True
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Verify a frozen swing-trader outbox review bundle"
    )
    command.add_argument("--bundle-dir", required=True)
    command.add_argument("--original", default="swing_trader.py.original")
    command.add_argument("--patched", required=True)
    command.add_argument("--patch", required=True)
    command.add_argument("--tests", required=True)
    command.add_argument("--manifest", required=True)
    command.add_argument(
        "--expected-original-sha256",
        default=EXPECTED_ORIGINAL_SHA256,
    )
    command.add_argument("--skip-mode-check", action="store_true")
    command.add_argument("--run-artifact-tests", action="store_true")
    command.add_argument("--test-timeout", type=int, default=180)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = verify(args)
    except (
        ArtifactVerificationError,
        OSError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
