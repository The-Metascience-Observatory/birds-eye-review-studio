"""Classification-stage configuration: seeding, PATCH merge, preflight, prompts.

Regression coverage for the failure that motivated this surface: a review created
without ``article_types`` handed the classifier an empty vocabulary, it invented
free-text labels, the PDF download step matched zero rows, and the run died on a
generic "expected non-empty directory" error several stages later.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


# --- Isolate BEFORE importing anything that reads settings / builds the engine.
_TMP = Path(tempfile.mkdtemp(prefix="ber_clscfg_"))
# DATABASE_URL and REDIS_URL are pinned session-wide in tests/conftest.py: they
# are read once into the cached `settings`, so a per-module assignment here would
# only take effect if this module happened to be imported first.
os.environ["DATA_ROOT"] = str(_TMP / "_data")
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx  # noqa: E402

from server.app.db.session import init_models  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services.configuration import _merge_config  # noqa: E402
from server.app.services import workspace as ws  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# --- Pure unit tests (no app) ------------------------------------------------

def test_article_types_mirror_matches_pipeline_constant() -> None:
    """workspace.py deliberately duplicates the taxonomy (it must not import the
    pipeline package). This is the guard that keeps the copy honest."""
    from ber_core import constants

    assert list(ws.ARTICLE_TYPES) == list(constants.ARTICLE_TYPES)


def test_studio_pdf_types_default_mirrors_the_pipeline() -> None:
    """The Screen stage pre-ticks the default PDF types, so the studio keeps its
    own copy of PDF_ELIGIBLE_TYPES (it cannot import the pipeline package). If
    the copy drifts, the checkboxes would claim a default the downloader does not
    actually use — the exact silent mismatch this file exists to prevent."""
    import re

    from ber_core import constants

    src = (_REPO_ROOT / "studio" / "src" / "lib" / "constants.ts").read_text()
    m = re.search(r"export const DEFAULT_PDF_TYPES = \[(.*?)\]", src, re.DOTALL)
    assert m, "DEFAULT_PDF_TYPES not found in studio/src/lib/constants.ts"
    mirrored = re.findall(r'"([^"]+)"', m.group(1))

    assert mirrored == list(constants.PDF_ELIGIBLE_TYPES)


def test_eligible_types_are_subsets_of_the_taxonomy() -> None:
    from ber_core import constants

    known = set(constants.ARTICLE_TYPES) | set(constants.LEGACY_ARTICLE_TYPES)
    assert set(constants.PDF_ELIGIBLE_TYPES) <= known
    assert set(constants.PRISMA_ELIGIBLE_TYPES) <= known


def test_extraction_pass2_field_defaults_mirror_the_pipeline() -> None:
    """workspace.EXTRACTION_PASS2_FIELD_DEFAULTS duplicates
    extract_pass2.PASS2_FIELD_DEFAULTS (no pipeline import from workspace). This
    guard keeps the copy honest so the seeded config matches what the extractor
    actually gates on."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_p2mirror", str(_REPO_ROOT / "steps/extract_pass2.py"))
    p2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p2)
    assert ws.EXTRACTION_PASS2_FIELD_DEFAULTS == p2.PASS2_FIELD_DEFAULTS


def test_build_config_dict_seeds_article_types() -> None:
    cfg = ws.build_config_dict(
        topic_name="X", synonyms=[], description="", not_about=[],
        include_observational=True,
    )
    assert cfg["article_types"], "new reviews must ship a non-empty vocabulary"
    assert "Original Research" in cfg["article_types"]


def test_build_config_dict_seeds_basic_extraction_fields() -> None:
    """New reviews ship the basic-extraction defaults: standard blocks on, deep
    meta-analysis blocks off."""
    cfg = ws.build_config_dict(
        topic_name="X", synonyms=[], description="", not_about=[],
        include_observational=True,
    )
    f = cfg["extraction_pass2_fields"]
    assert f["adverse_events"] is True          # standard on
    assert f["all_outcomes"] is False           # deep off
    assert f["provenance_quotes"] is False


