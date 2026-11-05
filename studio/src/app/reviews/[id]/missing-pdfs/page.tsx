"use client";

/** The "missing PDFs" worklist: papers we want to include (screened in / pending)
 *  that have no full text yet. Each row shows biblio, a manual-download link (the
 *  DOI resolver / publisher page), an Upload button that files the PDF and kicks
 *  off conversion (after which the paper gains full text and drops off), and a
 *  "skip / couldn't get" button that excludes the paper but keeps the
 *  row visible (greyed) as a record of what couldn't be obtained. */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, Loader2, Upload, XCircle } from "lucide-react";
import { toast } from "@/lib/toast";

import { excludePapers, getEvidence, uploadPaperPdf } from "@/lib/api";
import type { EvidenceRow } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { doiUrl, safeHttpUrl, stripTags } from "@/lib/format";

/** The manual-download target: a non-doi.org stored url, else the DOI resolver
 *  (rebuilt clean), else PubMed.
 *
 *  The stored `url` is third-party enrichment metadata rendered as an href, so
 *  it only passes through `safeHttpUrl` — a `javascript:` (or any non-http)
 *  value is dropped rather than linked. */
function manualUrl(r: EvidenceRow): string | undefined {
  const stored = safeHttpUrl(r.url);
  const isDoiOrg = !!stored && /(^|\.)doi\.org$/i.test(new URL(stored).hostname);
  if (stored && !isDoiOrg) return stored;
  if (r.doi) return doiUrl(r.doi) ?? undefined;
  if (stored) return safeHttpUrl(stored.replace(/--/g, "/"));
  if (r.pmid && /^\d+$/.test(String(r.pmid))) {
    return `https://pubmed.ncbi.nlm.nih.gov/${r.pmid}/`;
  }
  return undefined;
}

function leadAuthors(authors?: string): string {
  const raw = (authors || "").trim();
  if (!raw) return "—";
  const parts = raw.split(/;\s*/).filter(Boolean);
  return parts.length > 2 ? `${parts[0]}, ${parts[1]}, et al.` : parts.join(", ");
}

export default function MissingPdfsPage() {
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const { isAuthenticated, ready } = useAuth();

  // DOIs the user marked "couldn't get". Skipping excludes the paper server-side
  // (screen_state → excluded), which would otherwise drop it from the list — but
  // we KEEP skipped rows visible (greyed) so the user can track what they missed.
  const [skippedDois, setSkippedDois] = useState<Set<string>>(new Set());
  const markSkipped = (doi: string) =>
    setSkippedDois((prev) => new Set(prev).add(doi));

  const evidence = useQuery({
    queryKey: ["evidence", id],
    queryFn: () => getEvidence(id),
    // Gate like every sibling query: before the layout's auth redirect lands,
    // an ungated query would 401 and retry on the poll loop below.
    enabled: ready && isAuthenticated && !!id,
    // A convert job flips has_fulltext; poll so rows drop off as PDFs land.
    refetchInterval: 4000,
  });

  const missing = (evidence.data?.rows ?? []).filter(
    (r) =>
      // Still wanted + no full text, OR a row the user skipped this session
      // (which is now `excluded` server-side but stays shown as a record).
      skippedDois.has(r.doi) ||
      ((r.screen_state === "included" || r.screen_state === "pending") &&
        !r.has_fulltext),
  );
  const outstanding = missing.filter((r) => !skippedDois.has(r.doi)).length;

  return (
    <main className="mx-auto max-w-5xl px-4 py-10">
      <Link
        href={`/reviews/${id}/stages/extract`}
        className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft size={14} /> Back to Extract
      </Link>
      <h1 className="mt-2 text-2xl font-bold">Missing full text</h1>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">
        These papers are wanted for extraction but we couldn&apos;t fetch their
        full text automatically. Open the source to download the PDF by hand, then
        upload it here — it&apos;s converted and the paper joins the extraction set.
      </p>

      {evidence.isLoading ? (
        <p className="mt-6 text-sm text-muted-foreground">Loading…</p>
      ) : missing.length === 0 ? (
        <div className="mt-6 rounded-lg border border-border bg-card p-8 text-center">
          <p className="text-muted-foreground">
            No missing PDFs — every wanted paper has full text. 🎉
          </p>
        </div>
      ) : (
        <div className="mt-6 space-y-2">
          <p className="text-sm text-muted-foreground">
            {outstanding} paper{outstanding === 1 ? "" : "s"} missing full text
            {skippedDois.size > 0 && ` · ${skippedDois.size} skipped`}
          </p>
          <ul className="divide-y divide-border rounded-lg border border-border bg-card">
            {missing.map((r) => (
              <MissingRow
                key={r.doi}
                reviewId={id}
                row={r}
                skipped={skippedDois.has(r.doi)}
                onSkipped={() => markSkipped(r.doi)}
                onChanged={() => qc.invalidateQueries({ queryKey: ["evidence", id] })}
              />
            ))}
          </ul>
        </div>
      )}
    </main>
  );
}

