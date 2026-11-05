"""Render available trial PDFs for the model, with explicit page coverage."""
from pathlib import Path
import re

from ber_core.inspect_sources import digest


def prepare_visuals(records, out, max_pages=12):
    if max_pages < 0:
        raise ValueError("Visual page limit cannot be negative")
    try:
        import fitz
    except ImportError:
        fitz = None
    for record in records:
        record["visuals"] = {"status": "not_performed", "pages": [], "images": []}
        if not fitz or not max_pages or not record.get("paper_dir") or record["selection_status"] != "candidate":
            continue
        # INSPECT-SR was developed for RCTs; observational reports are synthesized
        # separately. Ambiguous quasi-experimental reports remain text-assessed.
        if str(record["design"]).casefold() not in {"rct", "crossover", "factorial"}:
            continue
        pdfs = sorted(Path(record["paper_dir"]).glob("*.pdf"))
        if len(pdfs) != 1:
            record["visuals"]["reason"] = "No unambiguous main PDF"
            continue
        pdf = pdfs[0]
        try:
            with fitz.open(pdf) as doc:
                if doc.page_count <= max_pages:
                    pages = list(range(doc.page_count))
                else:
                    # Prefer results/figure pages. A partial sample never becomes
                    # an affirmative all-figures clearance.
                    scores = []
                    for i, page in enumerate(doc):
                        text = page.get_text()
                        score = 3 * bool(re.search(r"\b(?:figure|fig\.|consort)\b", text, re.I))
                        score += bool(page.get_images()) + bool(re.search(r"\bresults\b", text, re.I))
                        scores.append((score, -i, i))
                    pages = sorted(x[2] for x in sorted(scores, reverse=True)[:max_pages])
                pdf_hash = digest(pdf.read_bytes())
                folder = Path(out) / "images" / pdf_hash[:20]
                folder.mkdir(parents=True, exist_ok=True)
                image_paths = []
                for i in pages:
                    page = doc[i]
                    path = folder / f"page-{i+1}.png"
                    if not path.exists():
                        # 120 dpi retains table/chart labels without giant inputs.
                        page.get_pixmap(matrix=fitz.Matrix(120/72, 120/72), alpha=False).save(path)
                    image_paths.append(str(path.resolve()))
                    record["sources"][f"image_page_{i+1}"] = {
                        "description": f"Rendered PDF page {i+1}", "pdf_sha256": pdf_hash,
                        "page_text": page.get_text(), "image_sha256": digest(path.read_bytes()),
                    }
                record["visuals"] = {"status": "complete_main_pdf" if len(pages) == doc.page_count else "partial_main_pdf",
                                     "pages": [i+1 for i in pages], "total_pages": doc.page_count,
                                     "images": image_paths, "pdf": str(pdf), "pdf_sha256": pdf_hash,
                                     "limitation": "AI visual screening is not specialist image forensics or an external image-duplication search. Supplements and other publications may be missing."}
        except Exception as exc:
            record["visuals"] = {"status": "unavailable", "pages": [], "images": [], "error": type(exc).__name__}
    return records