def test_normalize_config_fills_missing_pass2_fields() -> None:
    """A review predating the toggles gets the defaults; a partial dict is merged
    with the missing blocks (additive, no migration)."""
    out = ws.normalize_config({"topic": {"name": "X"}})
    assert out["extraction_pass2_fields"] == ws.EXTRACTION_PASS2_FIELD_DEFAULTS
    partial = ws.normalize_config({"extraction_pass2_fields": {"all_outcomes": True}})
    assert partial["extraction_pass2_fields"]["all_outcomes"] is True  # user's value kept
    assert partial["extraction_pass2_fields"]["adverse_events"] is True  # default filled


def test_normalize_config_fills_missing_article_types_without_mutating() -> None:
    original = {"topic": {"name": "X"}}
    out = ws.normalize_config(original)
    assert out["article_types"] == list(ws.ARTICLE_TYPES)
    assert "article_types" not in original, "input must not be mutated"


def test_normalize_config_preserves_a_users_narrowed_list() -> None:
    out = ws.normalize_config({"article_types": ["Clinical Trial"]})
    assert out["article_types"] == ["Clinical Trial"]


def test_merge_preserves_sibling_keys() -> None:
    """The whole point of PATCH: an editor that knows one key can't drop another."""
    base = {"search": {"pubmed": ["q1"]}, "article_types": ["A"], "topic": {"name": "X"}}
    out = _merge_config(base, {"article_types": ["B"]})
    assert out["search"] == {"pubmed": ["q1"]}
    assert out["article_types"] == ["B"]


def test_merge_is_one_level_deep_for_nested_dicts() -> None:
    base = {"topic": {"name": "X", "synonyms": ["a"], "not_about": ["z"]}}
    out = _merge_config(base, {"topic": {"not_about": ["new"]}})
    assert out["topic"]["name"] == "X"
    assert out["topic"]["synonyms"] == ["a"]
    assert out["topic"]["not_about"] == ["new"]


def test_merge_none_deletes() -> None:
    assert "gone" not in _merge_config({"gone": 1, "kept": 2}, {"gone": None})


def test_the_pregate_has_no_builtin_vocabulary() -> None:
    """THE REGRESSION, now structural. The gate used to ship a nasal
    ROUTE-OF-ADMINISTRATION term list (`PREGATE_BASE`) that defaulted ON, so
    every review inherited it: a vagus-nerve-stimulation review discarded 344 of
    368 papers (93%) before any LLM call — not because they were off-topic, but
    because VNS abstracts do not say "nostril". The fix is not a better default;
    it is removing the built-in vocabulary entirely. The gate is now fully
    general — it knows only the review's OWN terms.
    """
    from ber_core import pregate

    assert not hasattr(pregate, "PREGATE_BASE"), "no topic-specific base terms"
    assert "use_base_terms" not in pregate.pregate_settings({})
    # The gate is still ON by default — it is the TERMS that were topical, not
    # the gating itself.
    assert pregate.pregate_settings({})["enabled"] is True


def test_a_topic_with_no_terms_disables_the_gate_rather_than_guessing() -> None:
    """No synonyms and no extra terms means nothing to gate ON, and the honest
    answer is "no gate" — not an empty pattern (which matches everything)."""
    from ber_core.pregate import build_pregate_pattern

    assert build_pregate_pattern({"topic": {"synonyms": []}}) is None
    assert build_pregate_pattern({}) is None


def test_a_review_gates_on_its_own_terms() -> None:
    """Synonyms and extra_terms both feed the pattern; nothing else does."""
    from ber_core.pregate import build_pregate_pattern

    cfg = {
        "topic": {"synonyms": ["nattokinase"]},
        "classification": {"pregate": {"extra_terms": ["fibrinolytic"]}},
    }
    pattern = build_pregate_pattern(cfg)
    assert pattern.search("Nattokinase supplementation trial")
    assert pattern.search("a fibrinolytic enzyme study")
    assert not pattern.search("an intranasal spray study")


