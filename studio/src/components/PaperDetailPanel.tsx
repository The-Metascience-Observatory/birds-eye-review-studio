"use client";

/** The paper-detail drawer: a right-side panel that opens when a user clicks an
 *  evidence-table row (replacing the old title link).
 *
 *  Data-backed sections only — title/journal/year, the design/N/RoB tags, the
 *  verdict/confidence chip, the abstract (fetched on demand), and the PICO
 *  breakdown. The publisher + Explorer links live in the header here. QA-workflow
 *  and formal AMSTAR-2 assessment (in the mockup) are intentionally omitted until
 *  those features exist.
 *
 *  The bigger pieces live in ./paper-detail/: the PDF pane (and the hook that
 *  fetches into it), the author line, the abstract body, and the extraction
 *  detail. */

import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, FileCode2, FileText, Loader2, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Abstract } from "@/components/paper-detail/AbstractBlock";
import { AuthorsBlock } from "@/components/paper-detail/AuthorsBlock";
import { ExtractionDetail } from "@/components/paper-detail/ExtractionDetail";
import { PdfPane, usePaperPdf } from "@/components/paper-detail/PdfPane";
import { getPaperAbstract, getPaperExtraction } from "@/lib/api";
import type { EvidenceRow } from "@/lib/api";
import { doiUrl, explorerUrl, stripTags, titleCase } from "@/lib/format";

