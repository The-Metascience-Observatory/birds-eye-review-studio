/** Outbound link resolution for a trial/paper id. Shared by the forest plot and
 *  results table. Real DOIs -> doi.org; PMIDs / trial-registry ids -> their
 *  canonical page; non-resolvable ids (grey literature) -> null (no link). */
export function trialHref(paperId: string | undefined): string | null {
  if (!paperId) return null;
  if (paperId.startsWith("10.")) return `https://doi.org/${paperId}`;
  if (paperId.startsWith("pmid_")) return `https://pubmed.ncbi.nlm.nih.gov/${paperId.slice(5)}/`;
  if (paperId.startsWith("nct:")) return `https://clinicaltrials.gov/study/${paperId.slice(4)}`;
  return null;
}
