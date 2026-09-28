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
REPO = Path("/home/steve/git/genovalia/data-explorer")
METADONNEES = ROOT / ".metadonnees"  # clone of genovalia/metadonnees (validator, dictionary, existing schemas)
if not METADONNEES.exists():
    subprocess.run(["gh", "repo", "clone", "genovalia/metadonnees", str(METADONNEES), "--", "-q"], check=True)
DUMP = Path(os.environ.get("GENOVALIA_DUMP", "/home/steve/Desktop/dump-ul_val_prj_ext_genovalia-202609281022.sql"))
SEDNA = "https://sedna.apps.genovalia.ulaval.ca/datasets/"
CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
CCBY = "https://creativecommons.org/licenses/by/4.0/"
OPEN = "http://purl.org/eprint/accessRights/OpenAccess"
MISSING_RATE_FLAG = 0.20

# Sources that could not be fetched automatically. Drop the file at `dest` and re-run.
MANUAL_DOWNLOADS = [
    ("prusal1", "PDF de l'article (XML PMC déjà récupéré)", "https://europepmc.org/articles/PMC11536197?pdf=render", "prusal1/raw/papers/", "refusé (403)"),
    ("anogla1", "PDF de l'article (XML PMC déjà récupéré)", "https://europepmc.org/articles/PMC9234632?pdf=render", "anogla1/raw/papers/", "refusé (403)"),
    ("picsit1", "PDF de l'article (XML PMC déjà récupéré)", "https://europepmc.org/articles/PMC10989875?pdf=render", "picsit1/raw/papers/", "refusé (403)"),
    ("triaes1", "PDF de l'article (XML PMC déjà récupéré)", "https://europepmc.org/articles/PMC10230752?pdf=render", "triaes1/raw/papers/", "refusé (403)"),
]


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


def write_vcf(path, samples, rows, source):
    """rows: iterable of (chrom, pos, id, ref, alt, [GT strings])."""
    with open(path, "w") as f:
        f.write("##fileformat=VCFv4.2\n")
        f.write(f"##fileDate={TODAY.replace('-', '')}\n")
        f.write(f'##source="genovalia build.py conversion from {source}"\n')
        f.write('##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n')
        f.write("#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t" + "\t".join(samples) + "\n")
        for chrom, pos, vid, ref, alt, gts in rows:
            f.write(f"{chrom}\t{pos}\t{vid}\t{ref}\t{alt}\t.\t.\t.\tGT\t" + "\t".join(gts) + "\n")


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
ATTR = {
    "id": ("Text", "id", "ID referring to the individual in the genotyping file (vcf)", None),
    "organism": ("Text", "organism", "Species name in latin according to the Linneaus classification", None),
    "country": ("Text", "country", "Country of origin of the individuals sampled", None),
    "latitude": ("Numeric", "latitude", "Latitude in decimal degree. Ex: -42.23452", "degree"),
    "longitude": ("Numeric", "longitude", "Longitude in decimal degree. Ex: -42.23452", "degree"),
    "site_code": ("Text", "site_code", "Code of the sampling site as used in the source publication", None),
    "site_type": ("Text", "site_type", "General environmental or habitat category of the sampling site", None),
    "region": ("Text", "region", "Geographic region of the sampling site", None),
    "sampling_year": ("Numeric", "sampling_year", "Year the individual was sampled", None),
    "sample_tissue": ("Text", "sample_tissue", "The type of tissue sampled from the individual and used for DNA extraction", None),
    "sequenced_molecule": ("Text", "sequenced_molecule", "Sequenced molecule such as DNA, RNA...", None),
    "genotyping_technology": ("Text", "genotyping_technology", "Genotyping technology used to obtain genotypes, such as SNP chip, genotyping-by-sequencing, etc.", None),
}
UCUM = {"km": "km", "g": "g", "kg": "kg", "degree": "deg", "degree Celsius": "Cel", "mg/m3": "mg/m3", "m": "m", "ha": "har", "cm": "cm", "mm": "mm",
        "um": "um", "nm": "nm", "kg/m3": "kg/m3", "GPa": "GPa", "ug/m": "ug/m", "m2/kg": "m2/kg", "1/mm2": "/mm2",
        "ppm": "[ppm]", "%": "%", "MJ/m2": "MJ/m2", "degree-day": "d"}


def build_oca(name, description, columns, extra, entries=None, classification="RDF106"):
    """columns: ordered CSV columns; extra: {attr: (type, label, info, unit)} for dataset-specific
    attributes; entries: {attr: {code: label}} for categorical attributes.
    Mirrors the Semantic Engine oca_package/1.0 layout (key order included) so SAIDs verify."""
    spec = {c: (extra.get(c) or ATTR[c]) for c in columns}
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


def build_dcat(ds_id, m):
    """Same shape as the Genovalia DCAT builder (buildObj) and the metadonnees repo.
    'dct' is added to @context because metadata-api reads dct:relation and dct:* keys
    inside distributions (both prefixes expand to http://purl.org/dc/terms/)."""
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
        "dcat:distribution": [{"@type": "dcat:Distribution", "dct:title": t, "dcat:accessURL": u, "dct:license": m["license"]}
                              for t, u in m["distributions"]],
    }
    if m.get("modified"): d["dcterms:modified"] = m["modified"]
    if m.get("years"):
        y0, y1 = m["years"]
        t = {"@type": "dcterms:PeriodOfTime", "time:hasBeginning": {"@type": "time:Instant", "time:inXSDgYear": str(y0)}}
        if y1: t["time:hasEnd"] = {"@type": "time:Instant", "time:inXSDgYear": str(y1)}
        d["dcterms:temporal"] = t
    d["dcterms:spatial"] = f"https://sws.geonames.org/{m['geonames']}/"
    d["dcat:version"] = m.get("version", "1.0.0")
    d["dcterms:license"] = m["license"]
    d["dcterms:accessRights"] = OPEN
    c = m["people"][0]
    d["dcterms:creator"] = {k: v for k, v in {"@id": orcid(c), "@type": "foaf:Person", "foaf:name": c["name"]}.items() if v}
    d["dcat:qualifiedAttribution"] = [{"@type": "prov:Attribution", "prov:agent": agent(p), "dcat:hadRole": p["roles"]} for p in m["people"]]
    d["dct:relation"] = [f"https://doi.org/{x}" for x in m["relations"]]
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


meta_oca = _load_module("meta_oca", REPO / "metadata-api/app/services/oca.py")
meta_dcat = _load_module("meta_dcat", REPO / "metadata-api/app/services/dcat.py")
de_oca = _load_module("de_oca", REPO / "data-explorer-backend/app/services/oca_schema.py")
sys.path.insert(0, str(METADONNEES))
from catalogue_cli import check_dcat  # noqa: E402  (genovalia/metadonnees validate command)


def existing_ids():
    """metadata_api.datasets ids and data_explorer.dataset.dataset_id values in the SQL dump."""
    ids, section = set(), None
    for line in open(DUMP, encoding="utf-8"):
        if line.startswith("COPY "):
            section = line.split()[1]
            continue
        if line.startswith("\\."):
            section = None
            continue
        if section == "metadata_api.datasets":
            ids.add(line.split("\t", 1)[0])
        elif section == "data_explorer.dataset":
            v = line.rstrip("\n").split("\t")[-1]
            if v != "\\N": ids.add(v)
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
    sites = df["site_code"].nunique() if "site_code" in df else None
    if exp.get("sites") and sites is not None and sites != exp["sites"]:
        minor.append(f"{sites} sites dans les données vs {exp['sites']} annoncés ({exp['src']})")
    empty = [c for c in df.columns if df[c].isna().all()]
    if empty: blocking.append(f"colonnes entièrement vides : {empty}")

    # OCA ↔ CSV ↔ parsers
    oca = res["oca"]
    bad_said = verify_saids(oca)
    if bad_said: blocking.append(f"SAID invalides : {bad_said}")
    props = meta_oca.extract_properties(oca)
    oca_attrs = {p["name"] for p in props}
    if oca_attrs != set(df.columns): blocking.append(f"attributs OCA ≠ colonnes CSV : {sorted(oca_attrs ^ set(df.columns))}")
    pj = de_oca.OcaSchema._parse_json_file(json.dumps(oca).encode())
    de_vars = de_oca.OcaSchema(pj["root_content"], pj["format_content"], pj["information_content"],
                               pj["label_content"], pj["unit_content"]).variables
    if {v["key"] for v in de_vars} != set(df.columns): blocking.append("le parseur OCA de data-explorer ne retrouve pas toutes les colonnes")

    # DCAT
    dcat = res["dcat"]
    errs, warns = check_dcat(ds_id, dcat)
    try:
        facts = meta_dcat.extract_dataset_facts(dcat)
    except Exception as e:  # DcatFieldError
        errs.append(f"metadata-api refuse le DCAT : {e}")
        facts = {}
    for e in errs: blocking.append(f"DCAT : {e}")
    if facts and not facts["related_urls"]: minor.append("DCAT : dct:relation non lu par metadata-api")

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
           f"| DCAT (validate metadonnees + metadata-api) | {'OK' if not errs else len(errs)} erreurs, avertissements : {warns or 'aucun'} |", "",
           "## Problèmes bloquants", ""] + ([f"- {x}" for x in blocking] or ["Aucun."]) + [
           "", "## Problèmes mineurs / à vérifier", ""] + ([f"- {x}" for x in minor] or ["Aucun."]) + [
           "", "## Décisions de préparation", ""] + [f"- {x}" for x in res.get("decisions", [])]
    if only_vcf: rep += ["", "## ID du VCF absents du CSV", "", ", ".join(only_vcf[:200])]
    if only_csv: rep += ["", "## ID du CSV absents du VCF", "", ", ".join(only_csv[:200])]
    if high: rep += ["", f"## Individus > {MISSING_RATE_FLAG:.0%} manquants", "", ", ".join(f"{k} ({smiss[k]:.0%})" for k in high[:200])]
    if het_out: rep += ["", "## Individus à hétérozygotie élevée", "", ", ".join(f"{k} ({st['sample_het'][k]:.1%})" for k in het_out)]
    rep += ["", "## Colonnes du CSV", "", "| Colonne | Type OCA | Unité | Non-vides | Valeurs distinctes |", "|---|---|---|---|---|"]
    types = {p["name"]: p for p in props}
    for c in df.columns:
        rep.append(f"| {c} | {types.get(c, {}).get('type')} | {types.get(c, {}).get('unit') or ''} | {df[c].notna().sum()} | {df[c].nunique()} |")
    rep += ["", "## En-tête du VCF (extrait)", "", "```", *st["meta"][:15], "```", ""]
    return row, blocking, minor, "\n".join(rep)


