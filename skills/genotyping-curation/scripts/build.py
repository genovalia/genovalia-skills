"""Build injection-ready input files (DCAT, OCA, samples CSV, VCF) for public genotyping
datasets and validate their quality. One sub-folder per dataset, SUMMARY.md at the root.

Run: .venv/bin/python build.py [dataset_id ...]   (raw files come from fetch.py / papers.py)
Nothing is injected anywhere: this only writes files under this folder.
"""
import copy, gzip, io, json, math, os, re, statistics, subprocess, sys, tempfile, traceback
from collections import Counter
from pathlib import Path

import pandas as pd
import requests

from said import said

ROOT = Path(__file__).resolve().parent
TODAY = __import__("datetime").date.today().isoformat()
METADONNEES = ROOT / ".metadonnees"  # clone of genovalia/metadonnees (validator, dictionary, existing schemas)
if not METADONNEES.exists():
    subprocess.run(["gh", "repo", "clone", "genovalia/metadonnees", str(METADONNEES), "--", "-q"], check=True)
SEDNA = "https://sedna.apps.genovalia.ulaval.ca/datasets/"
CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
CCBY = "https://creativecommons.org/licenses/by/4.0/"
OPEN = "http://purl.org/eprint/accessRights/OpenAccess"
MISSING_RATE_FLAG = 0.20

# Sources that could not be fetched automatically. Drop the file at `dest` and re-run.
MANUAL_DOWNLOADS = []  # filled from the MANUAL rows of prep/<id>.py


# ─────────────────────────────── VCF ───────────────────────────────

def _open_text(path):
    return gzip.open(path, "rt") if str(path).endswith(".gz") else open(path, encoding="utf-8", errors="replace")


GT_RE = re.compile(r"^[0-9.]+([/|][0-9.]+)?$")


def vcf_stats(path):
    """One text pass: header info, sample IDs, per-sample/per-SNP missingness, heterozygosity,
    and everything the data-explorer loader (vcf_parser.py) would choke on."""
    s = dict(meta=[], samples=[], n_snps=0, non_numeric_pos=0, multiallelic=0, bad_gt=0,
             non_acgt=0, dup_pos=0, chroms=Counter(), snp_missing=[])
    seen = set()
    with _open_text(path) as f:
        for line in f:
            if line.startswith("##"):
                if len(s["meta"]) < 200: s["meta"].append(line.rstrip("\n"))
                continue
            if line.startswith("#"):
                s["samples"] = line.rstrip("\n").split("\t")[9:]
                n = len(s["samples"]); miss = [0] * n; het = [0] * n; called = [0] * n
                continue
            fld = line.rstrip("\n").split("\t")
            if not fld[1].isdigit():
                s["non_numeric_pos"] += 1
                continue
            s["n_snps"] += 1
            s["chroms"][fld[0]] += 1
            key = (fld[0], fld[1])
            if key in seen: s["dup_pos"] += 1
            seen.add(key)
            if "," in fld[4]: s["multiallelic"] += 1
            if not re.fullmatch(r"[ACGTN]+", fld[3]) or not re.fullmatch(r"[ACGTN.,]+", fld[4]): s["non_acgt"] += 1
            m = 0
            for i, g in enumerate(fld[9:]):
                gt = g.split(":", 1)[0]
                if gt[0] == ".":
                    miss[i] += 1; m += 1
                    continue
                if not GT_RE.match(gt):
                    s["bad_gt"] += 1
                    continue
                called[i] += 1
                a = re.split(r"[/|]", gt)
                if len(a) == 2 and a[0] != a[1]: het[i] += 1
            s["snp_missing"].append(m / max(n, 1))
    n = max(s["n_snps"], 1)
    s["sample_missing"] = dict(zip(s["samples"], (x / n for x in miss)))
    s["sample_het"] = dict(zip(s["samples"], (h / c if c else float("nan") for h, c in zip(het, called))))
    return s


def loader_check(path):
    """Replicate data-explorer-backend/app/services/vcf_parser.py: clean the file the same way,
    open it with cyvcf2, count variants, and collect [E::...] errors from htslib's stderr."""
    code = r'''
import sys, os
from cyvcf2 import VCF
v = VCF(sys.argv[1]); n = sum(1 for _ in v); print(len(v.samples), n)
'''
    with tempfile.NamedTemporaryFile("w", suffix=".vcf", delete=False, dir=ROOT / ".tmp") as out, _open_text(path) as f:
        for line in f:
            if line.startswith("##fileformat="): out.write(line)
            elif line.startswith("##"): continue
            elif line.startswith("#"): out.write(line)
            else:
                fld = line.split("\t", 2)
                if len(fld) > 1 and fld[1].isdigit(): out.write(line)
        tmp = out.name
    try:
        p = subprocess.run([sys.executable, "-c", code, tmp], capture_output=True, text=True)
    finally:
        os.remove(tmp)
    errors = [l for l in p.stderr.splitlines() if l.startswith(("[E::vcf_parse]", "[E::bcf_hdr_add_sample_len]"))]
    if p.returncode != 0 and not errors:
        errors = [l for l in p.stderr.splitlines() if l.strip()][-3:]
    out = p.stdout.split()
    return dict(ok=p.returncode == 0 and not errors, errors=errors,
                samples=int(out[0]) if out else None, variants=int(out[1]) if out else None)


UNMAPPED = "unmapped"


def write_vcf(path, samples, rows, source):
    """rows: iterable of (chrom, pos, id, ref, alt, [GT strings]).
    Markers without genomic positions (every POS = 1, CHROM = marker) go on one pseudo-chromosome UNMAPPED
    with sequential POS 1..n, the marker name kept in ID: one contig per marker makes htslib/cyvcf2 (and the
    data-explorer loader, which strips ##contig lines) quadratic (angang2: 445k contigs, ~30 min vs 4 s)."""
    rows = list(rows)
    if rows and all(int(r[1]) == 1 for r in rows) and len({r[0] for r in rows}) > 1:
        rows = [(UNMAPPED, i, chrom if vid in ("", ".", chrom) else f"{chrom};{vid}", ref, alt, gts)
                for i, (chrom, pos, vid, ref, alt, gts) in enumerate(rows, 1)]
        source += f"; then, since there are no genomic positions, every marker moved to CHROM = {UNMAPPED}, POS = marker rank (no genomic meaning), the former CHROM (marker name) in ID"
    with open(path, "w") as f:
        f.write("##fileformat=VCFv4.2\n")
        f.write(f"##fileDate={TODAY.replace('-', '')}\n")
        f.write(f'##source="genovalia build.py conversion from {source}"\n')
        f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples) + "\n")
        for chrom, pos, vid, ref, alt, gts in rows:
            f.write(f"{chrom}\t{pos}\t{vid}\t{ref}\t{alt}\t.\t.\t.\tGT\t" + "\t".join(gts) + "\n")


CONCORDANCE_MIN = 0.95


