/** Shared types mirroring the FastAPI contract (server/app/schemas.py). */

export interface UserOut {
  id: string;
  email: string;
  role: string;
}

export interface TokenOut {
  access_token: string;
  token_type: string;
}

/** /auth/register request. `invite_code` is required only when the server has
 *  SIGNUP_CODE set — the client never learns which, so the field is always sent
 *  when filled and the server is the single source of truth. */
export interface RegisterIn {
  email: string;
  password: string;
  invite_code?: string;
}

/** /auth/register response: the new user plus a token (signup logs you in).
 *  Fields are listed explicitly rather than via `extends UserOut` so the
 *  contract-drift test can compare them against the Pydantic model. */
export interface RegisterOut {
  id: string;
  email: string;
  role: string;
  access_token: string;
  token_type: string;
}

export interface ReviewOut {
  id: string;
  owner_id: string;
  slug: string;
  topic_name: string;
  title: string;
  status: string;
  llm_default_provider: string;
  visibility: string;
  created_at: string;
  updated_at: string;
}

export interface ReviewCreate {
  title: string;
  topic_name: string;
  /** Optional plain-language research question; falls back to description. */
  question?: string;
  synonyms: string[];
  description: string;
  not_about: string[];
  include_observational: boolean;
  /** Pooling is a property of the review (it decides whether the meta-analysis
   *  steps exist at all), so it is set at creation rather than per run. */
  generate_meta_analysis?: boolean;
}

export interface StepStateOut {
  number: number;
  name: string;
  script: string;
  status: "done" | "partial" | "pending" | "blocked";
  detail: string;
  count: number | null;
  last_modified: string | null;
  /** Canonical step numbers behind this row — units of the run endpoint's `only`. */
  canonical_nums: number[];
}

export interface ReviewStateOut {
  steps: StepStateOut[];
  slug: string;
  all_done: boolean;
  any_done: boolean;
  resume_from_num: number | null;
  resume_display_number: number | null;
  resume_label: string | null;
}

/** One wizard stage, resolved server-side against this review's canonical steps.
 *
 * The stage LIST is owned by the server (services/stages.py) and rendered as
 * given — the client must not hardcode it, or the two will drift.
 */
export interface StageOut {
  slug: string;
  title: string;
  /** One of the six display phases (see PHASES). Grouping only — gating is
   *  stage-level and nothing in the gate path reads this. */
  phase: string;
  kind: "setup" | "run" | "curate" | "review" | "output";
  description: string;
  order: number;
  /** Canonical `Step.id` values that exist for this review (never `Step.num`). */
  step_ids: string[];
  requires_approval: boolean;
  config_keys: string[];
  /** Start on arrival without a click: a pure fetch with no LLM spend. */
  autorun: boolean;
  /** Not shown in the walkthrough (no page/count/nav); still runs + gates. */
  hidden: boolean;
  /** Every declared step vanished for this review — no work to do, not blocked. */
  skipped: boolean;
  /** ARTIFACT axis only: did the work run? `n/a` = no filesystem evidence
   *  either way (settings/curation stages). Approval is a SEPARATE axis — see
   *  `gate` — because a stage can be done on disk and still blocked on a human. */
  status: "done" | "partial" | "pending" | "n/a";
  details: string[];
  /** APPROVAL axis. Null when this stage needs no sign-off. */
  gate: StageGateOut | null;
  /** JOB axis: is something touching this stage right now? */
  activity: StageActivityOut | null;
}

export interface StageGateOut {
  state: "open" | "blocked" | "approved" | "stale";
  approved_at: string | null;
  approved_by: string | null;
  approved_config_version: number | null;
  /** First unapproved upstream stage, when state is "blocked". */
  blocked_by: string | null;
  /** Config keys changed since approval. Empty when unknowable (an approval
   *  predating per-key digests) rather than guessed. */
  stale_keys: string[];
}

export interface StageActivityOut {
  running_job_id: string | null;
  last_job_state: string | null;
}

export interface PhaseOut {
  slug: string;
  label: string;
}

export interface StagesOut {
  stages: StageOut[];
  /** Slug the wizard should land on: the first unfinished stage. */
  current_stage: string | null;
  /** The six phases in order, for the stepper (server-owned, so no drift). */
  phases: PhaseOut[];
}

/** What publishing would change vs. the live dashboard. Empty on first publish. */
export interface PublishDiffOut {
  removed_ids: string[];
  added_ids: string[];
  shrunk_metrics: Record<string, unknown[]>;
}