# ─────────────────────────────── datasets ───────────────────────────────
# Each prepare_<id>(raw) returns dict(vcf, samples, vcf_origin, attr_level, reference, extra,
# entries, decisions, blocking, minor). Column meaning lives in `extra` (→ OCA).

def _dna(df, tech):
    df["sequenced_molecule"] = "DNA"
    df["genotyping_technology"] = tech
    return df


def _vcf_samples(path):
    with _open_text(path) as f:
        for line in f:
            if line.startswith("#CHROM"): return line.rstrip("\n").split("\t")[9:]


def prepare_malvil1(raw, out):
    dec, minor = [], []
    txt = (raw / "papers/biorxiv_782201v1.txt").read_text()
    sites = pd.DataFrame(
        [m.groups() for m in re.finditer(r"^([A-Z]\d+) (ARC|GRE|NWA) (\S+) ([\d.]+) (-[\d.]+)\s+(.+?)\s+(\d+)\s*$", txt, re.M)],
        columns=["site_code", "lineage_t1", "region", "latitude", "longitude", "site_type", "n_t1"])
    assert len(sites) == 31, len(sites)
    dec.append("Coordonnées, région, type de site et lignée : Table 1 du preprint bioRxiv 10.1101/782201 (31 sites).")
    pop = pd.read_excel(raw / "POP.xlsx", sheet_name=0)
    pop.columns = ["site_code", "id", "glacial_lineage"]
    pop["glacial_lineage"] = pop["glacial_lineage"].replace({"GREE": "GRE"})
    dec.append("Individu → site et lignée glaciaire : POP.xlsx (feuille 1). Code de lignée « GREE » harmonisé en « GRE » (Table 1).")
    env = pd.read_csv(raw / "Environmental_data.csv", sep=";")
    env.columns = ["site_code", "temperature", "chlorophyll"]
    dec.append("Température et chlorophylle : Environmental_data.csv (19 sites de fraie de plage de la lignée NWA). Unités confirmées par la Table S14 du supplément du preprint : °C et mg·m⁻³.")
    df = pd.DataFrame({"id": _vcf_samples(raw / "batch_4.maxmaf.pruned.whitelist3.maxmiss97.recode.vcf")})
    df = df.merge(pop, on="id", how="left").merge(sites, on="site_code", how="left").merge(env, on="site_code", how="left")
    extra_sites = sorted(set(pop.site_code) - set(sites.site_code))
    in_vcf = df[df.site_code.isin(extra_sites)].groupby("site_code").size().to_dict()
    if extra_sites:
        minor.append(f"Sites de POP.xlsx absents de la Table 1 (pas de coordonnées) : {extra_sites} ; individus présents dans le VCF : {in_vcf or 'aucun'}")
    disagree = df[df.lineage_t1.notna() & (df.lineage_t1 != df.glacial_lineage)]
    if len(disagree): minor.append(f"{len(disagree)} individus dont la lignée (POP.xlsx) diffère de celle du site (Table 1)")
    n_vcf = df.groupby("site_code").size()
    diff = {s: (int(n_vcf.get(s, 0)), int(n)) for s, n in zip(sites.site_code, sites.n_t1) if int(n_vcf.get(s, 0)) != int(n)}
    if diff: minor.append(f"Effectifs par site VCF vs Table 1 (preprint) différents pour {len(diff)} sites : " + ", ".join(f"{k} {a}/{b}" for k, (a, b) in sorted(diff.items())))
    df["organism"] = "Mallotus villosus"
    df["country"] = df.site_code.str[0].map(lambda c: "Greenland" if c == "G" else "Canada")
    df["latitude"] = pd.to_numeric(df.latitude); df["longitude"] = pd.to_numeric(df.longitude)
    df = _dna(df, "genotyping-by-sequencing (ddRAD, PstI/MspI, Ion Proton)")
    cols = ["id", "organism", "country", "latitude", "longitude", "site_code", "site_type", "region", "glacial_lineage",
            "temperature", "chlorophyll", "sequenced_molecule", "genotyping_technology"]
    link(raw / "batch_4.maxmaf.pruned.whitelist3.maxmiss97.recode.vcf", out / "malvil1.vcf")
    n31 = int(df.site_code.isin(sites.site_code).sum())
    minor.append(f"Le VCF Dryad n'est pas le jeu analysé dans l'article publié : il couvre 34 sites (1 474 individus, {n31} sur les 31 sites de l'article) et applique un seuil d'appel de 97 % (« maxmiss97 », 17 436 SNPs), alors que l'article retient 25 904 SNPs présents chez ≥ 90 % des individus, 1 310 individus, 31 sites. La liste des 1 310 individus analysés n'est pas déposée : la demander aux auteurs")
    minor.append("Sexe noté sur le terrain (article publié : « collected during the breeding period … and sexed ») mais absent des fichiers déposés")
    minor.append("Année d'échantillonnage absente du dépôt, du preprint et de l'article publié ; DCAT conserve 2014 (valeur existante, non vérifiée)")
    return dict(vcf=out / "malvil1.vcf", samples=df[cols], vcf_origin="fourni (Dryad)", attr_level="individuels (site, lignée) + site",
                reference="ébauche de génome capelan PacBio/Flye (ENA PRJEB38139, contig_*)", decisions=dec, minor=minor,
                extra={"glacial_lineage": ("Text", "glacial_lineage", "Glacial lineage the individual was assigned to (ARC: Arctic, GRE: Greenland, NWA: Northwest Atlantic)", None),
                       "temperature": ("Numeric", "temperature", "Water temperature at the spawning site, site-level value (Table S14 of the preprint supplement)", "degree Celsius"),
                       "chlorophyll": ("Numeric", "chlorophyll", "Chlorophyll concentration at the spawning site, site-level value (Table S14 of the preprint supplement)", "mg/m3")},
                entries={"glacial_lineage": {"ARC": "Arctic", "GRE": "Greenland", "NWA": "Northwest Atlantic"}})