function MissingRow({
  reviewId,
  row,
  skipped,
  onSkipped,
  onChanged,
}: {
  reviewId: string;
  row: EvidenceRow;
  skipped: boolean;
  onSkipped: () => void;
  onChanged: () => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploaded, setUploaded] = useState(false);
  const href = manualUrl(row);

  const upload = useMutation({
    mutationFn: (file: File) => uploadPaperPdf(reviewId, row.doi, file),
    onSuccess: () => {
      setUploaded(true);
      toast.success("PDF uploaded — converting…");
      onChanged();
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Upload failed"),
  });

  // "Couldn't get it" — drop the paper from the wanted set. This excludes it
  // (durable `user_exclusions.json`) so it won't hold up extraction, but the row
  // STAYS shown (greyed, via the lifted skippedDois set) as a visual record of
  // what couldn't be obtained.
  const skip = useMutation({
    mutationFn: () => excludePapers(reviewId, [row.doi], "full text unavailable"),
    onSuccess: () => {
      onSkipped();
      toast.success("Skipped — removed from the extraction set");
      onChanged();
    },
    onError: (e) => toast.error((e as Error)?.message ?? "Couldn't skip"),
  });

  const busy = upload.isPending || skip.isPending;
  const uploadDisabled = busy || uploaded || skipped;

  return (
    <li
      className={`flex flex-wrap items-start justify-between gap-3 px-4 py-3 ${
        skipped ? "bg-muted/40 opacity-60" : ""
      }`}
    >
      <div className="min-w-0 flex-1">
        <div className="font-medium">
          {row.title ? stripTags(row.title) : row.doi || "—"}
        </div>
        <div className="mt-0.5 text-sm text-muted-foreground">
          {leadAuthors(row.authors)}
          {row.journal ? (
            <>
              {" · "}
              <em>{row.journal}</em>
            </>
          ) : (
            ""
          )}
          {row.year ? ` · ${row.year}` : ""}
        </div>
        {href && !skipped && (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="mt-1 inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
          >
            <ExternalLink size={12} /> Open source to download
          </a>
        )}
        {skipped && (
          <div className="mt-1 text-xs text-muted-foreground">
            Skipped — couldn&apos;t obtain the full text.
          </div>
        )}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload.mutate(f);
            e.target.value = ""; // allow re-selecting the same file
          }}
        />
        <button
          type="button"
          disabled={uploadDisabled}
          onClick={() => fileRef.current?.click()}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-accent disabled:opacity-50"
        >
          {upload.isPending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <Upload size={14} />
          )}
          {uploaded ? "Converting…" : upload.isPending ? "Uploading…" : "Upload PDF"}
        </button>
        <button
          type="button"
          disabled={skipped || busy || uploaded}
          onClick={() => skip.mutate()}
          title="We couldn't obtain this PDF — drop it from the extraction set"
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium text-muted-foreground hover:bg-accent disabled:opacity-50"
        >
          {skip.isPending ? (
            <Loader2 size={14} className="animate-spin" />
          ) : (
            <XCircle size={14} />
          )}
          {skipped ? "Skipped" : "Skip / couldn't get"}
        </button>
      </div>
    </li>
  );
}
