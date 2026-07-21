"""Market Morning product vertical.

This package is deliberately isolated from the existing research, backtest,
and live-trading runtimes. Importing it never enables the product or opens a
database connection; both require explicit environment configuration.
"""

from src.market_morning.models import Base

__all__ = ["Base"]
