"""Pydantic v2 request/response models mirroring the API contract."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

# NOTE: email is typed as plain ``str`` (not EmailStr) to keep the optional
# email-validator dependency out of the lean server venv. A shape check lives in
# ``routers/auth.py`` instead; the access code, not the address, is what actually
# admits someone, so the address only has to be a plausible identifier.
#
# The password bounds below are the *structural* ones. The minimum LENGTH policy
# is enforced in the router rather than here, because it is settings-driven
# (``settings.effective_min_password_len``) and a Field(min_length=...) is frozen
# at class-definition time — it could not then be relaxed for the test suite or
# raised by PUBLIC_MODE.

# argon2 hashes whatever it is given, so an unbounded password is a cheap way to
# burn server CPU on an unauthenticated endpoint. 1024 is far above any real
# passphrase and far below anything that costs measurable time.
_MAX_PASSWORD_LEN = 1024

# Size bounds on free-text input. Not a style rule: unbounded strings and lists
# are a cheap way to make the server write huge files, build huge prompts, or
# hit an OS limit (a 300-character topic name became a 300-character directory
# name and failed with ENAMETOOLONG as an opaque 500).
_SHORT = 255          # identifiers, slugs, labels
_TOPIC = 200          # becomes the workspace directory name via slugify
_TEXT = 8000          # descriptions, questions, notes
_SECRET = 4096        # pasted API keys / tokens
_PATH = 4096          # filesystem paths (PATH_MAX)
_LIST = 500           # items in a user-supplied vocabulary list
_DOI_BATCH = 5000     # DOIs in one include/exclude call

ShortStr = Annotated[str, Field(max_length=_SHORT)]


# --- Auth ------------------------------------------------------------------
class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=_MAX_PASSWORD_LEN)
    # Shared access code, required only when settings.SIGNUP_CODE is set.
    # Optional with a default so existing callers (and the test suite) that send
    # only email+password keep working unchanged. NOTE: this model deliberately
    # has no extra="forbid" — see the class docstring convention on *In models.
    invite_code: str | None = None


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=_MAX_PASSWORD_LEN)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    role: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterOut(UserOut):
    """Registration response: the new user PLUS a ready-to-use access token.

    Signing up logs you in — the client shouldn't have to make a second
    /auth/login round-trip (which can fail on its own, stranding someone who
    now has an account but no session). Extends UserOut, so the previous
    id/email/role fields are unchanged.
    """

    access_token: str
    token_type: str = "bearer"


# Known LLM providers. Kept here so the settings endpoint and any provider-aware
# code validate against one list.
#
#   claude_cli    the HOST's shared `claude` login (one account for everyone;
#                 gated by the single-occupancy lease)
#   codex_cli     the HOST's Codex login (Luna; shares the same lease)
#   claude_token  the user's OWN subscription, via `claude setup-token`
#   anthropic_key the user's OWN Anthropic Console API key
#   openrouter    the user's OWN OpenRouter key
#
# The last three are "bring your own": billed to the user, exempt from the lease.
# See services/llm_prefs.py for how each is injected.
LLM_PROVIDERS = ("claude_cli", "codex_cli", "claude_token", "anthropic_key", "openrouter")


class UserSettingsOut(BaseModel):
    """The user's account settings. NEVER returns a stored secret — only whether
    one exists — so a credential never leaves the server after it is saved."""

    llm_provider: str
    has_openrouter_key: bool
    # Per-user Claude credentials (services/llm_prefs.py). Booleans only, for the
    # same reason as has_openrouter_key.
    has_claude_token: bool = False
    has_anthropic_key: bool = False
    # The account each credential authenticated as when it was verified, so the
    # user can see WHICH of their accounts will be billed rather than trusting an
    # opaque "saved".
    claude_token_account: str | None = None
    anthropic_key_account: str | None = None
    # When the subscription token stops working. `claude setup-token` mints a
    # fixed 12-month token, so this is known at save time and can be warned about
    # before anything breaks. None for API keys, which have no scheduled expiry.
    claude_token_expires_at: datetime | None = None
    # Absolute, normalized PDF download directory; None = per-review default.
    pdf_dir: str | None = None
    # Non-fatal advice about the saved path (e.g. it's on a Windows drive).
    pdf_dir_warning: str | None = None


class UserSettingsIn(BaseModel):
    """Partial update. ``llm_provider`` None = leave unchanged. Every secret field
    and ``pdf_dir``: None = leave unchanged, "" = clear, non-empty = set it.

    A non-empty ``claude_token``/``anthropic_key`` is VERIFIED against the CLI
    before it is stored (see routers/auth.py), so a bad paste fails here rather
    than three steps into a pipeline run."""

    model_config = ConfigDict(extra="forbid")

    llm_provider: str | None = Field(default=None, max_length=64)
    openrouter_key: str | None = Field(default=None, max_length=_SECRET)
    # Output of `claude setup-token`, run by the user on their own machine.
    claude_token: str | None = Field(default=None, max_length=_SECRET)
    # An Anthropic Console API key (sk-ant-…).
    anthropic_key: str | None = Field(default=None, max_length=_SECRET)
    pdf_dir: str | None = Field(default=None, max_length=_PATH)

    @model_validator(mode="after")
    def _check_provider(self):
        if self.llm_provider is not None and self.llm_provider not in LLM_PROVIDERS:
            raise ValueError(
                f"llm_provider must be one of {LLM_PROVIDERS}, got {self.llm_provider!r}"
            )
        return self


class LlmSessionOut(BaseModel):
    """Who currently owns the host's shared ``claude`` CLI login.

    The CLI authenticates from the server user's ``$HOME``, so LLM work runs as
    ONE Claude account for the whole host. This reports who holds it, so nobody
    unknowingly spends someone else's subscription.
    """

    # Lease state
    held: bool
    held_by_me: bool
    holder_email: str | None = None
    # The Claude account recorded when the lease was claimed.
    claude_account: str | None = None
    acquired_at: datetime | None = None
    # True when the lease is stale (idle past the TTL) and anyone may take over.
    expired: bool = False
    ttl_minutes: int

    # Host CLI state (from `claude auth status`)
    cli_installed: bool
    cli_logged_in: bool
    cli_account: str | None = None
    cli_detail: str = ""

    # The user's effective provider: "openrouter" users bypass the CLI entirely
    # and are never gated, so the UI can hide all of this for them.
    provider: str = "codex_cli"
    # Shell command to switch the host CLI to another account (login is
    # interactive and cannot be driven from the web UI).
    login_hint: str = ""


class LlmSessionClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Take over a lease whose holder has gone idle past the TTL.
    force: bool = False


# --- Reviews ---------------------------------------------------------------
class ReviewCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    # Slugified into the workspace DIRECTORY name, hence the tighter bound.
    topic_name: str = Field(min_length=1, max_length=_TOPIC)
    # Optional plain-language research question ("Does tVNS improve sleep in
    # chronic insomnia?"). When set it leads the search-drafting prompt; when
    # absent the description is used, so existing reviews are unaffected.
    question: str = Field(default="", max_length=_TEXT)
    synonyms: list[ShortStr] = Field(default_factory=list, max_length=_LIST)
    description: str = Field(default="", max_length=_TEXT)
    not_about: list[ShortStr] = Field(default_factory=list, max_length=_LIST)
    include_observational: bool = True
    # Pooling is a property of the review, so it lives in config rather than
    # being passed per-run — see pipeline_bridge.canonical_steps_for.
    generate_meta_analysis: bool = False


class ReviewPatch(BaseModel):
    """User-editable review fields.

    `status` and `visibility` are deliberately ABSENT: they are server-owned and
    written only by the publish path. Exposing them here let any owner
    `PATCH {"status": "published"}` straight past every permission check.
    `extra="forbid"` makes that attempt a loud 422 rather than a silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    # A concrete provider overrides the owner's account setting for this review;
    # "inherit" follows it. Validated: an unknown string used to be stored and
    # then handed to the pipeline as BER_LLM_PROVIDER.
    llm_default_provider: str | None = None

    @model_validator(mode="after")
    def _check_provider(self):
        allowed = (*LLM_PROVIDERS, "inherit")
        if self.llm_default_provider is not None and self.llm_default_provider not in allowed:
            raise ValueError(
                f"llm_default_provider must be one of {allowed}, "
                f"got {self.llm_default_provider!r}")
        return self


