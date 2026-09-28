"""Fetch open-access papers (PDF, JATS XML, supplementary zip) into <id>/raw/papers/."""
import pathlib, requests
H = {"User-Agent": "Mozilla/5.0 (genovalia data curation; curation@genovalia.ulaval.ca)"}
PMC = {"prusal1": "PMC11536197", "anogla1": "PMC9234632", "picsit1": "PMC10989875",
       "triaes1": "PMC10230752", "picgla2": "PMC4181072"}
BIORXIV = {"malvil1": "782201v1", "salfon1": "660621v1", "homame1": "2020.01.28.923490v1", "picmar2": "2025.10.30.685617v1"}

def get(url, dest):
    if dest.exists(): return "exists"
    r = requests.get(url, headers=H, timeout=120)
    ok = r.ok and len(r.content) > 2000 and b"Just a moment" not in r.content[:5000]
    if ok: dest.write_bytes(r.content)
    return f"{r.status_code} {len(r.content)} {'OK' if ok else 'FAIL'}"

for ds, pmc in PMC.items():
    d = pathlib.Path(ds, "raw", "papers"); d.mkdir(parents=True, exist_ok=True)
    print(ds, "xml", get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc}/fullTextXML", d / f"{pmc}.xml"))
    print(ds, "pdf", get(f"https://europepmc.org/articles/{pmc}?pdf=render", d / f"{pmc}.pdf"))
    print(ds, "supp", get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc}/supplementaryFiles", d / f"{pmc}_supplementary.zip"))
for ds, doi in BIORXIV.items():
    d = pathlib.Path(ds, "raw", "papers"); d.mkdir(parents=True, exist_ok=True)
    print(ds, "pdf", get(f"https://www.biorxiv.org/content/10.1101/{doi}.full.pdf", d / f"biorxiv_{doi}.pdf"))
