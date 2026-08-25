"""Opt-in, fail-closed VLM assistance for the symbols pipeline."""

from .legend import (
    LegendAssistBackend,
    LegendAssistError,
    LegendVlmConfig,
    assist_page_legend,
)
from .review import (
    SymbolReviewBackend,
    SymbolReviewError,
    SymbolReviewVlmConfig,
    review_page_symbols,
)

__all__ = [
    "LegendAssistBackend",
    "LegendAssistError",
    "LegendVlmConfig",
    "SymbolReviewBackend",
    "SymbolReviewError",
    "SymbolReviewVlmConfig",
    "assist_page_legend",
    "review_page_symbols",
]
