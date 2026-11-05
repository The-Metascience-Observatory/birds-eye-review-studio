"use client";

/** The downloaded-PDF pane beside the paper-detail drawer, and the hook that
 *  fetches the PDF into it. */

import { ExternalLink, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { getPaperPdfObjectUrl } from "@/lib/api";
import { usePref } from "@/lib/prefs";
import { toast } from "@/lib/toast";

/** The pane needs room beside the drawer; narrower viewports open a new tab. */
const WIDE_ENOUGH = "(min-width: 1024px)";

export interface PaperPdf {
  /** Blob object URL shown in the pane; null = pane closed. */
  pdfUrl: string | null;
  pdfLoading: boolean;
  /** Fetch and show the PDF. `auto` = fired by the "show PDF automatically"
   *  preference rather than a click: stay silent on failure, never pop a tab. */
  openPdf: (auto?: boolean) => Promise<void>;
  closePdf: () => void;
}

/**
 * Fetch the downloaded PDF with auth (the endpoint needs a Bearer header a
 * plain navigation can't send) and own the resulting blob URL.
 *
 * Every request is tagged with the DOI it was made for and an AbortController.
 * Switching papers (or unmounting) aborts the in-flight request, and a result
 * that still arrives for a paper that is no longer shown is revoked and
 * dropped — so paper A's PDF can never land beside paper B's detail, however
 * the two fetches interleave.
 */
export function usePaperPdf(
  reviewId: string,
  doi: string,
  hasFulltext: boolean,
): PaperPdf {
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfLoading, setPdfLoading] = useState(false);
  const inFlight = useRef<{ doi: string; ctrl: AbortController } | null>(null);
  // The DOI currently displayed, readable from async continuations.
  const currentDoi = useRef(doi);
  currentDoi.current = doi;

  // The pane owns its blob URL (unlike the new-tab path, which leaves the URL
  // for the new tab to own): revoke it whenever it's replaced, the pane
  // closes, or the panel unmounts.
  useEffect(() => {
    return () => {
      if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    };
  }, [pdfUrl]);

  // New paper (or unmount): abandon any fetch for the old one and close the
  // pane, so the previous paper's PDF never shows beside this one.
  useEffect(() => {
    setPdfUrl(null);
    setPdfLoading(false);
    return () => {
      inFlight.current?.ctrl.abort();
      inFlight.current = null;
    };
  }, [doi, reviewId]);

  const openPdf = useCallback(
    async (auto = false) => {
      // One request per paper at a time (the auto-open and a click can race).
      if (inFlight.current?.doi === doi) return;
      inFlight.current?.ctrl.abort();
      const ctrl = new AbortController();
      const req = { doi, ctrl };
      inFlight.current = req;
      setPdfLoading(true);
      const stale = () => ctrl.signal.aborted || currentDoi.current !== req.doi;
      try {
        const url = await getPaperPdfObjectUrl(reviewId, doi, ctrl.signal);
        if (stale()) {
          if (url) URL.revokeObjectURL(url);
          return;
        }
        if (!url) {
          if (!auto) toast.error("No downloaded PDF for this paper yet.");
          return;
        }
        if (window.matchMedia(WIDE_ENOUGH).matches) {
          setPdfUrl(url); // the effect above revokes any previous URL
        } else if (auto) {
          URL.revokeObjectURL(url);
        } else {
          // The object URL is left for the new tab to own.
          window.open(url, "_blank", "noopener,noreferrer");
        }
      } catch {
        if (!auto && !stale()) toast.error("Could not open the PDF.");
      } finally {
        if (inFlight.current === req) {
          inFlight.current = null;
          setPdfLoading(false);
        }
      }
    },
    [reviewId, doi],
  );

  // The settings preference: open the PDF pane as soon as the detail view
  // opens (or once full text arrives). Keyed on the paper + the availability
  // signal, NOT on pdfUrl — closing the pane by hand must not reopen it.
  const [autoOpenPdf] = usePref("autoOpenPdf");
  useEffect(() => {
    if (!autoOpenPdf || !hasFulltext) return;
    if (!window.matchMedia(WIDE_ENOUGH).matches) return;
    void openPdf(true);
  }, [autoOpenPdf, hasFulltext, openPdf]);

  const closePdf = useCallback(() => setPdfUrl(null), []);

  return { pdfUrl, pdfLoading, openPdf, closePdf };
}

/** The pane itself, to the detail panel's right. Its own "×" closes just this
 *  pane; closing the detail panel unmounts both. `hidden lg:flex` guards a
 *  resize-down after opening — openPdf only routes here on lg+. */
export function PdfPane({ url, onClose }: { url: string; onClose: () => void }) {
  return (
    <section
      className="hidden w-[min(48rem,50vw)] flex-col border-l border-border bg-card shadow-xl lg:flex"
      aria-label="Downloaded PDF"
    >
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Downloaded PDF
        </span>
        <div className="flex items-center gap-3">
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="text-muted-foreground hover:text-foreground"
            title="Open in a new tab"
            aria-label="Open PDF in a new tab"
          >
            <ExternalLink size={15} />
          </a>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
            aria-label="Close PDF"
          >
            <X size={18} />
          </button>
        </div>
      </div>
      {/* `sandbox` with no allow-* tokens: a blob: URL inherits THIS page's
          origin, and the PDFs are third-party files (publisher downloads and
          user uploads alike). Without the attribute a crafted PDF that the
          browser chose to treat as active content would run same-origin, with
          reach into the session token in localStorage. The built-in PDF
          viewer needs none of the tokens, so denying all of them costs
          nothing here. */}
      <iframe src={url} title="Downloaded PDF" sandbox="" className="h-full w-full flex-1" />
    </section>
  );
}
