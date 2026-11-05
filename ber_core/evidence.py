"""Shared identity, eligibility, overrides, and publication replacement rules."""
from pathlib import Path
import json

def norm_doi(doi: str) -> str:
    """Canonical, case-insensitive DOI for use as a JOIN key across files."""
    return (doi or "").strip().lower().replace("--", "/")


def norm_paper_id(paper_id: str) -> str:
    """Normalize a record's ``paper_id`` to a join-ready DOI.

    Paper ids reach us in three shapes: a bare DOI, a ``--``-escaped filename
    stem (see :func:`doi_to_filename`), and either of those with an ``#arm``
    suffix appended by the export. Strip the suffix, then normalize.
    """
    pid = (paper_id or "").split("#")[0]
    return norm_doi(pid)


def load_duplicate_dois(path) -> dict:
    """duplicate/preprint DOI -> canonical published DOI (both lowercased).

    Lets the same study published twice (e.g. a medRxiv preprint and its
    peer-reviewed version) count once. Keys starting with ``_`` are comments;
    a missing or invalid file yields an empty map.

    ONE loader for every consumer — the export, the meta-analysis, and the
    PRISMA funnel must agree on which records are duplicates, or the dashboard's
    trial count and the funnel's "Included in synthesis" silently disagree.
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {norm_paper_id(str(k)): norm_paper_id(str(v))
            for k, v in obj.items() if not str(k).startswith("_")}


def is_unpublishable_record(record: dict) -> bool:
    """Should this pass-2 record be dropped before it is counted or displayed?

    Mirrors — and is the single definition behind — the export's ``_is_failed``:
    a failed extraction status, or a status-ok record with no ``study_design``
    (the backbone the dashboard reads: arms, design_type, countries, blinding).
    A record without it has nothing displayable, e.g. a paper the extractor
    rejected as off-topic.

    Both the export and the PRISMA funnel apply this, so "Included in synthesis"
    cannot drift from the number of trials the dashboard actually shows.
    """
    from ber_core.constants import FAILED_STATUS

    if record.get("_status") in FAILED_STATUS:
        return True
    if not record.get("study_design"):
        return True
    return False


def drop_duplicate_records(records: list, dup_map: dict | None = None) -> list:
    """Records minus known duplicate publications, collapsed onto their canonical.

    A record is dropped only when ``dup_map`` maps it to a canonical DOI that is
    ALSO PRESENT in ``records`` — otherwise the "duplicate" is the only copy of
    that study and dropping it would lose the paper entirely. Same guard the
    export's ``_union_records`` applies.
    """
    dup_map = {norm_paper_id(k): norm_paper_id(v) for k, v in (dup_map or {}).items()}
    if not dup_map:
        return list(records)

    def _norm(pid) -> str:
        pid = pid or ""
        doi = norm_paper_id(pid)
        return doi.strip().lower()

    present = {_norm(r.get("paper_id")) for r in records
               if not is_unpublishable_record(r)}
    canon_present = {c for c in dup_map.values() if c in present}
    return [r for r in records
            if not (_norm(r.get("paper_id")) in dup_map
                    and dup_map[_norm(r.get("paper_id"))] in canon_present)]


class CorruptDecisionFile(ValueError):
    """A user_exclusions.json / user_inclusions.json that cannot be read."""


def _load_decision_file(path, wrapper: str) -> set:
    """Shared loader for the hand-maintained include/exclude files.

    FAIL LOUD, not open. These files record a PERSON's decisions. Reading a
    corrupt one as "no decisions" — what both loaders used to do — silently
    re-admitted every manually excluded paper (and dropped every force-include)
    into staging, the export and the meta-analysis, with nothing in any log. A
    missing or empty file is still simply "no decisions"; anything else that
    does not parse as a JSON object raises :class:`CorruptDecisionFile` naming
    the file, so the step stops and says which file to repair.
    """
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptDecisionFile(f"cannot read {p}: {exc}") from exc
    if not text.strip():
        return set()
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise CorruptDecisionFile(
            f"{p} is not valid JSON ({exc}). It holds manual include/exclude "
            f"decisions, so it is not ignored: repair it (or restore a backup) "
            f"and re-run.") from exc
    if not isinstance(obj, dict):
        raise CorruptDecisionFile(
            f"{p} must be a JSON object of {{doi: reason}}, not {type(obj).__name__}")
    # Wrapper form wins when present; otherwise the whole object is the mapping.
    inner = obj.get(wrapper) if isinstance(obj.get(wrapper), dict) else obj
    return {
        norm_paper_id(k) for k in (inner or {}) if not str(k).startswith("_")
    }


def load_exclusion_dois(path) -> set:
    """Load a hand-maintained exclusion file as a set of ``norm_doi`` keys.

    Accepts BOTH on-disk shapes, because two scripts historically disagreed:
    ``export_dashboard.py`` required a ``{"exclusions": {...}}`` wrapper while
    ``pool_meta_analysis.py`` also accepted a bare ``{doi: reason}`` mapping. A file
    written in the bare form was therefore honored by the meta-analysis and
    SILENTLY IGNORED by the export, so the published dashboard and the pooled
    estimates disagreed about which papers were in the review, with nothing
    flagging it. One loader, both shapes, no divergence.

    Keys starting with ``_`` are treated as comments. A missing (or empty) file
    yields an empty set — an exclusions file is always optional. A CORRUPT one
    raises :class:`CorruptDecisionFile` (see :func:`_load_decision_file`).
    """
    return _load_decision_file(path, "exclusions")


def load_inclusion_dois(path) -> set:
    """Load a hand-maintained FORCE-INCLUDE file as a set of ``norm_doi`` keys.

    The positive-inclusion counterpart to ``load_exclusion_dois``: when the user
    manually includes a paper in the studio's evidence table, its DOI is written
    to ``user_inclusions.json`` (bare ``{doi: reason}``, same shape). A DOI in this
    file is staged for extraction EVEN IF the cheap full-text eligibility pass
    (pass 1) marked it ineligible or never ran for it — the user's call overrides
    the automatic gate. ``stage_eligible`` reads this to force those papers into a
    design bucket.

    Keys starting with ``_`` are treated as comments. A missing (or empty) file
    yields an empty set; a corrupt one raises :class:`CorruptDecisionFile`.
    Accepts the same bare-or-``{"inclusions": {...}}``-wrapper shapes for
    symmetry with the loader above.
    """
    return _load_decision_file(path, "inclusions")
