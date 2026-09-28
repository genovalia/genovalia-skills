"""Stage 1 of the full-catalogue pass: pull repository metadata (files, sizes, authors, license, related works)
for every record of catalogue.json into triage/meta/<doi-slug>.json. Re-runnable (skips cached records)."""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "triage" / "meta"; OUT.mkdir(parents=True, exist_ok=True)
S = requests.Session(); S.headers["User-Agent"] = "genovalia-curation (steve.vissault@crchudequebec.ulaval.ca)"


def slug(doi): return re.sub(r"[^A-Za-z0-9]+", "_", doi)


def get(url, **kw):
    for wait in (0, 10, 30, 60, 120, 240):  # Dryad answers 429 beyond ~30 requests/min
        time.sleep(wait)
        r = S.get(url, timeout=60, **kw)
        if r.status_code != 429: break
    r.raise_for_status(); return r.json()


def dryad(doi):
    d = get(f"https://datadryad.org/api/v2/datasets/{quote('doi:' + doi, safe='')}")
    files, url = [], "https://datadryad.org" + d["_links"]["stash:version"]["href"] + "/files"
    while url:
        page = get(url)
        files += [{"name": f["path"], "size": f.get("size")} for f in page["_embedded"]["stash:files"]]
        nxt = page["_links"].get("next")
        url = "https://datadryad.org" + nxt["href"] if nxt else None
    return {"source": "dryad", "title": d.get("title"), "abstract": d.get("abstract"), "methods": d.get("methods"),
            "authors": [{"name": f'{a.get("firstName", "")} {a.get("lastName", "")}'.strip(), "affiliation": a.get("affiliation"),
                         "orcid": a.get("orcid")} for a in d.get("authors", [])],
            "keywords": d.get("keywords", []), "license": d.get("license"), "related": d.get("relatedWorks", []),
            "published": d.get("publicationDate"), "locations": d.get("locations"), "files": files}


def dataverse(doi):
    d = get(f"https://borealisdata.ca/api/datasets/:persistentId/?persistentId=doi:{doi}")["data"]
    v = d["latestVersion"]; md = {f["typeName"]: f["value"] for f in v["metadataBlocks"]["citation"]["fields"]}
    val = lambda x: x.get("value") if isinstance(x, dict) else x
    return {"source": "dataverse", "title": md.get("title"),
            "abstract": " ".join(val(x["dsDescriptionValue"]) for x in md.get("dsDescription", []) if "dsDescriptionValue" in x),
            "authors": [{"name": val(a.get("authorName")), "affiliation": val(a.get("authorAffiliation")),
                         "orcid": val(a.get("authorIdentifier"))} for a in md.get("author", [])],
            "keywords": [val(k.get("keywordValue")) for k in md.get("keyword", []) if k.get("keywordValue")],
            "license": (v.get("license") or {}).get("uri") or v.get("termsOfUse"),
            "related": [val(p.get("publicationURL") or p.get("publicationIDNumber") or p.get("publicationCitation"))
                        for p in md.get("publication", [])],
            "published": v.get("releaseTime"),
            "files": [{"name": f["dataFile"].get("originalFileName") or f["dataFile"]["filename"],
                       "size": f["dataFile"].get("originalFileSize") or f["dataFile"].get("filesize"),
                       "id": f["dataFile"]["id"], "restricted": f.get("restricted")} for f in v.get("files", [])]}


def zenodo(doi):
    d = get(f"https://zenodo.org/api/records/{doi.rsplit('.', 1)[1]}")
    m = d["metadata"]
    return {"source": "zenodo", "title": m.get("title"), "abstract": m.get("description"),
            "authors": [{"name": c.get("name"), "affiliation": c.get("affiliation"), "orcid": c.get("orcid")} for c in m.get("creators", [])],
            "keywords": m.get("keywords", []), "license": (m.get("license") or {}).get("id"),
            "related": m.get("related_identifiers", []), "published": m.get("publication_date"),
            "files": [{"name": f["key"], "size": f["size"]} for f in d.get("files", [])]}


def fetch(rec):
    doi = rec["doi"].replace("https://doi.org/", "")
    out = OUT / f"{slug(doi)}.json"
    if out.exists(): return doi, "cached"
    try:
        if doi.startswith("10.5061/"): meta = dryad(doi)
        elif doi.startswith("10.5683/"): meta = dataverse(doi)
        elif doi.startswith("10.5281/"): meta = zenodo(doi)
        else: return doi, f"no API for {rec['depot']}"
    except Exception as e:  # noqa: BLE001 - one bad record must not stop the pass
        return doi, f"FAILED {e}"
    out.write_text(json.dumps({"catalogue": rec, **meta}, ensure_ascii=False, indent=1))
    return doi, "ok"


if __name__ == "__main__":
    recs = json.load(open(ROOT / "catalogue.json"))
    with ThreadPoolExecutor(2) as ex:
        for doi, st in ex.map(fetch, recs):
            if st != "cached": print(st, doi, flush=True)
    print(len(list(OUT.glob("*.json"))), "/", len(recs), "records cached", file=sys.stderr)
