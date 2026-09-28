---
name: genotyping-curation
description: Prepare and validate public genotyping datasets (VCF or genotype matrices from Dryad, Zenodo, Borealis…) for injection into Genovalia's data-explorer and metadata-api - per-individual samples CSV keyed on VCF IDs, DCAT JSON-LD, Semantic Engine OCA bundle, mapper, QC report and issue summary. Use when the requester asks to add, curate, prepare or validate a new genotyping dataset for Genovalia/Sedna, extend the "lot" of public datasets, or re-run the curation.
---

# Genotyping dataset curation (Genovalia)

Locked method, established on lot 1 (9 datasets, 2026-09-28). Follow it as written; deviations must be stated to the requester up front.

**Scope: prepare and validate only.** Never inject into data-explorer, metadata-api or the database. Uploading to Pydio or publishing the summary artifact happens only when the requester asks.

## Workspace

- Work in `~/genovalia-injection/` (outside git). One sub-folder per dataset ID; `SUMMARY.md` at the root.
- Reference implementation lives in `scripts/` next to this file. If the workspace is missing or older, copy them in: `build.py fetch.py papers.py said.py pydio_upload.py triage.py`.
- New datasets (lot 2+) go in **one module per dataset, `prep/<id>.py`** (`FILES`, optional `MANUAL`, `CFG(h)`, `prepare(raw, out, h)` with `h` = the build module); `build.py` and `fetch.py` load them. Never add new datasets inside `build.py`: modules let several agents work in parallel without conflicts.
- Whole catalogue: `triage.py` caches repository metadata of every record of `catalogue.json` in `triage/meta/`; `triage/tiers.tsv` classifies each record (DONE, EXISTS, A = VCF provided, B = matrix to convert, C = archive to inspect, MSAT, OUT, DUP, RESTRICTED) and `triage/ids.tsv` assigns IDs. Parallel agents get `references/agent_brief.md` plus their list of DOI → ID.
- venv: `python3 -m venv .venv && .venv/bin/pip install pandas openpyxl blake3 cyvcf2 requests pypdf python-docx questionary`.
- `build.py` clones `genovalia/metadonnees` into `.metadonnees/` (DCAT validator, keyword dictionary, existing schemas), reads the DB dump from `$GENOVALIA_DUMP` (ask the requester for the latest dump), and reads `$GENOVALIA_DATA_EXPLORER_REPO` (local checkout of `genovalia/data-explorer`, used to import `metadata-api`'s and `data-explorer-backend`'s own OCA/DCAT code for validation). Set both env vars before running.

Per-dataset output (flat, no sub-folders except `raw/`):
```
<id>/raw/                 untouched downloads + SHA256SUMS, raw/papers/ (+ supp/)
<id>/<id>.vcf             uncompressed (the data-explorer loader reads plain text; .vcf.gz fails)
<id>/<id>_samples.csv     one row per VCF sample, first column `id` = exact VCF sample name, UTF-8 without BOM
<id>/dcat.json            DCAT builder shape (buildObj) + "dct" prefix in @context
<id>/oca.json             oca_package/1.0 with valid SAIDs (build_oca)
<id>/mapper.json          metadonnees mapper, fr title/description written by hand
<id>/QC_REPORT.md
```

## Dataset ID

`<3 letters of genus><3 letters of species epithet><n>`, `spp` for multi-species (e.g. `malvil1`, `corspp1`). Check the dump and `.metadonnees/catalogue.json`: if the species already has IDs, take the next number; if the *same study* already exists (compare title, authors, counts), enrich that ID instead of creating a duplicate, bump `dcat:version` minor, keep `dcterms:issued`, add `dcterms:modified`, and keep every existing OCA attribute.

## Steps for each new dataset

1. **Locate files** through the repository API, never by guessing: Dryad metadata `https://datadryad.org/api/v2/datasets/<doi-encoded>` (downloads need auth → use the Zenodo mirror found by title search), Zenodo `api/records/<id>`, Borealis `api/datasets/:persistentId/?persistentId=doi:...`. Record authors/ORCIDs, license, related works.
2. **Download** by adding the dataset to `FILES` in `fetch.py`, then run it one process per dataset in parallel. Prefer the filtered/analysed genotype file over raw calls. Skip multi-GB raw files unless no filtered version exists.
3. **Papers**: open access via Europe PMC (`fullTextXML` + `supplementaryFiles` zip; PMC PDFs return 403), bioRxiv PDF `…/<doi>v1.full.pdf` and supplements listed at `…v1.supplementary-material`. Whatever cannot be fetched goes into `MANUAL_DOWNLOADS` in `build.py` with URL, destination and reason. Never guess a document's content.
4. **Inspect before mapping**: read READMEs, print real column names and value counts, count VCF samples/SNPs, check how sample IDs encode site/family. Look for per-individual attributes in: deposit tables → paper tables (Table 1/S1) → supplements.
5. **Write `prepare_<id>(raw, out)`** in `build.py` and a `DATASETS["<id>"]` entry. Reuse the helpers: `link` (expose a provided VCF without copying), `write_vcf`, `letters_to_rows` (letter matrices → VCF, REF = major allele), `_vcf_samples`, `_dna`. Put the meaning of every non-standard column in `extra` (type, label, English description, unit from `UCUM`), categorical codes in `entries`. Record every choice in `decisions`, every limitation in `minor`/`blocking`.
6. **Run** `.venv/bin/python build.py <id>` until it completes, then `build.py` for the full lot (regenerates `SUMMARY.md`).
7. **Verify independently** anything surprising: re-read raw values for a random sample of converted genotypes, recompute a suspicious rate straight from the raw file. A number that looks wrong is a finding to explain, not something to tune away.
8. **Report** with the issue taxonomy below; update the summary artifact when asked.

