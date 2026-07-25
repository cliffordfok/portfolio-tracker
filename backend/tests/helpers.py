from __future__ import annotations

from typing import Any


def event(
    seq: int,
    action: str,
    *,
    portfolio: str = "paper",
    event_id: str | None = None,
    occurred_at: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    prefix = portfolio
    return {
        "event_id": event_id or f"{prefix}-{seq}",
        "portfolio": portfolio,
        "occurred_at": occurred_at or f"2024-01-{min(seq, 28):02d}T15:00:00Z",
        "created_at": f"2024-02-{min(seq, 28):02d}T10:00:00Z",
        "source": "test",
        "ledger_seq": seq,
        "action": action,
        **({"currency": "USD"} if action == "PORTFOLIO_OPEN" else {}),
        **fields,
    }


def candidate(
    action: str,
    *,
    portfolio: str,
    event_id: str,
    occurred_at: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "portfolio": portfolio,
        "occurred_at": occurred_at,
        "created_at": "2024-02-01T10:00:00Z",
        "source": "test",
        "action": action,
        **({"currency": "USD"} if action == "PORTFOLIO_OPEN" else {}),
        **fields,
    }
