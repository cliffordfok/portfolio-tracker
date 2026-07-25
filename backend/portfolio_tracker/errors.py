"""Domain errors with messages safe to expose in CLI output."""


class PortfolioError(Exception):
    """Base class for expected portfolio tracker failures."""


class ValidationError(PortfolioError):
    """An event does not satisfy the public ledger schema."""


class ConflictError(PortfolioError):
    """An idempotency key already exists with a different payload."""


class BusinessInvariantError(PortfolioError):
    """An event would make the portfolio state impossible."""


class LedgerCorruptionError(PortfolioError):
    """A JSONL ledger contains invalid bytes or a malformed middle record."""


class PublicationError(PortfolioError):
    """A snapshot cannot be safely published."""