def prepare_picmar2(raw, out):
    dec, minor = [], []
    g = pd.read_csv(raw / "BlackSpruceSNPs_FILTERED.csv", index_col=0, low_memory=False)
    meta = pd.read_csv(raw / "BlackSpruceSNPs_FILTERED_metadata.tsv", sep="\t")
    samples = list(g.index)
    rows = []
    for snp in g.columns:
        clone, off, alle = snp.split("-", 2)
        ref, alt = alle.split("/")
        gts = [{0: "0/0", 1: "0/1", 2: "1/1"}.get(v, "./.") for v in g[snp].tolist()]
        rows.append((f"Tag_{clone}", int(off) + 1, snp, ref, alt, gts))
    rows.sort(key=lambda r: (r[0], r[1]))
    write_vcf(out / "picmar2.vcf", samples, rows, "BlackSpruceSNPs_FILTERED.csv (dartR genlight, Zenodo 19961100)")
    dec.append("VCF produit depuis BlackSpruceSNPs_FILTERED.csv : valeur = nombre d'allèles ALT (0/1/2, NA = manquant), REF/ALT lus dans le nom du SNP « CloneID-offset-REF/ALT ». Codage vérifié : 58 295/58 295 génotypes concordants avec BlackSpruce_SNPs_subset.vcf.gz (PLINK).")
    dec.append("CHROM = Tag_<CloneID> (étiquette DArTseq, pas de génome de référence), POS = offset + 1.")
    df = meta.drop(columns="Sample_ID").rename(columns={"POP": "site_code", "SITE_ID": "common_garden", "TISSUE_FOR_DNA": "sample_tissue",
                              "cluster": "genetic_cluster", "STRATA": "strata", "OUR_REPLICATES": "technical_replicate",
                              "altBATCH_ID": "genotyping_batch", "lat": "latitude", "lon": "longitude"})
    df["organism"] = df.SPECIES_ID.map({"EPN": "Picea mariana", "EPR": "Picea rubens"})
    df["site_code"] = df.site_code.astype(str)
    df = _dna(df, "DArTseq")
    red = int((df.organism == "Picea rubens").sum())
    minor.append(f"{red} individus d'épinette rouge (Picea rubens) inclus dans le fichier (groupe externe) ; organism = Picea rubens pour eux")
    minor.append(f"{int(df.technical_replicate.sum())} réplicats techniques (même arbre génotypé deux fois) : à exclure des analyses de population")
    minor.append("Pays non fourni : provenances au Canada et aux États-Unis (Alaska, Maine, Wisconsin) ; colonne country omise")
    dec.append("latitude/longitude = lieu d'origine de la provenance (graines), pas le test de descendance où l'arbre a été échantillonné (common_garden).")
    sup = pd.read_excel(raw / "papers/supp/biorxiv_media-2.xlsx", sheet_name=None)
    d1 = sup["Dataset 1"].rename(columns={"genotype": "id", "Call rate": "call_rate", "K4.Q.Central": "ancestry_k4_central",
                                          "K4.Q.East": "ancestry_k4_east", "K4.Q.West": "ancestry_k4_west", "K4.Q.RedSpruce": "ancestry_k4_redspruce",
                                          "BestK4": "best_cluster_k4", "BestK6": "best_cluster_k6"})
    keep = ["call_rate", "ancestry_k4_central", "ancestry_k4_east", "ancestry_k4_west", "ancestry_k4_redspruce", "best_cluster_k4", "best_cluster_k6"]
    df = df.merge(d1[["id"] + keep], on="id", how="left")
    matched = int(df.call_rate.notna().sum())
    dec.append(f"Attributs individuels du Supplementary Dataset 1 (preprint, media-2.xlsx) : taux d'appel, coefficients d'ascendance ADMIXTURE K=4 et groupes K=4/K=6, joints par ID pour {matched}/{len(df)} arbres (les {len(df) - matched} épinettes rouges n'y figurent pas).")
    clim = sup["Dataset 3"].rename(columns={"Population/garden": "site_code", "Elevation": "provenance_elevation", "MAT": "provenance_mat",
                                            "TP": "provenance_tp", "CMI": "provenance_cmi", "GDD5": "provenance_gdd5"})
    clim["site_code"] = clim.site_code.map(lambda v: str(int(v)) if isinstance(v, float) and v == int(v) else str(v))
    clim = clim[["site_code", "provenance_elevation", "provenance_mat", "provenance_tp", "provenance_cmi", "provenance_gdd5"]]
    df = df.merge(clim, on="site_code", how="left")
    dec.append(f"Climat de la provenance d'origine (Supplementary Dataset 3) : altitude, MAT, TP, CMI, GDD5 pour {int(df.provenance_mat.notna().sum())}/{len(df)} arbres.")
    import docx
    st1 = [[c.text.strip() for c in r.cells] for t in docx.Document(raw / "papers/supp/biorxiv_media-1.docx").tables[:2] for r in t.rows[1:]]
    st1 = pd.DataFrame(st1, columns=["site_code", "region_st1", "lat_st1", "lon_st1", "AC", "CH", "ML", "PR", "VL", "total"])
    chk = df.merge(st1, on="site_code", how="left")
    far = chk[(pd.to_numeric(chk.lat_st1, errors="coerce") - chk.latitude).abs().gt(0.05) | (pd.to_numeric(chk.lon_st1, errors="coerce") - chk.longitude).abs().gt(0.05)]
    nost1 = sorted(set(df.site_code[df.organism == "Picea mariana"]) - set(st1.site_code))  # red spruce outgroup is not in ST1
    dec.append(f"Coordonnées des provenances recoupées avec la Supplementary Table 1 du preprint ({len(st1)} provenances) : {len(far)} individus avec un écart > 0,05° (sur {chk.lat_st1.notna().sum()} recoupés).")
    if nost1: minor.append(f"{len(nost1)} codes de provenance absents de la Supplementary Table 1 : {nost1}")
    cols = ["id", "organism", "latitude", "longitude", "site_code", "common_garden", "strata", "genetic_cluster",
            "technical_replicate", "sample_tissue", "genotyping_batch", "call_rate", "ancestry_k4_central", "ancestry_k4_east",
            "ancestry_k4_west", "ancestry_k4_redspruce", "best_cluster_k4", "best_cluster_k6", "provenance_elevation", "provenance_mat",
            "provenance_tp", "provenance_cmi", "provenance_gdd5", "sequenced_molecule", "genotyping_technology"]
    return dict(vcf=out / "picmar2.vcf", samples=df[cols], vcf_origin="converti (matrice CSV 0/1/2)", attr_level="individuels + provenance",
                reference="aucune (étiquettes DArTseq)", decisions=dec, minor=minor,
                extra={"site_code": ("Text", "provenance", "Provenance (seed source population) code from the Range-Wide Provenance Study", None),
                       "common_garden": ("Text", "common_garden", "Common garden where the tree was sampled (CH: Chibougamau, AC: Acadia, ML: Mont-Laurier, PR: Peace River, VL: Valcartier)", None),
                       "strata": ("Text", "strata", "Sampling stratum code from the source metadata", None),
                       "genetic_cluster": ("Text", "genetic_cluster", "Genetic cluster assigned in the source study", None),
                       "technical_replicate": ("Boolean", "technical_replicate", "TRUE if this sample is a technical replicate of another tree in the file", None),
                       "genotyping_batch": ("Text", "genotyping_batch", "DArTseq genotyping batch", None),
                       "call_rate": ("Numeric", "call_rate", "Genotype call rate of the tree (Supplementary Dataset 1)", None),
                       "ancestry_k4_central": ("Numeric", "ancestry_k4_central", "ADMIXTURE ancestry coefficient, K=4, Central cluster", None),
                       "ancestry_k4_east": ("Numeric", "ancestry_k4_east", "ADMIXTURE ancestry coefficient, K=4, East cluster", None),
                       "ancestry_k4_west": ("Numeric", "ancestry_k4_west", "ADMIXTURE ancestry coefficient, K=4, West cluster", None),
                       "ancestry_k4_redspruce": ("Numeric", "ancestry_k4_redspruce", "ADMIXTURE ancestry coefficient, K=4, red spruce cluster", None),
                       "best_cluster_k4": ("Text", "best_cluster_k4", "Cluster with the highest ancestry coefficient at K=4", None),
                       "best_cluster_k6": ("Text", "best_cluster_k6", "Cluster with the highest ancestry coefficient at K=6", None),
                       "provenance_elevation": ("Numeric", "provenance_elevation", "Elevation of the seed provenance", "m"),
                       "provenance_mat": ("Numeric", "provenance_mat", "Mean annual temperature at the seed provenance", "degree Celsius"),
                       "provenance_tp": ("Numeric", "provenance_tp", "Total annual precipitation at the seed provenance", "mm"),
                       "provenance_cmi": ("Numeric", "provenance_cmi", "Climate moisture index at the seed provenance", None),
                       "provenance_gdd5": ("Numeric", "provenance_gdd5", "Degree-days above 5 °C at the seed provenance", "degree-day")},
                entries={"common_garden": {"CH": "Chibougamau (QC)", "AC": "Acadia (NB)", "ML": "Mont-Laurier (QC)", "PR": "Peace River (AB)", "VL": "Valcartier (QC)"}})


def prepare_prusal1(raw, out):
    dec, minor, dropped = [], [], 0
    with open(raw / "Postimputation_SNPs.vcf", encoding="utf-8") as f, open(out / "prusal1.vcf", "w") as o:
        for line in f:
            line = line.rstrip("\r\n")
            if line.startswith("##"):
                o.write(line.rstrip("\t").replace('""', '"') + "\n")
            elif line.startswith("#"):
                o.write(line.rstrip("\t") + "\n")
            elif line.split("\t", 1)[0] == "9":
                dropped += 1
            else:
                o.write(line + "\n")
    dec.append("VCF réparé : tabulations en fin de ligne d'en-tête retirées et guillemets doublés (\"\") corrigés (fichier passé par un tableur).")
    dec.append(f"{dropped} SNPs du « chromosome 9 » retirés : selon le README, ce sont les SNPs rejetés au contrôle qualité (P. salicina n'a que 8 chromosomes).")
    df = pd.DataFrame({"id": _vcf_samples(out / "prusal1.vcf")})
    m = df.id.str.extract(r"^(\d{5})-R(\d+)T(\d+)$")
    df["cross_id"], df["orchard_row"], df["orchard_tree"] = m[0], pd.to_numeric(m[1]), pd.to_numeric(m[2])
    df["accession_type"] = m[0].notna().map({True: "breeding selection", False: "cultivar or named selection"})
    df["organism"], df["country"] = "Prunus salicina", "Canada"
    df["site_code"] = "Vineland Research Station"
    df = _dna(df, "genotyping-by-sequencing (ApeKI, Fast-GBS, imputed with BEAGLE 4.1)")
    minor.append("Phénotypes de nodule noir : l'article Horticulturae 2025 (récupéré) ne publie que des distributions (échelle 0-5, médiane de 4 notes, avril 2023) sans table par arbre, malgré « data available in the manuscript ». Demander la table à J. Subramanian (jsubrama@uoguelph.ca)")
    minor.append("Génotypes imputés (BEAGLE) : pas de données manquantes, mais une partie des génotypes est inférée")
    minor.append("Pas de coordonnées : un seul verger (Vineland Research Station, ON) ; l'origine géographique des cultivars n'est pas documentée")
    cols = ["id", "organism", "country", "site_code", "accession_type", "cross_id", "orchard_row", "orchard_tree", "sequenced_molecule", "genotyping_technology"]
    return dict(vcf=out / "prusal1.vcf", samples=df[cols], vcf_origin="fourni, réparé (Borealis)", attr_level="nom d'accession seulement",
                reference="GCA_014863905.1 (P. salicina « Sanyueli »)", decisions=dec, minor=minor,
                extra={"site_code": ("Text", "site", "Orchard where the tree grows and was sampled", None),
                       "accession_type": ("Text", "accession_type", "Whether the accession is a breeding selection (cross-row-tree code) or a named cultivar/selection", None),
                       "cross_id": ("Text", "cross_id", "Breeding cross identifier parsed from the accession code (e.g. 20005 in 20005-R1T59)", None),
                       "orchard_row": ("Numeric", "orchard_row", "Orchard row of the tree, parsed from the accession code", None),
                       "orchard_tree": ("Numeric", "orchard_tree", "Tree number within the orchard row, parsed from the accession code", None)})


