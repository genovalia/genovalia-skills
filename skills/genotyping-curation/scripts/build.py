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
    "site_code": ("Text", "site_code", "Code of the sampling site as used in the source publication", None),
    "site_type": ("Text", "site_type", "General environmental or habitat category of the sampling site", None),
    "region": ("Text", "region", "Geographic region of the sampling site", None),
    "sampling_year": ("Numeric", "sampling_year", "Year in which the sample was collected", None),
    "sample_tissue": ("Text", "sample_tissue", "The type of tissue sampled from the individual and used for DNA extraction", None),
    "date_of_birth": ("DateTime", "date_of_birth", "Birth date of the individual, expressed in the standard ISO 8601 format YYYY-MM-DD", None),
    "sequenced_molecule": ("Text", "sequenced_molecule", "Sequenced molecule such as DNA or RNA.", None),
    "genotyping_technology": ("Text", "genotyping_technology", "Genotyping technology used to obtain genotypes, such as whole genome sequencing, genotyping-by-sequencing or SNP chip.", None),
}
UCUM = {"km": "km", "g": "g", "kg": "kg", "degree": "deg", "decimal degree": "deg", "meter": "m", "degree Celsius": "Cel", "mg/m3": "mg/m3", "m": "m", "ha": "har", "cm": "cm", "mm": "mm",
        "um": "um", "nm": "nm", "kg/m3": "kg/m3", "GPa": "GPa", "ug/m": "ug/m", "m2/kg": "m2/kg", "1/mm2": "/mm2",
        "ppm": "[ppm]", "%": "%", "MJ/m2": "MJ/m2", "degree-day": "d",
        "nanogram per gram": "ng/g", "microgram per gram": "ug/g"}  # tissue concentrations (angang5)


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
    inside distributions (both prefixes expand to http://purl.org/dc/terms/).
    License, distribution and dct:relation are left empty, as on every metadonnees dataset
    (AGENTS.md "Deliberately left for later"): Sedna takes the access-request link from
    dcat:distribution -> dcat:accessURL, so a repository URL there would replace Genovalia's
    access form. m["license"], m["distributions"] and m["relations"] stay in the recipes as
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
    sites = df["site_code"].nunique() if "site_code" in df else None
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