class ReviewOut(BaseModel):
    """A review as the client sees it. Deliberately NO filesystem path: the
    workspace location is the server's business (and on a hosted instance a
    map of its disk)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    owner_id: str
    slug: str
    topic_name: str
    title: str
    status: str
    llm_default_provider: str
    visibility: str
    created_at: datetime
    updated_at: datetime


# --- Pipeline state --------------------------------------------------------
class StepStateOut(BaseModel):
    number: int
    name: str
    script: str
    status: str
    detail: str
    count: int | None = None
    last_modified: str | None = None
    # Canonical step numbers behind this display row (run endpoint `only` units).
    canonical_nums: list[int] = Field(default_factory=list)


class ReviewStateOut(BaseModel):
    steps: list[StepStateOut]
    slug: str
    # Resume hint (canonical execution order). resume_from_num is the run
    # `from_num` to pass; the display number/label describe it for the UI.
    all_done: bool = False
    any_done: bool = False
    resume_from_num: int | None = None
    resume_display_number: int | None = None
    resume_label: str | None = None


class StageGateOut(BaseModel):
    """The APPROVAL axis. Null when a stage needs no sign-off.

    Separate from `StageOut.status` (the ARTIFACT axis) on purpose: a stage can
    be done on disk and still blocked on a human, and one enum could not say so.
    """

    state: str  # open | blocked | approved | stale
    approved_at: str | None = None
    approved_by: str | None = None
    approved_config_version: int | None = None
    # Slug of the first unapproved upstream stage, when state == "blocked".
    blocked_by: str | None = None
    # Which config keys changed since approval. Empty when unknowable (an
    # approval predating per-key digests) rather than guessed.
    stale_keys: list[str] = Field(default_factory=list)


class StageActivityOut(BaseModel):
    """The JOB axis: is something touching this stage right now?"""

    running_job_id: str | None = None
    last_job_state: str | None = None


class StageOut(BaseModel):
    """One resolved wizard stage (see services/stages.py).

    ``step_ids`` are canonical ``Step.id`` values that actually exist for THIS
    review — never ``Step.num``, which is gappy and shared between steps.
    """

    slug: str
    title: str
    # One of the six display phases (services/stages.py PHASES). Grouping only —
    # gating is stage-level and nothing in the gate path reads this.
    phase: str = "define"
    kind: str
    description: str = ""
    order: int
    step_ids: list[str] = Field(default_factory=list)
    requires_approval: bool = True
    config_keys: list[str] = Field(default_factory=list)
    # Start on arrival without a click: pure fetch, no LLM spend. Opt-in per
    # stage, never inferred.
    autorun: bool = False
    # Not shown in the walkthrough (no page/count/nav); still runs + gates.
    hidden: bool = False
    # True when every declared step vanished for this review (e.g. the
    # observational pass on an RCT-only review): no work to do, not blocked.
    skipped: bool = False
    status: str = "pending"  # done | partial | pending | n/a (ARTIFACT axis only)
    details: list[str] = Field(default_factory=list)
    # The other two axes. See StageGateOut for why they are not folded into
    # `status`.
    gate: StageGateOut | None = None
    activity: StageActivityOut | None = None


class AgentMessageOut(BaseModel):
    """One transcript turn. System rows carry stage transitions and blocker
    dismissals, so the client can reconstruct the journey from messages alone."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    stage: str | None = None
    role: str  # user | assistant | system
    content: str
    # Assistant rows: {cost_usd, model, provider, session_id, kind?, job_id?,
    # blockers?}.
    meta: dict | None = None
    created_at: datetime