def prepare_anogla1(raw, out):
    dec, minor = [], []
    a, b = raw / "Agla_359_6102.vcf", raw / "Agla_37_6102.vcf"
    la = [l for l in open(a) if not l.startswith("##")]
    lb = [l for l in open(b) if not l.startswith("##")]
    ka = [tuple(l.split("\t", 5)[:5]) for l in la[1:]]; kb = [tuple(l.split("\t", 5)[:5]) for l in lb[1:]]
    assert ka == kb, "the two beetle VCFs do not share the same variants"
    with open(out / "anogla1.vcf", "w") as o:
        o.writelines(l for l in open(a) if l.startswith("##"))
        for x, y in zip(la, lb):
            o.write(x.rstrip("\n") + "\t" + y.rstrip("\n").split("\t", 9)[9] + "\n")
    dec.append("Agla_359_6102.vcf (Chine) et Agla_37_6102.vcf (Corée du Sud) fusionnés : mêmes 6102 variants dans le même ordre (vérifié).")
    s = pd.read_csv(raw / "sample_list.txt", sep="\t")
    s.columns = ["id", "site_code", "latitude", "longitude", "locality", "region", "country", "sampling_year", "life_stage", "collector", "filter", "note"]
    df = pd.DataFrame({"id": _vcf_samples(out / "anogla1.vcf")}).merge(s, on="id", how="left")
    excl = s[~s.id.isin(df.id)]
    if len(excl): dec.append(f"{len(excl)} individus de sample_list.txt absents des VCF (colonne FILTER : {excl['filter'].value_counts().to_dict()}) ; non inclus dans le CSV.")
    df["relatedness_filtered"] = df["filter"].fillna("NO").str.strip() != "NO"
    kept_flag = df[df.relatedness_filtered]
    if len(kept_flag): minor.append(f"{len(kept_flag)} individus présents dans le VCF mais marqués « filtered by relatedness in SNPrelate » dans sample_list.txt : conservés, colonne relatedness_filtered = TRUE pour les exclure au besoin")
    df["organism"] = "Anoplophora glabripennis"
    df = _dna(df, "genotyping-by-sequencing (Fast-GBS/Platypus)")
    cols = ["id", "organism", "country", "latitude", "longitude", "site_code", "locality", "region", "sampling_year", "life_stage", "relatedness_filtered", "sequenced_molecule", "genotyping_technology"]
    return dict(vcf=out / "anogla1.vcf", samples=df[cols], vcf_origin="fourni (Borealis), 2 fichiers fusionnés", attr_level="individuels (site, année, stade)",
                reference="GCA_000390285.1 (Agla_1.0, 10 474 scaffolds)", decisions=dec, minor=minor,
                extra={"site_code": ("Text", "population_code", "Population code as used in the source publication", None),
                       "locality": ("Text", "locality", "Sampling locality (city, province)", None),
                       "region": ("Text", "region", "Geographic region of the sampling locality", None),
                       "life_stage": ("Text", "life_stage", "Life stage of the sampled individual", None),
                       "relatedness_filtered": ("Boolean", "relatedness_filtered", "TRUE if the authors flagged the individual as related to another one (SNPrelate) and excluded it from their analyses", None)})


def _salfon_table1(txt):
    t = re.sub(r"[ \t]*\n-\n", " -", txt)  # pypdf splits negative numbers onto their own line
    t = re.sub(r"[ \t]*\nanadro\nmous", " anadromous", t)
    rows = re.findall(r"^([A-Z]{3}) +([\d.]+) +(-[\d.]+) +([A-Za-z]+) +(\d+) +(\d+) +(\S+) +(-?[\d.]+) +([\d.]+) +([\d.]+)", t, re.M)
    return pd.DataFrame(rows, columns=["site_code", "latitude", "longitude", "site_type", "altitude", "n_t1", "lake_size",
                                       "min_air_temperature", "total_radiation", "growing_degree_days"])


