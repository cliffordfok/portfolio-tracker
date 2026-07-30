from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "check_yfinance_close.py"
)
SPEC = importlib.util.spec_from_file_location("check_yfinance_close", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load check_yfinance_close.py")
CHECK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECK
SPEC.loader.exec_module(CHECK)


class YfinanceCloseCheckTests(unittest.TestCase):
    def test_raw_and_adjusted_closes_are_reported_separately(self) -> None:
        calls: list[bool] = []

        def fetch_close(
            symbol: str,
            session_date: str,
            *,
            adjust: bool,
        ) -> float:
            self.assertEqual(symbol, "SPY")
            self.assertEqual(session_date, "2026-07-21")
            calls.append(adjust)
            return 748.28 if adjust else 749.11

        result = CHECK.check_close(
            SimpleNamespace(fetch_close=fetch_close),
            symbol="spy",
            session_date="2026-07-21",
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["raw_close"], "749.11")
        self.assertEqual(result["adjusted_close"], "748.28")
        self.assertEqual(calls, [False, True])

    def test_missing_provider_row_fails_closed(self) -> None:
        provider = SimpleNamespace(
            fetch_close=lambda *_args, **_kwargs: None,
        )
        with self.assertRaisesRegex(
            CHECK.QuoteCheckError,
            "no complete session row",
        ):
            CHECK.check_close(
                provider,
                symbol="SPY",
                session_date="2026-07-21",
            )

    def test_provider_script_requires_fetch_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = Path(directory) / "provider.py"
            provider.write_text("VALUE = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(
                CHECK.QuoteCheckError,
                "no fetch_close",
            ):
                CHECK.load_provider(provider)


if __name__ == "__main__":
    unittest.main()
