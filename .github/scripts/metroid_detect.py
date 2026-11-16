#!/usr/bin/env python3
"""Find the newest stable Metroid build and derive everything needed to rebuild its GKI kernel.

Usage: metroid_detect.py <workdir> [--tag TAG] [--force]
Writes key=value lines to $GITHUB_OUTPUT (or stdout) and leaves <workdir>/images/boot.img.
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "flasher"))
import nothing_flash as nf  # noqa: E402  (reuses archive listing, download + verification)

GITILES = "https://android.googlesource.com/kernel/common"
REL_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(android\d+)-(\d+)-g([0-9a-f]{12,40})(?:-ab\d+)?(?:-(4k|16k))?$")


def gitiles(path):
    req = urllib.request.Request(f"{GITILES}/{path}", headers={"User-Agent": "nothing-susfs"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read().decode()
    return json.loads(body[body.index("\n") + 1:]) if body.startswith(")]}'") else body


def kernel_banner(boot_img):
    data = Path(boot_img).read_bytes()
    m = re.search(rb"Linux version (\S+) \(([^)]*)\).*?(#\d+ SMP[^\x00\n]*)", data)
    if not m:
        sys.exit("No 'Linux version' banner in boot.img (compressed kernel?)")
    release, uts = m.group(1).decode(), m.group(3).decode().strip()
    ts = re.search(r"(\w{3} \w{3} [ \d]\d \d\d:\d\d:\d\d \w+ \d{4})$", uts)
    if not ts:
        sys.exit(f"Can't parse build timestamp from '{uts}'")
    return release, ts.group(1)


def containing_branch(family, sha):
    refs = gitiles("+refs/heads?format=JSON")
    pat = re.compile(rf"^(?:refs/heads/)?{re.escape(family)}-(\d{{4}}-\d{{2}})$")
    months = sorted(m.group(1) for r in refs if (m := pat.match(r)))
    for ym in months:  # oldest first: the monthly release branch the build was cut from
        log = gitiles(f"+log/refs/heads/{family}-{ym}..{sha}?format=JSON&n=1")
        if not log.get("log"):
            return ym
    sys.exit(f"No {family}-YYYY-MM branch contains {sha}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--tag")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    out = {}

    if a.tag:
        rel = next((r for r in nf.list_releases(True) if r["tag"] == a.tag), None) or sys.exit(f"{a.tag} not found")
    else:
        rel = nf.list_releases()[0]  # newest stable (no prerelease/beta/OBT)
    tag = rel["tag"]
    out["tag"] = tag
    repo = os.environ.get("GITHUB_REPOSITORY", "sr2echa/nothing-susfs")
    if not a.force and nf.gh_api(f"repos/{repo}/releases/tags/{tag}"):
        print(f"{tag} already released in {repo}; nothing to do.")
        out["build"] = "false"
        return emit(out)

    hashes = nf.parse_hash_file(nf.fetch_asset(nf.archive_assets(rel, "hash")[0], a.workdir / "dl"))
    boot7z = nf.fetch_asset(nf.archive_assets(rel, "boot")[0], a.workdir / "dl")
    nf.extract_and_verify([boot7z], a.workdir / "images", hashes, nf.need_7z())
    if nf.sha256_file(a.workdir / "images" / "boot.img") != hashes.get("boot.img"):
        sys.exit("boot.img failed hash verification")

    release, stamp = kernel_banner(a.workdir / "images" / "boot.img")
    m = REL_RE.match(release) or sys.exit(f"Unrecognised kernel release '{release}'")
    major, minor, sub, android, _kmi, short, page = m.groups()
    family = f"{android}-{major}.{minor}"
    full = gitiles(f"+/{short}?format=JSON")["commit"]
    mk = gitiles(f"+/{full}/Makefile?format=TEXT")
    if f"SUBLEVEL = {sub}" not in base64.b64decode(mk).decode():
        sys.exit(f"Commit {full} is not sublevel {sub}")
    ym = containing_branch(family, full)
    epoch = int(dt.datetime.strptime(stamp.replace("UTC ", ""), "%a %b %d %H:%M:%S %Y")
                .replace(tzinfo=dt.timezone.utc).timestamp())

    body = (nf.gh_api(f"repos/{nf.ARCHIVE_REPO}/releases/tags/{tag}") or {}).get("body", "")
    fps = re.findall(r"Nothing/Metroid\w*/Metroid:[^\s]+", body)
    out.update(build="true", version=family, android_version=android, kernel_version=f"{major}.{minor}",
               sublevel=sub, os_patch_level=ym, page_size=page or "4k", common_commit=full,
               kernel_release=release, kernel_timestamp=stamp, source_date_epoch=str(epoch),
               fingerprints=" ".join(fps), boot_img=str(a.workdir / "images" / "boot.img"))
    emit(out)


def emit(out):
    lines = "".join(f"{k}={v}\n" for k, v in out.items())
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(lines)
    print(lines, end="")


if __name__ == "__main__":
    main()
