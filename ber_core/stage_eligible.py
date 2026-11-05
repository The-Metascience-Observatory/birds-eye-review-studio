"""Stage Pass-1-eligible papers into design-type buckets for Pass 2.

Reads the Pass-1 output (``data/trial_extractions.jsonl``), keeps records
flagged ``eligible_for_pass2``, routes each by its Pass-1 ``study_design``
(falling back to ``trial_type`` in ``data/trial_screening.csv``), and symlinks
each paper's folder in the paper store into a per-design bucket so
``extract_pass2.py`` can pick them up via ``discover_papers``.

    python steps/stage_eligible.py --config configs/config_my_topic.yaml
    python steps/stage_eligible.py --config <cfg> --execute     # actually create links

By default it does a dry run (prints counts only); pass ``--execute`` to create
the symlinks.

Buckets created under the project dir:
    eligible_rct/<stem>            -> papers/<Type>/<stem>
    eligible_observational/<stem>  -> papers/<Type>/<stem>
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from ber_core import paper_store
from ber_core.utils import (
    RED, RESET, YELLOW,
    load_config,
    load_exclusion_dois,
    load_inclusion_dois,
    norm_doi,
    project_paths,
    read_jsonl,
)

# Routing uses ber_core.scientific.design_label — the single definition of
# randomized vs not — for both the pass-1 ``study_design`` and the screening
# ``trial_type`` fallback. Quasi-experimental and single-arm designs are NOT
# randomized, so they go to the observational bucket (ROBINS-I), not the RCT
# bucket (RoB 2) they used to be staged into.


def _load_trial_types(screening_csv: Path) -> dict[str, str]:
    """Map DOI -> trial_type from the screening CSV (best-effort)."""
    if not screening_csv.exists():
        return {}
    out: dict[str, str] = {}
    with open(screening_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip()
            tt = (row.get("trial_type") or "").strip().lower()
            if doi:
                out[doi] = tt
    return out


def _component_ineligible(screening: dict, include_animals: bool,
                          include_observational: bool = False) -> str | None:
    """A CLEAR component verdict that contradicts ``eligible_for_pass2=true``.

    Defense-in-depth mirror of pass-1's ``_enforce_eligibility``: the model
    self-reports the gate, and older pass-1 files predate the Python-side
    enforcement — so re-check the components here before staging a paper into
    the expensive pass-2 bucket. Only explicit negatives count ("no"/False/a
    named non-human organism); "unclear"/null stay with the model's call.
    """
    def _v(key):
        v = screening.get(key)
        return v.get("verdict") if isinstance(v, dict) else v

    if _v("reports_original_data") is False:
        return "not_original_report"
    if _v("is_protocol_not_article") is True:
        return "protocol"
    ict = screening.get("is_clinical_trial")
    if isinstance(ict, dict):
        # An observational review accepts non-trial designs, so neither "not a
        # trial" nor "no control arm" disqualifies an observational paper. This
        # mirrors pass-1's `_enforce_eligibility`; if the two disagree, this gate
        # silently re-blocks exactly what pass 1 just allowed.
        observational_ok = (
            include_observational
            and (ict.get("study_design") or "").strip().lower() == "observational"
        )
        if not observational_ok:
            if ict.get("verdict") == "no":
                return "not_a_trial"
            if ict.get("has_control_arm") is False:
                return "no_control_arm"
    if not include_animals:
        org = _v("organism")
        if org in ("mouse", "other_animal", "other"):
            return f"organism_{org}"
    if _v("is_relevant") == "no":
        return "not_relevant"
    return None


# Non-randomized TRIAL labels (as opposed to observational designs). They are
# routed to the observational bucket when that extraction runs; when it does not,
# the RCT prompt (whose design_type vocabulary includes quasi-experimental) is
# their only extraction path, and the analysis layer still classifies them as
# non-randomized from the extracted design_type.
_NONRANDOMIZED_TRIAL = ("quasi", "non randomized trial", "nonrandomized trial",
                        "non randomised trial", "single arm")


def _route(study_design: str | None, trial_type: str | None,
           include_observational: bool = True) -> str:
    """Return 'rct' or 'observational' for a paper.

    The pass-1 design label decides; when it is missing or unresolved
    ('other', 'unclear') the screening trial_type is consulted. A paper with no
    resolvable design at all cleared the control-arm gate, so it is staged as a
    controlled trial and pass 2 records its actual design.
    """
    from ber_core.scientific import design_label, RANDOMIZED_CLASSES
    for label in (study_design, trial_type):
        cls = design_label(label)
        if cls in RANDOMIZED_CLASSES:
            return "rct"
        if cls == "observational":
            norm = " ".join(str(label or "").lower().replace("_", " ").replace("-", " ").split())
            if not include_observational and any(t in norm for t in _NONRANDOMIZED_TRIAL):
                return "rct"
            return "observational"
    return "rct"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True,
                        help="Topic config YAML (e.g. configs/config_my_topic.yaml)")
    parser.add_argument("--pass1", default=None,
                        help="Pass-1 JSONL (default: <project>/data/trial_extractions.jsonl)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually create symlinks (default: dry run)")
    parser.add_argument("--clean", action="store_true",
                        help="Remove existing bucket dirs before staging")
    args = parser.parse_args()

    cfg = load_config(args.config)
    paths = project_paths(cfg)
    project_dir = paths["project_dir"]
    data_dir = paths["data_dir"]

    pass1_path = Path(args.pass1) if args.pass1 else data_dir / "trial_extractions.jsonl"

    # Papers the user manually excluded in the studio's evidence table. The same
    # file is honored by the export and the meta-analysis (shared loader), so an
    # exclusion here propagates end to end. Pass-2 only ever sees what this step
    # stages, so skipping a DOI here removes it from the expensive extraction.
    user_excluded = load_exclusion_dois(data_dir / "user_exclusions.json")
    # Papers the user manually INCLUDED: they are staged for pass-2 even if the
    # full-text eligibility pass (pass 1) marked them ineligible or never ran for
    # them — the user's call overrides the automatic gate. Exclusion wins over
    # inclusion (an explicit exclude beats a stale include).
    user_included = load_inclusion_dois(data_dir / "user_inclusions.json") - user_excluded
    n_user_excluded = 0
    n_user_included = 0
    # Missing the pass-1 file is only fatal if there is also nothing to force-in:
    # a user can hand-include papers before the full-text screen has ever run.
    if not pass1_path.exists() and not user_included and not args.execute:
        print(f"{RED}Pass-1 file not found: {pass1_path}{RESET}", file=sys.stderr)
        sys.exit(1)

    trial_types = _load_trial_types(data_dir / "trial_screening.csv")

    buckets: dict[str, list[tuple[Path, str]]] = {"rct": [], "observational": []}
    missing: list[str] = []   # eligible but no paper folder on disk
    staged: set[str] = set()  # norm_doi of every paper already routed into a bucket
    n_records = n_eligible = n_mismatch = 0
    include_animals = bool(cfg.get("include_animal_studies", False))
    # Match the canonical runner: reviews include observational studies unless
    # they explicitly opt out. Otherwise direct staging silently drops them.
    include_observational = bool(cfg.get("include_observational", True))

    # Paper folders keep the DOI's ORIGINAL case (doi_to_filename deliberately
    # does not lowercase — see the contract note in utils.py), but force-included
    # DOIs arrive already normalized to lowercase from load_inclusion_dois. The
    # store index is keyed by normalized DOI, so the lookup is case-insensitive
    # and a manual include can't silently fail to stage. It is also type-folder
    # agnostic: a paper is found wherever the screen most recently filed it.
    paper_index = paper_store.build_index(project_dir, root=paper_store.papers_root(project_dir, config=cfg))

    def _stage(doi: str, bucket: str) -> None:
        """Route one paper into a bucket if its full text exists (dedup-safe)."""
        key = norm_doi(doi.split("#")[0])
        if key in staged:
            return
        paper_dir = paper_index.get(key)
        if paper_dir is None:
            missing.append(doi)
            return
        buckets[bucket].append((paper_dir, doi))
        staged.add(key)

    # A malformed pass-1 line ABORTS staging (as it always has): skipping it
    # would silently withhold an eligible paper from pass 2.
    for rec in read_jsonl(pass1_path, on_error="raise"):
        n_records += 1
        screening = rec.get("screening", {}) or {}
        if not screening.get("eligible_for_pass2"):
            continue

        doi = rec.get("paper_id", "")
        if norm_doi(doi.split("#")[0]) in user_excluded:
            n_user_excluded += 1
            continue
        mismatch = _component_ineligible(screening, include_animals,
                                         include_observational)
        if mismatch:
            # A user force-include still stages it (later pass).
            n_mismatch += 1
            print(f"  {YELLOW}WARN: {doi}: eligible_for_pass2=true "
                  f"contradicted by {mismatch}; not staged{RESET}",
                  file=sys.stderr)
            continue
        n_eligible += 1
        ict = screening.get("is_clinical_trial", {}) or {}
        study_design = ict.get("study_design") if isinstance(ict, dict) else None
        _stage(doi, _route(study_design, trial_types.get(doi), include_observational))

    # Force-include pass: stage any manually-included DOI not already routed by
    # pass-1. Prefer the screening trial_type for routing (from trial_screening's
    # DOI keys); a force-included DOI that never reached screening still stages,
    # routed to rct by default, as long as its converted text exists. `_stage`
    # dedups against `staged`, so re-including an already-eligible paper is a no-op.
    trial_type_by_norm = {
        norm_doi(d.split("#")[0]): tt for d, tt in trial_types.items()
    }
    for key in user_included:
        before = len(staged)
        _stage(key, _route(None, trial_type_by_norm.get(key), include_observational))
        if len(staged) > before:
            n_user_included += 1

    print(f"Pass-1 records: {n_records}  eligible_for_pass2: {n_eligible}", file=sys.stderr)
    if n_mismatch:
        print(f"  {n_mismatch} paper(s) not staged: component verdicts "
              f"contradict eligible_for_pass2", file=sys.stderr)
    if n_user_excluded:
        print(f"  {n_user_excluded} eligible paper(s) skipped: manually excluded", file=sys.stderr)
    if n_user_included:
        print(f"  {n_user_included} paper(s) force-staged: manually included", file=sys.stderr)
    for name in ("rct", "observational"):
        print(f"  {name:13s}: {len(buckets[name])} papers", file=sys.stderr)
    if missing:
        print(f"  {YELLOW}WARN: {len(missing)} eligible papers have no "
              f"converted/<stem>/ folder (skipped){RESET}", file=sys.stderr)

    if not args.execute:
        print(f"\n{YELLOW}Dry run — pass --execute to create symlinks.{RESET}",
              file=sys.stderr)
        return

    for name in ("rct", "observational"):
        bucket_dir = project_dir / f"eligible_{name}"
        if args.clean and bucket_dir.exists():
            # Buckets are append-only in normal operation (the studio passes only
            # --execute), so a paper that later loses eligibility — or is manually
            # excluded — would otherwise keep its stale symlink and keep feeding
            # pass 2. --clean drops every entry so the bucket is rebuilt to match
            # the CURRENT verdicts. Only symlinks are removed: a real directory
            # here is a user's own file, not something this script created, and
            # deleting it recursively would be destructive beyond our remit.
            for child in bucket_dir.iterdir():
                if child.is_symlink():
                    child.unlink()
                else:
                    print(f"  {YELLOW}WARN: --clean left {child.name} in "
                          f"eligible_{name}/ (not a symlink){RESET}",
                          file=sys.stderr)
        bucket_dir.mkdir(parents=True, exist_ok=True)
        created = 0
        for paper_dir, _doi in buckets[name]:
            link = bucket_dir / paper_dir.name
            if link.exists() or link.is_symlink():
                continue
            # Relative target keeps the link valid if the project dir moves.
            # Computed from the paper's actual folder rather than hardcoded:
            # its depth varies with the type bucket it was filed into.
            rel = paper_store.bucket_link_target(paper_dir, bucket_dir)
            link.symlink_to(rel, target_is_directory=True)
            created += 1
        print(f"  eligible_{name}/: {created} new symlinks "
              f"({len(buckets[name])} total)", file=sys.stderr)


if __name__ == "__main__":
    main()
