"use client";

/** Classify papers: results bar charts + the classification settings panel.
 *
 *  Extracted verbatim from the old review-detail page's classify step row. The
 *  results (three BarLists) sit at the top when the stage has produced output;
 *  the settings panel (preflight issues, list editors, keyword pre-gate,
 *  optional taxonomies, and raw prompt overrides) is always rendered below.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronUp,
  Eye,
  Loader2,
  Pencil,
  RotateCcw,
  X,
} from "lucide-react";
import { useState } from "react";
import { toast } from "@/lib/toast";

import { BarList } from "@/components/BarList";
import { ListEditorCard } from "@/components/ListEditorCard";
import { DEFAULT_PDF_TYPES } from "@/lib/constants";
import {
  getClassifySummary,
  getPreflight,
  getPregatePreview,
  getPrompts,
  patchReviewConfig,
  savePrompt,
} from "@/lib/api";
import { useReviewConfig } from "@/hooks/useReview";
import type {
  ClassifySummaryOut,
  ConfigPatch,
  PregateConfig,
  PregatePreviewOut,
  PreflightOut,
  PromptOut,
  StageOut,
} from "@/lib/types";

// Config keys for classifiers the DEFAULT pipeline does not run — grouped
// behind their own disclosure so the UI doesn't imply they take effect.
const OPTIONAL_TAXONOMIES: { key: string; label: string }[] = [
  { key: "intervention_categories", label: "Intervention categories" },
  { key: "drug_class_categories", label: "Drug class categories" },
  { key: "study_design_categories", label: "Study design categories" },
  { key: "organism_categories", label: "Organism categories" },
  { key: "indication_categories", label: "Indication categories" },
];

export function ClassifyStage({
  reviewId,
  stage,
}: {
  reviewId: string;
  stage: StageOut;
}) {
  const queryClient = useQueryClient();

  // Sub-toggles kept from the old panel.
  const [showTaxonomies, setShowTaxonomies] = useState(false);
  const [showPrompts, setShowPrompts] = useState(false);
  const [showPdfTypes, setShowPdfTypes] = useState(false);
  // Which list card is open; only one at a time (keys are config paths).
  const [editingList, setEditingList] = useState<string | null>(null);
  const [listDraft, setListDraft] = useState("");
  const [editingPrompt, setEditingPrompt] = useState<string | null>(null);
  const [promptDraft, setPromptDraft] = useState("");

  const config = useReviewConfig(reviewId);

  const classify = useQuery<ClassifySummaryOut>({
    queryKey: ["classify-summary", reviewId],
    queryFn: () => getClassifySummary(reviewId),
  });

  const preflight = useQuery<PreflightOut>({
    queryKey: ["preflight", reviewId, stage.slug],
    queryFn: () => getPreflight(reviewId, stage.slug),
  });

  const prompts = useQuery<PromptOut[]>({
    queryKey: ["prompts", reviewId],
    queryFn: () => getPrompts(reviewId),
    enabled: showPrompts,
  });

  const pregatePreview = useQuery<PregatePreviewOut>({
    queryKey: ["pregate-preview", reviewId],
    queryFn: () => getPregatePreview(reviewId),
    enabled: false, // on demand only — the user clicks "Preview"
  });

  // Classification config writes go through PATCH: the server merges against
  // the current row, so editing one key can never drop another.
  const saveConfig = useMutation({
    mutationFn: (partial: ConfigPatch) => patchReviewConfig(reviewId, partial),
    onSuccess: () => {
      setEditingList(null);
      queryClient.invalidateQueries({ queryKey: ["review-config", reviewId] });
      // Editing config can stale a stage approval, so refresh the stage list too.
      queryClient.invalidateQueries({ queryKey: ["stages", reviewId] });
      queryClient.invalidateQueries({ queryKey: ["preflight", reviewId] });
      if (pregatePreview.data) pregatePreview.refetch();
    },
    onError: (err) => toast.error((err as Error)?.message ?? "Save failed"),
  });

  // The animal policy gets its OWN mutation rather than riding `saveConfig`:
  // it changes which papers read as screened_out, so it must invalidate the
  // evidence table + funnel, and every other classify patch would then pay for
  // a corpus-wide refetch it doesn't need.
  const saveAnimals = useMutation({
    mutationFn: (include: boolean) =>
      patchReviewConfig(reviewId, { include_animal_studies: include }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["review-config", reviewId] });
      queryClient.invalidateQueries({ queryKey: ["evidence", reviewId] });
      queryClient.invalidateQueries({ queryKey: ["prisma", reviewId] });
      queryClient.invalidateQueries({ queryKey: ["stages", reviewId] });
    },
    onError: (err) => toast.error((err as Error)?.message ?? "Save failed"),
  });

  const savePromptMut = useMutation({
    mutationFn: (args: { name: string; text: string | null }) =>
      savePrompt(reviewId, args.name, args.text),
    onSuccess: () => {
      setEditingPrompt(null);
      queryClient.invalidateQueries({ queryKey: ["prompts", reviewId] });
    },
    onError: (err) =>
      toast.error((err as Error)?.message ?? "Could not save prompt"),
  });

  const cfgJson = config.data?.config_json ?? {};
  const pregate = cfgJson.classification?.pregate ?? {};
  const pregateOn = pregate.enabled !== false;
  // The pre-gate is fully general: it gates on the review's OWN synonyms plus
  // any extra terms. It carries no built-in vocabulary — a hardcoded term list
  // encodes a topic assumption wrong for every other review.
  

  /** Patch one key of `classification.pregate`, preserving its siblings. */
  const setPregate = (partial: PregateConfig) =>
    saveConfig.mutate({
      classification: { pregate: { ...pregate, ...partial } },
    });

  const fulltext = cfgJson.fulltext ?? {};
  const supplementsOn = fulltext.supplementary === true;
  /** Patch one key of `fulltext`, preserving its siblings.
   *  The config PATCH merge is exactly two levels deep, so spreading the whole
   *  block is required — a bare `{ supplementary: x }` would drop the rest. */
  const setSupplements = (on: boolean) =>
    saveConfig.mutate({ fulltext: { ...fulltext, supplementary: on } });

  // --- `pdf_types`: which article types get their full text downloaded -------
  // Unset means "use the pipeline default" (download_pdfs.py falls back to
  // PDF_ELIGIBLE_TYPES), so the checkboxes show that default pre-ticked rather
  // than showing nothing — an empty list here means "fetch nothing", which is a
  // very different state and must be reachable only on purpose.
  const knownArticleTypes = cfgJson.article_types ?? [];
  const pdfTypesRaw = cfgJson.pdf_types ?? [];
  const pdfTypesCustom = pdfTypesRaw.length > 0;
  // Intersect the default with this review's own vocabulary: a type the
  // classifier can never emit would be dead weight in the list.
  const defaultPdfTypes = knownArticleTypes.filter((t) =>
    (DEFAULT_PDF_TYPES as readonly string[]).includes(t),
  );
  const effectivePdfTypes = pdfTypesCustom ? pdfTypesRaw : defaultPdfTypes;
  // Count only what the user can actually see and untick, so the summary can't
  // disagree with the boxes when a stale config names a retired type.
  const selectedPdfTypes = knownArticleTypes.filter((t) =>
    effectivePdfTypes.includes(t),
  );

  /** Tick/untick one type.
   *
   *  From the default state this MATERIALISES the default first, so the first
   *  click changes exactly one type instead of collapsing the list to a single
   *  entry. Saved in vocabulary order so the stored config reads predictably.
   */
  const togglePdfType = (type: string, on: boolean) => {
    const next = on
      ? [...effectivePdfTypes, type]
      : effectivePdfTypes.filter((t) => t !== type);
    saveConfig.mutate({
      pdf_types: knownArticleTypes.filter((t) => next.includes(t)),
    });
  };

  const startListEdit = (key: string, values: string[]) => {
    setEditingList(key);
    setListDraft(values.join("\n"));
  };

  const clsIssues = preflight.data?.issues ?? [];
  const includeAnimals = cfgJson.include_animal_studies === true;

  return (
    <div className="space-y-4">
      {classify.data && classify.data.total > 0 && (
        <div className="grid w-full gap-x-8 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
          <BarList title="Article type" data={classify.data.article_types} />
          <BarList title="Relevant to topic" data={classify.data.relevance} />
          <BarList
            title="Studies a treatment"
            data={classify.data.studies_treatment}
          />
        </div>
      )}

      <div className="w-full space-y-3">
        {/* Problems that would break or silently degrade a run. */}
        {clsIssues.length > 0 && (
          <ul className="space-y-1.5">
            {clsIssues.map((iss) => (
              <li
                key={iss.code}
                className={`flex items-start gap-1.5 text-xs ${
                  iss.level === "error" ? "text-red-600" : "text-amber-600"
                }`}
              >
                <AlertTriangle
                  className="mt-0.5 h-3.5 w-3.5 shrink-0"
                  aria-hidden
                />
                <span>{iss.message}</span>
              </li>
            ))}
          </ul>
        )}

        {/* Which organisms count as in-scope. This belongs with the screening
            settings, not on the human-review page: the screen is what labels
            each paper's organism, and this flag decides what that label MEANS.
            It is applied deterministically downstream (the evidence table,
            PRISMA, and the pass-1 eligibility gate all read the same key), so
            toggling it re-colours the corpus at once — no re-screen needed. */}
        <div className="rounded-lg border border-border bg-card p-3">
          <div className="text-sm font-medium">Studies in scope</div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            The screen labels every paper&apos;s organism (human, mouse, other
            animal, in-vitro) and the full-text pass refines it. This decides
            which of those labels stay in the review.
          </p>

          <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={includeAnimals}
              disabled={saveAnimals.isPending}
              onChange={(e) => saveAnimals.mutate(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-border accent-primary"
            />
            <span>
              <span className="font-medium">Include animal studies</span>
              <span className="block text-[11px] text-muted-foreground">
                Animal and other non-human studies (mouse, other animal,
                in-vitro) are excluded by default. Check this to include them in
                the review. Takes effect immediately on papers already screened.
              </span>
            </span>
          </label>
        </div>

        <ListEditorCard
          title="Article types"
          values={cfgJson.article_types ?? []}
          note="The vocabulary the classifier must choose from. It is rendered into the prompt, so an empty list makes the model invent labels that later stages reject."
          editing={editingList === "article_types"}
          draft={listDraft}
          isPending={saveConfig.isPending}
          onStartEdit={() =>
            startListEdit("article_types", cfgJson.article_types ?? [])
          }
          onCancel={() => setEditingList(null)}
          onDraftChange={setListDraft}
          onSave={(lines) => saveConfig.mutate({ article_types: lines })}
          onReset={() => saveConfig.mutate({ article_types: null })}
        />

        <ListEditorCard
          title="Exclusion criteria"
          values={cfgJson.topic?.not_about ?? []}
          note="Shown to the model as “this paper is NOT about…” bullets during relevance, combined and trial screening."
          editing={editingList === "not_about"}
          draft={listDraft}
          isPending={saveConfig.isPending}
          onStartEdit={() =>
            startListEdit("not_about", cfgJson.topic?.not_about ?? [])
          }
          onCancel={() => setEditingList(null)}
          onDraftChange={setListDraft}
          onSave={(lines) =>
            saveConfig.mutate({
              topic: { not_about: lines },
            })
          }
        />

        <ListEditorCard
          title="Synonyms"
          values={cfgJson.topic?.synonyms ?? []}
          note="Dual-purpose: only the first 5 appear in the relevance prompt, but ALL of them feed the keyword pre-gate below. Include the topic's own name, or the pre-gate may skip most of your corpus."
          editing={editingList === "synonyms"}
          draft={listDraft}
          isPending={saveConfig.isPending}
          onStartEdit={() =>
            startListEdit("synonyms", cfgJson.topic?.synonyms ?? [])
          }
          onCancel={() => setEditingList(null)}
          onDraftChange={setListDraft}
          onSave={(lines) => saveConfig.mutate({ topic: { synonyms: lines } })}
        />

        {/* Keyword pre-gate */}
        <div className="rounded-lg border border-border bg-card p-3">
          <div className="text-sm font-medium">Keyword pre-gate</div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Skips abstracts with no on-topic keyword before any model call — a
            large cost saving, but a mismatched term list silently discards
            papers. Preview before running.
          </p>

          <label className="mt-2 flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={pregateOn}
              disabled={saveConfig.isPending}
              onChange={(e) => setPregate({ enabled: e.target.checked })}
              className="h-4 w-4 rounded border-border accent-primary"
            />
            Enable the pre-gate
          </label>
          {pregatePreview.data &&
            pregatePreview.data.total > 0 &&
            pregatePreview.data.would_skip / pregatePreview.data.total > 0.6 && (
              // A gate that discards most of the corpus is far more likely to be
              // misconfigured than to be right, and it happens BEFORE any LLM
              // call — so there is no later signal that anything went wrong.
              <p className="mt-2 flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-700 dark:text-amber-400">
                <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                <span>
                  This would skip{" "}
                  {Math.round(
                    (pregatePreview.data.would_skip /
                      pregatePreview.data.total) *
                      100,
                  )}
                  % of your papers before any model call. Check that your topic
                  synonyms and extra terms actually cover your intervention.
                </span>
              </p>
            )}

          <div className="mt-2.5 flex items-center gap-2">
            <button
              onClick={() => pregatePreview.refetch()}
              disabled={pregatePreview.isFetching}
              className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground disabled:opacity-60"
            >
              {pregatePreview.isFetching ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Eye className="h-3.5 w-3.5" />
              )}
              Preview pre-gate
            </button>
            {pregatePreview.data && (
              <span className="text-xs tabular-nums text-muted-foreground">
                {pregatePreview.data.total === 0
                  ? pregatePreview.data.detail ?? "No abstracts yet."
                  : `${pregatePreview.data.would_send.toLocaleString()} sent to the model · ${pregatePreview.data.would_skip.toLocaleString()} skipped of ${pregatePreview.data.total.toLocaleString()}`}
              </span>
            )}
          </div>

          <div className="mt-2.5">
            <ListEditorCard
              title="Extra pre-gate terms"
              values={pregate.extra_terms ?? []}
              note="Matched in addition to the synonyms above."
              editing={editingList === "extra_terms"}
              draft={listDraft}
              isPending={saveConfig.isPending}
              onStartEdit={() =>
                startListEdit("extra_terms", pregate.extra_terms ?? [])
              }
              onCancel={() => setEditingList(null)}
              onDraftChange={setListDraft}
              onSave={(lines) => setPregate({ extra_terms: lines })}
            />
          </div>
        </div>

        {/* Full-text acquisition. Lives here because this stage already owns
            `pdf_types` — which papers get their full text downloaded. */}
        <div className="rounded-lg border border-border bg-card p-3">
          <div className="text-sm font-medium">Full text to download</div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            The publisher&apos;s XML is fetched automatically alongside every
            PDF. It carries tables as real markup, so extraction reads it first
            and stops losing outcomes that only appear in a table.
          </p>

          <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={supplementsOn}
              disabled={saveConfig.isPending}
              onChange={(e) => setSupplements(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-border accent-primary"
            />
            <span>
              <span className="font-medium">
                Also download supplementary material
              </span>
              <span className="block text-[11px] text-muted-foreground">
                Appendices, protocols and data files published alongside each
                paper. Slower, and some files are very large. Turning this on
                later backfills papers you have already fetched.
              </span>
            </span>
          </label>

          {/* `pdf_types` — the article types whose full text gets fetched.
              Preflight warns when this is unset, so it needs a control here:
              a warning that names a setting the UI never exposes is a dead end. */}
          <div className="mt-3 border-t border-border pt-2.5">
            <button
              onClick={() => setShowPdfTypes((v) => !v)}
              className="flex w-full items-center gap-1.5 text-left text-xs"
            >
              {showPdfTypes ? (
                <ChevronUp className="h-3.5 w-3.5 shrink-0" aria-hidden />
              ) : (
                <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden />
              )}
              <span className="font-medium">Which article types to download</span>
              <span className="ml-auto text-[11px] text-muted-foreground">
                {pdfTypesCustom
                  ? `${selectedPdfTypes.length} of ${knownArticleTypes.length} selected`
                  : `Default · ${selectedPdfTypes.length} types`}
              </span>
            </button>

            {showPdfTypes && (
              <div className="mt-2">
                <p className="text-[11px] text-muted-foreground">
                  Only papers the screen labelled with a ticked type get their
                  full text fetched. The default covers primary studies and
                  leaves out reviews, editorials, protocols and errata — there
                  are no trial results in those to extract.
                </p>

                {knownArticleTypes.length === 0 ? (
                  <p className="mt-2 text-[11px] text-amber-600">
                    Set the article-type vocabulary above first — these are
                    chosen from it.
                  </p>
                ) : (
                  <>
                    <div className="mt-2 grid gap-x-4 gap-y-1 sm:grid-cols-2">
                      {knownArticleTypes.map((t) => (
                        <label
                          key={t}
                          className="flex cursor-pointer items-center gap-2 text-xs"
                        >
                          <input
                            type="checkbox"
                            checked={effectivePdfTypes.includes(t)}
                            disabled={saveConfig.isPending}
                            onChange={(e) => togglePdfType(t, e.target.checked)}
                            className="h-3.5 w-3.5 rounded border-border accent-primary"
                          />
                          <span>{t}</span>
                        </label>
                      ))}
                    </div>

                    {selectedPdfTypes.length === 0 && (
                      <p className="mt-2 flex items-start gap-1.5 text-[11px] text-red-600">
                        <AlertTriangle
                          className="mt-0.5 h-3.5 w-3.5 shrink-0"
                          aria-hidden
                        />
                        <span>
                          Nothing is ticked, so no paper qualifies and the
                          download step will fetch nothing at all.
                        </span>
                      </p>
                    )}

                    {pdfTypesCustom && (
                      <button
                        onClick={() => saveConfig.mutate({ pdf_types: null })}
                        disabled={saveConfig.isPending}
                        className="mt-2 inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
                      >
                        <RotateCcw className="h-3 w-3" aria-hidden />
                        Reset to default
                      </button>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Optional taxonomies — not used by the default run. */}
        <div>
          <button
            onClick={() => setShowTaxonomies((v) => !v)}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            {showTaxonomies ? (
              <ChevronUp className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" aria-hidden />
            )}
            Optional taxonomies
          </button>
          {showTaxonomies && (
            <div className="mt-2 space-y-3">
              <p className="text-[11px] text-muted-foreground">
                These drive extra classifiers (drug class, study design,
                indication…) that the standard pipeline does not currently run.
                Setting them is only useful if you invoke those steps yourself.
              </p>
              {OPTIONAL_TAXONOMIES.map((t) => (
                <ListEditorCard
                  key={t.key}
                  title={t.label}
                  values={(cfgJson[t.key] as string[]) ?? []}
                  editing={editingList === t.key}
                  draft={listDraft}
                  isPending={saveConfig.isPending}
                  onStartEdit={() =>
                    startListEdit(t.key, (cfgJson[t.key] as string[]) ?? [])
                  }
                  onCancel={() => setEditingList(null)}
                  onDraftChange={setListDraft}
                  onSave={(lines) => saveConfig.mutate({ [t.key]: lines })}
                />
              ))}
            </div>
          )}
        </div>

        {/* Raw prompt overrides */}
        <div>
          <button
            onClick={() => setShowPrompts((v) => !v)}
            className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
          >
            {showPrompts ? (
              <ChevronUp className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" aria-hidden />
            )}
            Prompt templates
          </button>
          {showPrompts && (
            <div className="mt-2 space-y-3">
              <p className="text-[11px] text-muted-foreground">
                The exact instructions sent to the model. Placeholders in braces
                are substituted at run time and must be kept.
              </p>
              {prompts.isLoading && (
                <p className="text-xs text-muted-foreground">Loading…</p>
              )}
              {prompts.data?.map((p) => {
                const editing = editingPrompt === p.name;
                return (
                  <div
                    key={p.name}
                    className="rounded-lg border border-border bg-card p-3"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="min-w-0 text-sm font-medium">
                        {p.label}
                        <span
                          className={`ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                            p.is_override
                              ? "bg-amber-100 text-amber-700"
                              : "bg-muted text-muted-foreground"
                          }`}
                        >
                          {p.is_override ? "edited" : "default"}
                        </span>
                      </div>
                      <div className="flex shrink-0 items-center gap-1">
                        {editing ? (
                          <>
                            <button
                              onClick={() =>
                                savePromptMut.mutate({
                                  name: p.name,
                                  text: promptDraft,
                                })
                              }
                              disabled={savePromptMut.isPending}
                              className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-green-700 hover:bg-green-50 disabled:opacity-60"
                            >
                              {savePromptMut.isPending ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                              ) : (
                                <Check className="h-3.5 w-3.5" />
                              )}
                              Save
                            </button>
                            <button
                              onClick={() => setEditingPrompt(null)}
                              className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
                            >
                              <X className="h-3.5 w-3.5" />
                              Cancel
                            </button>
                          </>
                        ) : (
                          <>
                            {p.is_override && (
                              <button
                                onClick={() =>
                                  savePromptMut.mutate({
                                    name: p.name,
                                    text: null,
                                  })
                                }
                                title="Discard this review's edits and use the shipped prompt"
                                className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
                              >
                                <RotateCcw className="h-3.5 w-3.5" />
                                Revert
                              </button>
                            )}
                            <button
                              onClick={() => {
                                setEditingPrompt(p.name);
                                setPromptDraft(p.text);
                              }}
                              className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                              Edit
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      Required:{" "}
                      <span className="font-mono">
                        {p.required_placeholders.join(" ")}
                      </span>
                    </p>
                    {editing ? (
                      <textarea
                        value={promptDraft}
                        onChange={(e) => setPromptDraft(e.target.value)}
                        rows={14}
                        spellCheck={false}
                        className="mt-2 w-full resize-y rounded-md border border-border bg-background p-2 font-mono text-xs leading-relaxed"
                      />
                    ) : (
                      <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 p-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
                        {p.text}
                      </pre>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