def _bases(ref, alt, gt):
    """Unordered pair of allele bases of a GT, or None when missing."""
    al = [ref] + alt.split(",")
    a = re.split(r"[/|]", gt.split(":", 1)[0])
    if len(a) != 2 or "." in a or not all(x.isdigit() and int(x) < len(al) for x in a): return None
    return tuple(sorted((al[int(a[0])], al[int(a[1])])))


_COMP = str.maketrans("ACGT", "TGCA")


def _concordance(x, y):
    """Share of individuals with the same genotype in two markers, reading the second on either strand
    (Illumina fwd/rev probes of one SNP report complementary bases, e.g. A/G vs T/C)."""
    both = [(a, b) for a, b in zip(x, y) if a and b]
    if not both: return 0.0
    same = sum(a == b for a, b in both)
    comp = sum(a == tuple(sorted(c.translate(_COMP) for c in b)) for a, b in both)
    return max(same, comp) / len(both)


def resolve_duplicate_positions(path):
    """Markers sharing a CHROM:POS (the loader keeps one per position). Rule of 2026-10-08: if their genotypes agree
    (>= CONCORDANCE_MIN of individuals called by both, compared as allele bases), the marker with the fewest missing genotypes
    keeps the position and the others move to CHROM = UNMAPPED (next rank, ID kept); if they disagree, nobody can tell which one
    is placed correctly, so every marker of that position is removed. A provided VCF is rewritten; raw/ is untouched.
    Returns (moved, removed, n_positions)."""
    count = Counter()
    with _open_text(path) as f:
        for line in f:
            if line[0] != "#":
                c, pos = line.split("\t", 2)[:2]
                if c != UNMAPPED: count[(c, pos)] += 1
    dup_pos = {k for k, n in count.items() if n > 1}
    if not dup_pos or str(path).endswith(".gz"): return 0, 0, 0
    groups = {k: [] for k in dup_pos}
    last_unmapped = 0
    with _open_text(path) as f:
        for line in f:
            if line[0] == "#": continue
            fld = line.rstrip("\n").split("\t")
            if fld[0] == UNMAPPED: last_unmapped = max(last_unmapped, int(fld[1]))
            elif (fld[0], fld[1]) in groups: groups[(fld[0], fld[1])].append(fld)
    keep, unmap, drop = {}, [], set()
    for k, rows in groups.items():
        calls = [[_bases(r[3], r[4], g) for g in r[9:]] for r in rows]
        best = min(range(len(rows)), key=lambda i: sum(x is None for x in calls[i]))
        ok = True
        for i in range(len(rows)):
            if i == best: continue
            if _concordance(calls[best], calls[i]) < CONCORDANCE_MIN: ok = False
        if ok:
            keep[k] = rows[best][2]; unmap += [r for i, r in enumerate(rows) if i != best]
        else:
            drop.add(k)
    tmp = Path(str(path) + ".tmp"); written = set()
    with _open_text(path) as f, open(tmp, "w") as o:
        for line in f:
            if line[0] == "#":
                if line.startswith("#CHROM"):
                    o.write(f'##source_adjustment="markers sharing a CHROM:POS: {len(unmap)} concordant duplicates moved to CHROM={UNMAPPED} (the one with fewest missing genotypes keeps the position); '
                            f'{sum(len(groups[k]) for k in drop)} markers at {len(drop)} positions with discordant genotypes removed"\n')
                o.write(line); continue
            fld = line.split("\t", 3)
            k = (fld[0], fld[1])
            if k in drop: continue
            if k in keep:
                if fld[2] != keep[k] or k in written: continue
                written.add(k)
            o.write(line)
        for i, r in enumerate(unmap, last_unmapped + 1):
            o.write("\t".join([UNMAPPED, str(i)] + r[2:]) + "\n")
    os.replace(tmp, path)
    return len(unmap), sum(len(groups[k]) for k in drop), len(dup_pos)


def link(src, dest):
    """Expose a raw VCF at the dataset root without copying it."""
    dest = Path(dest)
    if dest.exists() or dest.is_symlink(): dest.unlink()
    os.link(src, dest)


def letters_to_rows(markers, genos, allele_split, missing, chrom_pos=None, drop=()):
    """Convert letter genotypes (e.g. 'A:G', 'AG', 'A/G') to VCF rows.
    REF = most frequent allele (no reference genome available), ALT = the other one.
    Returns (rows, dropped_markers_by_reason)."""
    rows, dropped = [], Counter()
    for j, m in enumerate(markers):
        calls = [allele_split(g) if g not in missing and isinstance(g, str) else None for g in genos[j]]
        alleles = Counter(a for c in calls if c for a in c)
        if any(a in drop or len(a) != 1 or a not in "ACGT" for a in alleles):
            dropped["allèles non nucléotidiques (indels I/D)"] += 1
            continue
        if len(alleles) > 2:
            dropped["plus de 2 allèles"] += 1
            continue
        order = [a for a, _ in alleles.most_common()]
        if not order:
            dropped["aucun génotype"] += 1
            continue
        ref, alt = order[0], (order[1] if len(order) > 1 else ".")
        idx = {ref: "0", alt: "1"}
        gts = ["./." if c is None else f"{idx[c[0]]}/{idx[c[1]]}" for c in calls]
        chrom, pos = chrom_pos(j, m) if chrom_pos else (m, 1)
        rows.append((chrom, pos, m, ref, alt, gts))
    return rows, dropped


# ─────────────────────────────── OCA ───────────────────────────────

