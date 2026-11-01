"""Filesystem layout of an engine *host* (a portfolio instance).

The engine package itself is read-only. All writable state lives under one
home directory, resolved per call as $MARKET_ENGINE_HOME, else the current
working directory:

    <home>/
        config.json           the portfolio config you provide (see README)
        data/cache/           yfinance price/fundamentals cache (JSON, 6h TTL)
        data/state.json       dashboard snapshot written by scripts/refresh.py
        data/broker_raw.json  raw broker book (optional, for broker_merge)
        ledger/               decisions.jsonl + nav.json (append-only history)

Run the tools from your instance directory (or export MARKET_ENGINE_HOME
before invoking them) and everything stays where you put it — the repo never
accumulates portfolio data.
"""
import os


def home():
    return os.path.abspath(os.environ.get("MARKET_ENGINE_HOME") or os.getcwd())


def config_path():
    return os.path.join(home(), "config.json")


def data_dir():
    return os.path.join(home(), "data")


def cache_dir():
    return os.path.join(data_dir(), "cache")


def state_path():
    return os.path.join(data_dir(), "state.json")


def broker_raw_path():
    return os.path.join(data_dir(), "broker_raw.json")


def ledger_dir():
    return os.path.join(home(), "ledger")


def decisions_path():
    return os.path.join(ledger_dir(), "decisions.jsonl")


def nav_path():
    return os.path.join(ledger_dir(), "nav.json")