def prepare_salfon1(raw, out):
    dec, minor = [], []
    vcf = raw / "50_pop_without_outliers_ind_filtered_4_70_0_2.singleton.unlinked_0.5.1M.vcf"
    link(vcf, out / "salfon1.vcf")
    sites = _salfon_table1((raw / "papers/biorxiv_660621v1.txt").read_text())
    dec.append(f"Coordonnées et variables de site : Table 1 du preprint bioRxiv 10.1101/660621 ({len(sites)} sites extraits du PDF).")
    df = pd.DataFrame({"id": _vcf_samples(vcf)})
    df["site_code"] = df.id.str.split("_").str[0]
    env = pd.read_excel(raw / "papers/supp/biorxiv_media-4.xlsx", sheet_name="Suppl Table 1").rename(columns=lambda c: str(c).strip())
    env = env.rename(columns={"NAME": "site_code", "Region": "region", "Latitude": "lat_s1", "Longitude": "lon_s1",
                              "Average_of_Mean_TMean": "mean_air_temperature", "Average_of_Mean_TMin": "mean_min_air_temperature",
                              "Average_of_Mean_TMax": "mean_max_air_temperature", "Average_of_Frost_Free_Days": "frost_free_days",
                              "River_drainage": "river_drainage"})
    env = env[["site_code", "region", "lat_s1", "lon_s1", "mean_air_temperature", "mean_min_air_temperature", "mean_max_air_temperature", "frost_free_days", "river_drainage"]]
    dec.append(f"Région, bassin versant et températures moyennes : Supplementary Table 1 du preprint (xlsx, {len(env)} sites).")
    both = sites.merge(env, on="site_code")
    gap = both[(pd.to_numeric(both.latitude) - both.lat_s1).abs().gt(1e-4) | (pd.to_numeric(both.longitude) - both.lon_s1).abs().gt(1e-4)]
    dec.append(f"Coordonnées Table 1 (PDF) et Supplementary Table 1 (xlsx) : {len(both)} sites communs, {len(gap)} écarts.")
    if len(gap): minor.append(f"Coordonnées différentes entre Table 1 et Supplementary Table 1 pour {list(gap.site_code)}")
    df = df.merge(sites, on="site_code", how="left").merge(env.drop(columns=["lat_s1", "lon_s1"]), on="site_code", how="left")
    missing = sorted(set(df.site_code) - set(sites.site_code))
    if missing: minor.append(f"{len(missing)} sites du VCF absents de la Table 1 extraite (pas de coordonnées) : {missing}")
    for c in ["latitude", "longitude", "altitude", "min_air_temperature", "total_radiation", "growing_degree_days"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["lake_size"] = pd.to_numeric(df.lake_size.replace({"NA": None, "-": None}), errors="coerce")
    df["organism"], df["country"] = "Salvelinus fontinalis", "Canada"
    df["sampling_year"] = df.site_type.map({"anadromous": "2000-2001", "lake": "2014-2015", "river": "2014-2015"})
    df = _dna(df, "genotyping-by-sequencing (ddRAD)")
    minor.append("Année d'échantillonnage connue seulement par type de site (lacs/rivières 2014-2015, anadromes 2000-2001) : colonne sampling_period")
    dec.append(f"Effectifs : la Table 1 du preprint totalise {sites.n_t1.astype(int).sum()} individus ; l'article publié (10.1111/mec.15566) confirme 1 416 individus sur 50 sites, soit le VCF déposé.")
    df = df.rename(columns={"sampling_year": "sampling_period"})
    cols = ["id", "organism", "country", "latitude", "longitude", "site_code", "site_type", "region", "river_drainage", "altitude", "lake_size",
            "mean_air_temperature", "mean_min_air_temperature", "mean_max_air_temperature", "min_air_temperature", "frost_free_days",
            "total_radiation", "growing_degree_days", "sampling_period", "sequenced_molecule", "genotyping_technology"]
    return dict(vcf=out / "salfon1.vcf", samples=df[cols], vcf_origin="fourni (Dryad → Zenodo)", attr_level="site seulement (préfixe de l'ID)",
                reference="NC_036838.1… (génome de l'omble chevalier, Salvelinus sp. ASM291031v2)", decisions=dec, minor=minor,
                extra={"altitude": ("Numeric", "altitude", "Altitude of the sampling site", "m"),
                       "lake_size": ("Numeric", "lake_size", "Lake surface area (lakes only)", "ha"),
                       "min_air_temperature": ("Numeric", "min_air_temperature", "Average of lowest minimum air temperature (BioSim 2004-2015), as reported in Table 1 of the source preprint", "degree Celsius"),
                       "total_radiation": ("Numeric", "total_radiation", "Average of total radiation (BioSim), as reported in Table 1 of the source preprint", "MJ/m2"),
                       "growing_degree_days": ("Numeric", "growing_degree_days", "Growing degree-days (BioSim)", "degree-day"),
                       "river_drainage": ("Numeric", "river_drainage", "River drainage (watershed) number from Supplementary Table 1 of the source preprint", None),
                       "mean_air_temperature": ("Numeric", "mean_air_temperature", "Average of mean air temperature (BioSim 2004-2015)", "degree Celsius"),
                       "mean_min_air_temperature": ("Numeric", "mean_min_air_temperature", "Average of mean minimum air temperature (BioSim 2004-2015)", "degree Celsius"),
                       "mean_max_air_temperature": ("Numeric", "mean_max_air_temperature", "Average of mean maximum air temperature (BioSim 2004-2015)", "degree Celsius"),
                       "frost_free_days": ("Numeric", "frost_free_days", "Average number of frost-free days (BioSim)", None),
                       "sampling_period": ("Text", "sampling_period", "Sampling years for the site type (lakes and rivers 2014-2015, anadromous 2000-2001)", None)})


def prepare_homame1(raw, out):
    dec, minor = [], []
    vcf = raw / "filtered_singleton_SNPs.vcf"
    link(vcf, out / "homame1.vcf")
    t = (raw / "papers/supp/biorxiv_media-1.txt").read_text()
    sites = pd.DataFrame(re.findall(r"^(T\dP\d) ([\d.]+) (-[\d.]+) (\d+) [\d.,]+ [\d,]+ (-?[\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+)", t, re.M),
                         columns=["site_code", "latitude", "longitude", "n_s1", "sst_min", "sst_max", "sst_variance", "sst_mean", "sst_range"])
    assert len(sites) == 21, len(sites)
    dec.append("Coordonnées et températures de surface (SST annuelle) : Table S1 du supplément du preprint bioRxiv 10.1101/2020.01.28.923490.")
    df = pd.DataFrame({"id": _vcf_samples(vcf)})
    df["site_code"] = df.id.str.split("-").str[0].replace({"T2X": "T2P5"})
    dec.append("Code de site « T2X » des ID du VCF associé à T2P5 de la Table S1 (seul site manquant, même effectif : 36).")
    dec.append("Article publié : 1 141 homards séquencés, 60 retirés au contrôle qualité (profondeur, > 15 % manquants, hétérozygotie, apparentement) → 1 081 ; 14 534 SNPs simple copie, identiques au VCF.")
    minor.append("Préfixe « T2X » des ID VCF ≠ code T2P5 de la Table S1 : correspondance déduite (effectif identique), à confirmer")
    df = df.merge(sites, on="site_code", how="left")
    for c in ["latitude", "longitude", "sst_min", "sst_max", "sst_variance", "sst_mean", "sst_range"]: df[c] = pd.to_numeric(df[c])
    df["organism"], df["country"], df["sampling_year"] = "Homarus americanus", "Canada", 2016
    df["sample_tissue"] = "walking leg"
    df = _dna(df, "RAD capture (Rapture)")
    minor.append("Seul le VCF des SNPs simple copie est préparé ; le VCF des SNPs dupliqués et les CNV (profondeur normalisée) sont aussi dans le dépôt")
    cols = ["id", "organism", "country", "latitude", "longitude", "site_code", "sampling_year", "sst_mean", "sst_min", "sst_max",
            "sst_range", "sst_variance", "sample_tissue", "sequenced_molecule", "genotyping_technology"]
    sst = lambda w: ("Numeric", f"sst_{w}", f"Annual {w} sea surface temperature at the sampling site (Bio-ORACLE, 30 arc-second)", "degree Celsius")
    return dict(vcf=out / "homame1.vcf", samples=df[cols], vcf_origin="fourni (Dryad → Zenodo)", attr_level="site seulement (préfixe de l'ID)",
                reference="aucune (catalogue de sondes Rapture, locus-*)", decisions=dec, minor=minor,
                extra={"sst_mean": sst("mean"), "sst_min": sst("minimum"), "sst_max": sst("maximum"), "sst_range": sst("range"),
                       "sst_variance": ("Numeric", "sst_variance", "Annual variance of sea surface temperature at the sampling site", None)})


def prepare_picgla2(raw, out):
    dec, minor = [], []
    g = pd.read_csv(raw / "S2.txt", sep="\t", index_col=0, dtype=str)
    rows, dropped = letters_to_rows(list(g.columns), [g[c].tolist() for c in g.columns],
                                    lambda x: x.split(":"), {"N", "NA"})
    write_vcf(out / "picgla2.vcf", list(g.index), rows, "S2.txt (Dryad 10.5061/dryad.6rd6f)")
    dec.append("VCF produit depuis S2.txt (génotypes en lettres « A:G », N = manquant). Aucun génome de référence ni position : CHROM = nom du marqueur, POS = 1, REF = allèle majeur, ALT = allèle mineur.")
    if dropped: dec.append("Marqueurs non convertis : " + ", ".join(f"{n} ({k})" for k, n in dropped.items()))
    ph = pd.read_csv(raw / "S1.txt", sep="\t").rename(columns={"Tree": "id"})
    part = ph.id.str.split("_", expand=True)
    ph["genetic_test"], ph["block"], ph["family"], ph["tree_number"] = part[0], pd.to_numeric(part[1]), part[2], pd.to_numeric(part[3])
    ph = ph.rename(columns={"q1": "mds_q1", "q2": "mds_q2", "cellpop": "cell_population", "coars": "fibre_coarseness", "cryst": "crystallite_width",
                            "dens": "wood_density", "mfa": "microfibril_angle", "moe": "wood_stiffness", "rad": "cell_radial_diameter",
                            "rwidth": "ring_width", "specsurf": "specific_fibre_surface", "tad": "cell_tangential_diameter",
                            "cellwt": "cell_wall_thickness", "h97": "height_22y"})
    ph["organism"], ph["country"] = "Picea glauca", "Canada"
    ph["site_code"], ph["latitude"], ph["longitude"], ph["elevation"] = "Mastigouche Arboretum", 46 + 38 / 60, -(73 + 13 / 60), 230
    df = _dna(ph, "SNP array (Illumina Infinium HD iSelect PgAS1)")
    dec.append("Article Heredity 2014 (fourni) : test de provenances-descendances de Mastigouche (46°38' N, 73°13' W, 230 m) planté en mai 1979 ; 3 arbres par famille échantillonnés 27 ans après la plantation (2006) et 5 autres 4 ans plus tard (2010). latitude/longitude = test, pas la provenance.")
    dec.append("Unités des 12 caractères confirmées par la Table 1 de l'article ; q1/q2 = 2 covariables de positionnement multidimensionnel (structure de population).")
    g_miss = (g == "N").values.mean()
    dec.append(f"Taux global de génotypes manquants : {g_miss:.2%} (article : 1,21 %).")
    minor.append("Provenance (43 populations naturelles du Québec) des 214 familles non fournie par famille : pas de coordonnées d'origine. Demander la correspondance famille → provenance (J. Beaulieu, J. Bousquet)")
    minor.append("Pas de positions génomiques (marqueurs géniques du catalogue GCAT) : les variants ne pourront pas être localisés")
    minor.append("Le dépôt contient plus de SNPs que l'article (6 385 après filtres GenTrain ≥ 0,25, |F| < 0,5, MAF ≥ 0,003, appel ≥ 50 %) : les filtres reproductibles sans GenTrain en retiennent 6 710 ; la liste des 6 385 n'est pas déposée")
    cols = ["id", "organism", "country", "latitude", "longitude", "elevation", "site_code", "genetic_test", "block", "family", "tree_number", "mds_q1", "mds_q2", "cell_population", "fibre_coarseness",
            "crystallite_width", "wood_density", "microfibril_angle", "wood_stiffness", "cell_radial_diameter", "ring_width",
            "specific_fibre_surface", "cell_tangential_diameter", "cell_wall_thickness", "height_22y", "sequenced_molecule", "genotyping_technology"]
    N = lambda label, info, unit=None: ("Numeric", label, info, unit)
    return dict(vcf=out / "picgla2.vcf", samples=df[cols], vcf_origin="converti (matrice TXT de lettres)", attr_level="individuels (famille, bloc, 12 phénotypes)",
                reference="aucune (marqueurs géniques PGAS1)", decisions=dec, minor=minor,
                extra={"site_code": ("Text", "test_site", "Provenance-progeny test site where the tree grows and was sampled", None),
                       "elevation": ("Numeric", "elevation", "Elevation of the test site", "m"),
                       "latitude": ("Numeric", "latitude", "Latitude of the test site (not of the seed provenance), decimal degrees", "degree"),
                       "longitude": ("Numeric", "longitude", "Longitude of the test site (not of the seed provenance), decimal degrees", "degree"),
                       "genetic_test": ("Text", "genetic_test", "Identifier of the progeny test (E560A3)", None),
                       "block": N("block", "Block number in the progeny test"), "family": ("Text", "family", "Open-pollinated family number", None),
                       "tree_number": N("tree_number", "Tree number within the family and block"),
                       "mds_q1": N("mds_q1", "First MDS coefficient accounting for population structure"),
                       "mds_q2": N("mds_q2", "Second MDS coefficient accounting for population structure"),
                       "cell_population": N("cell_population", "Wood cell population", "1/mm2"), "fibre_coarseness": N("fibre_coarseness", "Fibre coarseness", "ug/m"),
                       "crystallite_width": N("crystallite_width", "Cellulose crystallite width", "nm"), "wood_density": N("wood_density", "Wood density", "kg/m3"),
                       "microfibril_angle": N("microfibril_angle", "Microfibril angle", "degree"), "wood_stiffness": N("wood_stiffness", "Wood stiffness (modulus of elasticity)", "GPa"),
                       "cell_radial_diameter": N("cell_radial_diameter", "Cell radial diameter", "um"), "ring_width": N("ring_width", "Ring width", "mm"),
                       "specific_fibre_surface": N("specific_fibre_surface", "Specific fibre surface", "m2/kg"),
                       "cell_tangential_diameter": N("cell_tangential_diameter", "Cell tangential diameter", "um"),
                       "cell_wall_thickness": N("cell_wall_thickness", "Cell wall thickness", "um"), "height_22y": N("height_22y", "Tree height at 22 years (1997), column h97 of the deposit", "cm")})


def prepare_picsit1(raw, out):
    dec, minor = [], []
    lines = [l.split() for l in open(raw / "RADSeq_Genotype_Table.txt")]
    snps, chrom, pos, body = lines[0][1:], lines[1][1:], lines[2][1:], lines[3:]
    ids = [r[0] for r in body]
    rows, dropped = letters_to_rows(snps, [[r[j + 1] for r in body] for j in range(len(snps))],
                                    lambda x: x.split("/"), {"NA"}, chrom_pos=lambda j, m: (f"RAD_{chrom[j]}", int(pos[j])))
    rows.sort(key=lambda r: (r[0], r[1]))
    write_vcf(out / "picsit1.vcf", ids, rows, "RADSeq_Genotype_Table.txt (Dryad 10.5061/dryad.ghx3ffbv6)")
    dec.append("VCF produit depuis RADSeq_Genotype_Table.txt (le plus grand des 3 jeux : RAD-Seq, puce SNP, combiné). CHROM/POS = identifiants de séquence RAD du pipeline (pas des chromosomes, cf. SNPtable_to_VCF.sh), REF = allèle majeur.")
    if dropped: dec.append("Marqueurs non convertis : " + ", ".join(f"{n} ({k})" for k, n in dropped.items()))
    ped = pd.read_csv(raw / "RADSeq_Pedigree_vertical.csv", dtype=str).rename(columns={"ID": "id", "FAM": "family", "P1": "father", "P2": "mother"})
    df = pd.DataFrame({"id": ids}).merge(ped, on="id", how="left")
    df["role"] = df.father.eq("0").map({True: "parent", False: "offspring"})
    df["sex"] = df.Sex.where(df.role == "parent").map({"1": "male", "2": "female"})
    df["father"], df["mother"] = df.father.replace("0", None), df.mother.replace("0", None)
    df["organism"], df["country"] = "Picea sitchensis", "United Kingdom"
    df = _dna(df, "RAD-Seq")
    minor.append("Population de cartographie (2 familles de pleins-frères) : pas de coordonnées ni d'attributs environnementaux, seulement le pedigree")
    minor.append("Deux autres jeux de génotypes déposés (puce SNP : 615 ind., RAD+puce : 528 ind.) non convertis")
    cols = ["id", "organism", "country", "family", "role", "father", "mother", "sex", "sequenced_molecule", "genotyping_technology"]
    return dict(vcf=out / "picsit1.vcf", samples=df[cols], vcf_origin="converti (matrice TXT de lettres)", attr_level="pedigree (famille, parents)",
                reference="aucune (séquences RAD numérotées)", decisions=dec, minor=minor,
                extra={"family": ("Text", "family", "Full-sib family identifier (LM1 or LM2)", None),
                       "role": ("Text", "role", "Parent or offspring in the mapping pedigree", None),
                       "father": ("Text", "father", "ID of the father (offspring only)", None),
                       "mother": ("Text", "mother", "ID of the mother (offspring only)", None),
                       "sex": ("Text", "sex", "Sex of the parent tree (Sitka spruce is monoecious; assigned for parents only)", None)})


def prepare_triaes1(raw, out):
    dec, minor, blocking = [], [], []
    h = pd.read_csv(raw / "B2_Genotypic_data_2.tab", dtype=str)
    samples = list(h.columns[5:])
    split = lambda x: [x[0], x[1]] if len(x) == 2 else None
    rows, dropped = letters_to_rows(list(h["rs#"]), [r[5:].tolist() for _, r in h.iterrows()], split,
                                    {"NN", "N", "--"}, chrom_pos=lambda j, m: (h.at[j, "chrom"], int(h.at[j, "pos"])))
    write_vcf(out / "triaes1.vcf", samples, rows, "B2_Genotypic_data_2.tab (HapMap, Borealis 10.5683/SP3/Z9RLPP)")
    dec.append("VCF produit depuis le fichier HapMap B2_Genotypic_data_2.tab (puce 90K). CHROM/POS repris tels quels ; REF = allèle majeur (le format HapMap ne donne pas l'allèle de référence).")
    if dropped: dec.append("Marqueurs non convertis : " + ", ".join(f"{n} ({k})" for k, n in dropped.items()))
    df = pd.DataFrame({"id": samples})
    df["population"] = df.id.str.split(".").str[1]
    df["organism"], df["country"], df["site_code"] = "Triticum aestivum", "Canada", "Elora Research Station"
    df = _dna(df, "SNP array (Illumina 90K wheat iSelect)")
    ph = pd.read_csv(raw / "D2_phenotypic_data.tab")
    blocking.append(f"Phénotypes (DON, FDK, FHB 2017 et 2019) : {len(ph)} lignes sans identifiant d'individu (seulement un n° d'observation) ; impossible de les relier aux {len(samples)} génotypes de façon sûre. Non inclus : ordre des lignes à confirmer avec les auteurs")
    minor.append("Référence des positions non indiquée dans le dépôt (l'article utilise IWGSC RefSeq v1.0 pour les QTL)")
    minor.append("Coordonnées : un seul site d'essai (Elora, ON) ; origine des lignées CIMMYT non documentée")
    cols = ["id", "organism", "country", "site_code", "population", "sequenced_molecule", "genotyping_technology"]
    return dict(vcf=out / "triaes1.vcf", samples=df[cols], vcf_origin="converti (HapMap)", attr_level="population seulement (phénotypes non liables)",
                reference="non précisée (IWGSC RefSeq v1.0 probable)", decisions=dec, minor=minor, blocking=blocking,
                extra={"site_code": ("Text", "site", "Field trial site where the lines were evaluated", None),
                       "population": ("Text", "population", "Population label from the sample identifier (Pop1, Pop2, ...)", None)})


# ─────────────────────────────── configuration ───────────────────────────────
# Descriptions use {n} (individuals in VCF) and {snps} (variants in VCF), filled after preparation.
P = person
DATASETS = {
    "malvil1": dict(
        species="Mallotus villosus", taxid=30960, geonames=3411923, bbox=((40, 72), (-75, -45)),
        spatial_label={"en": "Northwest Atlantic", "fr": "Atlantique Nord-Ouest"},
        expected=dict(individuals=1310, snps=25904, sites=31, src="Dryad / article 2020"),
        paper_status="article publié + preprint + supplément (10.1111/mec.15499)",
        title="Capelin genotypic dataset for studying local adaptation to marine environments",
        title_fr="Données génotypiques pour l’étude de l’adaptation locale au milieu marin chez le capelan",
        description="This dataset contains genotypes at {snps} SNPs for {n} capelin individuals (Mallotus villosus) sampled at 31 spawning sites in the Northwest Atlantic (eastern Canada and Greenland) to study how demographic history, shared ancestral polymorphism, and gene flow among glacial lineages contribute to local adaptation to sea conditions. Genotypes were obtained using a genotyping-by-sequencing approach.",
        description_fr="Ce jeu de données contient les génotypes de {n} capelans (Mallotus villosus) à {snps} SNP, échantillonnés dans 31 sites de fraie de l’Atlantique Nord-Ouest (est du Canada et Groenland) afin d’étudier comment l’histoire démographique, le polymorphisme ancestral partagé et le flux de gènes entre lignées glaciaires contribuent à l’adaptation locale aux conditions marines. Les génotypes ont été obtenus par génotypage par séquençage (GBS).",
        keywords=["capelin", "Mallotus villosus", "genomics", "local adaptation", "marine environment", "population genetics"],
        people=[P("Louis Bernatchez", "0000-0002-8085-9709", ["principalInvestigator"]),
                P("Quentin Rougemont", "0000-0003-2987-3801", ["pointOfContact", "collaborator"]),
                P("Hugo Cayuela", "0000-0003-3250-6295", ["pointOfContact", "collaborator"])],
        years=(2014, 2014), license=CC0, relations=["10.1111/mec.15499", "10.1101/782201", "10.5061/dryad.hx3ffbgbp"],
        distributions=[("Dryad dataset (VCF, POP.xlsx, Environmental_data.csv)", "https://doi.org/10.5061/dryad.hx3ffbgbp")],
        issued="2026-09-08", modified=TODAY, version="1.1.0", oca_name="mallotus_villosus_local_adaptation"),
    "picmar2": dict(
        species="Picea mariana", taxid=3335, geonames=6255149, bbox=((35, 70), (-170, -50)),
        spatial_label={"en": "North America", "fr": "Amérique du Nord"},
        expected=dict(src="dépôt Zenodo / preprint 2025"),
        paper_status="preprint + suppléments (10.1101/2025.10.30.685617)",
        title="DArTseq genotypic dataset for testing genomic offset in black spruce common gardens",
        title_fr="Données génotypiques DArTseq pour tester le décalage génomique de l’épinette noire en tests de provenances",
        description="This dataset contains DArTseq genotypes at {snps} SNPs for {n} trees sampled between 2014 and 2019 in common gardens of the Range-Wide Provenance Study of black spruce (Picea mariana), representing provenances from the species' natural range in Canada and the USA, plus a red spruce (Picea rubens) outgroup. It was used to test genomic offset predictions against growth in common gardens.",
        description_fr="Ce jeu de données contient les génotypes DArTseq de {n} arbres à {snps} SNP, échantillonnés entre 2014 et 2019 dans les tests de l’étude des provenances de l’aire de répartition de l’épinette noire (Picea mariana), couvrant des provenances du Canada et des États-Unis, ainsi qu’un groupe externe d’épinette rouge (Picea rubens). Il a servi à tester les prédictions de décalage génomique face à la croissance en tests de provenances.",
        keywords=["black spruce", "Picea mariana", "DArTseq", "genomic offset", "common garden", "provenance trial"],
        people=[P("Anna Fijarczyk", "0000-0003-2278-9842"), P("Nathalie Isabel", None), P("Patrick Lenz", None)],
        years=(2014, 2019), license=CCBY, relations=["10.1101/2025.10.30.685617", "10.5281/zenodo.19961100"],
        distributions=[("Zenodo record (genotype matrices, metadata, VCF subset)", "https://doi.org/10.5281/zenodo.19961100")],
        oca_name="picea_mariana_genomic_offset"),
    "prusal1": dict(
        species="Prunus salicina", taxid=88123, geonames=6093943, bbox=None,
        spatial_label={"en": "Ontario, Canada", "fr": "Ontario, Canada"},
        expected=dict(individuals=200, snps=20659, src="README Borealis (20 579 dans l'article)"),
        paper_status="XML PMC + article Horticulturae fourni (10.1002/pei3.70016)",
        title="Genotypic dataset for studying black knot disease resistance in Japanese plum",
        title_fr="Données génotypiques pour l’étude de la résistance au nodule noir chez le prunier japonais",
        description="This dataset contains imputed genotyping-by-sequencing genotypes at {snps} SNPs for {n} Japanese plum (Prunus salicina) cultivars and breeding selections grown at the Vineland Research Station (Ontario, Canada), used in a genome-wide association study of black knot disease resistance.",
        description_fr="Ce jeu de données contient les génotypes imputés, obtenus par génotypage par séquençage, de {n} cultivars et sélections de prunier japonais (Prunus salicina) cultivés à la station de recherche de Vineland (Ontario, Canada) à {snps} SNP, utilisés dans une étude d’association pangénomique de la résistance au nodule noir.",
        keywords=["Japanese plum", "Prunus salicina", "black knot", "disease resistance", "GWAS", "Genotyping-by-Sequencing"],
        people=[P("Chloe Shum", "0009-0009-8275-8467"), P("Mohsen Najafabadi", "0000-0003-3631-4493"), P("Maxime de Ronne", "0000-0003-4062-3512"),
                P("Davoud Torkamaneh", "0000-0002-9782-5695"), P("Jayasankar Subramanian", "0000-0002-1236-6240", ["author", "pointOfContact"])],
        years=(2015, 2015), license=CCBY, relations=["10.1002/pei3.70016", "10.3390/horticulturae11050482", "10.5683/SP3/YXK7V3"],
        distributions=[("Borealis dataset (VCF, genotype matrix, sample names)", "https://doi.org/10.5683/SP3/YXK7V3")],
        oca_name="prunus_salicina_black_knot"),
    "anogla1": dict(
        species="Anoplophora glabripennis", taxid=217634, geonames=6255147, bbox=((20, 50), (100, 135)),
        spatial_label={"en": "China and South Korea", "fr": "Chine et Corée du Sud"},
        expected=dict(individuals=396, snps=6102, src="noms des VCF Borealis (359 + 37)"),
        paper_status="XML PMC téléchargé (10.1111/eva.13381)",
        title="Genotypic dataset for resolving the native population structure of the Asian longhorned beetle",
        title_fr="Données génotypiques pour résoudre la structure des populations indigènes du longicorne asiatique",
        description="This dataset contains genotyping-by-sequencing genotypes at {snps} SNPs for {n} Asian longhorned beetles (Anoplophora glabripennis) sampled across the native range in China and South Korea, used to resolve the species' native population structure and phylogeography.",
        description_fr="Ce jeu de données contient les génotypes, obtenus par génotypage par séquençage, de {n} longicornes asiatiques (Anoplophora glabripennis) à {snps} SNP, échantillonnés dans l’aire d’origine en Chine et en Corée du Sud, afin de résoudre la structure des populations indigènes et la phylogéographie de l’espèce.",
        keywords=["Asian longhorned beetle", "Anoplophora glabripennis", "invasive species", "phylogeography", "Genotyping-by-Sequencing"],
        people=[P("Mingming Cui", "0000-0001-8111-6555"), P("Yunke Wu"), P("Marion Javal"), P("Isabelle Giguère"), P("Géraldine Roux"),
                P("Jose Andres"), P("Melody Keena"), P("Juan Shi"), P("Baode Wang"), P("Evan Braswell"), P("Scott Pfister"),
                P("Richard Hamelin", "0000-0003-4006-532X", ["author", "principalInvestigator"]), P("Amanda Roe"), P("Ilga Porth", None, ["author", "principalInvestigator"])],
        years="samples", license=CC0, relations=["10.1111/eva.13381", "10.5683/SP3/JSFBFN"],
        distributions=[("Borealis dataset (VCFs, sample list)", "https://doi.org/10.5683/SP3/JSFBFN")],
        oca_name="anoplophora_glabripennis_phylogeography"),
    "salfon1": dict(
        species="Salvelinus fontinalis", taxid=8038, geonames=6115047, bbox=((44, 60), (-80, -56)),
        spatial_label={"en": "Quebec, Canada", "fr": "Québec, Canada"},
        expected=dict(individuals=1416, snps=14779, sites=50, src="titre Dryad"),
        paper_status="article publié + preprint + suppléments (10.1111/mec.15566)",
        title="Genotypic dataset for studying local adaptation and maladaptation in small brook trout populations",
        title_fr="Données génotypiques pour l’étude de l’adaptation et de la maladaptation locales dans de petites populations d’omble de fontaine",
        description="This dataset contains genotypes at {snps} high-quality SNPs for {n} brook trout (Salvelinus fontinalis) sampled in 50 lakes, rivers and anadromous populations in Quebec, Canada, to study adaptive and maladaptive genetic diversity in small populations.",
        description_fr="Ce jeu de données contient les génotypes de {n} ombles de fontaine (Salvelinus fontinalis) à {snps} SNP de haute qualité, échantillonnés dans 50 lacs, rivières et populations anadromes du Québec, afin d’étudier la diversité génétique adaptative et maladaptative dans de petites populations.",
        keywords=["brook trout", "Salvelinus fontinalis", "local adaptation", "small populations", "Genotyping-by-Sequencing", "population genetics"],
        people=[P("Anne-Laure Ferchaud", "0000-0002-9577-5508"), P("Maeva Leitwein"), P("Martin Laporte"), P("Damien Boivin-Delisle"), P("Bérénice Bougas"),
                P("Cécilia Hernandez", "0000-0002-4520-6569"), P("Eric Normandeau"), P("Isabel Thibault"),
                P("Louis Bernatchez", "0000-0002-8085-9709", ["author", "principalInvestigator"])],
        years=(2000, 2015), license=CC0, relations=["10.1111/mec.15566", "10.1101/660621", "10.5061/dryad.n02v6wwv6"],
        distributions=[("Dryad dataset (VCF)", "https://doi.org/10.5061/dryad.n02v6wwv6")],
        oca_name="salvelinus_fontinalis_local_adaptation"),
    "homame1": dict(
        species="Homarus americanus", taxid=6706, geonames=6251999, bbox=((44, 50), (-67, -58)),
        spatial_label={"en": "Southern Gulf of St. Lawrence, Canada", "fr": "Sud du golfe du Saint-Laurent, Canada"},
        expected=dict(individuals=1081, snps=14534, sites=21, src="article publié (1 141 séquencés − 60 retirés au QC)"),
        paper_status="article publié + preprint + supplément (10.1111/mec.15565)",
        title="Genotypic dataset for studying genotype-temperature associations in American lobster",
        title_fr="Données génotypiques pour l’étude des associations génotype-température chez le homard d’Amérique",
        description="This dataset contains RAD-capture genotypes at {snps} single-copy SNPs for {n} American lobsters (Homarus americanus) sampled in 2016 at 21 sites in the southern Gulf of St. Lawrence, Canada, used to compare SNPs and copy number variants for detecting genotype-temperature associations.",
        description_fr="Ce jeu de données contient les génotypes, obtenus par capture RAD, de {n} homards d’Amérique (Homarus americanus) à {snps} SNP simple copie, échantillonnés en 2016 dans 21 sites du sud du golfe du Saint-Laurent, afin de comparer les SNP et les variants de nombre de copies pour détecter des associations génotype-température.",
        keywords=["American lobster", "Homarus americanus", "copy number variants", "sea surface temperature", "population genetics"],
        people=[P("Yann Dorant", "0000-0002-7295-9398"), P("Hugo Cayuela"), P("Kyle Wellband"), P("Martin Laporte"), P("Quentin Rougemont"),
                P("Claire Mérot"), P("Éric Normandeau"), P("Rémy Rochette"), P("Louis Bernatchez", "0000-0002-8085-9709", ["author", "principalInvestigator"])],
        years=(2016, 2016), license=CC0, relations=["10.1111/mec.15565", "10.1101/2020.01.28.923490", "10.5061/dryad.vt4b8gtnv"],
        distributions=[("Dryad dataset (VCFs, CNV read depth, sea surface temperatures)", "https://doi.org/10.5061/dryad.vt4b8gtnv")],
        oca_name="homarus_americanus_temperature_association"),
    "picgla2": dict(
        species="Picea glauca", taxid=3330, geonames=6115047, bbox=((45, 50), (-75, -70)),
        spatial_label={"en": "Quebec, Canada", "fr": "Québec, Canada"},
        expected=dict(individuals=1694, snps=6385, src="article Heredity 2014"),
        paper_status="article fourni (10.1038/hdy.2014.36)",
        title="Genotypic and phenotypic dataset for genomic selection in open-pollinated families of white spruce",
        title_fr="Données génotypiques et phénotypiques pour la sélection génomique de familles uniparentales d’épinette blanche",
        description="This dataset contains gene SNP genotypes at {snps} markers and wood and growth phenotypes for {n} white spruce (Picea glauca) trees from 214 open-pollinated families representing 43 natural populations of Quebec, sampled in 2006 and 2010 in the Mastigouche provenance-progeny test (Quebec, Canada), used to assess the accuracy of genomic selection models.",
        description_fr="Ce jeu de données contient les génotypes de {n} épinettes blanches (Picea glauca) à {snps} marqueurs SNP géniques, ainsi que des phénotypes de bois et de croissance, provenant de 214 familles uniparentales représentant 43 populations naturelles du Québec, échantillonnées en 2006 et 2010 dans le test de provenances-descendances de Mastigouche (Québec), utilisés pour évaluer la précision de modèles de sélection génomique.",
        keywords=["white spruce", "Picea glauca", "genomic selection", "wood properties", "conifer", "genomics"],
        people=[P("Jean Beaulieu"), P("Trevor Doerksen"), P("Sébastien Clément"), P("John MacKay"), P("Jean Bousquet", None, ["author", "principalInvestigator"])],
        years=(2006, 2010), license=CC0, relations=["10.1038/hdy.2014.36", "10.5061/dryad.6rd6f"],
        distributions=[("Dryad dataset (genotypes S2.txt, phenotypes S1.txt)", "https://doi.org/10.5061/dryad.6rd6f")],
        oca_name="picea_glauca_genomic_selection"),
    "picsit1": dict(
        species="Picea sitchensis", taxid=3332, geonames=2635167, bbox=None,
        spatial_label={"en": "United Kingdom", "fr": "Royaume-Uni"},
        expected=dict(individuals=1111, snps=20269, src="README Dryad (RADSeq_Genotype_Table)"),
        paper_status="XML PMC téléchargé (10.1093/g3journal/jkae020)",
        title="Genotypic dataset for high-density genetic linkage mapping in Sitka spruce",
        title_fr="Données génotypiques pour la cartographie génétique à haute densité de l’épinette de Sitka",
        description="This dataset contains RAD-Seq genotypes at {snps} SNPs for {n} Sitka spruce (Picea sitchensis) trees from two full-sib families of the UK breeding population (parents and offspring), used to build a high-density genetic linkage map.",
        description_fr="Ce jeu de données contient les génotypes RAD-Seq de {n} épinettes de Sitka (Picea sitchensis) à {snps} SNP, issues de deux familles de pleins-frères de la population d’amélioration du Royaume-Uni (parents et descendants), utilisés pour construire une carte de liaison génétique à haute densité.",
        keywords=["Sitka spruce", "Picea sitchensis", "linkage map", "RAD-Seq", "conifer", "pedigree"],
        people=[P("Hayley Tumas", "0000-0003-1083-1687", ["author", "pointOfContact"]), P("Joanna J. Ilska"), P("Sebastien Gérardi"), P("Jerome Laroche"),
                P("Stuart A'Hara"), P("Brian Boyle"), P("Mateja Janes"), P("Paul McLean"), P("Gustavo Lopez"), P("Steve J. Lee"), P("Joan Cottrell"),
                P("Gregor Gorjanc"), P("Jean Bousquet"), P("John A. Woolliams"), P("John J. Mackay", None, ["author", "principalInvestigator"])],
        years=(2017, 2020), license=CC0, relations=["10.1093/g3journal/jkae020", "10.1101/2023.08.21.554184", "10.5061/dryad.ghx3ffbv6"],
        distributions=[("Dryad dataset (genotype tables, pedigrees, linkage maps)", "https://doi.org/10.5061/dryad.ghx3ffbv6")],
        oca_name="picea_sitchensis_linkage_map"),
    "triaes1": dict(
        species="Triticum aestivum", taxid=4565, geonames=6093943, bbox=None,
        spatial_label={"en": "Ontario, Canada", "fr": "Ontario, Canada"},
        expected=dict(individuals=200, src="article (194 lignées + 6 témoins)"),
        paper_status="XML PMC téléchargé (10.1186/s12870-023-04306-8)",
        title="Genotypic dataset for identifying fusarium head blight resistance markers in synthetic hexaploid derived wheat",
        title_fr="Données génotypiques pour l’identification de marqueurs de résistance à la fusariose de l’épi chez le blé dérivé d’hexaploïdes synthétiques",
        description="This dataset contains 90K SNP array genotypes at {snps} SNPs for {n} CIMMYT spring synthetic hexaploid derived wheat (Triticum aestivum) lines evaluated for fusarium head blight resistance at the Elora Research Station (Ontario, Canada) in 2017 and 2019.",
        description_fr="Ce jeu de données contient les génotypes, obtenus sur puce 90K, de {n} lignées de blé de printemps dérivé d’hexaploïdes synthétiques du CIMMYT (Triticum aestivum) à {snps} SNP, évaluées pour la résistance à la fusariose de l’épi à la station de recherche d’Elora (Ontario, Canada) en 2017 et 2019.",
        keywords=["wheat", "Triticum aestivum", "fusarium head blight", "GWAS", "disease resistance", "SNP array"],
        people=[P("Mitra Serajazari"), P("Davoud Torkamaneh", "0000-0002-9782-5695"), P("Emily Gordon"), P("Elizabeth Lee"),
                P("Helen Booker", "0000-0002-9800-1786"), P("Karl Peter Pauls", "0000-0003-0636-9151"), P("Alireza Navabi", None, ["author", "principalInvestigator"])],
        years=(2017, 2019), license=CCBY, relations=["10.1186/s12870-023-04306-8", "10.5683/SP3/Z9RLPP"],
        distributions=[("Borealis dataset (HapMap genotypes, GAPIT files, phenotypes)", "https://doi.org/10.5683/SP3/Z9RLPP")],
        oca_name="triticum_aestivum_fhb_resistance"),
}


# ─────────────────────────────── main ───────────────────────────────

# Lot 2+: one module per dataset, prep/<id>.py, so datasets can be prepared in parallel without touching this file.
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


def process(ds_id, cfg, taken):
    out = ROOT / ds_id
    res = globals()[f"prepare_{ds_id}"](out / "raw", out)
    df = res["samples"].copy()
    st = vcf_stats(res["vcf"])
    fill = dict(n=f"{len(st['samples']):,}".replace(",", " "), snps=f"{st['n_snps']:,}".replace(",", " "))
    m = dict(cfg, title=cfg["title"], description=cfg["description"].format(**fill), description_fr=cfg["description_fr"].format(**fill))
    if m["years"] == "samples":
        y = pd.to_numeric(df["sampling_year"], errors="coerce")
        m["years"] = (int(y.min()), int(y.max()))
    res["oca"] = build_oca(cfg["oca_name"], m["description"], list(df.columns), res.get("extra", {}), res.get("entries"))
    res["dcat"] = build_dcat(ds_id, m)
    df.to_csv(out / f"{ds_id}_samples.csv", index=False, encoding="utf-8")
    (out / "oca.json").write_text(json.dumps(res["oca"], indent=2, ensure_ascii=False) + "\n")
    (out / "dcat.json").write_text(json.dumps(res["dcat"], indent=2, ensure_ascii=False) + "\n")
    (out / "mapper.json").write_text(json.dumps(build_mapper(ds_id, m), indent=2, ensure_ascii=False) + "\n")
    res["samples"] = df
    if not ID_RE.match(ds_id): res.setdefault("blocking", []).append(f"ID {ds_id} non conforme à la convention genre3+espèce3+n°")
    if ds_id in taken and ds_id != "malvil1": res.setdefault("blocking", []).append(f"ID {ds_id} déjà utilisé dans la BD")
    if ds_id == "malvil1":
        res.setdefault("decisions", []).append("malvil1 existe déjà (metadata-api) : DCAT passé en version 1.1.0 (issued conservé, modified ajouté), OCA étend le capture_base existant.")
    row, blocking, minor, rep = run_qc(ds_id, cfg, res, st)
    (out / "QC_REPORT.md").write_text(rep)
    return row, blocking, minor, m


def main(only):
    (ROOT / ".tmp").mkdir(exist_ok=True)
    assert not verify_saids(json.loads((METADONNEES / "lymdis1/oca.json").read_text())), "SAID self-test failed"
    taken = existing_ids()
    dictionary = json.loads((METADONNEES / "dictionary.json").read_text())
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
    md += ["## Corrections DCAT reportées pour malvil1 (déjà dans metadata-api)", "",
           "- Description : 1360 → nombre réel d'individus du VCF ; mention du Groenland (4 sites GRE).",
           "- Titre : « Caplin » → « Capelin » ; mot-clé « caplin » → « capelin ».",
           "- ORCID de Hugo Cayuela : 0000-0003-3250-6295 → 0000-0002-3529-0736 (celui du dépôt Dryad). **Les deux dossiers ORCID existent au nom de Hugo Cayuela, sans publications : doublon à signaler, choix à confirmer.**",
           "- Licence CC0 et distribution Dryad ajoutées ; `dct:relation` vers l'article, le preprint et le DOI Dryad.",
           "- `dcterms:spatial` : Canada → North Atlantic Ocean (GeoNames 3411923), car 4 sites sont au Groenland.",
           "- Couverture temporelle 2014 conservée, **non vérifiée** (absente du dépôt et du preprint).", "",
           "## Mots-clés à ajouter à `dictionary.json` (metadonnees)", "", ", ".join(sorted(new_kw)) or "Aucun.", "",
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