# Attribute catalogue. Wording of the shared attributes is copied from the existing
# lymdis1/malvil1 schemas so every Genovalia dataset describes them identically.
# Harmonised on 2026-10-01 (review of metadata-api /v1/oca-properties, ednaspp excluded):
# units written in full words (decimal degree, meter; UCUM code kept in unit_framing),
# no ellipsis in definitions.
ATTR = {
    "id": ("Text", "id", "ID referring to the individual in the genotyping file (vcf)", None),
    "organism": ("Text", "organism", "Species name in latin according to the Linneaus classification", None),
    "country": ("Text", "country", "Country of origin of the individuals sampled", None),
    "latitude": ("Numeric", "latitude", "Latitude in decimal degree. Ex: -42.23452", "decimal degree"),
    "longitude": ("Numeric", "longitude", "Longitude in decimal degree. Ex: -42.23452", "decimal degree"),
    "elevation": ("Numeric", "elevation", "Elevation at the sites where individuals were collected (in meters)", "meter"),
    "sampling_location": ("Text", "sampling_location", "Name of the location where the individual was sampled (site, locality, river or lake)", None),
    "site_type": ("Text", "site_type", "General environmental or habitat category of the sampling site", None),
    "region": ("Text", "region", "Regional grouping of the sampling sites used in the source study, such as a management, biogeographic or genetic region", None),
    "sampling_year": ("Numeric", "sampling_year", "Year in which the sample was collected", None),
    "sampling_date": ("DateTime", "sampling_date", "Date on which the sample was collected, in the ISO 8601 format YYYY-MM-DD, or YYYY-MM when the day is unknown", None),
    "sampling_season": ("Text", "sampling_season", "Season in which the sample was collected: spring, summer, fall or winter", None),
    "sex": ("Text", "sex", "Sex of the individual: female, male or unknown", None),
    "life_stage": ("Text", "life_stage", "Life stage of the sampled individual, such as egg, larva, juvenile or adult", None),
    "age": ("Numeric", "age", "Age of the individual", "year"),
    "sample_tissue": ("Text", "sample_tissue", "The type of tissue sampled from the individual and used for DNA or RNA extraction", None),
    "population": ("Text", "population", "Non-geographic group the individual belongs to, such as a breeding population", None),
    "population_type": ("Text", "population_type", "General category describing the origin or management status of the population", None),
    "population_status": ("Text", "population_status", "Biogeographic status of the sampled population, such as native or introduced.", None),
    "ecotype": ("Text", "ecotype", "Distinct population within a species that is adapted to specific environmental conditions, such as anadromous or freshwater resident", None),
    "breed": ("Text", "breed", "Breed of the individual (animals)", None),
    "variety": ("Text", "variety", "Variety, cultivar, breeding line or landrace of the individual (plants)", None),
    "strain": ("Text", "strain", "Laboratory or microbial strain of the individual", None),
    "pedigree": ("Text", "pedigree", "Ancestry of the individual: breeding pedigree (cross formula), breeding line or reference to its registration", None),
    "family": ("Text", "family", "Family (progeny) the individual belongs to", None),
    "mother": ("Text", "mother", "ID of the mother", None),
    "father": ("Text", "father", "ID of the father", None),
    "block": ("Numeric", "block", "Block (replication) of the field or progeny test where the individual grows", None),
    "tree_number": ("Numeric", "tree_number", "Tree number within the family and block", None),
    "body_length": ("Numeric", "body_length", "Body length of the individual", "millimeter"),
    "body_mass": ("Numeric", "body_mass", "Body mass of the individual", "gram"),
    "technical_replicate": ("Boolean", "technical_replicate", "TRUE if this sample is a technical replicate of another sample of the dataset", None),
    "possible_duplicate_of": ("Text", "possible_duplicate_of", "ID of another sample with nearly identical genotypes (likely the same individual)", None),
    "biosample_accession": ("Text", "biosample_accession", "Accession number of the sequenced sample in the BioSample database of NCBI (shared with EBI and DDBJ), which links to its public raw sequence data", None),
    "date_of_birth": ("DateTime", "date_of_birth", "Birth date of the individual, expressed in the standard ISO 8601 format YYYY-MM-DD", None),
    "sequenced_molecule": ("Text", "sequenced_molecule", "Sequenced molecule such as DNA or RNA.", None),
    "genotyping_technology": ("Text", "genotyping_technology", "Genotyping technology used to obtain the genotypes: genotyping-by-sequencing, SNP chip, whole genome sequencing, targeted sequencing, targeted SNP assay or RNA sequencing.", None),
}
# Controlled vocabulary of genotyping_technology (validated 2026-10-07), written as OCA entry codes. The exact
# protocol or platform (ddRAD, DArTseq, Axiom 220K, KASP, GT-seq...) goes in the recipe's CFG `method`, which
# process() copies into the QC_REPORT decisions.
TECHNOLOGIES = ("genotyping-by-sequencing", "SNP chip", "whole genome sequencing", "targeted sequencing", "targeted SNP assay", "RNA sequencing")
# Controlled codes of other vocabulary attributes (2026-10-08), written as OCA entry codes when the column is present.
VOCAB_CODES = {"population_type": ("wild", "hatchery", "domesticated"), "population_status": ("native", "introduced"),
               "sampling_season": ("spring", "summer", "fall", "winter"), "sex": ("female", "male", "unknown")}
# Unit labels are written in full words (decision 2026-10-01); recipes may give a symbol, build_oca normalises it.
UNIT_WORDS = {"cm": "centimeter", "mm": "millimeter", "m": "meter", "km": "kilometer", "um": "micrometer", "nm": "nanometer",
              "g": "gram", "kg": "kilogram", "ha": "hectare", "m3": "cubic meter", "a": "year", "deg": "degree", "%": "percent",
              "kg/m3": "kilogram per cubic meter", "g/cm3": "gram per cubic centimeter", "km/s": "kilometer per second", "m/s": "meter per second",
              "GPa": "gigapascal", "ug/m": "microgram per meter", "m2/kg": "square meter per kilogram", "1/mm2": "per square millimeter",
              "MJ/m2": "megajoule per square meter", "mg/m3": "milligram per cubic meter", "ppm": "parts per million"}
# Full names of the pollutant codes used in column names (angang5, angros3): definitions spell them out.
CHEMICALS = {
    "ag": "silver (Ag)", "as": "arsenic (As)", "cd": "cadmium (Cd)", "cr": "chromium (Cr)", "cu": "copper (Cu)", "hg": "mercury (Hg)",
    "ni": "nickel (Ni)", "pb": "lead (Pb)", "se": "selenium (Se)", "zn": "zinc (Zn)",
    "2_4_ddd_cb154_77": "2,4'-dichlorodiphenyldichloroethane (2,4'-DDD; reported together with the polychlorinated biphenyls PCB 154 and PCB 77)",
    "2_4_dde": "2,4'-dichlorodiphenyldichloroethylene (2,4'-DDE)", "2_4_ddt": "2,4'-dichlorodiphenyltrichloroethane (2,4'-DDT)",
    "4_4_ddd": "4,4'-dichlorodiphenyldichloroethane (4,4'-DDD)", "4_4_dde": "4,4'-dichlorodiphenyldichloroethylene (4,4'-DDE)",
    "4_4_ddt": "4,4'-dichlorodiphenyltrichloroethane (4,4'-DDT)", "hcb": "hexachlorobenzene (HCB)", "lindane": "lindane (gamma-hexachlorocyclohexane)",
    "pcb_50_28": "the polychlorinated biphenyls PCB 50 and PCB 28 (reported together)",
}
CHEMICALS.update({f"pbde_{n}": f"the polybrominated diphenyl ether BDE-{n} (PBDE {n})" for n in (28, 47, 49, 99, 100, 153, 154, 183, 209)})
CHEMICALS.update({f"pcb_{n}": f"the polychlorinated biphenyl PCB {n}" for n in (52, 101, 118, 138, 153, 180)})

UCUM = {"km": "km", "g": "g", "kg": "kg", "degree": "deg", "decimal degree": "deg", "meter": "m", "degree Celsius": "Cel", "mg/m3": "mg/m3", "m": "m", "ha": "har", "cm": "cm", "mm": "mm",
        "um": "um", "nm": "nm", "kg/m3": "kg/m3", "GPa": "GPa", "ug/m": "ug/m", "m2/kg": "m2/kg", "1/mm2": "/mm2",
        "ppm": "[ppm]", "%": "%", "MJ/m2": "MJ/m2", "degree-day": "d",
        "nanogram per gram": "ng/g", "microgram per gram": "ug/g",
        "centimeter": "cm", "millimeter": "mm", "kilometer": "km", "micrometer": "um", "nanometer": "nm", "gram": "g", "kilogram": "kg",
        "hectare": "har", "cubic meter": "m3", "year": "a", "percent": "%", "kilogram per cubic meter": "kg/m3",
        "gram per cubic centimeter": "g/cm3", "kilometer per second": "km/s", "meter per second": "m/s", "gigapascal": "GPa",
        "microgram per meter": "ug/m", "square meter per kilogram": "m2/kg", "per square millimeter": "/mm2",
        "megajoule per square meter": "MJ/m2", "milligram per cubic meter": "mg/m3", "parts per million": "[ppm]", "square meter": "m2"}  # tissue concentrations (angang5)


