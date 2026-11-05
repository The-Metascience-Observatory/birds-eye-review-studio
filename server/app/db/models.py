"""ORM models (skeleton subset).

Uses SQLAlchemy generic types (String / Integer / DateTime / JSON / Numeric /
Text) and a String-based ``status`` field (NOT native enums) so the identical
models run on both sqlite and postgres. IDs are string UUIDs (uuid4().hex) for
portability.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .._time import utcnow as _now  # column defaults reference `_now`


def _uuid() -> str:
    return uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(
        String(320), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)

    # Account-level LLM choice. New reviews inherit it unless overridden.
    llm_provider: Mapped[str] = mapped_column(
        String(64), default="codex_cli", nullable=False
    )

    # --- billing (columns present now, logic deferred) --------------------
    # TODO(phase-3): billing/credit ledger.
    billing_mode: Mapped[str] = mapped_column(
        String(32), default="prepaid", nullable=False
    )
    # LEGACY: the OpenRouter key used to be stored here. It now lives in
    # ``user_credentials`` (kind="openrouter"); this column is read as a
    # fallback only and never written.
    openrouter_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Account-level PDF download directory. NULL = use each review's default
    # (``projects/<slug>/pdfs``). Validated + normalized by services/pdf_dir.py
    # before it ever reaches here; a review's ``dirs.base_pdf_dir`` still wins.
    pdf_dir: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )

    reviews: Mapped[list["Review"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id"), index=True, nullable=False
    )
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    topic_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    workspace_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    llm_default_provider: Mapped[str] = mapped_column(
        String(64), default="codex_cli", nullable=False
    )
    visibility: Mapped[str] = mapped_column(
        String(32), default="private", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    owner: Mapped["User"] = relationship(back_populates="reviews")
    config: Mapped["ReviewConfig | None"] = relationship(
        back_populates="review", uselist=False, cascade="all, delete-orphan"
    )


class ReviewConfig(Base):
    __tablename__ = "review_configs"

    # 1:1 with Review — review_id doubles as the primary key.
    review_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("reviews.id"), primary_key=True
    )
    config_json: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )

    review: Mapped["Review"] = relationship(back_populates="config")


class Job(Base):
    """A background pipeline run — one step, or a range of steps.

    Backed by arq/redis: ``arq_job_id`` links to the enqueued task. All state is
    tracked here (not in redis) so the API can report on jobs after they finish.
    String enums (no native enums) so it runs identically on sqlite + postgres.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    review_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("reviews.id"), index=True, nullable=False
    )
    # "step" (one step_id) | "pipeline" (a from/to/only range).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    arq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The arq queue this job was dispatched to: "ber:pdf" for the non-LLM
    # background kinds (pdf, metadata), the LLM queue otherwise. Rows written
    # before routing existed carry the old "default" placeholder.
    queue: Mapped[str] = mapped_column(String(64), default="default", nullable=False)
    # queued | running | succeeded | failed | cancelled
    state: Mapped[str] = mapped_column(
        String(32), default="queued", nullable=False
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {current, total, step_id, step_label} live progress for pipeline runs.
    progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Enqueue-time run params for a "pipeline" job: {from_num, to_num, only,
    # since, fresh}. Read back by the worker's run_pipeline_task.
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StageState(Base):
    """A human's sign-off on one wizard stage.

    Sparse by design: NO ROW MEANS NOT APPROVED. Absence is the default state, so
    introducing this table needs no backfill and every existing review starts
    correctly unapproved.

    Approval is deliberately NOT a status field on some other table, because it
    is a different KIND of fact from the pipeline's own status. Whether a stage
    ran is observable from artifacts on disk (`services/stages.py`); whether a
    human accepted the result is only knowable here. Keeping them apart is what
    lets a stage be simultaneously "done" and "not approved" — the normal state
    of a wizard step waiting for review.
    """

    __tablename__ = "stage_states"

    review_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("reviews.id"), primary_key=True
    )
    # A StageDef.slug. Slugs are stable forever (see services/stages.py), which
    # is what makes them safe as a persisted key.
    stage: Mapped[str] = mapped_column(String(64), primary_key=True)

    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=True
    )
    # Shown to the user ("approved at config v7"). NOT used to compute staleness
    # — see stage_config_digest.
    approved_config_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # Hash of THIS STAGE's config_keys subtree at approval time. Staleness is
    # digest != current, never version != current: ReviewConfig.version bumps on
    # every PATCH including no-op ones, so version comparison would mark stages
    # stale on saves that changed nothing. A digest is also immune to edits that
    # touch only OTHER stages' keys, and correctly treats edit-away-and-back as
    # not stale.
    stage_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-stage decisions and overrides, including AI-raised blockers the user
    # dismissed: {blockers: [{code, severity, overridden_by, overridden_at,
    # reason}], ...}. Decision-shaped from the start so the generic checkpoint
    # layer can absorb it later.
    decisions: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class AgentMessage(Base):
    """One turn of the review's agent conversation.

    The studio owns the transcript, NOT the LLM provider: `claude --resume` is a
    Claude-CLI feature with no OpenRouter equivalent, so conversation memory
    must be replayable from rows we hold. The CLI session id is kept only as an
    optimisation, riding in ``meta`` on assistant rows — deliberately NOT a
    column on ``Review``, because there is no Alembic here and ``create_all``
    can create this table but could never add that column.

    Rows, not a JSON blob on the review: appending a turn must not rewrite the
    whole history, and per-stage filtering should be a WHERE clause.
    """

    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    review_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("reviews.id"), index=True, nullable=False
    )
    # Which wizard stage the turn happened on (None for pre-wizard rows).
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # user | assistant | system. System rows record stage transitions and
    # blocker dismissals, so the transcript alone reconstructs the journey.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Assistant rows: {cost_usd, model, provider, session_id, kind?, job_id?,
    # blockers?}. `kind="review"` marks an auto sanity-check; its job_id is the
    # idempotency key that stops one run being reviewed twice.
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )


class PaperMetadata(Base):
    """Bibliographic metadata for one paper in one review, from DOI lookup.

    Populated by a background enrichment worker that runs IN PARALLEL with the
    rest of the pipeline (it only fetches from Crossref and writes here, touching
    none of the resume-critical workspace files), so titles/authors/years surface
    in the evidence table right after search — long before screening narrows the
    corpus. The pipeline's own CSVs still carry `paper_*` columns for the scripts
    that read them; this table is the studio's fast, queryable mirror.

    Composite PK (review_id, doi): metadata is per-review because the same DOI in
    two reviews is two rows to enrich independently, and a review deletion takes
    its metadata with it.
    """

    __tablename__ = "paper_metadata"

    review_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("reviews.id"), primary_key=True
    )
    doi: Mapped[str] = mapped_column(String(255), primary_key=True)  # normalized

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    authors: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[str | None] = mapped_column(String(16), nullable=True)
    journal: Mapped[str | None] = mapped_column(Text, nullable=True)
    pmid: Mapped[str | None] = mapped_column(String(32), nullable=True)
    volume: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pages: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # "crossref" | "pubmed" | ... | "not_found" — so an unresolvable DOI is a
    # recorded fact (don't re-fetch every poll), not a perpetual gap.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class PublishedDashboard(Base):
    """One dashboard live on the public site, keyed on its public ROUTE.

    The route is the PRIMARY KEY, so the database itself enforces one dashboard
    per public URL. This closes P2: today the publish path is
    ``slugify(topic_name)`` with no owner check, so two users each making a
    "Long Covid" review silently overwrite each other's live site. With the route
    as PK, the second publish to an occupied route is a conflict the API can
    refuse instead of a silent clobber.

    ``review_id`` is NULLABLE on purpose: the dashboards deployed by the old CLI
    scripts have no owning ``Review`` row, and they are grandfathered in as rows
    with ``source="grandfathered"`` and no review — enough for route-uniqueness
    to protect them, without inventing a fake owner.
    """

    __tablename__ = "published_dashboards"

    # The public URL segment (e.g. "long-covid"). PK ⇒ one review per URL.
    route: Mapped[str] = mapped_column(String(255), primary_key=True)
    # The pipeline slug the feed lives under in the website's data dir. Usually
    # differs from route ("long_covid" vs "long-covid").
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    # The studio review this was published from — NULL for grandfathered CLI
    # dashboards that predate the studio.
    review_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("reviews.id"), nullable=True, index=True
    )
    published_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=True
    )
    # Snapshot of what the live dashboard CLAIMS, so staleness and "142 new
    # since" can be computed without re-reading the deployed feed every time.
    n_trials: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feasibility_overall: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # mtime of the bundle at publish time — compared against the workspace bundle
    # to detect "the live site is older than the built data" (publication stale).
    built_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # "studio" | "grandfathered"
    source: Mapped[str] = mapped_column(String(32), default="studio", nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now, nullable=False
    )