## Rules that encode past bugs (see references/lessons.md)

- Matrix conversions: prove the genotype coding against an independent source (a VCF subset, the README) before trusting it; state REF choice in `##source`.
- A header with one column fewer than the rows shifts pandas columns: check which column really holds the VCF ID (ID match must be 100 %).
- PDF tables via pypdf split negative numbers and long words across lines: normalise (`\n-\n` → ` -`), then assert the expected row count.
- Duplicate CHROM:POS is **blocking**: the loader keys variants on (CHROM, POS) and silently drops the 2nd marker's genotypes.
- Rejected-SNP pseudo-chromosomes, spreadsheet-damaged VCF headers, split per-country VCFs: repair/merge in `prepare_`, and say so.
- Never join attribute tables by row order when no ID links them: report as blocking, ask the authors.
- Keep replicates, outgroups and author-filtered individuals, flag them with a boolean/`organism` column instead of dropping.
- DCAT: `dcterms:identifier` = ID, `@id`/`landingPage` = `https://sedna.apps.genovalia.ulaval.ca/datasets/<id>`, `dcat:version` semver, GeoNames `https://sws.geonames.org/<id>/` and NCBI taxonomy IDs verified by request, ISO 19115 roles, `prov:agent` (not `prov:Agent`), license + distribution + `dct:relation` filled. Checked by metadonnees `check_dcat` and metadata-api `extract_dataset_facts`.
- OCA: attribute descriptions shared across datasets come from `ATTR`; SAIDs are Blake3-256/CESR over compact JSON in insertion order; `build.py` self-tests the SAID code on `lymdis1` before running.

## Issue taxonomy (SUMMARY.md, QC_REPORT.md, artifact)

- **Réglé** - fixed in the delivered files; say how.
- **Contourné** - usable thanks to a documented decision; the limitation remains.
- **Ouvert** - needs an action (document to fetch, authors to contact, fix before loading); mark **bloquant** when it prevents a reliable load.

## Deliverables beyond the files

- Tell the requester which keywords must be added to metadonnees `dictionary.json` and which platform findings are new.

### Pydio upload

`scripts/pydio_upload.py <workspace/path> [dataset_id ...] [--dry-run]` uploads every prepared dataset (VCF, CSV, DCAT/OCA/mapper JSON, QC report, raw papers) over WebDAV, keeping the same folder layout the summary artifact links to. Only the requester runs this — never search for or read the credentials yourself, and never invoke it unprompted.

```bash
export PYDIO_URL="https://<pydio-host>"        # e.g. https://pydio.apps.genovalia.ulaval.ca
export PYDIO_USERNAME="<username>"
export PYDIO_TOKEN="<personal access token>"    # not the account password
.venv/bin/python pydio_upload.py "<workspace>/<path>" --dry-run   # list what would upload, verify the byte sizes
.venv/bin/python pydio_upload.py "<workspace>/<path>"             # then actually upload
```

Re-runnable: a file already on Pydio is skipped only when its remote byte size matches the local one exactly, so a partial or changed file is always re-uploaded, never silently kept stale. Limit to specific datasets by appending their IDs after the path.

### Summary artifact update

The curated dataset list lives in a published artifact (one row per dataset, searchable/paginated tables, Pydio links per file). To refresh it after new datasets or new issue resolutions:
1. Recompute `issues.json`-derived stats (individuals, SNPs, issue counts by status) across all prepared datasets.
2. Regenerate the artifact's embedded dataset array from that data.
3. Republish to the **same artifact URL** (pass it explicitly) so the link stays stable — never publish without a URL when updating an existing artifact, or it creates a duplicate page.
4. Validate the embedded script's syntax before publishing (e.g. `node -e` on the extracted `<script>` block).

The artifact is private by default: remind the requester to share it (via the page's Share menu) with anyone else who needs to open the link.