def build_oca(name, description, columns, extra, entries=None, classification="RDF106"):
    """columns: ordered CSV columns; extra: {attr: (type, label, info, unit)} for dataset-specific
    attributes; entries: {attr: {code: label}} for categorical attributes.
    Mirrors the Semantic Engine oca_package/1.0 layout (key order included) so SAIDs verify."""
    # vocabulary attributes always take their ATTR definition (a recipe never redefines them); units in full words
    spec = {c: (ATTR[c] if c in ATTR else extra[c]) for c in columns}
    spec = {c: (t, l, i, UNIT_WORDS.get(u, u) if u else u) for c, (t, l, i, u) in spec.items()}
    entries = entries or {}
    attrs = sorted(spec)
    cb = {"d": "", "type": "spec/capture_base/1.1",
          "attributes": {a: spec[a][0] for a in attrs}, "classification": classification, "flagged_attributes": []}
    cb["d"] = said(cb)
    base = lambda typ: {"d": "", "capture_base": cb["d"], "type": f"spec/overlays/{typ}/1.1"}

    def seal(o):
        o["d"] = said(o); return o

    ov = {}
    if entries:
        ov["entry"] = [seal({**base("entry"), "language": "eng",
                             "attribute_entries": {a: entries[a] for a in sorted(entries)}})]
        ov["entry_code"] = seal({**base("entry_code"),
                                 "attribute_entry_codes": {a: list(entries[a]) for a in sorted(entries)}})
    ov["information"] = [seal({**base("information"), "language": "eng",
                               "attribute_information": {a: spec[a][2] for a in attrs}})]
    ov["label"] = [seal({**base("label"), "language": "eng", "attribute_categories": [],
                         "attribute_labels": {a: spec[a][1] for a in attrs}, "category_labels": {}})]
    ov["meta"] = [seal({**base("meta"), "language": "eng", "description": description, "name": name})]
    units = {a: spec[a][3] for a in attrs if spec[a][3]}
    if units:
        ov["unit"] = seal({**base("unit"), "attribute_unit": units})
    ov = dict(sorted(ov.items()))

    bundle = {"v": "OCAS11JSON000000_", "d": "", "capture_base": cb, "overlays": ov}
    bundle["d"] = "#" * 44
    bundle["v"] = "OCAS11JSON%06x_" % len(json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode())
    bundle["d"] = said(bundle)

    ext = {"d": "", "type": "community/adc/extension/1.0", "overlays": {
        "ordering": seal({"d": "", "capture_base": cb["d"], "type": "community/overlays/adc/ordering/1.0",
                          "attribute_ordering": list(columns),
                          "entry_code_ordering": {a: list(entries[a]) for a in sorted(entries)}})}}
    used = sorted({u for u in units.values()})
    if used:
        ext["overlays"]["unit_framing"] = seal({
            "d": "", "capture_base": cb["d"], "type": "community/overlays/adc/unit_framing/1.0",
            "framing_metadata": {"id": "UCUM", "label": "Unified Code for Units of Measure", "location": "https://ucum.org/", "version": ""},
            "units": {u: {"framing_justification": "semapv:ManualMappingCuration", "predicate_id": "skos:exactMatch", "term_id": UCUM[u]} for u in used}})
    ext["d"] = said(ext)
    pkg = {"d": "", "type": "oca_package/1.0", "oca_bundle": {"bundle": bundle, "dependencies": []},
           "extensions": {"adc": {cb["d"]: ext}}}
    pkg["d"] = said(pkg)
    return pkg


def verify_saids(pkg):
    """Recompute every SAID of an oca_package; returns list of mismatching paths."""
    bad = []
    chk = lambda o, p: bad.append(p) if said(o) != o["d"] else None
    b = pkg["oca_bundle"]["bundle"]
    chk(pkg, "package"); chk(b, "bundle"); chk(b["capture_base"], "capture_base")
    for k, v in b["overlays"].items():
        for o in (v if isinstance(v, list) else [v]): chk(o, f"overlays.{k}")
    for cbd, e in pkg.get("extensions", {}).get("adc", {}).items():
        chk(e, "extension")
        for k, o in e["overlays"].items(): chk(o, f"extension.{k}")
    return bad


# ─────────────────────────────── DCAT ───────────────────────────────

def person(name, orcid=None, roles=("author",)):
    return {"name": name, "orcid": orcid, "roles": list(roles)}


REPOSITORY_DOI = ("10.5061/", "10.5281/", "10.5683/", "10.20383/", "10.6084/", "10.17632/")  # Dryad, Zenodo, Dataverse, FRDR, figshare, Mendeley


def relation_urls(relations):
    """dct:relation: every publication and data deposit DOI of the recipe, except bioRxiv preprints
    (10.1101/) when a published article is also listed."""
    rel = list(relations or [])
    published = any(not x.startswith(("10.1101/",) + REPOSITORY_DOI) for x in rel)
    return [f"https://doi.org/{x}" for x in rel if not (published and x.startswith("10.1101/"))]