/** The evidence packet an admin reviews before approving a publish. */
export interface PublishPreflightOut {
  bundle_exists: boolean;
  built_at: string | null;
  stale: boolean;
  stale_reason: string | null;
  route: string;
  route_available: boolean;
  route_taken_by: string | null;
  first_publish: boolean;
  n_trials: number | null;
  feasibility_overall: string | null;
  panels_rendered: string[];
  panels_skipped: Record<string, unknown>[];
  diff: PublishDiffOut | null;
  shrink_detected: boolean;
  /** Publication staleness (republish only): the live snapshot vs. what would
   *  go out now. A changed feasibility verdict is called out explicitly. */
  live_n_trials: number | null;
  live_feasibility_overall: string | null;
  feasibility_changed: boolean;
  /** Approve must echo this back; a rebuild since preflight invalidates it. */
  confirm_token: string;
}

export interface PublishRequestOut {
  id: string;
  review_id: string;
  requested_by: string;
  requested_route: string;
  state: string;
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
  decision: Record<string, unknown> | null;
}

/** One access code, as the admin list shows it. Deliberately has no `code`
 *  field: the raw code exists only in the mint response below, because only its
 *  hash is stored server-side. */
export interface InviteOut {
  id: string;
  label: string;
  note: string | null;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  redeemed_at: string | null;
  redeemed_by_user_id: string | null;
  redeemed_by_email: string | null;
  /** pending | active | redeemed | revoked | expired — derived server-side. */
  state: string;
}

export interface InviteCreateIn {
  label: string;
  note: string | null;
  expires_in_days: number | null;
}

/** The ONLY response carrying the raw code. It is not recoverable afterwards. */
export interface InviteCreatedOut {
  invite: InviteOut;
  code: string;
}

export interface PublishedDashboardOut {
  route: string;
  slug: string;
  review_id: string | null;
  published_by: string | null;
  n_trials: number | null;
  feasibility_overall: string | null;
  source: string;
  published_at: string;
}

/** The pooled meta-analysis from the built bundle. `groups` are the raw
 *  meta_analysis.json shape; adaptForestGroups turns them into ForestGroup[]. */
export interface MetaAnalysisOut {
  groups: Record<string, unknown>[];
  min_trials: number | null;
  methods: Record<string, unknown> | null;
  rules_version: string | null;
  validation_status: string | null;
  withheld: Record<string, unknown>[];
  validation: Record<string, unknown>[];
  possible_companion_reports: Record<string, unknown>[];
}

/** Options for one review-document synthesis run (all optional — absent fields
 *  fall back to the config's `synthesis` block). Mirrors schemas.SynthesizeIn. */
export interface SynthesizeIn {
  depth?: string | null;
  audience?: string | null;
  sections?: string[] | null;
  model?: string | null;
}

/** The synthesized narrative review document + its generation metadata.
 *  `exists: false` (everything else null) is the pre-generation shape. */
export interface ReviewDocumentOut {
  exists: boolean;
  stale: boolean;
  stale_reason: string | null;
  generated_at: string | null;
  depth: string | null;
  audience: string | null;
  model: string | null;
  provider: string | null;
  word_count: number | null;
  /** Per-section provenance from the script's sidecar: id/title/source/model. */
  sections: Record<string, unknown>[] | null;
  markdown: string | null;
}

/** Categorical counts from the classify stage, for the pipeline bar chart. */
export interface ClassifySummaryOut {
  article_types: Record<string, number>;
  relevance: Record<string, number>;
  studies_treatment: Record<string, number>;
  total: number;
}

export interface PregateConfig {
  enabled?: boolean;
  /** Extra keywords that also let an abstract through the pre-gate (beyond the topic synonyms). */
  extra_terms?: string[];
}

export interface ReviewConfigOut {
  config_json: {
    topic?: {
      name?: string;
      synonyms?: string[];
      description?: string;
      not_about?: string[];
    };
    search?: Record<string, string[]>;
    /** Vocabulary the article-type classifier must choose from. */
    article_types?: string[];
    /** Article types whose full text is downloaded. Absent/empty means the
     *  pipeline default (constants.PDF_ELIGIBLE_TYPES); see lib/constants.ts. */
    pdf_types?: string[];
    intervention_categories?: string[];
    drug_class_categories?: string[];
    study_design_categories?: string[];
    organism_categories?: string[];
    indication_categories?: string[];
    classification?: { pregate?: PregateConfig };
    /** What to fetch besides the PDF, and which source extraction reads first.
     *  Mirrors workspace.FULLTEXT_DEFAULTS; see ConfigPatch["fulltext"]. */
    fulltext?: {
      structured?: boolean;
      supplementary?: boolean;
      refresh_supplementary?: boolean;
      data_artifacts?: boolean;
      max_supplementary_mb?: number;
      xml_first?: boolean;
      retry_on_empty?: boolean;
    };
    [k: string]: unknown;
  };
}