class PublishRequest(Base):
    """An owner's request to publish a review, and its admin decision.

    Publishing is a two-party act: the owner PROPOSES a route, an admin APPROVES
    (which runs the deploy) or REJECTS. The decision is stored Decision-shaped —
    ``{by, at, reason, confirm_token, diff}`` — so the generic checkpoint/audit
    layer the correctness plan wants can absorb it later, exactly like
    ``StageState.decisions``.
    """

    __tablename__ = "publish_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    review_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("reviews.id"), index=True, nullable=False
    )
    requested_by: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=False
    )
    # The route the owner proposes; an admin may override at approval time.
    requested_route: Mapped[str] = mapped_column(String(255), nullable=False)
    # pending | approved | rejected | withdrawn
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    decided_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Decision-shaped: {reason?, confirm_token?, route?, diff?}. The confirm_token
    # hashes the bundle state + diff the admin reviewed, so an approval that
    # raced a rebuild is rejected rather than publishing a different diff.
    decision: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LlmLease(Base):
    """WHO currently owns the host's shared ``claude`` CLI login.

    The CLI authenticates from files in the server user's ``$HOME/.claude`` —
    there is no per-request credential, so every LLM subprocess on this host
    runs as whichever Claude account is logged in. On a multi-user deployment
    that means one person silently spends another's subscription.

    This table makes that occupancy EXPLICIT: exactly one row (id="singleton"),
    naming the user who currently holds the CLI. Others are refused LLM work
    with a message naming the holder, rather than quietly billing them.

    It is a coordination lock among trusted colleagues, NOT a security boundary
    — all steps still run as one OS user (see services/runner.py).
    """

    __tablename__ = "llm_lease"

    # Single fixed row; see SINGLETON_ID in services/llm_lease.py.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder_user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=True
    )
    # The Claude account the CLI reported at claim time, so the UI can show
    # "running under alice@example.com" — the thing that actually gets billed.
    claude_account: Mapped[str | None] = mapped_column(String(320), nullable=True)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Refreshed by activity; a lease idle longer than the TTL is claimable by
    # anyone, so a user who closes their laptop doesn't block the team forever.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# TODO(phase-3): LLMUsage + CreditLedger (Numeric(12, 6) money columns) tables.


class InviteCode(Base):
    """One access code, issued to one person, revocable independently.

    Replaces the single shared ``settings.SIGNUP_CODE``, which admits everyone
    who has ever seen it, records nothing about who used it, and can only be
    "revoked" by rotating it and re-telling everybody the new one.

    WHY A NEW TABLE AND NOT A COLUMN ON ``users``: this repo has no Alembic, and
    ``create_all`` creates tables but never adds columns to existing ones (see
    CLAUDE.md). A new table is therefore the only migration-free option — which
    is also why *revocation* state lives here, keyed to the user, rather than as
    a ``users.disabled`` flag.

    WHY THE HASH IS SHA-256 AND NOT ARGON2: the code is 24 random bytes from
    ``secrets.token_urlsafe`` (~192 bits), so there is no dictionary or brute
    force to slow down — the reason to use a slow KDF for passwords simply does
    not apply. A deterministic hash is what makes redemption a single indexed
    lookup instead of a scan-and-verify over every outstanding code.
    """

    __tablename__ = "invite_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    # sha256 hexdigest of the raw code. The raw code is shown ONCE at mint time
    # and never stored, so a database leak does not hand over usable codes.
    code_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # Who this was issued to, in your words ("Alice Chen — Stanford"). The whole
    # point of per-person codes: the audit trail is only useful if you can tell
    # which row is whose before it has been redeemed.
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id"), nullable=True
    )
    # NULL = never expires. Checked on redemption AND on every request, so
    # setting one retroactively ends an existing session too.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    redeemed_by_user_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id"), index=True, nullable=True
    )