def build_dcat(ds_id, m):
    """Same shape as the Genovalia DCAT builder (buildObj) and the metadonnees repo.
    'dct' is added to @context because metadata-api reads dct:relation (exposed as related_urls)
    and dct:* keys inside distributions (both prefixes expand to http://purl.org/dc/terms/).
    dct:relation lists the publication(s) and data deposit(s) from m["relations"] (see
    relation_urls), as in metadonnees since 2026-10-07. License and distribution are left empty,
    as on every metadonnees dataset (AGENTS.md "Deliberately left for later"): Sedna takes the
    access-request link from dcat:distribution -> dcat:accessURL, so a repository URL there would
    replace Genovalia's access form. m["license"] and m["distributions"] stay in the recipes as
    provenance for QC_REPORT/issues.json only."""
    orcid = lambda p: f"https://orcid.org/{p['orcid']}" if p.get("orcid") else None
    agent = lambda p: {k: v for k, v in {"@id": orcid(p), "@type": "prov:Person", "foaf:name": p["name"]}.items() if v}
    d = {
        "@context": {"dcat": "http://www.w3.org/ns/dcat#", "dcterms": "http://purl.org/dc/terms/",
                     "dct": "http://purl.org/dc/terms/", "foaf": "http://xmlns.com/foaf/0.1/",
                     "prov": "http://www.w3.org/ns/prov#", "vcard": "http://www.w3.org/2006/vcard/ns#",
                     "rdfs": "http://www.w3.org/2000/01/rdf-schema#", "xsd": "http://www.w3.org/2001/XMLSchema#",
                     "time": "http://www.w3.org/2006/time#"},
        "@id": SEDNA + ds_id, "@type": "dcat:Dataset",
        "dcterms:identifier": ds_id, "dcterms:issued": m.get("issued", TODAY),
        "dcterms:language": "en", "dcterms:title": m["title"], "dcterms:description": m["description"],
        "dcat:landingPage": SEDNA + ds_id,
        "dcterms:publisher": {"@id": "https://genovalia.ulaval.ca/", "@type": "foaf:Organization",
                              "foaf:name": "Genovalia", "foaf:homepage": "https://genovalia.ulaval.ca/"},
        "dcat:contactPoint": {"@type": "vcard:Kind", "vcard:fn": "Genovalia", "vcard:hasEmail": {"@id": "mailto:genovalia@ulaval.ca"}},
        "dcat:keyword": m["keywords"],
        "dcat:theme": f"https://www.ncbi.nlm.nih.gov/datasets/taxonomy/{m['taxid']}/",
        "dcat:distribution": [],
    }
    if m.get("modified"): d["dcterms:modified"] = m["modified"]
    if m.get("years"):
        y0, y1 = m["years"]
        t = {"@type": "dcterms:PeriodOfTime", "time:hasBeginning": {"@type": "time:Instant", "time:inXSDgYear": str(y0)}}
        if y1: t["time:hasEnd"] = {"@type": "time:Instant", "time:inXSDgYear": str(y1)}
        d["dcterms:temporal"] = t
    d["dcterms:spatial"] = f"https://sws.geonames.org/{m['geonames']}/"
    d["dcat:version"] = m.get("version", "1.0.0")
    d["dcterms:license"] = ""
    d["dcterms:accessRights"] = OPEN
    c = m["people"][0]
    d["dcterms:creator"] = {k: v for k, v in {"@id": orcid(c), "@type": "foaf:Person", "foaf:name": c["name"]}.items() if v}
    d["dcat:qualifiedAttribution"] = [{"@type": "prov:Attribution", "prov:agent": agent(p), "dcat:hadRole": p["roles"]} for p in m["people"]]
    rel = relation_urls(m.get("relations"))
    if rel: d["dct:relation"] = rel
    return d


def build_mapper(ds_id, m):
    """Same structure as metadonnees/<id>/mapper.json (fr/en presentation layer)."""
    jp = lambda p: {"type": "jsonpath", "path": p}
    lit = lambda v: {"type": "literal", "value": v}
    common = lambda lang: {
        "theme": lit(m["species"]),
        "publisher": {"name": jp("'dcterms:publisher'.'foaf:name'"), "url": jp("'dcterms:publisher'.'foaf:homepage'")},
        "contact": {"name": jp("'dcat:contactPoint'.'vcard:fn'"), "email": jp("'dcat:contactPoint'.'vcard:hasEmail'.'@id'")},
        "species": lit(m["species"]),
        "temporal": {"year_begin": jp("'dcterms:temporal'.'time:hasBeginning'.'time:inXSDgYear'"),
                     "year_end": jp("'dcterms:temporal'.'time:hasEnd'.'time:inXSDgYear'")},
        "spatial": lit(m["spatial_label"][lang]),
        "creators": jp("'dcterms:creator'.'foaf:name'"),
        "creators_urls": jp("'dcterms:creator'.'@id'"),
        "access_request_url": "https://wagtail.apps.genovalia.ulaval.ca/documents/20/Annexe_partage_donnees_sept2025.pdf",
    }
    return {"id": ds_id, "en": common("en"),
            "fr": {"title": lit(m["title_fr"]), "description": lit(m["description_fr"]), **common("fr")}}


# ─────────────────────────── validators reused from the repos ───────────────────────────

def _load_module(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(METADONNEES))
from catalogue_cli import check_dcat  # noqa: E402  (genovalia/metadonnees validate command)


METADATA_API = "https://metadata-api.apps.genovalia.ulaval.ca/v1"  # public GETs, spec at …/docs


def metadata_api(path, **params):
    """Read-only GET on metadata-api (no key needed for reads). Returns None if unreachable."""
    try:
        r = requests.get(f"{METADATA_API}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"metadata-api injoignable ({path}) : {e}", flush=True)
        return None


def existing_ids(workspace=True):
    """IDs already taken: datasets loaded in metadata-api and metadonnees dataset folders, plus
    (workspace=True, for choosing a new ID) the workspace's own dataset folders (no DB dump any more).
    metadonnees replaced catalogue.json by catalog.json (2026-10-01), which no longer lists datasets:
    the old file is read only if present."""
    ids = {f.parent.name for f in METADONNEES.glob("*/dcat.json")}
    if (old := METADONNEES / "catalogue.json").exists():
        ids |= {e["id"] for e in json.loads(old.read_text()).get("content", [])}
    if workspace:
        ids |= {p.name for p in ROOT.iterdir() if p.is_dir() and ID_RE.match(p.name)}
    page = 1
    while (lst := metadata_api("datasets", page=page, page_size=100)) and lst["content"]:
        ids |= {d["identifier"] for d in lst["content"]}
        if page * lst["page_size"] >= lst["total"]: break
        page += 1
    return ids


ID_RE = re.compile(r"^[a-z]{3}([a-z]{3}|spp)\d+$")


# ─────────────────────────────── QC ───────────────────────────────

def fmt_pct(x):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f} %"