/** A partial config for PATCH. `null` deletes a key, restoring the default. */
export interface ConfigPatch {
  topic?: {
    name?: string;
    synonyms?: string[] | null;
    description?: string | null;
    not_about?: string[] | null;
  };
  search?: Record<string, string[]> | null;
  article_types?: string[] | null;
  /** `null` clears the override, restoring the pipeline default. */
  pdf_types?: string[] | null;
  intervention_categories?: string[] | null;
  drug_class_categories?: string[] | null;
  study_design_categories?: string[] | null;
  organism_categories?: string[] | null;
  indication_categories?: string[] | null;
  classification?: { pregate?: PregateConfig } | null;
  /** Dashboard layout (the export stage's editor). Keys stay FLAT at depth 2 —
   *  the server's config merge is exactly two levels deep, so a deeper nest
   *  would clobber sibling keys. `null` deletes (reverts to defaults). */
  dashboard?: {
    panels?: string[] | null;
    columns?: string[] | null;
  } | null;
  /** Review-synthesis options (the report stage). Same depth-2 constraint as
   *  `dashboard` — `sections` is a level-2 value, never a deeper nest. */
  synthesis?: {
    depth?: string;
    audience?: string;
    sections?: string[] | null;
  } | null;
  /** What to fetch besides the PDF, and which source extraction reads first.
   *  Depth-2 like `dashboard`/`synthesis`, so patch the whole block: the server
   *  merge stops at level 2 and a partial object replaces its siblings. */
  fulltext?: {
    /** Publisher XML (else full-text HTML) alongside the PDF. Default true. */
    structured?: boolean;
    /** Supplementary/supporting-information files. Default false — the one the
     *  Screen stage exposes to the user. */
    supplementary?: boolean;
    refresh_supplementary?: boolean;
    data_artifacts?: boolean;
    max_supplementary_mb?: number;
    /** Read XML before the PDF-derived markdown when extracting. */
    xml_first?: boolean;
    /** Re-extract from the next source when a paper yields no numbers. */
    retry_on_empty?: boolean;
  } | null;
  /** Per-step model overrides ("models.synthesis" etc.); merges one level deep. */
  models?: Record<string, string> | null;
  [k: string]: unknown;
}

export interface PreflightIssue {
  level: "error" | "warning";
  code: string;
  message: string;
}

export interface PreflightOut {
  issues: PreflightIssue[];
  has_errors: boolean;
}

export interface PregatePreviewOut {
  total: number;
  would_send: number;
  term_match: number;
  nonlatin_bypass: number;
  would_skip: number;
  no_abstract: number;
  enabled: boolean;
  detail?: string | null;
}

export interface PromptOut {
  name: string;
  label: string;
  text: string;
  /** True when this review has its own edited copy of the template. */
  is_override: boolean;
  required_placeholders: string[];
}

export interface SearchDraftOut {
  queries: Record<string, string[]>;
  provider: string;
  meta: { cost_usd?: number; elapsed?: number; model?: string; [k: string]: unknown };
}

/** The two AI-drafted extraction vocabularies + which provider produced them. */
export interface TaxonomyDraftOut {
  intervention_categories: string[];
  outcome_domains: string[];
  provider: string;
}

export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface JobStepRec {
  num: number | null;
  step_id: string;
  script: string;
  label: string;
  duration_s: number;
  item_total: number | null;
  item_done: number | null;
  state: "done" | "failed";
}

export interface JobLive {
  num: number | null;
  step_id: string;
  script: string;
  label: string;
  done: number | null;
  total: number | null;
  elapsed_s: number | null;
  eta_s: number | null;
  rate: number | null;
}

export interface JobProgress {
  current?: number;
  total?: number;
  step_id?: string | null;
  step_label?: string | null;
  steps?: JobStepRec[];
  live?: JobLive | null;
}

export interface JobOut {
  id: string;
  review_id: string;
  kind: string;
  step_id: string | null;
  state: JobState;
  exit_code: number | null;
  error: string | null;
  queue: string;
  progress: JobProgress | null;
  params: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
}

