# Third-party material

## INSPECT-SR guidance

`steps/systematic_review.py` and `ber_core/inspect_sr.py` use the INSPECT-SR
trustworthiness-assessment guidance (version 1.1.2), which is downloaded on demand
by `scripts/download_inspect_sr.py` into `docs/inspect_sr/upstream/` and is not
redistributed in this repository.

- Source: https://github.com/ianhussey/inspect-sr-guidance
- © Jack Wilkinson. Guidance text licensed CC BY 4.0; code MIT.
- Cite: Wilkinson J, et al. INSPECT-SR. doi:10.1101/2025.09.03.25334905.

## Data sources

The pipeline queries public scholarly APIs: for search, PubMed/Entrez, OpenAlex,
Europe PMC, Semantic Scholar, CyberLeninka and LessWrong; for abstracts and
metadata, also Crossref, DataCite, arXiv, Unpaywall, BASE, Scopus, Dimensions,
CORE, DBLP and OpenCitations; for the INSPECT-SR step, Retraction Watch (via
Crossref) and PubPeer. Full-text download goes through the separate `fetchpdf`
package and its own sources. Use of each service is subject to its provider's
terms; API keys are supplied by the user (see `.env.example`).

## Optional components

`fetchpdf` and `pdf4llm` (PDF download and conversion) are separate repositories
with their own licenses. PDF conversion can use Docling (MIT), GROBID
(Apache-2.0) and PyMuPDF (AGPL-3.0, optional); none of them are bundled here.