def run_qc(ds_id, cfg, res, st):
    """Returns (summary_row, blocking, minor, report_markdown)."""
    blocking, minor = list(res.get("blocking", [])), list(res.get("minor", []))
    vcf, df = res["vcf"], res["samples"]
    ld = loader_check(vcf)
    exp = cfg["expected"]
    vcf_ids, csv_ids = set(st["samples"]), set(df["id"])
    only_vcf, only_csv = sorted(vcf_ids - csv_ids), sorted(csv_ids - vcf_ids)
    match = len(vcf_ids & csv_ids) / max(len(vcf_ids | csv_ids), 1)
    dup_ids = df["id"][df["id"].duplicated()].tolist()
    ws_ids = [i for i in df["id"] if i != i.strip() or " " in i]

    if only_vcf: blocking.append(f"{len(only_vcf)} ID du VCF absents du CSV (le chargeur refusera le VCF)")
    if only_csv: minor.append(f"{len(only_csv)} individus du CSV sans génotypes dans le VCF")
    if dup_ids: blocking.append(f"{len(dup_ids)} ID en double dans le CSV")
    if ws_ids: blocking.append(f"{len(ws_ids)} ID avec espaces")
    if not ld["ok"]: blocking.append("cyvcf2 (chargeur data-explorer) échoue : " + "; ".join(ld["errors"][:2]))
    if st["non_numeric_pos"]: minor.append(f"{st['non_numeric_pos']} lignes à POS non numérique (ignorées par le chargeur)")
    if st["dup_pos"]: blocking.append(f"{st['dup_pos']} doublons CHROM:POS (marqueurs distincts à la même position) : le chargeur regroupe par (CHROM, POS) et ignore les génotypes du 2e marqueur pour les individus déjà vus → perte silencieuse ; dédoublonner ou décaler avant chargement")
    if st["multiallelic"]: minor.append(f"{st['multiallelic']} SNPs multialléliques")
    if st["bad_gt"]: blocking.append(f"{st['bad_gt']} génotypes illisibles")

    if exp.get("individuals") and len(vcf_ids) != exp["individuals"]:
        minor.append(f"{len(vcf_ids)} individus dans le VCF vs {exp['individuals']} annoncés ({exp['src']})")
    if exp.get("snps") and st["n_snps"] != exp["snps"]:
        minor.append(f"{st['n_snps']} SNPs dans le VCF vs {exp['snps']} annoncés ({exp['src']})")

    smiss = st["sample_missing"]
    high = sorted((k for k, v in smiss.items() if v > MISSING_RATE_FLAG), key=lambda k: -smiss[k])
    if high: minor.append(f"{len(high)} individus avec > {MISSING_RATE_FLAG:.0%} de données manquantes")
    hets = [v for v in st["sample_het"].values() if not math.isnan(v)]
    het_out = []
    if len(hets) > 10:
        mu, sd = statistics.mean(hets), statistics.pstdev(hets)
        het_out = sorted(k for k, v in st["sample_het"].items() if sd and v > mu + 3 * sd)
        if het_out: minor.append(f"{len(het_out)} individus à hétérozygotie > moyenne + 3 σ (contamination ou mélange ?)")

    geo = "absentes"
    if {"latitude", "longitude"} <= set(df.columns):
        lat, lon = pd.to_numeric(df["latitude"], errors="coerce"), pd.to_numeric(df["longitude"], errors="coerce")
        no_coord = int((lat.isna() | lon.isna()).sum())
        (la0, la1), (lo0, lo1) = cfg.get("bbox") or ((-90, 90), (-180, 180))
        out = int(((lat < la0) | (lat > la1) | (lon < lo0) | (lon > lo1)).sum())
        geo = "OK" if not no_coord and not out else f"{no_coord} manquantes, {out} hors zone"
        if no_coord: minor.append(f"{no_coord} individus sans coordonnées")
        if out: blocking.append(f"{out} individus avec coordonnées hors de la zone attendue {cfg['bbox']}")
    site_col = next((c for c in ("site_code", "sampling_location") if c in df), None)
    sites = df[site_col].nunique() if site_col else (df[["latitude", "longitude"]].drop_duplicates().dropna().shape[0] if {"latitude", "longitude"} <= set(df.columns) else None)
    if exp.get("sites") and sites is not None and sites != exp["sites"]:
        minor.append(f"{sites} sites dans les données vs {exp['sites']} annoncés ({exp['src']})")
    empty = [c for c in df.columns if df[c].isna().all()]
    if empty: blocking.append(f"colonnes entièrement vides : {empty}")

    # OCA ↔ CSV ↔ parsers
    oca = res["oca"]
    bad_said = verify_saids(oca)
    if bad_said: blocking.append(f"SAID invalides : {bad_said}")
    oca_attrs = set(oca["oca_bundle"]["bundle"]["capture_base"]["attributes"])
    if oca_attrs != set(df.columns): blocking.append(f"attributs OCA ≠ colonnes CSV : {sorted(oca_attrs ^ set(df.columns))}")

    # DCAT
    dcat = res["dcat"]
    errs, warns = check_dcat(ds_id, dcat)
    for e in errs: blocking.append(f"DCAT : {e}")

    ref = res.get("reference", "?")
    row = {
        "ID": ds_id, "Espèce": cfg["species"],
        "Individus VCF / CSV / annoncés": f"{len(vcf_ids)} / {len(df)} / {exp.get('individuals', '?')}",
        "SNPs VCF / annoncés": f"{st['n_snps']} / {exp.get('snps', '?')}",
        "VCF": res["vcf_origin"], "Corresp. ID": fmt_pct(match),
        "Attributs individuels": res["attr_level"], "Coordonnées": geo,
        "Manquants médian": fmt_pct(statistics.median(smiss.values()) if smiss else None),
        "Référence génome": ref, "Article": cfg["paper_status"],
        "Bloquants": len(blocking), "Mineurs": len(minor),
    }
    rep = [f"# QC — {ds_id} ({cfg['species']})", "", f"Généré par build.py le {TODAY}.", "",
           "## Fichiers", "", f"- `{Path(vcf).name}` ({res['vcf_origin']})", f"- `{ds_id}_samples.csv` ({len(df)} lignes, {len(df.columns)} colonnes)",
           "- `dcat.json`, `oca.json`, `mapper.json`", "", "## Chiffres", "",
           f"| Mesure | Valeur |", "|---|---|",
           f"| Individus VCF | {len(vcf_ids)} |", f"| Individus CSV | {len(df)} |", f"| Individus annoncés ({exp['src']}) | {exp.get('individuals', '?')} |",
           f"| SNPs | {st['n_snps']} (annoncés : {exp.get('snps', '?')}) |", f"| Chromosomes/contigs | {len(st['chroms'])} |",
           f"| Correspondance ID VCF↔CSV | {fmt_pct(match)} |", f"| Sites | {sites} (annoncés : {exp.get('sites', '?')}) |",
           f"| Manquants par individu (médiane / max) | {fmt_pct(statistics.median(smiss.values()) if smiss else None)} / {fmt_pct(max(smiss.values()) if smiss else None)} |",
           f"| Manquants par SNP (médiane) | {fmt_pct(statistics.median(st['snp_missing']) if st['snp_missing'] else None)} |",
           f"| Hétérozygotie par individu (médiane) | {fmt_pct(statistics.median(hets) if hets else None)} |",
           f"| Multialléliques / POS non numériques / doublons CHROM:POS | {st['multiallelic']} / {st['non_numeric_pos']} / {st['dup_pos']} |",
           f"| Chargeur data-explorer (cyvcf2) | {'OK' if ld['ok'] else 'ÉCHEC'} — {ld['samples']} individus, {ld['variants']} variants |",
           f"| Référence génomique | {ref} |", f"| SAID OCA | {'valides' if not bad_said else bad_said} |",
           f"| DCAT (validate metadonnees) | {'OK' if not errs else len(errs)} erreurs, avertissements : {warns or 'aucun'} |", "",
           "## Problèmes bloquants", ""] + ([f"- {x}" for x in blocking] or ["Aucun."]) + [
           "", "## Problèmes mineurs / à vérifier", ""] + ([f"- {x}" for x in minor] or ["Aucun."]) + [
           "", "## Décisions de préparation", ""] + [f"- {x}" for x in res.get("decisions", [])]
    if only_vcf: rep += ["", "## ID du VCF absents du CSV", "", ", ".join(only_vcf[:200])]
    if only_csv: rep += ["", "## ID du CSV absents du VCF", "", ", ".join(only_csv[:200])]
    if high: rep += ["", f"## Individus > {MISSING_RATE_FLAG:.0%} manquants", "", ", ".join(f"{k} ({smiss[k]:.0%})" for k in high[:200])]
    if het_out: rep += ["", "## Individus à hétérozygotie élevée", "", ", ".join(f"{k} ({st['sample_het'][k]:.1%})" for k in het_out)]
    rep += ["", "## Colonnes du CSV", "", "| Colonne | Type OCA | Unité | Non-vides | Valeurs distinctes |", "|---|---|---|---|---|"]
    # Type and unit read from the OCA bundle itself (the metadata-api/data-explorer parsers that gave `props` were dropped)
    bundle = oca["oca_bundle"]["bundle"]
    types = bundle["capture_base"]["attributes"]
    unit_ov = bundle["overlays"].get("unit", {})
    units = (unit_ov[0] if isinstance(unit_ov, list) else unit_ov).get("attribute_unit", {}) if unit_ov else {}
    for c in df.columns:
        rep.append(f"| {c} | {types.get(c)} | {units.get(c, '')} | {df[c].notna().sum()} | {df[c].nunique()} |")
    rep += ["", "## En-tête du VCF (extrait)", "", "```", *st["meta"][:15], "```", ""]
    return row, blocking, minor, "\n".join(rep)


