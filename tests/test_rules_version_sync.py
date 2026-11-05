"""The dashboard UI and the pipeline must agree on the scientific rules version.

`packages/dashboard-ui/src/summary.ts` hardcodes SCIENTIFIC_RULES_VERSION and
shows every summary stamped with a different version as "Historical output",
and `forestAdapter.ts` drops pooled groups from another version. So a bump to
`ber_core.scientific.RULES_VERSION` that misses the TS twin flags every freshly
built dashboard as stale (and hides its forest plots).
"""
import re
from pathlib import Path

from ber_core.scientific import RULES_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_ui_rules_version_matches_pipeline():
    ts = (ROOT / "packages" / "dashboard-ui" / "src" / "summary.ts").read_text(encoding="utf-8")
    m = re.search(r'export const SCIENTIFIC_RULES_VERSION = "([^"]+)"', ts)
    assert m, "SCIENTIFIC_RULES_VERSION not found in summary.ts"
    assert m.group(1) == RULES_VERSION