class AgentAskIn(BaseModel):
    stage: str = Field(max_length=64)
    message: str = Field(min_length=1, max_length=8000)


class AgentReviewIn(BaseModel):
    """Fired by the rail when a stage's run finishes. `job_id` is the
    idempotency key: one run gets at most one review."""

    stage: str = Field(max_length=64)
    job_id: str = Field(max_length=64)


class AgentDismissIn(BaseModel):
    stage: str = Field(max_length=64)
    code: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=2000)


class EvidenceExcludeIn(BaseModel):
    """Manually exclude/include papers from downstream processing."""

    dois: list[ShortStr] = Field(min_length=1, max_length=_DOI_BATCH)
    reason: str = Field(default="", max_length=2000)


class StageApproveIn(BaseModel):
    """Optional free-text note recorded with an approval."""

    notes: str | None = Field(default=None, max_length=_TEXT)


class PhaseOut(BaseModel):
    """A display phase: its slug and human label. Order is the list order."""

    slug: str
    label: str


class StagesOut(BaseModel):
    stages: list[StageOut]
    # Slug the wizard should land on: the first not-done stage in execution order.
    current_stage: str | None = None
    # The six phases in order, so the client renders the stepper without
    # hardcoding the list (same anti-drift rule as the stage list itself).
    phases: list[PhaseOut] = Field(default_factory=list)