# ─────────────────────────────── datasets ───────────────────────────────
# Each prep/<id>.py prepare(raw, out, h) returns dict(vcf, samples, vcf_origin, attr_level, reference, extra,
# entries, decisions, blocking, minor). Column meaning lives in `extra` (→ OCA). Helpers shared by the recipes:

def _dna(df, tech):
    assert tech in TECHNOLOGIES, f"genotyping_technology hors vocabulaire : {tech!r} (méthode exacte → CFG method)"
    df["sequenced_molecule"] = "DNA"
    df["genotyping_technology"] = tech
    return df


def _vcf_samples(path):
    with _open_text(path) as f:
        for line in f:
            if line.startswith("#CHROM"): return line.rstrip("\n").split("\t")[9:]


# ─────────────────────────────── configuration ───────────────────────────────
# Descriptions use {n} (individuals in VCF) and {snps} (variants in VCF), filled after preparation.
P = person
DATASETS = {}  # filled from prep/<id>.py: CFG(h) of each module


# ─────────────────────────────── main ───────────────────────────────

# One module per dataset, prep/<id>.py (every dataset, lot 1 included since 2026-10-06), so datasets can be
# prepared in parallel without touching this file. Modules in prep/retired/ are not loaded.
# It defines FILES [(url, name)], optional MANUAL [MANUAL_DOWNLOADS rows], CFG(h) -> DATASETS entry, prepare(raw, out, h);
# h is this module (helpers P, CC0, CCBY, link, write_vcf, letters_to_rows, _dna, _vcf_samples, ATTR, UCUM...).
H = sys.modules[__name__]
for _f in sorted((ROOT / "prep").glob("[a-z]*.py")):
    try:
        _m = _load_module(f"prep_{_f.stem}", _f)
        DATASETS[_f.stem] = _m.CFG(H)
        globals()[f"prepare_{_f.stem}"] = lambda raw, out, _m=_m: _m.prepare(raw, out, H)
        MANUAL_DOWNLOADS += getattr(_m, "MANUAL", [])
    except Exception as e:  # one broken module must not stop the other datasets
        print(f"prep/{_f.name} ignoré : {type(e).__name__}: {e}", file=sys.stderr)


def apply_attributes(ds_id, cfg, df, res):
    """Recipe CFG `attributes` (vocabulary review of 2026-10-08): {"drop": [...], "rename": {old: new}, "values": {col: {old: new}},
    "scale": {col: factor}, "types": {col: OCA type}, "info": {col: definition}, "units": {col: unit}}. Keys of values/scale/types/info/units
    use the FINAL column name. Renamed columns move their `extra` entry; vocabulary names (ATTR) use the ATTR definition."""
    spec = cfg.get("attributes") or {}
    extra = dict(res.get("extra", {})); entries = dict(res.get("entries") or {})
    df = df.apply(lambda c: c.map(lambda v: v.strip() if isinstance(v, str) else v))
    SEX = {"f": "female", "female": "female", "m": "male", "male": "male", "u": "unknown", "unknown": "unknown", "?": "unknown"}
    drop = [c for c in spec.get("drop", []) if c in df.columns]
    df = df.drop(columns=drop)
    for c in drop: extra.pop(c, None); entries.pop(c, None)
    ren = {a: b for a, b in spec.get("rename", {}).items() if a in df.columns}
    df = df.rename(columns=ren)
    for a, b in ren.items():
        if a in extra: extra[b] = extra.pop(a)
        if a in entries: entries[b] = entries.pop(a)
    for c, m in spec.get("values", {}).items():
        df[c] = df[c].map(lambda v: m.get(str(v), v) if v is not None and v == v else v)
        if c in entries: entries[c] = {m.get(k, k): v for k, v in entries[c].items() if m.get(k, k) is not None}
    if "sex" in df.columns:  # sex codes of the vocabulary: female, male, unknown
        df["sex"] = df["sex"].map(lambda v: SEX.get(v.lower(), v) if isinstance(v, str) else v)
    for c, f in spec.get("scale", {}).items():
        df[c] = pd.to_numeric(df[c], errors="coerce") * f
    for c in df.columns:
        if c in ATTR: extra.pop(c, None)
    for key, pos in (("types", 0), ("info", 2), ("units", 3)):
        for c, v in spec.get(key, {}).items():
            t = list(extra.get(c, ATTR.get(c))); t[pos] = v; extra[c] = tuple(t)
    if drop or ren:
        res.setdefault("decisions", []).append(
            "Attributs harmonisés (revue du vocabulaire du 2026-10-08) : "
            + (f"retirés : {', '.join(drop)}" if drop else "")
            + (" ; " if drop and ren else "") + (f"renommés : {', '.join(f'{a} → {b}' for a, b in ren.items())}" if ren else "")
            + (f" ; conversions d'unité : {', '.join(f'{c} ×{f}' for c, f in spec['scale'].items())}" if spec.get("scale") else "") + ".")
    res = dict(res, extra=extra, entries=entries, samples=df)
    return df, res


