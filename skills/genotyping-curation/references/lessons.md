# Lessons from lot 1 (2026-09-28)

Concrete cases behind the rules in SKILL.md. Read the entry for a format before handling a similar dataset.

## Access

| Source | What works | What fails |
|---|---|---|
| Dryad | metadata API `api/v2/datasets/<doi>` and `/versions/<n>/files` | file download (401, `downloads/file_stream` 403) → same files on the Zenodo mirror (search Zenodo by exact title) |
| Zenodo | `api/records/<id>/files/<name>/content` | - |
| Borealis | `api/access/datafile/<id>?format=original` | - |
| Europe PMC | `rest/<PMCID>/fullTextXML`, `rest/<PMCID>/supplementaryFiles` (zip) | `europepmc.org/articles/<PMCID>?pdf=render` (403) |
| bioRxiv | `content/10.1101/<doi>v1.full.pdf`, supplementary list page, some supplement PDFs | docx/xlsx supplements often blocked (anti-bot) → MANUAL_DOWNLOADS |
| Wiley/Nature/MDPI | - | paywalled or 403 → MANUAL_DOWNLOADS |
| ORCID | `pub.orcid.org/v3.0/<id>/person` to confirm a name | duplicates exist (Hugo Cayuela has two iDs) |
| GeoNames | `sws.geonames.org/<id>/about.rdf` to confirm a name | search API needs an account (demo quota exhausted) |

## Formats met and how they were handled

- **malvil1**: VCF from Stacks on an unpublished draft genome. Deposit numbers (1310 × 25 904) ≠ file (1474 × 17 436): reported, not "fixed". POP.xlsx lineage `GREE` vs paper `GRE`. Site table only in the preprint (Table 1); 3 sites absent. Existing metadata-api record enriched (v1.1.0).
- **picmar2**: dartR genlight CSV, value = count of the ALT allele from the SNP name `CloneID-offset-REF/ALT`, proven against the PLINK VCF subset (58 295/58 295). Metadata TSV header one column short → pandas shifts columns; the real ID is `id`, not `Sample_ID`. Outgroup (Picea rubens) and technical replicates kept and flagged.
- **prusal1**: VCF saved through a spreadsheet (trailing tabs, `""` quotes) → repaired; SNPs on pseudo-chromosome 9 = QC rejects → removed; 8 duplicate positions.
- **anogla1**: two VCFs (per country) with identical variant rows → merged column-wise after asserting identical CHROM/POS/ID/REF/ALT. `sample_list.txt` FILTER column → `relatedness_filtered`.
- **salfon1**: no individual table; site = ID prefix; Table 1 of the preprint parsed from PDF (negatives on their own line, "anadro\nmous").
- **homame1**: site coordinates only in the bioRxiv supplement PDF (Table S1); VCF prefix `T2X` = Table S1 `T2P5` (only unmatched site, same n = 36). Sea-surface temperature file has no site column → use Table S1.
- **picgla2**: letter matrix `A:G`, `N` missing, `I/D` indels → excluded; no positions (CHROM = marker, POS = 1).
- **picsit1**: letter tables with CHR/POS rows (RAD pseudo-positions); three alternative tables, the largest converted; 45 % missing is real.
- **triaes1**: HapMap (`rs#, alleles, chrom, pos, strand, samples…`), `NN` missing; phenotypes have no individual ID (not joined); 1 367 duplicate positions from redundant array probes.

### Lot 2 (extended catalogue, ~80 datasets)