class ClassifySummaryOut(BaseModel):
    """Categorical counts from the classify stage, for the studio's bar chart."""

    article_types: dict[str, int] = Field(default_factory=dict)
    relevance: dict[str, int] = Field(default_factory=dict)
    studies_treatment: dict[str, int] = Field(default_factory=dict)
    total: int = 0


class PreflightIssue(BaseModel):
    """One config/data problem found before a run. `error` blocks; `warning` advises."""

    level: str
    code: str
    message: str


class PreflightOut(BaseModel):
    issues: list[PreflightIssue] = Field(default_factory=list)
    has_errors: bool = False


class PregatePreviewOut(BaseModel):
    """How the keyword pre-gate would partition the corpus, before any LLM call."""

    total: int = 0
    would_send: int = 0
    term_match: int = 0
    nonlatin_bypass: int = 0
    would_skip: int = 0
    no_abstract: int = 0
    enabled: bool = True
    detail: str | None = None


class PromptOut(BaseModel):
    """One editable classification prompt template."""

    name: str
    label: str
    text: str
    is_override: bool          # True when this review has its own edited copy
    required_placeholders: list[str] = Field(default_factory=list)


class PromptIn(BaseModel):
    # None/empty reverts to the shipped default (deletes the override file).
    text: str | None = Field(default=None, max_length=200_000)


# --- Search-query drafting -------------------------------------------------
class SearchDraftIn(BaseModel):
    # Model alias for the drafting call. Default good-quality.
    model: str = Field(default="sonnet", max_length=64)


class SearchDraftOut(BaseModel):
    # Per-source query lists (pubmed, openalex_title, …); only non-empty sources.
    queries: dict[str, list[str]]
    provider: str
    meta: dict


class TaxonomyDraftOut(BaseModel):
    # The two AI-drafted extraction vocabularies + which provider produced them.
    intervention_categories: list[str]
    outcome_domains: list[str]
    provider: str


# --- Config ----------------------------------------------------------------
class ConfigOut(BaseModel):
    config_json: dict


class ConfigIn(BaseModel):
    config_json: dict


# --- Jobs (engine) ---------------------------------------------------------
class RunPipelineIn(BaseModel):
    from_num: int | None = None
    to_num: int | None = None
    # Canonical step NUMBERS. Legacy CLI selector: nums are gappy and SHARED
    # (num 3 covers several steps), so they cannot identify one step or a stage.
    only: list[int] | None = None
    # Canonical step IDS — unique and stable, and the only way to name a stage's
    # work. Prefer this; `only` is kept for the existing UI and CLI parity.
    only_ids: list[str] | None = None
    since: str | None = None
    fresh: bool = False

    @model_validator(mode="after")
    def _one_selector(self):
        if self.only is not None and self.only_ids is not None:
            raise ValueError(
                "Pass `only` or `only_ids`, not both — they are different "
                "selectors over the same steps and would silently disagree.")
        return self


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    review_id: str
    kind: str
    step_id: str | None = None
    state: str
    exit_code: int | None = None
    error: str | None = None
    queue: str
    progress: dict | None = None
    params: dict | None = None
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None


# --- Publishing ------------------------------------------------------------
class PublishRequestIn(BaseModel):
    """An owner proposing a public route for their review."""

    route: str = Field(min_length=1, max_length=255)


class PublishApproveIn(BaseModel):
    """An admin approving a request. `confirm_token` must match preflight's, so a
    rebuild between preflight and approval is caught. `route` optionally overrides
    the requested one; `allow_shrink` overrides the information-loss guard."""

    confirm_token: str = Field(max_length=256)
    route: str | None = Field(default=None, max_length=255)
    allow_shrink: bool = False


class PublishRejectIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


class PublishDiffOut(BaseModel):
    """What publishing would change vs. the live dashboard. Empty on first publish."""

    removed_ids: list[str] = Field(default_factory=list)
    added_ids: list[str] = Field(default_factory=list)
    shrunk_metrics: dict[str, list] = Field(default_factory=dict)


