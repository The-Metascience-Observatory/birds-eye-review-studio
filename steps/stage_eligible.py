#!/usr/bin/env python3
"""Stage pass-1-eligible papers into design buckets for pass 2.

Entry point only; the logic lives in ``ber_core/stage_eligible.py``.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for ber_core

from ber_core.stage_eligible import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
