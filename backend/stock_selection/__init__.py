"""A-share stock-selection research module."""

from .engine import BacktestResult, SelectionConfig, StockSelector, load_bars

__all__ = ["BacktestResult", "SelectionConfig", "StockSelector", "load_bars"]

