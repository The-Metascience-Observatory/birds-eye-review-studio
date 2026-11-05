"""Pass 2 must extract the STAGED subset, never the whole converted corpus.

Regression tests for a bug that cost real money and corrupted real output.

`extract_pass2.py` resolved its input through `utils.resolve_input_dir`, whose
last-resort fallback is the project's `converted/` dir. The coupling to the
staged bucket lived only in the optional `dirs.eligible_trials` config key —
which NOTHING in the repo ever set (`ber_core/projects.py` hardcoded it to `""`).
So pass 2 silently extracted every converted paper instead of the staged-eligible
ones: on one review, 370 papers instead of 14, and 102 of a sampled 112 had been
marked excluded by screening.

The blast radius was not only cost. `evidence._screen_state` used to rank
"extracted" ABOVE the pass-1 verdict, so those ineligible papers then presented
as *Included* — laundering themselves into the PRISMA counts and the
meta-analysis pool.

Three properties are pinned here:

1. A missing bucket is a HARD FAILURE, not a fallback. (The bug.)
2. An empty-but-present bucket is legitimate (single-design corpus) — exit 0,
   extract nothing.
3. The pass-1 verdict outranks `extracted` in the evidence table, so a stray
   extraction can never again present as included.

Note the asymmetry that hid the bug for so long: `extract_pass1.py` uses the
SAME helper and the converted/ fallback is CORRECT there — pass 1 is supposed to
screen the whole corpus. Only pass 2 must be narrow, so the fix is deliberately
pass-2-local rather than a change to `resolve_input_dir`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS2 = REPO_ROOT / "steps/extract_pass2.py"


def _workspace(tmp: Path, *, converted: int = 3, bucket: str | None = None) -> Path:
    """A minimal project: `converted/` always populated, bucket optional.

    `converted` is deliberately non-empty in every case — that is the whole
    point. If pass 2 ever falls back to it, the test must see extractions it
    should not have made.
    """
    projects = tmp / "projects"
    proj = projects / "test_topic"
    (proj / "data").mkdir(parents=True)
    for i in range(converted):
        stem = f"10.1000--paper{i}"
        (proj / "converted" / stem).mkdir(parents=True)
        (proj / "converted" / stem / "body.md").write_text(
            "# Trial\n\n" + ("lorem ipsum " * 200), encoding="utf-8")
    if bucket is not None:
        (proj / bucket).mkdir(parents=True)

    cfg = {
        "topic": {"name": "Test Topic", "synonyms": ["test"], "description": "d",
                  "not_about": []},
        "article_types": ["Clinical Trial"],
        "intervention_categories": ["a", "other"],
        "outcome_domains": ["x", "other"],
        "models": {"extraction_pass2": "sonnet"},
    }
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return cfg_path


def _run_pass2(cfg_path: Path, tmp: Path, design: str = "rct"):
    # These subprocess tests inspect staging, not extraction. An unknown
    # provider fails closed if the one-paper case reaches the extraction loop.
    # Managed mode also prevents loading the operator's local provider config.
    env = {**os.environ, "BER_PROJECTS_DIR": str(tmp / "projects"),
           "BER_LLM_PROVIDER": "input_gate_tests_no_inference", "BER_MANAGED_RUN": "1"}
    return subprocess.run(
        [sys.executable, str(PASS2), "--config", str(cfg_path),
         "--design-type", design],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(REPO_ROOT),
    )


def test_missing_bucket_fails_loudly_and_extracts_nothing() -> None:
    """THE regression. Before the fix this extracted the whole converted corpus.

    A missing bucket means staging never ran. The only safe answer is to stop:
    silently extracting something else is what turned a config gap into a
    four-figure Sonnet bill and a contaminated evidence table.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = _workspace(tmp, converted=3, bucket=None)  # no eligible_rct/
        res = _run_pass2(cfg, tmp)

        assert res.returncode != 0, (
            "pass 2 must FAIL when the staged bucket is absent; instead it "
            f"exited 0 and would have extracted from converted/.\n{res.stderr}"
        )
        # The message has to name the fix, not just complain.
        assert "stage_eligible" in res.stderr
        # And nothing may have been written.
        out = tmp / "projects" / "test_topic" / "data" / "trial_extractions_pass2.jsonl"
        assert not out.exists() or not out.read_text().strip(), (
            "pass 2 wrote extractions despite having no staged input")


def test_empty_bucket_is_legitimate_not_an_error() -> None:
    """A single-design corpus has an empty observational bucket. That is normal.

    Guards against over-correcting the fix above into "any absence is fatal",
    which would break every RCT-only review's observational step.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = _workspace(tmp, converted=3, bucket="eligible_rct")  # exists, empty
        res = _run_pass2(cfg, tmp)

        assert res.returncode == 0, (
            f"an empty staged bucket must be a no-op, not a failure\n{res.stderr}")
        assert "Found 0 papers" in res.stderr, res.stderr
        out = tmp / "projects" / "test_topic" / "data" / "trial_extractions_pass2.jsonl"
        assert not out.exists() or not out.read_text().strip()


def test_bucket_is_preferred_over_converted_when_both_exist() -> None:
    """The work list comes from the bucket, not from whatever is on disk.

    `converted/` holds 3 papers and the bucket holds 1. Pass 2 must see 1.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        cfg = _workspace(tmp, converted=3, bucket="eligible_rct")
        proj = tmp / "projects" / "test_topic"
        (proj / "eligible_rct" / "10.1000--paper0").symlink_to(
            Path("..") / "converted" / "10.1000--paper0", target_is_directory=True)

        res = _run_pass2(cfg, tmp)
        assert "Found 1 papers" in res.stderr, (
            "pass 2 read the wrong directory — it must count the staged bucket "
            f"(1), not converted/ (3).\n{res.stderr}")


# --- The evidence-table half of the bug -------------------------------------

def test_pass1_verdict_outranks_a_stray_extraction() -> None:
    """An extracted-but-ineligible paper must NOT show as included.

    Extraction is downstream of the verdict, so "it was extracted" is evidence
    that something extracted the wrong set — not that the paper became eligible.
    This is what let 198 ineligible papers present as Included on a real review.
    """
    os.environ.setdefault("JWT_SECRET", "test-secret")
    from server.app.services import evidence

    row = {"extracted": True, "eligible": False, "relevance": "yes"}
    assert evidence._screen_state(row) == "screened_out"

    # A MANUAL include still wins — that override is intentional.
    forced = {**row, "user_included": True}
    assert evidence._screen_state(forced) == "included"


def test_extracted_still_implies_included_when_pass1_never_ruled() -> None:
    """Reviews extracted before the eligibility pass existed have no `eligible`
    key at all. Their rows must stay green — the fix above must not regress them."""
    os.environ.setdefault("JWT_SECRET", "test-secret")
    from server.app.services import evidence

    legacy = {"extracted": True, "relevance": "yes"}  # no `eligible` key
    assert evidence._screen_state(legacy) == "included"