def process(ds_id, cfg, taken):
    out = ROOT / ds_id
    res = globals()[f"prepare_{ds_id}"](out / "raw", out)
    df = res["samples"].copy()
    moved, removed, npos = resolve_duplicate_positions(res["vcf"])
    if npos:
        res.setdefault("decisions", []).append(f"{npos} positions CHROM:POS partagées par plusieurs marqueurs (le chargeur n'en garde qu'un par position) : {moved} doublons concordants (≥ {CONCORDANCE_MIN:.0%} de génotypes identiques) déplacés sur « {UNMAPPED} », le marqueur le moins manquant gardant la position ; {removed} marqueurs à génotypes discordants retirés, faute de savoir lequel est bien positionné.")
    st = vcf_stats(res["vcf"])
    fill = dict(n=f"{len(st['samples']):,}".replace(",", " "), snps=f"{st['n_snps']:,}".replace(",", " "))
    m = dict(cfg, title=cfg["title"], description=cfg["description"].format(**fill), description_fr=cfg["description_fr"].format(**fill))
    if m["years"] == "samples":
        y = pd.to_numeric(df["sampling_year"], errors="coerce")
        m["years"] = (int(y.min()), int(y.max()))
    df, res = apply_attributes(ds_id, cfg, df, res)
    entries = dict(res.get("entries") or {})
    for a, codes in VOCAB_CODES.items():
        if a in df.columns:
            entries[a] = {c: c for c in codes}
            bad = sorted(set(df[a].dropna().astype(str)) - set(codes) - {"NA"})
            if bad: res.setdefault("blocking", []).append(f"{a} hors codes du vocabulaire : {bad}")
    if "genotyping_technology" in df.columns:
        entries.setdefault("genotyping_technology", {t: t for t in TECHNOLOGIES})
        techs = sorted(set(df["genotyping_technology"].dropna()))
        bad = [t for t in techs if t not in TECHNOLOGIES]
        if bad: res.setdefault("blocking", []).append(f"genotyping_technology hors vocabulaire : {bad}")
        missing_kw = [t for t in techs if t not in cfg["keywords"]]
        if missing_kw: res.setdefault("minor", []).append(f"technologie absente des mots-clés : {missing_kw}")
        if cfg.get("method"):
            res.setdefault("decisions", []).append(f"genotyping_technology = {' / '.join(techs)} (vocabulaire Genovalia) ; méthode exacte : {cfg['method']}.")
    res["oca"] = build_oca(cfg["oca_name"], m["description"], list(df.columns), res.get("extra", {}), entries)
    res["dcat"] = build_dcat(ds_id, m)
    if set(st["chroms"]) == {UNMAPPED}:
        res.setdefault("decisions", []).append(f"Pas de positions génomiques : à l'écriture du VCF, tous les marqueurs ont ensuite été placés sur le pseudo-chromosome « {UNMAPPED} », POS = rang du marqueur (sans valeur génomique), l'ancien CHROM (nom du marqueur) dans ID ; un contig par marqueur rendait le chargement quadratique (angang2 : ~30 min au lieu de quelques secondes).")
    df = df.apply(lambda c: c.map(lambda v: v.strip() if isinstance(v, str) else v))  # no leading/trailing spaces in values
    df = df.replace("", pd.NA)
    df.to_csv(out / f"{ds_id}_samples.csv", index=False, encoding="utf-8", na_rep="NA")  # missing values written NA (requester convention)
    (out / "oca.json").write_text(json.dumps(res["oca"], indent=2, ensure_ascii=False) + "\n")
    (out / "dcat.json").write_text(json.dumps(res["dcat"], indent=2, ensure_ascii=False) + "\n")
    (out / "mapper.json").write_text(json.dumps(build_mapper(ds_id, m), indent=2, ensure_ascii=False) + "\n")
    res["samples"] = df
    if not ID_RE.match(ds_id): res.setdefault("blocking", []).append(f"ID {ds_id} non conforme à la convention genre3+espèce3+n°")
    if ds_id in taken: res.setdefault("blocking", []).append(f"ID {ds_id} déjà utilisé dans la BD")
    row, blocking, minor, rep = run_qc(ds_id, cfg, res, st)
    (out / "QC_REPORT.md").write_text(rep)
    return row, blocking, minor, m


def main(only):
    (ROOT / ".tmp").mkdir(exist_ok=True)
    assert not verify_saids(json.loads((METADONNEES / "lymdis1/oca.json").read_text())), "SAID self-test failed"
    taken = existing_ids(workspace=False)  # the workspace folders are the datasets being built
    # metadonnees dropped dictionary.json (2026-09-29): compare with the keywords already in use
    # (metadonnees DCATs + metadata-api /keywords and /dictionary)
    dictionary = {k for f in METADONNEES.glob("*/dcat.json") for k in json.loads(f.read_text()).get("dcat:keyword", [])}
    dictionary |= {k["value"] for k in metadata_api("keywords") or []} | set(metadata_api("dictionary", lang="en") or {})
    rows, details, new_kw = [], [], set()
    for ds_id, cfg in DATASETS.items():
        if only and ds_id not in only: continue
        print(f"== {ds_id}", flush=True)
        try:
            row, blocking, minor, m = process(ds_id, cfg, taken)
            new_kw |= {k for k in m["keywords"] if k not in dictionary}
        except Exception as e:
            traceback.print_exc()
            row = {"ID": ds_id, "Espèce": cfg["species"], "Bloquants": 1, "Mineurs": 0}
            blocking, minor = [f"ÉCHEC de la préparation : {type(e).__name__}: {e}"], []
        rows.append(row); details.append((ds_id, blocking, minor))
        print(f"   bloquants={len(blocking)} mineurs={len(minor)}", flush=True)
    if only: return
    cols = ["ID", "Espèce", "Individus VCF / CSV / annoncés", "SNPs VCF / annoncés", "VCF", "Corresp. ID", "Attributs individuels",
            "Coordonnées", "Manquants médian", "Référence génome", "Article", "Bloquants", "Mineurs"]
    md = ["# Synthèse — jeux de génotypage publics", "", f"Généré par `build.py` le {TODAY}. Détails par jeu dans `<id>/QC_REPORT.md`.", "",
          "Chaque dossier contient `<id>.vcf` (non compressé : le chargeur data-explorer lit le fichier en texte), `<id>_samples.csv`, `dcat.json`, `oca.json`, `mapper.json`.", "",
          "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows]
    md += ["", "## Problèmes par jeu", ""]
    for ds_id, blocking, minor in details:
        md += [f"### {ds_id}", ""] + [f"- **Bloquant** : {x}" for x in blocking] + [f"- {x}" for x in minor] + ([""] if blocking or minor else ["Aucun.", ""])
    md += ["## Nouveaux mots-clés (absents des jeux de metadonnees : vérifier les quasi-doublons)", "", ", ".join(sorted(new_kw)) or "Aucun.", "",
           "## Constats sur la plateforme", "",
           "- Le chargeur VCF de data-explorer accepte l'extension `.vcf.gz` mais lit le fichier ligne par ligne en UTF-8 : un VCF compressé échouerait. Les VCF sont donc fournis non compressés.",
           "- metadata-api lit `dct:relation` et les clés `dct:*` des distributions, alors que le `@context` du DCAT builder ne déclare que `dcterms`. Le préfixe `dct` a été ajouté au `@context` (même IRI).",
           "- Les VCF convertis depuis des matrices (picmar2, picgla2, picsit1, triaes1) n'ont pas d'allèle de référence génomique : REF = allèle majeur (ou lu dans le nom du SNP pour picmar2).", "",
           "## Téléchargements manuels", "", "Déposer le fichier à l'emplacement indiqué puis relancer `build.py`.", "",
           "| Jeu | Document | URL | Déposer dans | Raison |", "|---|---|---|---|---|"]
    md += [f"| {a} | {b} | {c} | `{d}` | {e} |" for a, b, c, d, e in MANUAL_DOWNLOADS]
    (ROOT / "SUMMARY.md").write_text("\n".join(md) + "\n")
    print("SUMMARY.md written")


if __name__ == "__main__":
    main(sys.argv[1:])