/* ── Agent (the review collaborator) ─────────────────────────────────────── */

export interface AgentBlocker {
  severity: "high" | "medium";
  code: string;
  message: string;
  evidence: string;
  suggested_fix: string;
}

export interface AgentMessageOut {
  id: string;
  stage: string | null;
  /** System rows carry stage transitions and blocker dismissals, so the
   *  transcript alone reconstructs the journey. */
  role: "user" | "assistant" | "system";
  content: string;
  meta: AgentMeta | null;
  created_at: string;
}

/** `AgentMessageOut.meta` — a free-form JSON column server-side, written by
 *  services/agent.py. Assistant rows carry the LLM bookkeeping; post-run checks
 *  add `kind: "review"`, the `job_id` they reviewed and their `blockers`;
 *  system rows for a dismissal carry `kind: "dismiss"` and the blocker `code`. */
export interface AgentMeta {
  cost_usd?: number | null;
  model?: string;
  provider?: string;
  session_id?: string | null;
  kind?: "review" | "dismiss" | (string & {});
  job_id?: string;
  blockers?: AgentBlocker[];
  code?: string;
  [k: string]: unknown;
}

/* ── Account-level AI-provider settings ───────────────────────────────────── */

/** The LLM providers a user can pick. Mirrors the server's `LLM_PROVIDERS`
 *  (schemas.py):
 *    claude_cli     the HOST's shared Claude login — one account for everyone,
 *                   gated by the single-occupancy lease
 *    codex_cli      the HOST's Codex login (Luna), using the same lease
 *    claude_token   the user's OWN subscription, via `claude setup-token`
 *    anthropic_key  the user's OWN Anthropic Console API key
 *    openrouter     the user's OWN OpenRouter key
 *  The last three are bring-your-own: billed to the user, exempt from the lease. */
export type LlmProvider =
  | "claude_cli"
  | "codex_cli"
  | "claude_token"
  | "anthropic_key"
  | "openrouter";

/** Read model for the account settings. The raw OpenRouter key is never
 *  returned — only whether one is stored. */
/* ── Shared Claude CLI login (single-occupancy lease) ─────────────────────── */
/** The host's `claude` CLI authenticates from the SERVER's $HOME, so LLM work
 *  runs as one Claude account for everyone. This reports who currently holds
 *  it, so nobody unknowingly spends someone else's subscription. */
export interface LlmSessionOut {
  held: boolean;
  held_by_me: boolean;
  holder_email: string | null;
  claude_account: string | null;
  acquired_at: string | null;
  /** Idle past the TTL — anyone may take over. */
  expired: boolean;
  ttl_minutes: number;

  cli_installed: boolean;
  cli_logged_in: boolean;
  cli_account: string | null;
  cli_detail: string;

  /** "openrouter" users bring their own key and are never gated. */
  provider: string;
  login_hint: string;
}

export interface LlmSessionClaimIn {
  force?: boolean;
}

export interface UserSettingsOut {
  llm_provider: LlmProvider;
  has_openrouter_key: boolean;
  /** Whether a per-user Claude credential is stored. Booleans only — a stored
   *  secret is never returned. */
  has_claude_token: boolean;
  has_anthropic_key: boolean;
  /** The account each credential verified as, so the user can see which of
   *  their accounts will be billed. */
  claude_token_account: string | null;
  anthropic_key_account: string | null;
  /** `claude setup-token` mints a fixed 12-month token, so this is known when it
   *  is saved and can be warned about before anything breaks. */
  claude_token_expires_at: string | null;
  /** Absolute, server-normalized PDF download dir; null = per-review default. */
  pdf_dir: string | null;
  /** Non-fatal advice about the saved path (e.g. it is on a Windows drive). */
  pdf_dir_warning: string | null;
}

/** Partial update. `llm_provider` omitted = unchanged; every secret field and
 *  `pdf_dir`: omit = unchanged, "" = clear, non-empty = set.
 *
 *  A non-empty `claude_token`/`anthropic_key` is VERIFIED against the CLI before
 *  it is stored, so a bad paste 422s here rather than failing mid-run. */
export interface UserSettingsIn {
  llm_provider?: LlmProvider;
  openrouter_key?: string;
  /** Output of `claude setup-token`, run by the user on their own machine. */
  claude_token?: string;
  /** An Anthropic Console API key. */
  anthropic_key?: string;
  pdf_dir?: string;
}