- **Non-nucleotide allele codes with no documented convention** (genepop 01-04, Arlequin 1-4, rubias 1/2, plain 0/1/2 without a legend): never guess a real base. Write `REF="A"`, `ALT="C"` as explicit placeholders and say so in `##source`; keep the true genotype pattern (hom-ref/het/hom-alt) intact. When the convention *is* provable — a Stacks/PGDSpider genepop export documented in the paper's Methods, flanking sequences deposited alongside the SNP table (gadmor1), or cross-checking against another file of the same dataset that already carries real bases — use the real base and say how it was proven.
- **Prove a codebook, don't infer it from population sizes**: when a paper's Table 1/S1 gives per-population or per-site counts that exactly match the deposit's undocumented population codes, that is real proof (checked count-by-count, not just "plausible"); a majority-vote or single-locus guess is not — leave those `open` for the authors.
- **Same study, several repositories**: Dryad and its own Borealis/Zenodo mirror carry identical files (skip re-processing, just alias); but two different papers from the same lab on the same species can share individuals under different ID conventions (lot 1 homame1 vs lot 2 homame2-homame6, salsal3/salsal7). Prove overlap with genotype identity on shared markers (≥ ~95% match = same individual, not just adjacency in an ID scheme), never by name similarity alone, and flag it as a `shared_individual_of`/boolean column rather than merging or dropping.
- **Same dataset genotyped twice** (GBS vs Rapture, RAD vs SNP-chip): keep as two separate dataset IDs when the marker panels differ, even if largely the same individuals — merging would either duplicate individuals or duplicate CHROM:POS. If the two panels' sample labels disagree for the same physical animal (mislabelling), that is a real, reportable problem, not something to silently fix.
- **Multi-species deposits**: when a single file mixes several species analysed *together* (shared clustering/analysis), use one `spp` dataset; when the deposit contains species analysed *separately* (disjoint marker sets, no joint analysis), split into one dataset per species even if a single ID was assigned ahead of time — check the new ID against `existing_ids()` and the workspace before creating it.
- **A result derived from genotypes (heterozygosity 0/1 code, inversion karyotype class, fragment-length polymorphism, microsatellite allele) is not a genotype**: classify it out of scope rather than force a VCF out of it, even when the file is named `*_SNP*.txt`.
- **PLINK / TPED-TFAM / PED-MAP**: allele columns already carry real bases when the platform is a genotyping chip (perman1, ovican2); positions on a short/fragmented assembly can put unrelated markers at the same CHROM:POS — deduplicate them exactly like duplicate VCF positions, and say why (assembly fragmentation, not a data error).
- **Haploid or ambiguous-ploidy genotypes** (an ancestral-state row, a haploid marker in a diploid VCF): `vcf_parser.py`/cyvcf2 assumes diploid GT and will misread a haploid call as heterozygous or as N/REF — write them as homozygous diploid before delivery and note the workaround (picabi1).
- **A repository's own file naming can mislabel a site** when two files (e.g. an environmental table and a genotype table) use different codes for the same site: cross-check by matching sample count, not just the code string, before accepting a join (salsal6 CWB→USW).
- **Inferred codes must be flagged, not buried in `decisions`** (angang2, 2026-09-29): the deposit's ID prefixes (BG, CAG, GG, LEG, RHG, VG) were mapped to sites "with the same codes as angang4 (Table 4)", but angang4's Table 4 holds no codes at all — the codes were angang4's own ID prefixes, matched to Table 4 by site name. The mapping was right, yet the report gave a false source, called ICG/MOG "probably Iceland and a Scandinavian site" (actually Iceland and Morocco), and a reviewer took `LG` in the paper's Table 1 for a population missing from the VCF. This study's Table 1 (open PDF on VLIZ, Wiley blocked) uses ICE/LG/MOR for the deposit's ICG/LEG/MOG; all 8 N matched count-by-count. Each such mapping now gets its own issue with its evidence.

## Platform facts verified in code

Informational only — **never** a reason to skip preparing a dataset. Prepare it, note the limitation as a problem, let the requester decide.

- `data-explorer-backend/app/services/vcf_parser.py`: strips `##` lines except fileformat, drops non-digit POS, reads the file as UTF-8 text (so `.vcf.gz` fails despite being "accepted"), requires every VCF sample to exist in `dataset_sample.sample_id`, upserts variants on (chrom, pos).
- `data-explorer-backend/app/repository/dataset.py`: samples CSV must have `id` (lowercase), no UTF-8 BOM; lowercases string values except id; capitalises country and organism.
- `metadata-api/app/services/dcat.py` reads `dct:relation` and `dct:*` inside distributions → declare `dct` in `@context`.
- OCA bundle `v` = `OCAS11JSON` + hex length of the compact bundle JSON + `_`; package, bundle, capture base, overlays and ADC extension overlays each carry a SAID.

## Documents fetched manually by the requester

- The requester drops them in `~/Downloads` with the server's generic names (`media-1.docx`, `media-1 (1).docx`, `media-4.xlsx`…). Identify each one by its content (title paragraph, table headers), never by name, then copy it to the `dest` listed in `MANUAL_DOWNLOADS` and remove that entry.
- Re-read every open issue against the new document and change its status explicitly: lot 1 closed malvil1 units (Table S14), salfon1 environment (Suppl. Table 1 xlsx, coordinates identical to the PDF), picgla2 site/years/units (Heredity), and gave picmar2 an independent coordinate check (1467/1467).
- A data availability statement ("available in the manuscript") does not guarantee per-individual data: Horticulturae 2025 only shows distributions. Keep the issue open and name the contact.
- When a paper's filter uses a field that is not deposited (GenTrain score), reproduce the other filters and report the count instead of forcing the published number.