class PublishPreflightOut(BaseModel):
    """The evidence packet an admin reviews before approving. Writes nothing."""

    bundle_exists: bool
    built_at: str | None = None
    stale: bool = False
    stale_reason: str | None = None
    route: str
    route_available: bool
    route_taken_by: str | None = None  # slug of the review/source holding it
    first_publish: bool
    n_trials: int | None = None
    feasibility_overall: str | None = None
    # Honesty numbers straight from the manifest.
    panels_rendered: list[str] = Field(default_factory=list)
    panels_skipped: list[dict] = Field(default_factory=list)
    diff: PublishDiffOut | None = None
    shrink_detected: bool = False
    # Publication staleness (only when republishing an already-live route): the
    # live dashboard's snapshot vs. what would be published now. A changed
    # feasibility verdict is called out explicitly — the live site is then making
    # a DIFFERENT claim than the data supports, worse than merely out of date.
    live_n_trials: int | None = None
    live_feasibility_overall: str | None = None
    feasibility_changed: bool = False
    # Hashes the bundle state + diff; approve must echo it back.
    confirm_token: str


class PublishRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    review_id: str
    requested_by: str
    requested_route: str
    state: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision: dict | None = None


class PublishedDashboardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    route: str
    slug: str
    review_id: str | None = None
    published_by: str | None = None
    n_trials: int | None = None
    feasibility_overall: str | None = None
    source: str
    published_at: datetime


class SynthesizeIn(BaseModel):
    """Options for one review-document synthesis run.

    Every field optional: absent fields fall back to the review config's
    ``synthesis`` block (and its defaults). Values are validated in the router
    against the literals synthesize_review.py owns, so the two cannot drift."""

    depth: str | None = Field(default=None, max_length=64)
    audience: str | None = Field(default=None, max_length=64)
    sections: list[Annotated[str, Field(max_length=64)]] | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=64)


class ReviewDocumentOut(BaseModel):
    """The synthesized narrative review document + its generation metadata.

    ``exists: False`` (with every other field null) is the pre-generation
    shape — a 200, not a 404, so the ReportStage body renders its empty state
    without error handling. ``sections`` is passed through verbatim from the
    script's sidecar (id/title/source/model per section)."""

    exists: bool
    stale: bool = False
    stale_reason: str | None = None
    generated_at: str | None = None
    depth: str | None = None
    audience: str | None = None
    model: str | None = None
    provider: str | None = None
    word_count: int | None = None
    sections: list[dict] | None = None
    markdown: str | None = None


class MetaAnalysisOut(BaseModel):
    """The pooled meta-analysis for a review's forest plots.

    `groups` are passed through verbatim (they already match the client's
    ForestGroup shape) rather than re-modeled field by field — the nested
    per-trial / pooled / certainty structure is large and owned by the pipeline;
    validating it here would only add drift. Empty when no pooling was done."""

    groups: list[dict] = Field(default_factory=list)
    min_trials: int | None = None
    methods: dict | None = None
    rules_version: str | None = None
    validation_status: str | None = None
    withheld: list[dict] = Field(default_factory=list)
    validation: list[dict] = Field(default_factory=list)
    possible_companion_reports: list[dict] = Field(default_factory=list)


# --- Invite codes ----------------------------------------------------------
class InviteCreateIn(BaseModel):
    # Who the code is for, in the admin's own words. Required because an
    # unlabelled invite defeats the purpose — you cannot revoke "one of these
    # eleven codes" if you don't know whose is whose.
    label: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=2000)
    # NULL/omitted = never expires. Days rather than a timestamp because that is
    # how the decision is actually made ("give them a month").
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class InviteOut(BaseModel):
    """An invite as the admin list shows it. Deliberately has NO code field —
    the raw code exists only in the mint response (see InviteCreatedOut)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    note: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    redeemed_at: datetime | None = None
    redeemed_by_user_id: str | None = None
    # Joined for display so the admin sees a person, not a uuid.
    redeemed_by_email: str | None = None
    # Derived: pending | active | redeemed | revoked | expired.
    state: str


class InviteCreatedOut(BaseModel):
    """The ONLY response that ever carries the raw code. It is not stored, so
    this is the single opportunity to copy it."""

    invite: InviteOut
    code: str


# --- Account switch-off ------------------------------------------------------
class UserDisableIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class UserAccessOut(BaseModel):
    user_id: str
    email: str
    disabled: bool
