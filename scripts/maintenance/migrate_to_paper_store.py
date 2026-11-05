#!/usr/bin/env python3
"""Migrate a legacy pdfs/ + converted/ project into the per-paper store.

Entry point only; the logic lives in ``ber_core/migrate_to_paper_store.py``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, for ber_core

from ber_core.migrate_to_paper_store import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
