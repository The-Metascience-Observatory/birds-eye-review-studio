"use client";

/** The Extract stage body.
 *
 *  One extraction pass (Sonnet, full text). The user chooses WHAT to extract via
 *  block toggles that write `extraction_pass2_fields` — which shape the extractor
 *  prompt. The default is a "basic" extraction (standard blocks on, the heavy
 *  meta-analysis blocks off): enough for the evidence table, the dashboard, and a
 *  per-study positive/negative/no-effect verdict, without paying to pull per-arm
 *  data for a pooling that most corpora can't support.
 *
 *  When the feasibility verdict says pooling looks possible, a card suggests
 *  enabling the deep meta-analysis blocks. A `not_possible` verdict is NOT shown
 *  here — it reads as a conclusion mid-extraction and is measured over a corpus
 *  that is still growing — so we link to the Meta-analysis stage instead, which
 *  renders the same card as the definitive verdict. The two list/model configs
 *  (`outcome_domains`, `models.extraction_pass2`) are delegated to GenericStage
 *  under "Advanced settings" (the MetaStage pattern).
 */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { toast } from "@/lib/toast";
import { FeasibilityCard } from "@bird/dashboard-ui";

import { GenericStage } from "@/components/stages/GenericStage";
import { ListEditorCard } from "@/components/ListEditorCard";
import { Banner } from "@/components/ui/Banner";
import { draftTaxonomies, getFeasibility, patchReviewConfig } from "@/lib/api";
import { useReviewConfig } from "@/hooks/useReview";
import type { StageOut } from "@/lib/types";

/** One toggleable extraction block. `core` blocks are always extracted — the
 *  toggle is a display of intent and a warning, not a real disable (turning them
 *  off would break the results table + verdict, so the prompt still asks for
 *  them). `deep` blocks are the meta-analysis payload, off by default. */
interface Block {
  key: string;
  label: string;
  desc: string;
  tier: "core" | "standard" | "deep";
}

const CORE_BLOCKS: Block[] = [
  { key: "study_design", label: "Study design & arms", tier: "core",
    desc: "Design type, intervention & comparator arms, blinding, setting, countries." },
  { key: "participants", label: "Population (PICO)", tier: "core",
    desc: "Who was studied — condition definition, inclusion/exclusion criteria." },
  { key: "sample_sizes", label: "Sample size", tier: "core",
    desc: "Total randomized/enrolled and the per-arm counts." },
  { key: "primary_outcomes", label: "Primary outcome & effect", tier: "core",
    desc: "The primary outcome, its effect size + p-value → the positive/negative/no-effect verdict." },
  { key: "risk_of_bias", label: "Risk of bias", tier: "core",
    desc: "RoB2 / ROBINS-I overall judgment and the key concern." },
];

const STANDARD_BLOCKS: Block[] = [
  { key: "baseline_demographics", label: "Baseline demographics", tier: "standard",
    desc: "Per-arm age, sex, BMI, comorbidities at baseline." },
  { key: "adverse_events", label: "Adverse events", tier: "standard",
    desc: "Per-arm adverse-event and serious-event counts." },
  { key: "follow_up", label: "Follow-up durations", tier: "standard",
    desc: "Treatment, total, and post-treatment follow-up lengths." },
  { key: "study_design_methodology", label: "Methodology detail", tier: "standard",
    desc: "Randomization, allocation concealment, and (observational) adjustment methods." },
];

const DEEP_BLOCKS: Block[] = [
  { key: "all_outcomes", label: "All outcomes & timepoints", tier: "deep",
    desc: "Every secondary outcome across all arms and timepoints (not just the primary)." },
  { key: "per_arm_sample_detail", label: "Per-arm sample detail", tier: "deep",
    desc: "Completions, analysis populations, withdrawals and their reasons." },
  { key: "subgroup_analyses", label: "Subgroup analyses", tier: "deep",
    desc: "Pre-specified and post-hoc subgroup results." },
  { key: "provenance_quotes", label: "Verbatim source quotes", tier: "deep",
    desc: "A verbatim quote for every extracted number (for provenance checking)." },
];

/** Core blocks are on unless explicitly false; standard default on; deep default
 *  off. Mirrors steps/extract_pass2.py PASS2_FIELD_DEFAULTS. */