def test_route_terms_belong_in_a_config_as_extra_terms() -> None:
    """Route terms (e.g. "intranasal") are right for one review, not a shipped
    default every other review inherits, so a review that needs them adds them
    as `extra_terms` in its own config."""
    cfg = {"topic": {"name": "Antiviral sprays", "synonyms": ["antiviral"]},
           "classification": {"pregate": {"extra_terms": ["intranasal", "nasal"]}}}
    from ber_core.pregate import build_pregate_pattern

    pattern = build_pregate_pattern(cfg)
    assert pattern is not None and pattern.search("an intranasal spray study")
    assert not build_pregate_pattern({"topic": cfg["topic"]}).search("an intranasal spray study")


def test_audit_counts_partition_the_corpus(tmp_path: Path) -> None:
    from ber_core.pregate import audit_pregate, build_pregate_pattern

    (tmp_path / "a.md").write_text("nattokinase reduced blood pressure. " * 5)
    (tmp_path / "b.md").write_text("a study of unrelated polymer chemistry. " * 5)
    (tmp_path / "c.md").write_text("tiny")  # below the usable-abstract threshold
    papers = [(p.stem, p, "") for p in sorted(tmp_path.glob("*.md"))]

    a = audit_pregate(build_pregate_pattern({"topic": {"synonyms": ["nattokinase"]}}), papers)
    assert a["total"] == 3
    assert a["term_match"] == 1
    assert a["would_skip"] == 1
    assert a["no_abstract"] == 1
    assert a["would_send"] + a["would_skip"] + a["no_abstract"] == a["total"]


def test_missing_placeholders_detected() -> None:
    from server.app.services.pipeline_bridge import missing_placeholders

    assert missing_placeholders("classify_type.txt", "no placeholders") == [
        "{article_types_list}", "{abstract}",
    ]
    ok = "Types:\n{article_types_list}\nAbstract:\n{abstract}"
    assert missing_placeholders("classify_type.txt", ok) == []


# --- API-level tests ---------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _auth_and_review(client: httpx.AsyncClient) -> tuple[dict, str]:
    await client.post("/auth/register",
                      json={"email": "cls@example.com", "password": "hunter2pw"})
    r = await client.post("/auth/login",
                          json={"email": "cls@example.com", "password": "hunter2pw"})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/reviews", headers=auth, json={
        "title": "Cls Review", "topic_name": "Cls Topic",
        "synonyms": ["cls topic"], "description": "d",
        "not_about": ["x"], "include_observational": True,
    })
    assert r.status_code == 201, r.text
    return auth, r.json()["id"]


