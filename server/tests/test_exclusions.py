"""The shared exclusion loader (`utils.load_exclusion_dois`).

Guards a silent data bug: `export_dashboard.py` used to require an
``{"exclusions": {...}}`` wrapper while `pool_meta_analysis.py` also accepted a
bare ``{doi: reason}`` mapping. A hand-written file in the bare form therefore
excluded papers from the pooled estimates but NOT from the published dashboard,
so the two disagreed about which papers were in the review — with nothing
flagging it. Both now delegate here.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ber_core.utils import load_exclusion_dois, load_inclusion_dois, norm_paper_id  # noqa: E402


def _write(obj) -> Path:
    p = Path(tempfile.mkdtemp()) / "delivery_exclusions.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_wrapper_and_bare_forms_agree() -> None:
    """The exact divergence that caused the bug."""
    wrapped = _write({"exclusions": {"10.1/AB": "oral", "_note": "comment"}})
    bare = _write({"10.1/AB": "oral", "_note": "comment"})
    assert load_exclusion_dois(wrapped) == load_exclusion_dois(bare) == {"10.1/ab"}


def test_underscore_keys_are_comments_not_dois() -> None:
    assert load_exclusion_dois(_write({"_comment": "why", "10.1/x": "r"})) == {"10.1/x"}


def test_escaped_filenames_and_arm_suffixes_normalize() -> None:
    """Paper ids appear as DOIs, as `--`-escaped stems, and with an `#arm` suffix.

    A mismatch here means an excluded paper silently reappears in the output,
    which is worse than a crash because nothing reports it.
    """
    assert load_exclusion_dois(_write({"exclusions": {"10.1--AB#arm1": "r"}})) == {
        "10.1/ab"
    }
    assert norm_paper_id("10.1--AB#arm1") == norm_paper_id("10.1/ab") == "10.1/ab"


def test_inclusion_loader_mirrors_the_exclusion_loader() -> None:
    """The force-include loader is the positive twin: same bare/wrapper shapes,
    same normalization + comment handling, same optional-file tolerance."""
    wrapped = _write({"inclusions": {"10.1/AB": "want", "_note": "c"}})
    bare = _write({"10.1/AB": "want", "_note": "c"})
    assert load_inclusion_dois(wrapped) == load_inclusion_dois(bare) == {"10.1/ab"}
    assert load_inclusion_dois(_write({"10.1--AB#arm1": "r"})) == {"10.1/ab"}
    assert load_inclusion_dois(None) == set()
    assert load_inclusion_dois(Path(tempfile.mkdtemp()) / "nope.json") == set()


def test_missing_file_is_empty_and_a_corrupt_one_is_loud() -> None:
    """An exclusions file is optional: missing, None or empty means "no
    decisions". A file that EXISTS but is unreadable is different — reading it
    as "no decisions" silently re-admitted every manually excluded paper — so
    the loader raises once it knows how (``CorruptDecisionFile``)."""
    import pytest
    import ber_core.evidence as be

    missing = Path(tempfile.mkdtemp()) / "nope.json"
    assert load_exclusion_dois(missing) == set()
    assert load_exclusion_dois(None) == set()
    empty = Path(tempfile.mkdtemp()) / "empty.json"
    empty.write_text("  \n", encoding="utf-8")
    assert load_exclusion_dois(empty) == set()

    bad = Path(tempfile.mkdtemp()) / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    not_an_object = _write(["a", "list"])
    corrupt = getattr(be, "CorruptDecisionFile", None)
    if corrupt is None:  # loader predates fail-loud: corrupt reads as empty
        assert load_exclusion_dois(bad) == set()
        assert load_exclusion_dois(not_an_object) == set()
    else:
        with pytest.raises(corrupt):
            load_exclusion_dois(bad)
        with pytest.raises(corrupt):
            load_exclusion_dois(not_an_object)


def test_both_scripts_delegate_to_the_shared_loader() -> None:
    """Import each script and assert its loader returns the shared result.

    Without this, the two could drift apart again — which is exactly how the
    original bug arose.
    """
    import importlib.util

    bare = _write({"10.1/AB": "oral"})
    for name, path in (
        ("export", REPO_ROOT / "steps/export_dashboard.py"),
        ("meta", REPO_ROOT / "scripts/pool_meta_analysis.py"),
    ):
        spec = importlib.util.spec_from_file_location(f"_excl_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod._load_delivery_exclusions(bare) == {"10.1/ab"}, (
            f"{name} does not honor the bare form"
        )