function blockOn(fields: Record<string, unknown> | undefined, b: Block): boolean {
  const v = fields?.[b.key];
  if (typeof v === "boolean") return v;
  return b.tier !== "deep"; // core + standard default on, deep default off
}

export function ExtractStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const qc = useQueryClient();
  const config = useReviewConfig(reviewId);
  const cfg = config.data?.config_json;
  const fields =
    (cfg?.extraction_pass2_fields as Record<string, unknown> | undefined) ??
    undefined;

  const interventionCategories = (cfg?.intervention_categories as string[]) ?? [];
  const outcomeDomains = (cfg?.outcome_domains as string[]) ?? [];
  const customQuestions = (cfg?.custom_extraction_questions as string[]) ?? [];
  // BOTH vocabularies are required to run extraction, so ONE empty list is
  // enough to need a draft — `&&` here once left a review with a single empty
  // vocabulary blocked, with the auto-draft never firing.
  const vocabsEmpty =
    config.isSuccess &&
    (interventionCategories.length === 0 || outcomeDomains.length === 0);

  const running = !!stage.activity?.running_job_id;
  const feasibility = useQuery({
    queryKey: ["feasibility", reviewId],
    queryFn: () => getFeasibility(reviewId),
    refetchInterval: running ? 3000 : false,
    retry: false,
  });

  // Patch a subset of extraction_pass2_fields. The server merges one level deep,
  // so a `{extraction_pass2_fields: {block: bool}}` patch updates just that block.
  const save = useMutation({
    mutationFn: (patch: Record<string, boolean>) =>
      patchReviewConfig(reviewId, { extraction_pass2_fields: patch }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
  });

  // Save one edited list — a vocabulary or the custom questions (ListEditorCards).
  const saveVocab = useMutation({
    mutationFn: (patch: {
      intervention_categories?: string[];
      outcome_domains?: string[];
      custom_extraction_questions?: string[];
    }) => patchReviewConfig(reviewId, patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Save failed"),
  });

  // Concurrent extraction workers — a plain top-level int, patched on blur.
  const saveWorkers = useMutation({
    mutationFn: (n: number) => patchReviewConfig(reviewId, { extraction_workers: n }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Save failed"),
  });
  const workers =
    typeof cfg?.extraction_workers === "number" ? cfg.extraction_workers : 10;

  // AI-draft both vocabularies. Auto-fires ONCE when both are empty (a new review
  // has no buckets and extraction would blocked); a Regenerate button re-runs it.
  const draft = useMutation({
    mutationFn: () => draftTaxonomies(reviewId),
    onSuccess: () => {
      // Mark that these values are AI-generated (until the user edits them).
      setAiGenerated(true);
      qc.invalidateQueries({ queryKey: ["review-config", reviewId] });
      qc.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (e) =>
      toast.error((e as Error)?.message ?? "Could not generate categories"),
  });
  const [aiGenerated, setAiGenerated] = useState(false);
  const autoDrafted = useRef(false);
  // Survives /reviews/[id] navigation (no remount): review A's auto-draft must
  // not suppress review B's, or B sits blocked on empty vocabularies silently.
  useEffect(() => {
    autoDrafted.current = false;
  }, [reviewId]);
  useEffect(() => {
    if (autoDrafted.current) return;
    if (!vocabsEmpty) return;
    if (draft.isPending) return;
    autoDrafted.current = true;
    draft.mutate();
  }, [vocabsEmpty, draft]);

  // Which card (if any) is open for editing — one at a time.
  const [editing, setEditing] = useState<string | null>(null);
  const [vdraft, setVdraft] = useState("");

  const deepAllOn = DEEP_BLOCKS.every((b) => blockOn(fields, b));
  const poolable =
    feasibility.data?.overall === "supported" ||
    feasibility.data?.overall === "fragmented";

  // Delegate the model config to GenericStage; we own the two vocab lists + the
  // field toggles ourselves.
  const advancedStage: StageOut = {
    ...stage,
    config_keys: (stage.config_keys ?? []).filter(
      (k) =>
        k !== "extraction_pass2_fields" &&
        k !== "intervention_categories" &&
        k !== "outcome_domains" &&
        k !== "custom_extraction_questions" &&
        k !== "extraction_workers",
    ),
  };

  const renderVocab = (
    key: "intervention_categories" | "outcome_domains",
    title: string,
    values: string[],
    note: string,
  ) => (
    <ListEditorCard
      title={title}
      note={note}
      values={values}
      editing={editing === key}
      draft={vdraft}
      isPending={saveVocab.isPending && editing === key}
      onStartEdit={() => {
        setEditing(key);
        setVdraft(values.join("\n"));
      }}
      onCancel={() => setEditing(null)}
      onDraftChange={setVdraft}
      onSave={(lines) => {
        setAiGenerated(false); // a hand edit is no longer "AI generated"
        saveVocab.mutate({ [key]: lines }, { onSuccess: () => setEditing(null) });
      }}
    />
  );

  return (
    <div className="space-y-4">
      <Banner tone="info">
        Choose what to pull from each study&apos;s full text. The defaults are a
        quick, useful extraction; the deep meta-analysis fields are off until you
        need them for pooling.
      </Banner>

      {/* The two extraction vocabularies — REQUIRED (empty means every
          intervention/outcome falls into one meaningless "other" bucket, and the
          run is blocked). AI-drafted from the topic on first visit; editable. */}
      <div className="rounded-lg border border-border bg-card p-3">
        <div className="flex items-center justify-between gap-2">
          <div>
            <span className="text-sm font-semibold">Extraction categories</span>
            {aiGenerated && (
              <span className="ml-2 inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
                <Sparkles size={11} className="shrink-0" />
                AI generated
              </span>
            )}
          </div>
          <button
            type="button"
            disabled={draft.isPending}
            onClick={() => draft.mutate()}
            className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-50"
          >
            <Sparkles size={12} className="shrink-0" />
            {draft.isPending ? "Generating…" : aiGenerated || vocabsEmpty ? "Regenerate" : "Generate with AI"}
          </button>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          The buckets the extractor sorts every intervention and outcome into.
          Generated from your topic — review and add any it missed.
        </p>
        {draft.isPending && vocabsEmpty ? (
          <p className="mt-3 text-sm text-muted-foreground">
            Generating categories from the topic…
          </p>
        ) : (
          <div className="mt-3 space-y-3">
            {renderVocab(
              "intervention_categories",
              "Intervention categories",
              interventionCategories,
              "Kinds of treatment compared — group by mechanism, not brand.",
            )}
            {renderVocab(
              "outcome_domains",
              "Outcome domains",
              outcomeDomains,
              "Families of outcome measured — many instruments group into one domain.",
            )}
          </div>
        )}
      </div>

      <BlockGroup
        title="Always extracted"
        subtitle="The results table, dashboard, and effect verdict need these."
        blocks={CORE_BLOCKS}
        fields={fields}
        pending={save.isPending}
        onToggle={(key, on) => save.mutate({ [key]: on })}
      />

      <BlockGroup
        title="Standard"
        subtitle="Useful extra detail. On by default; turn off to save time and cost."
        blocks={STANDARD_BLOCKS}
        fields={fields}
        pending={save.isPending}
        onToggle={(key, on) => save.mutate({ [key]: on })}
      />

      <BlockGroup
        title="Deep meta-analysis"
        subtitle="The full per-arm / per-timepoint payload needed to pool trials. Off by default — the most expensive part."
        blocks={DEEP_BLOCKS}
        fields={fields}
        pending={save.isPending}
        onToggle={(key, on) => save.mutate({ [key]: on })}
      />

      {/* Free-form review-specific questions, appended to the extraction prompt.
          The model answers each in the record's `custom_answers` — shown in the
          paper detail panel. Pass-2 resume skips papers already in the output
          jsonl, so edits only reach papers extracted AFTER the change. */}
      <ListEditorCard
        title="Custom questions"
        note="Extra questions the extractor answers for every paper — answers appear in the paper detail panel. Only papers extracted after you save are asked; already-extracted papers keep their existing record."
        placeholder="What disease or condition is the drug being studied to treat in this paper, if any?"
        values={customQuestions}
        editing={editing === "custom_extraction_questions"}
        draft={vdraft}
        isPending={saveVocab.isPending && editing === "custom_extraction_questions"}
        onStartEdit={() => {
          setEditing("custom_extraction_questions");
          setVdraft(customQuestions.join("\n"));
        }}
        onCancel={() => setEditing(null)}
        onDraftChange={setVdraft}
        onSave={(lines) =>
          saveVocab.mutate(
            { custom_extraction_questions: lines },
            { onSuccess: () => setEditing(null) },
          )
        }
      />

      {/* Concurrency: how many papers extract in parallel. Higher is faster but
          runs more full-text Claude calls at once (shared subscription). */}
      <div className="rounded-lg border border-border bg-card p-3">
        <label
          htmlFor="extraction-workers"
          className="text-sm font-semibold"
        >
          Parallel workers
        </label>
        <p className="text-xs text-muted-foreground">
          How many papers to extract at once. Higher finishes sooner but makes more
          concurrent full-text calls — lower it if you hit rate limits. Default 10.
        </p>
        <input
          id="extraction-workers"
          type="number"
          min={1}
          max={32}
          defaultValue={workers}
          key={workers} /* reset the uncontrolled input when the saved value changes */
          disabled={saveWorkers.isPending}
          onBlur={(e) => {
            const n = Math.max(1, Math.min(32, Math.round(Number(e.target.value))));
            if (Number.isFinite(n) && n !== workers) saveWorkers.mutate(n);
          }}
          className="mt-2 w-24 rounded-md border border-border bg-background px-2 py-1 text-sm disabled:opacity-50"
        />
      </div>

      {/* Feasibility-driven suggestion: if pooling looks possible and the deep
          blocks are off, offer to enable them (then the user re-runs extraction). */}
      {poolable && !deepAllOn && (
        <div className="rounded-lg border border-primary/40 bg-primary/5 p-3 text-sm">
          <div className="font-medium">Meta-analysis looks possible</div>
          <p className="mt-1 text-muted-foreground">
            The feasibility check rates this corpus{" "}
            <span className="font-medium">{feasibility.data?.overall}</span>. Enable
            the deep meta-analysis fields, then re-run extraction on the pooled set
            to capture the per-arm data pooling needs.
          </p>
          <button
            type="button"
            disabled={save.isPending}
            onClick={() =>
              save.mutate(
                Object.fromEntries(DEEP_BLOCKS.map((b) => [b.key, true])),
              )
            }
            className="mt-2 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            Enable deep extraction
          </button>
        </div>
      )}

      {/* The definitive poolability verdict belongs on the Meta-analysis stage
          (Synthesize), not here: "no meta-analysis possible" reads as a
          conclusion while extraction is still the task, and mid-run it is
          measured over a partial corpus that is still growing. Here we show
          only what is actionable while extracting — the "pooling looks
          possible" signal — and otherwise point at the real verdict. */}
      {!running && feasibility.data ? (
        feasibility.data.overall === "not_possible" ? (
          <p className="text-sm text-muted-foreground">
            Pooling looks unlikely for this corpus —{" "}
            <Link
              href={`/reviews/${reviewId}/stages/meta`}
              className="underline underline-offset-2 hover:text-foreground"
            >
              see Meta-analysis for the details
            </Link>
            .
          </p>
        ) : (
          <FeasibilityCard feasibility={feasibility.data} />
        )
      ) : null}

      {advancedStage.config_keys.length > 0 && (
        <div>
          <h3 className="mb-2 text-sm font-semibold">Advanced settings</h3>
          <GenericStage reviewId={reviewId} stage={advancedStage} />
        </div>
      )}
    </div>
  );
}

function BlockGroup({
  title,
  subtitle,
  blocks,
  fields,
  pending,
  onToggle,
}: {
  title: string;
  subtitle: string;
  blocks: Block[];
  fields: Record<string, unknown> | undefined;
  pending: boolean;
  onToggle: (key: string, on: boolean) => void;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="text-sm font-semibold">{title}</div>
      <p className="text-xs text-muted-foreground">{subtitle}</p>
      <div className="mt-2 space-y-1.5">
        {blocks.map((b) => {
          const on = blockOn(fields, b);
          const coreOff = b.tier === "core" && !on;
          return (
            <label
              key={b.key}
              className="flex cursor-pointer items-start gap-2 text-sm"
            >
              <input
                type="checkbox"
                className="mt-0.5 h-4 w-4 accent-primary"
                checked={on}
                disabled={pending}
                onChange={(e) => onToggle(b.key, e.target.checked)}
              />
              <span>
                <span className="font-medium">{b.label}</span>
                <span className="block text-xs text-muted-foreground">{b.desc}</span>
                {coreOff && (
                  <span className="block text-xs text-amber-600 dark:text-amber-400">
                    Warning: this is a core field — turning it off leaves the
                    results table and effect verdict incomplete.
                  </span>
                )}
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}
