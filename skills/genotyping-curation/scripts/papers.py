"""Fetch open-access papers (Europe PMC full-text XML, PDF, supplementary zip; bioRxiv PDF) into <id>/raw/papers/.

Each prep/<id>.py may declare its open-access papers:
    PAPERS = {"pmc": ["PMC9234632"], "biorxiv": ["782201v1"]}   # bioRxiv: DOI suffix after 10.1101/ + version
Usage: .venv/bin/python papers.py [id ...]   (no id = every recipe that declares PAPERS). Re-runnable (skips existing files).
"""
import importlib.util, pathlib, sys
import requests

ROOT = pathlib.Path(__file__).resolve().parent
H = {"User-Agent": "Mozilla/5.0 (genovalia data curation; curation@genovalia.ulaval.ca)"}


def get(url, dest):
    if dest.exists(): return "exists"
    r = requests.get(url, headers=H, timeout=120)
    ok = r.ok and len(r.content) > 2000 and b"Just a moment" not in r.content[:5000]
    if ok: dest.write_bytes(r.content)
    return f"{r.status_code} {len(r.content)} {'OK' if ok else 'FAIL'}"


def declared():
    """(id, PAPERS) of every prep/<id>.py module that declares PAPERS (prep/retired/ is not loaded)."""
    for f in sorted((ROOT / "prep").glob("[a-z]*.py")):
        spec = importlib.util.spec_from_file_location(f"papers_{f.stem}", f); m = importlib.util.module_from_spec(spec)
        try: spec.loader.exec_module(m)
        except Exception as e: print(f"prep/{f.name} ignoré : {e}", file=sys.stderr); continue
        if getattr(m, "PAPERS", None): yield f.stem, m.PAPERS


def as_list(v): return [v] if isinstance(v, str) else list(v or [])


if __name__ == "__main__":
    only = set(sys.argv[1:])
    for ds, papers in declared():
        if only and ds not in only: continue
        d = ROOT / ds / "raw" / "papers"; d.mkdir(parents=True, exist_ok=True)
        for pmc in as_list(papers.get("pmc")):
            print(ds, "xml", get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc}/fullTextXML", d / f"{pmc}.xml"))
            print(ds, "pdf", get(f"https://europepmc.org/articles/{pmc}?pdf=render", d / f"{pmc}.pdf"))
            print(ds, "supp", get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc}/supplementaryFiles", d / f"{pmc}_supplementary.zip"))
        for doi in as_list(papers.get("biorxiv")):
            print(ds, "pdf", get(f"https://www.biorxiv.org/content/10.1101/{doi}.full.pdf", d / f"biorxiv_{doi}.pdf"))
