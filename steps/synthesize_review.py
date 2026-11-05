#!/usr/bin/env python3
"""Synthesize a narrative review document from a built bundle.

Entry point only; the logic lives in ``ber_core/synthesize_review.py``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.synthesize_review import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
