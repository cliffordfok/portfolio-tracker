#!/usr/bin/env python3
"""Read one historical close through the deployed yfinance quote provider."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any


SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")


class QuoteCheckError(ValueError):
    """Raised when the provider cannot return one trustworthy close."""


def load_provider(path: Path) -> ModuleType:
    selected = path.resolve()
    if not selected.is_file() or selected.is_symlink():
        raise QuoteCheckError(
            "provider script must be a regular non-symlink file"
        )
    spec = importlib.util.spec_from_file_location(
        "deployed_market_quote_provider",
        selected,
    )
    if spec is None or spec.loader is None:
        raise QuoteCheckError("provider script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (ImportError, OSError, RuntimeError) as exc:
        raise QuoteCheckError(f"provider import failed: {exc}") from exc
    if not callable(getattr(module, "fetch_close", None)):
        raise QuoteCheckError("provider script has no fetch_close function")
    return module


def _normalized_close(value: Any, *, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QuoteCheckError(f"provider returned an invalid {field}")
    if value <= 0:
        raise QuoteCheckError(f"provider returned a non-positive {field}")
    return format(value, ".8f").rstrip("0").rstrip(".")


def check_close(
    provider: ModuleType,
    *,
    symbol: str,
    session_date: str,
) -> dict[str, str]:
    selected_symbol = symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(selected_symbol):
        raise QuoteCheckError("symbol is invalid")
    try:
        session = date.fromisoformat(session_date)
    except ValueError as exc:
        raise QuoteCheckError(
            "session date must be a real YYYY-MM-DD date"
        ) from exc
    if session.isoformat() != session_date:
        raise QuoteCheckError("session date must use YYYY-MM-DD")
    try:
        raw = provider.fetch_close(
            selected_symbol,
            session_date,
            adjust=False,
        )
        adjusted = provider.fetch_close(
            selected_symbol,
            session_date,
            adjust=True,
        )
    except Exception as exc:
        raise QuoteCheckError(f"provider fetch failed: {exc}") from exc
    if raw is None or adjusted is None:
        raise QuoteCheckError("provider returned no complete session row")
    return {
        "status": "verified",
        "provider": "yfinance",
        "symbol": selected_symbol,
        "session_date": session_date,
        "raw_close": _normalized_close(raw, field="raw close"),
        "adjusted_close": _normalized_close(
            adjusted,
            field="adjusted close",
        ),
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="check-yfinance-close")
    command.add_argument("--provider-script", required=True)
    command.add_argument("--symbol", required=True)
    command.add_argument("--session-date", required=True)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        provider = load_provider(Path(args.provider_script))
        result = check_close(
            provider,
            symbol=args.symbol,
            session_date=args.session_date,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except QuoteCheckError as exc:
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