export function PaperDetailPanel({
  reviewId,
  row,
  onClose,
}: {
  reviewId: string;
  row: EvidenceRow;
  onClose: () => void;
}) {
  const [showFullAbstract, setShowFullAbstract] = useState(false);
  const { pdfUrl, pdfLoading, openPdf, closePdf } = usePaperPdf(
    reviewId,
    row.doi,
    !!row.has_fulltext,
  );
  const dialogRef = useRef<HTMLElement>(null);

  // Dialog focus management: move focus in on open, give it back on close.
  // Restore only when focus is still inside the drawer (or nowhere) — if the
  // user closed it by clicking another row, that row keeps its focus.
  useEffect(() => {
    const opener =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialogRef.current?.focus({ preventScroll: true });
    const dialog = dialogRef.current;
    return () => {
      const active = document.activeElement;
      const focusLost = !active || active === document.body || !!dialog?.contains(active);
      if (focusLost && opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  // Escape closes the drawer (the PDF pane's iframe swallows keys while it has
  // focus, which is the browser's PDF viewer's business).
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const abstractQ = useQuery({
    queryKey: ["paper-abstract", reviewId, row.doi],
    queryFn: () => getPaperAbstract(reviewId, row.doi),
    enabled: !!row.doi,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const abstract = abstractQ.data?.abstract ?? null;

  // The full pass-2 record (all outcomes, per-arm results, verbatim quotes,
  // provenance) — fetched on demand once extraction has produced one.
  const extractionQ = useQuery({
    queryKey: ["paper-extraction", reviewId, row.doi],
    queryFn: () => getPaperExtraction(reviewId, row.doi),
    enabled: !!row.doi && !!row.extracted,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });
  const extractionRecords = extractionQ.data?.records ?? [];

  const design = titleCase(row.design_type || row.trial_type || "");
  const pico = row.pico;
  const verdict = row.verdict && row.verdict !== "unknown" ? row.verdict : null;
  const title = stripTags(row.title) || "Untitled";

  return (
    // Right-anchored flex row: when the PDF pane opens as a second child, the
    // container grows leftward, which is what slides the detail panel left.
    <div className="fixed inset-y-0 right-0 z-40 flex">
    <aside
      ref={dialogRef}
      tabIndex={-1}
      className="flex w-screen max-w-md flex-col border-l border-border bg-card shadow-xl focus:outline-none"
      role="dialog"
      aria-modal="true"
      aria-label={`Paper detail: ${title}`}
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Paper detail
        </span>
        <button
          type="button"
          onClick={onClose}
          className="text-muted-foreground hover:text-foreground"
          aria-label="Close"
        >
          <X size={18} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {/* Title + citation line */}
        <h2 className="text-lg font-bold leading-snug">{title}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {row.journal ? <em>{row.journal}</em> : "Unknown journal"}
          {row.year ? ` · ${row.year}` : ""}
        </p>
        <AuthorsBlock reviewId={reviewId} doi={row.doi} storedAuthors={row.authors} />
        {row.doi && (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
            <a
              href={doiUrl(row.doi) ?? undefined}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
            >
              Open at publisher
              <ExternalLink size={11} />
            </a>
            <a
              href={explorerUrl(row.doi) ?? undefined}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
            >
              Open at Metascience Observatory Explorer
              <ExternalLink size={11} />
            </a>
          </div>
        )}

        {/* Confidence chip */}
        {verdict && (
          <div className="mt-3">
            <span className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-xs font-medium text-amber-700 dark:text-amber-400">
              Result: {titleCase(verdict)}
            </span>
          </div>
        )}

        {/* Abstract */}
        <Section title="Abstract">
          {abstractQ.isLoading ? (
            <p className="text-xs text-muted-foreground">Loading…</p>
          ) : abstract ? (
            <Abstract
              text={abstract}
              expanded={showFullAbstract}
              onToggle={() => setShowFullAbstract((v) => !v)}
            />
          ) : (
            <p className="text-xs text-muted-foreground">
              No abstract available for this paper.
            </p>
          )}
        </Section>

        {/* Quick facts */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {design && <Tag>{design}</Tag>}
          <Tag>N = {row.n != null ? Number(row.n).toLocaleString() : "–"}</Tag>
          <Tag>RoB: {row.rob ? titleCase(row.rob) : "–"}</Tag>
          {/* When a full text (converted PDF) exists, offer to open the
              downloaded PDF; otherwise say the paper is abstract-only. */}
          {row.has_fulltext ? (
            <button
              type="button"
              onClick={() => openPdf()}
              disabled={pdfLoading}
              className="inline-flex items-center gap-1 rounded-full border border-blue-600/30 bg-blue-600/10 px-2 py-0.5 text-xs font-medium text-blue-700 hover:bg-blue-600/20 disabled:opacity-60 dark:text-blue-400"
            >
              {pdfLoading ? (
                <Loader2 size={11} className="animate-spin" />
              ) : (
                <FileText size={11} />
              )}
              Show downloaded PDF
            </button>
          ) : (
            row.has_fulltext != null && <Tag>Abstract only</Tag>
          )}
        </div>

        {/* PDF conversion quality remains visible when other sources exist. */}
        {(row.conversion_degraded || row.conversion_requires_review) && (
          <p
            className="mt-2 flex items-start gap-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] leading-relaxed text-amber-700 dark:text-amber-400"
            title={`Conversion mode: ${row.conversion_mode ?? "unknown"}`}
          >
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            <span>
              PDF conversion needs review. Some table values may be incomplete.
              {row.conversion_warnings?.length
                ? ` ${row.conversion_warnings.join("; ")}`
                : " Check the source tables before using extracted statistics."}
            </span>
          </p>
        )}

        {/* Available publisher markup may supply additional table evidence. */}
        {row.has_xml && (
          <p className="mt-2 flex items-start gap-1.5 rounded border border-emerald-500/40 bg-emerald-500/10 px-2 py-1.5 text-[11px] leading-relaxed text-emerald-700 dark:text-emerald-400">
            <FileCode2 size={12} className="mt-0.5 shrink-0" />
            <span>
              Publisher full-text XML is available for extraction, including
              structured tables where supplied.
              {row.has_supplement && " Supplementary files were downloaded too."}
            </span>
          </p>
        )}
        {!row.has_xml && row.has_supplement && (
          <p className="mt-2 text-[11px] text-muted-foreground">
            Supplementary files were downloaded for this paper.
          </p>
        )}

        {/* PICO breakdown */}
        {pico &&
          (pico.population ||
            pico.intervention.length > 0 ||
            pico.comparator.length > 0 ||
            pico.outcome.length > 0) && (
            <Section title="PICO breakdown">
              <dl className="space-y-3">
                <PicoRow label="Population" value={pico.population} />
                <PicoRow label="Intervention" value={pico.intervention.join(", ")} />
                <PicoRow label="Comparator" value={pico.comparator.join(", ")} />
                <PicoRow label="Outcome" value={pico.outcome.join("; ")} />
              </dl>
            </Section>
          )}

        {/* Full extraction detail — everything pass-2 captured, data-gated per
            subsection so lighter extraction tiers just show fewer blocks. */}
        {extractionRecords.length > 0 && (
          <Section title="Extraction">
            {extractionRecords.map((rec, i) => (
              <div key={rec.paper_id ?? i}>
                {extractionRecords.length > 1 && (
                  <p className="mb-1 mt-3 text-xs font-semibold text-muted-foreground">
                    Record {i + 1}
                  </p>
                )}
                <ExtractionDetail rec={rec} />
              </div>
            ))}
          </Section>
        )}
      </div>
    </aside>

    {pdfUrl && <PdfPane url={pdfUrl} onClose={closePdf} />}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-5 border-t border-border pt-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Tag({ children }: { children: ReactNode }) {
  return (
    <span className="rounded bg-muted px-2 py-0.5 text-xs text-foreground/70">
      {children}
    </span>
  );
}

function PicoRow({ label, value }: { label: string; value: string | null }) {
  return (
    <div className="grid grid-cols-[7rem_1fr] gap-3">
      <dt className="text-sm font-semibold">{label}</dt>
      <dd className="text-sm text-foreground/80">
        {value || <span className="text-muted-foreground">—</span>}
      </dd>
    </div>
  );
}