class UserCredential(Base):
    """One LLM credential a user brought themselves, encrypted at rest.

    WHY THIS EXISTS: the host's ``claude`` CLI authenticates from ONE login in
    the server user's ``$HOME``, so without this every AI step on the machine
    spends the same subscription. ``claude setup-token`` closes that gap — a user
    runs it on their OWN machine, approves the browser flow against their own
    Claude account, and gets a one-year OAuth token. Pasted here and injected as
    ``CLAUDE_CODE_OAUTH_TOKEN``, it outranks the host's ``/login`` credential
    (rank 5 vs rank 7 in Claude Code's authentication precedence), so their jobs
    bill their plan, not the operator's.

    WHY A NEW TABLE AND NOT COLUMNS ON ``users``: there is no Alembic in this
    repo — ``create_all`` creates tables but never adds columns (see CLAUDE.md).
    Same constraint that shaped ``InviteCode``. It also generalizes: one row per
    (user, kind) means adding a provider later is data, not a migration.

    ``kind="openrouter"`` holds the OpenRouter key. It used to live on the
    legacy ``User.openrouter_key_encrypted`` column; that column is still READ
    as a fallback (see ``llm_prefs.openrouter_key_for``) but never written, so
    a key saved before the move keeps working until the user next saves one.
    """

    __tablename__ = "user_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_user_credential_kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id"), index=True, nullable=False
    )
    # "claude_token" (a `claude setup-token` OAuth token, their subscription) or
    # "anthropic_key" (an Anthropic Console API key, pay-per-token).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Fernet ciphertext (app/crypto.py). Never returned by any endpoint — the
    # settings response exposes only whether a credential exists.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # The account the credential actually authenticated as, captured by probing
    # the CLI at save time. Shown in Settings so the user can see WHICH of their
    # accounts will be billed, rather than trusting an opaque "saved".
    account: Mapped[str | None] = mapped_column(String(320), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # setup-token lifetime is a fixed 12 months and known at save time, so it is
    # recorded rather than discovered when a job fails. NULL for API keys, which
    # do not expire on a schedule.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserDisabled(Base):
    """An account an operator has switched off.

    Invite revocation (``InviteCode.revoked_at``) already ends the session of a
    user who registered WITH a code — but accounts made without one (open
    signup on a dev box, the legacy shared ``SIGNUP_CODE``, seeded admins) had
    no off switch short of deleting the row. A row here does that: checked by
    ``deps.get_current_user`` on every request, so it takes effect on the
    user's next call despite stateless JWTs.

    A new TABLE rather than a ``users.disabled`` column because ``create_all``
    cannot add columns to an existing table (no Alembic — see CLAUDE.md).
    Deleting the row re-enables the account.
    """

    __tablename__ = "user_disabled"

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id"), primary_key=True
    )
    disabled_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )
    disabled_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PdfUpload(Base):
    """One hand-uploaded PDF, for the per-user upload caps.

    The upload endpoint writes into the review's paper store; this ledger is
    what lets ``UPLOAD_MAX_FILES_PER_USER`` / ``UPLOAD_MAX_TOTAL_MB_PER_USER``
    be enforced without walking every workspace the user owns.
    """

    __tablename__ = "pdf_uploads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id"), index=True, nullable=False
    )
    review_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    doi: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, nullable=False
    )


class ReviewAutomation(Base):
    """Explicitly activated reviews only; historical workspaces stay idle."""
    __tablename__ = "review_automation"
    review_id: Mapped[str] = mapped_column(String(32), ForeignKey("reviews.id"), primary_key=True)
