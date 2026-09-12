#!/usr/bin/env python3
"""Compatibility entry point. Legacy unsafe operations are intentionally disabled.

The legacy implementation remains in Git history, not as an executable fallback.
An already running legacy process retains its in-memory code: stop it explicitly
before using write operations in this offline safety preview.
"""
import sys

sys.dont_write_bytecode = True

if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10+ is required.")

from wb_account_sync.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
