"""Upload the prepared datasets to a Pydio Cells workspace over WebDAV, keeping the folder layout.

Same connection convention as CaribouGenotype (R/pydio-helpers.R):
  PYDIO_URL, PYDIO_USERNAME, PYDIO_TOKEN (personal access token), WebDAV at $PYDIO_URL/dav/<workspace>/...

Usage:
  .venv/bin/python pydio_upload.py <workspace/path> [dataset_id ...] [--dry-run]
  e.g. .venv/bin/python pydio_upload.py "personal-files/genovalia-injection" --dry-run

Raw VCFs that are hard links of <id>/<id>.vcf are uploaded once (at the dataset root).
Existing remote files with the same size are skipped, so the script can be re-run after a failure.
"""
import os, sys
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent
TOP_FILES = ["SUMMARY.md", "build.py", "fetch.py", "papers.py", "said.py", "pydio_upload.py"]


def files_to_upload(only):
    for name in TOP_FILES:
        yield ROOT / name
    for d in sorted(p for p in ROOT.iterdir() if p.is_dir() and (p / "dcat.json").exists()):
        if only and d.name not in only: continue
        root_inodes = {f.stat().st_ino for f in d.glob("*.vcf")}
        for f in sorted(d.rglob("*")):
            if f.is_file() and not f.name.endswith(".part") and not (f.parent != d and f.stat().st_ino in root_inodes):
                yield f


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if not args: sys.exit(__doc__)
    workspace, only = args[0].strip("/"), set(args[1:])
    url, user, token = (os.environ.get(k) for k in ("PYDIO_URL", "PYDIO_USERNAME", "PYDIO_TOKEN"))
    if not dry and not (url and user and token):
        sys.exit("Set PYDIO_URL, PYDIO_USERNAME and PYDIO_TOKEN first.")
    base = f"{(url or 'https://PYDIO_URL').rstrip('/')}/dav/{quote(workspace)}"
    s = requests.Session(); s.auth = (user, token)
    made = set()
    files = list(files_to_upload(only))
    print(f"{len(files)} files, {sum(f.stat().st_size for f in files) / 1e9:.2f} GB -> {base}")
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        target = f"{base}/{quote(rel)}"
        if dry:
            print(f"  {rel} ({f.stat().st_size:,} B)"); continue
        parts = rel.split("/")[:-1]
        for i in range(len(parts) + 1):  # MKCOL every parent folder once (405 = already exists)
            col = "/".join(parts[:i])
            if col in made: continue
            r = s.request("MKCOL", f"{base}/{quote(col)}" if col else base)
            if r.status_code not in (201, 405): r.raise_for_status()
            made.add(col)
        head = s.head(target)
        if head.ok and head.headers.get("Content-Length") == str(f.stat().st_size):
            print(f"  skip {rel} (already there)"); continue
        with open(f, "rb") as fh:
            s.put(target, data=fh).raise_for_status()
        print(f"  put  {rel}", flush=True)
    print("done")


if __name__ == "__main__":
    main()