async def test_config_patch_and_preflight_flow() -> None:
    await init_models()
    async with _client() as client:
        auth, rid = await _auth_and_review(client)

        # Seeded on creation, and rendered into the on-disk YAML.
        r = await client.get(f"/reviews/{rid}/config", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["config_json"]["article_types"]

        # PATCH one key; the search block seeded alongside it must survive.
        await client.put(f"/reviews/{rid}/config", headers=auth, json={
            "config_json": {**r.json()["config_json"], "search": {"pubmed": ["q"]}},
        })
        r = await client.patch(f"/reviews/{rid}/config", headers=auth, json={
            "config_json": {"article_types": ["Clinical Trial", "Original Research"]},
        })
        assert r.status_code == 200, r.text
        cfg = r.json()["config_json"]
        assert cfg["article_types"] == ["Clinical Trial", "Original Research"]
        assert cfg["search"] == {"pubmed": ["q"]}, "PATCH must not clobber siblings"
        assert cfg["topic"]["name"] == "Cls Topic"

        # An empty vocabulary is the exact silent failure this surface exists to
        # prevent — it must be refused, not stored.
        r = await client.patch(f"/reviews/{rid}/config", headers=auth,
                               json={"config_json": {"article_types": []}})
        assert r.status_code == 422, r.text
        assert "article_types" in r.json()["detail"]

        # Renaming would re-slug the workspace and orphan existing artifacts.
        r = await client.patch(f"/reviews/{rid}/config", headers=auth,
                               json={"config_json": {"topic": {"name": "Renamed"}}})
        assert r.status_code == 422, r.text

        # A pre-gate with no terms at all is refused too.
        r = await client.patch(f"/reviews/{rid}/config", headers=auth, json={
            "config_json": {
                "topic": {"synonyms": []},
                "classification": {"pregate": {"use_base_terms": False}},
            },
        })
        assert r.status_code == 422, r.text

        # Preflight: a freshly seeded review has no blocking errors.
        r = await client.get(f"/reviews/{rid}/preflight", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["has_errors"] is False

        # Pre-gate preview works before any abstracts exist.
        r = await client.get(f"/reviews/{rid}/pregate-preview", headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 0


async def test_prompt_override_roundtrip() -> None:
    await init_models()
    async with _client() as client:
        auth, rid = await _auth_and_review(client)

        r = await client.get(f"/reviews/{rid}/prompts", headers=auth)
        assert r.status_code == 200, r.text
        prompts = {p["name"]: p for p in r.json()}
        assert "classify_type.txt" in prompts
        assert prompts["classify_type.txt"]["is_override"] is False
        assert prompts["classify_type.txt"]["text"], "default text should be served"

        # Dropping a placeholder yields a prompt that silently classifies nothing.
        r = await client.put(f"/reviews/{rid}/prompts/classify_type.txt",
                             headers=auth, json={"text": "no placeholders here"})
        assert r.status_code == 422, r.text
        assert "{abstract}" in r.json()["detail"]

        good = "Pick one:\n{article_types_list}\n\nAbstract:\n{abstract}\n"
        r = await client.put(f"/reviews/{rid}/prompts/classify_type.txt",
                             headers=auth, json={"text": good})
        assert r.status_code == 200, r.text
        assert r.json()["is_override"] is True

        # The override must land where BER_PROMPTS_DIR resolution will find it.
        override = (ws.workspace_dir(rid) / "prompts" /
                    "cls_topic" / "classify_type.txt")
        assert override.is_file()
        assert override.read_text() == good

        # Empty text reverts to the shipped default.
        r = await client.put(f"/reviews/{rid}/prompts/classify_type.txt",
                             headers=auth, json={"text": ""})
        assert r.status_code == 200, r.text
        assert r.json()["is_override"] is False
        assert not override.exists()

        r = await client.put(f"/reviews/{rid}/prompts/nope.txt",
                             headers=auth, json={"text": "x"})
        assert r.status_code == 404, r.text


def test_preflight_scopes_warnings_to_the_stage_that_can_act_on_them() -> None:
    """Every page used to show every warning. The screening page carried four,
    of which three were about pass-2 extraction keys — stages away, not yet
    actionable — burying the one that mattered (a pre-gate about to discard 93%
    of the corpus). A warning a user cannot act on here is noise, and noise is
    what makes the real ones invisible."""
    from server.app.services.pipeline_bridge import preflight

    import tempfile

    import yaml

    # Self-contained workspace. This used to read a hardcoded review UUID under
    # the GITIGNORED server/_data/, so it skipped on every machine but one — the
    # test looked green in CI while asserting nothing. `preflight` only needs a
    # config.yaml, and the cfg passed in overrides the file anyway (see below).
    ws = Path(tempfile.mkdtemp(prefix="ber_preflight_"))
    cfg = {
        "topic": {"name": "Scoping", "synonyms": [], "description": "d",
                  "not_about": []},
        "article_types": ["Clinical Trial"],
        "search": {"pubmed": ["q"]},
        # Blanked deliberately: the point under test is WHERE a warning
        # surfaces, not whether a particular review currently trips it.
        "outcome_domains": [],
        "intervention_categories": [],
    }
    (ws / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    everything = {i["code"] for i in preflight(ws, cfg)}
    classify = {i["code"] for i in preflight(ws, cfg, stage_slug="classify")}
    extract = {i["code"] for i in preflight(ws, cfg, stage_slug="extract")}

    assert classify < everything, "scoping must narrow, not repeat"
    # Synonyms feed the relevance prompt and the pre-gate: actionable HERE.
    assert "config_topic_synonyms_empty" in classify
    # Extraction taxonomies are not — and they surface where they bite, as
    # errors rather than warnings, because pass-2 is the expensive step.
    assert "config_outcome_domains_empty" not in classify
    assert "config_outcome_domains_empty" in extract
    assert any(
        i["level"] == "error"
        for i in preflight(ws, cfg, stage_slug="extract")
    )
