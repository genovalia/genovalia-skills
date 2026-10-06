"""Download raw repository files into <id>/raw/ and write SHA256SUMS. Re-runnable (skips existing)."""
import hashlib, pathlib, requests, sys

# Each prep/<id>.py module lists its own FILES [(url, name)]. Dryad API downloads need auth: use the
# dataset's Zenodo mirror (zenodo.org/api/records/<id>/files/<name>/content) or Borealis (api/access/datafile/<id>).
FILES = {}

# FILES of each prep/<id>.py module (prep/retired/ is not loaded)
import importlib.util
for _f in sorted(pathlib.Path(__file__).resolve().parent.joinpath("prep").glob("[a-z]*.py")):
    _spec = importlib.util.spec_from_file_location(f"prep_{_f.stem}", _f); _m = importlib.util.module_from_spec(_spec)
    try: _spec.loader.exec_module(_m); FILES[_f.stem] = _m.FILES
    except Exception as e: print(f"prep/{_f.name} ignoré : {e}", file=sys.stderr)


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

for ds, files in FILES.items():
    if len(sys.argv) > 1 and ds not in sys.argv[1:]: continue
    raw = pathlib.Path(ds, "raw"); raw.mkdir(parents=True, exist_ok=True)
    for url, name in files:
        dest = raw / name
        if dest.exists(): continue
        print(ds, name, flush=True)
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for b in r.iter_content(1 << 20): f.write(b)
                tmp.rename(dest)
        except requests.RequestException as e:
            print(f"FAILED {ds} {name}: {e}", flush=True)
    (raw / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in sorted(raw.iterdir())
                                            if p.is_file() and p.name != "SHA256SUMS" and not p.name.endswith(".part")))
print("done")
