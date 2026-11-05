"""`studio/src/lib/types.ts` is a HAND-MAINTAINED mirror of `schemas.py`.

Its own first line says so. Nothing enforced the mirror, so a field added on one
side could silently go missing on the other — the client would read `undefined`
and render nothing, with no error anywhere. This matters most for the stage
contract, which is the app's most complex payload and the one the wizard's
gating depends on.

Precedent: `test_classification_config.py` already asserts the studio's
ARTICLE_TYPES copy matches the pipeline constant. This is the same idea for the
API contract.

The parser here is deliberately dumb (a brace-matching regex, not a TS parser):
it only needs to read the flat interfaces in this one file, and a real parser
would be a dependency for no gain.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TYPES_TS = REPO_ROOT / "studio" / "src" / "lib" / "types.ts"

from server.app import schemas  # noqa: E402


def _ts_interface_fields(source: str, name: str) -> set[str]:
    """Field names of `export interface <name> { ... }`, ignoring comments."""
    m = re.search(rf"export interface {re.escape(name)}\s*{{", source)
    if not m:
        raise AssertionError(f"interface {name} not found in types.ts")

    # Walk from the opening brace to its match so nested object literals are safe.
    depth, i = 0, m.end() - 1
    for i in range(m.end() - 1, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                break
    body = source[m.end() : i]

    body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
    body = re.sub(r"//.*", "", body)

    fields: set[str] = set()
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        # Only take names at the interface's own level, not inside nested objects.
        if depth == 0:
            fm = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\??\s*:", stripped)
            if fm:
                fields.add(fm.group(1))
        depth += stripped.count("{") - stripped.count("}")
    return fields


@pytest.fixture(scope="module")
def ts_source() -> str:
    assert TYPES_TS.exists(), f"missing {TYPES_TS}"
    return TYPES_TS.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "model_name, ts_name",
    [
        ("StageOut", "StageOut"),
        ("StagesOut", "StagesOut"),
        ("PhaseOut", "PhaseOut"),
        ("ReviewOut", "ReviewOut"),
        # The CREATE side was unguarded, and drifted: `generate_meta_analysis`
        # existed server-side but not in types.ts, so the new-review form could
        # not enable pooling at all — users had to create the review and then
        # PATCH it from the meta stage.
        ("ReviewCreate", "ReviewCreate"),
        ("ReviewStateOut", "ReviewStateOut"),
        ("JobOut", "JobOut"),
        ("SearchDraftOut", "SearchDraftOut"),
        ("UserOut", "UserOut"),
        ("TokenOut", "TokenOut"),
        # Registration returns a token (signup logs you in) — guard it so the
        # access_token can't be dropped from either side silently.
        ("RegisterOut", "RegisterOut"),
        # The request side too: invite_code must not drift out of the client, or
        # a gated server would reject every signup with no clue why.
        ("RegisterIn", "RegisterIn"),
        ("PublishPreflightOut", "PublishPreflightOut"),
        ("PublishRequestOut", "PublishRequestOut"),
        ("PublishedDashboardOut", "PublishedDashboardOut"),
        ("MetaAnalysisOut", "MetaAnalysisOut"),
        ("ClassifySummaryOut", "ClassifySummaryOut"),
        ("PreflightOut", "PreflightOut"),
        ("PregatePreviewOut", "PregatePreviewOut"),
        ("PromptOut", "PromptOut"),
        ("StepStateOut", "StepStateOut"),
        ("AgentMessageOut", "AgentMessageOut"),
        # Account settings were previously unguarded by this test, so a field
        # could be added server-side and silently missed in types.ts.
        ("UserSettingsOut", "UserSettingsOut"),
        ("UserSettingsIn", "UserSettingsIn"),
        ("LlmSessionOut", "LlmSessionOut"),
        ("LlmSessionClaimIn", "LlmSessionClaimIn"),
        # The synthesized-review document (report stage): both directions of
        # its contract.
        ("SynthesizeIn", "SynthesizeIn"),
        ("ReviewDocumentOut", "ReviewDocumentOut"),
        # Access codes. The admin page is the only way to issue and revoke them
        # from the UI, so a dropped field here means an admin silently cannot
        # see who a code belongs to or whether it is still live.
        ("InviteOut", "InviteOut"),
        ("InviteCreateIn", "InviteCreateIn"),
        ("InviteCreatedOut", "InviteCreatedOut"),
        # The stage contract's nested blocks and the remaining small payloads
        # the studio renders field by field.
        ("StageGateOut", "StageGateOut"),
        ("StageActivityOut", "StageActivityOut"),
        ("PublishDiffOut", "PublishDiffOut"),
        ("PreflightIssue", "PreflightIssue"),
        ("TaxonomyDraftOut", "TaxonomyDraftOut"),
        ("ConfigOut", "ReviewConfigOut"),
    ],
)
def test_typescript_mirrors_pydantic(model_name: str, ts_name: str, ts_source: str) -> None:
    model = getattr(schemas, model_name)
    py_fields = set(model.model_fields)
    ts_fields = _ts_interface_fields(ts_source, ts_name)

    missing = py_fields - ts_fields
    extra = ts_fields - py_fields
    assert not missing, (
        f"{ts_name} in types.ts is missing field(s) the server sends: "
        f"{sorted(missing)} — the client will read undefined."
    )
    assert not extra, (
        f"{ts_name} in types.ts declares field(s) the server never sends: "
        f"{sorted(extra)} — the client will read undefined."
    )


# --- stage slug -> body component -------------------------------------------

STAGE_PAGE_TSX = (
    REPO_ROOT / "studio" / "src" / "app" / "reviews" / "[id]"
    / "stages" / "[slug]" / "page.tsx"
)


def test_every_stage_with_a_bespoke_body_is_reachable_by_its_slug() -> None:
    """`BODIES` in the stage page maps SLUG -> component, and an unknown slug
    falls back to GenericStage.

    That fallback renders "This stage has no settings" — it does not error. So a
    slug rename that misses this map turns a rich page into a blank one with no
    warning anywhere. It happened: renaming `export` -> `view-dashboard` left the
    key as `export:`, and the whole dashboard silently vanished behind the
    generic body.

    Guard: every slug the map mentions must be a real stage slug (or a
    documented alias for a renamed one).
    """
    src = STAGE_PAGE_TSX.read_text(encoding="utf-8")
    body_map = re.search(r"const BODIES[^{]*\{(.*?)\n\};", src, re.S)
    assert body_map, "could not find the BODIES map in the stage page"

    keyed = set(re.findall(r'^\s*"?([a-z][a-z0-9-]*)"?\s*:', body_map.group(1),
                           re.M))
    # Aliases assigned after the literal, for slugs that were renamed.
    keyed |= set(re.findall(r'^BODIES\.([a-z][a-z0-9-]*)\s*=', src, re.M))

    from app.services.stages import STAGES

    real = {s.slug for s in STAGES}
    unknown = {k for k in keyed if k not in real}
    # `export` is retained deliberately so old bookmarks still render.
    unknown -= {"export"}
    assert not unknown, (
        f"BODIES maps slug(s) that no stage declares: {sorted(unknown)} — "
        "those pages fall back to GenericStage and render blank."
    )

    # The dashboard stage in particular must have a bespoke body.
    assert "view-dashboard" in keyed, (
        "the dashboard stage has no BODIES entry — it would render as "
        "'This stage has no settings' instead of the dashboard."
    )
