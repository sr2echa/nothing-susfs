#!/usr/bin/env python3
"""Nothing Phone (3) "Metroid" firmware flasher: safe, data-preserving, cross-platform.

Downloads stock Nothing OS images from spike0en/nothing_archive, verifies them, and
flashes them with fastboot without ever wiping user data. Optionally swaps in the
KernelSU-Next + SUSFS boot.img built for the same firmware by sr2echa/nothing-susfs.

Partition logic mirrors spike0en/nothing_fastboot_flasher (branch: metroid).
Standard library only. Needs: Python 3.9+, adb/fastboot (auto-download offered), 7-Zip.

Run with no arguments for the interactive menu, or see --help.
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DEVICE = "Metroid"
FASTBOOT_PRODUCTS = {"metroid", "sun"}  # fastboot reports the SoC ("sun") on Phone (3)
ARCHIVE_REPO = "spike0en/nothing_archive"
ROOT_REPO = "sr2echa/nothing-susfs"

# From nothing_fastboot_flasher (metroid) bash/flash_all.sh
BOOT_PARTS = ["boot", "init_boot", "vendor_boot", "dtbo", "recovery"]
FIRMWARE_PARTS = ["abl", "aop", "aop_config", "bluetooth", "cpucp", "cpucp_dtb", "devcfg", "dsp",
                  "featenabler", "hyp", "imagefv", "keymaster", "modem", "multiimgoem",
                  "multiimgqti", "pvmfw", "qupfw", "shrm", "soccp_dcd", "soccp_debug", "tz",
                  "uefi", "uefisecapp", "xbl", "xbl_config", "xbl_ramdump"]
LOGICAL_PARTS = ["system", "system_dlkm", "system_ext", "product", "vendor", "vendor_dlkm", "odm"]
VBMETA_LATE = ["vbmeta_system", "vbmeta_vendor"]

TAG_RE = re.compile(r"^Metroid_([A-Z])(\d+\.\d+)-(\d{6})-(\d{4})$")
UNSTABLE_TOKENS = {"beta", "obt", "cbt", "preview", "dp", "rc", "test"}
MIN_BATTERY = 30

LOG_FILE = None
DRY_RUN = False


# ----------------------------------------------------------------------------- output
def log(msg=""):
    print(msg, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


def die(msg, code=1):
    log(f"\n[x] {msg}")
    sys.exit(code)


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or (default or "")


def yes_no(prompt, default=False):
    d = "Y/n" if default else "y/N"
    ans = ask(f"{prompt} ({d})").lower()
    return default if not ans else ans.startswith("y")


def pick_number(prompt, count, default=1):
    while True:
        ans = ask(prompt, str(default))
        if ans.isdigit() and 1 <= int(ans) <= count:
            return int(ans)
        log(f"  enter a number from 1 to {count}")


# ----------------------------------------------------------------------------- safety
_WIPE_ARGS = {"-w", "--wipe", "erase", "format", "wipe-super", "flashall", "update"}
_WIPE_TARGETS = {"userdata", "metadata", "data", "cache"}


def blocked_reason(cmd):
    """Return why a command must never run (it could wipe data or relock), else None."""
    args = [str(a).lower() for a in cmd]
    tool = Path(args[0]).name if args else ""
    rest = args[1:]
    if tool.startswith("fastboot"):
        for i, a in enumerate(rest):
            base = a.split(":", 1)[0]  # format:ext4 -> format
            if a in _WIPE_ARGS or base in _WIPE_ARGS:
                return f"'{a}' can erase data"
            if a in ("flashing", "oem") and i + 1 < len(rest) and rest[i + 1].startswith("lock"):
                return "relocking the bootloader wipes the phone"
            if a == "flash" and i + 1 < len(rest) and rest[i + 1].split("_")[0] in _WIPE_TARGETS:
                return f"flashing {rest[i + 1]} overwrites user data"
    if tool.startswith("adb"):
        joined = " ".join(rest)
        if any(k in joined for k in ("wipe_data", "master_clear", "factory_reset", "wipe data")):
            return "factory-reset command"
    return None


def run(cmd, check=True, capture=False, timeout=None, mutating=True):
    """Run a command; refuses denylisted ones. In dry-run, mutating commands are only printed."""
    reason = blocked_reason(cmd)
    if reason:
        die(f"REFUSED: {' '.join(map(str, cmd))}  ({reason})")
    shown = " ".join(str(c) for c in cmd)
    if DRY_RUN and mutating:
        log(f"  (dry-run) {shown}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if mutating:
        log(f"  $ {shown}")
    try:
        r = subprocess.run([str(c) for c in cmd], text=True, timeout=timeout,
                           stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.STDOUT if capture else None)
    except subprocess.TimeoutExpired:
        if check:
            die(f"Timed out: {shown}")
        return subprocess.CompletedProcess(cmd, 1, "", "")
    if check and r.returncode != 0:
        if capture and r.stdout:
            log(r.stdout.strip())
        raise RuntimeError(f"Command failed ({r.returncode}): {shown}")
    return r


# ----------------------------------------------------------------------------- tools
def home_dir():
    return Path(os.environ.get("NOTHING_FLASHER_HOME", Path.home() / ".nothing-flasher"))


def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    local = home_dir() / "platform-tools" / exe
    if local.exists():
        return str(local)
    return shutil.which(name)


def ensure_platform_tools():
    adb, fb = find_tool("adb"), find_tool("fastboot")
    if adb and fb:
        return adb, fb
    osname = {"Darwin": "darwin", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    if not osname:
        die("adb/fastboot not found. Install Android platform-tools and re-run.")
    url = f"https://dl.google.com/android/repository/platform-tools-latest-{osname}.zip"
    log("adb/fastboot not found.")
    if not yes_no(f"Download Google platform-tools from {url}?", True):
        die("Install platform-tools (https://developer.android.com/tools/releases/platform-tools) and re-run.")
    home_dir().mkdir(parents=True, exist_ok=True)
    zpath = home_dir() / "platform-tools.zip"
    download(url, zpath)  # ponytail: Google publishes no checksum for -latest; HTTPS from dl.google.com only
    with zipfile.ZipFile(zpath) as z:
        z.extractall(home_dir())
    zpath.unlink()
    for exe in (home_dir() / "platform-tools").iterdir():
        if exe.is_file():
            exe.chmod(exe.stat().st_mode | 0o111)
    adb, fb = find_tool("adb"), find_tool("fastboot")
    if not (adb and fb):
        die("platform-tools download did not contain adb/fastboot.")
    return adb, fb


def find_7z():
    for n in ("7zz", "7z", "7za"):
        p = shutil.which(n)
        if p:
            return p
    for p in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(p):
            return p
    return None


def need_7z():
    p = find_7z()
    if p:
        return p
    hint = {"Darwin": "brew install sevenzip",
            "Linux": "sudo apt install 7zip   (or p7zip-full / your distro's 7zip package)",
            "Windows": "winget install 7zip.7zip   (or https://www.7-zip.org)"}.get(platform.system(), "install 7-Zip")
    die(f"7-Zip is required to extract firmware archives. Install it: {hint}")


# ----------------------------------------------------------------------------- GitHub
def gh_api(path):
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "nothing-flasher"})
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 403 and "rate limit" in e.read().decode(errors="ignore").lower():
                die("GitHub API rate limit hit. Set GITHUB_TOKEN to a token and retry.")
            if attempt == 3:
                raise
        except urllib.error.URLError:
            if attempt == 3:
                raise
        time.sleep(3 * (attempt + 1))


def parse_tag(tag):
    m = TAG_RE.match(tag or "")
    if not m:
        return None
    letter, ver, ymd, hm = m.groups()
    return {"tag": tag, "android": letter, "nos": ver, "date": ymd, "time": hm,
            "display": tag[len("Metroid_"):], "sort": ymd + hm}


def is_stable(rel):
    if rel.get("prerelease") or rel.get("draft"):
        return False
    if not parse_tag(rel.get("tag_name", "")):
        return False
    extra = (rel.get("name") or "").lower().replace(rel["tag_name"].lower(), "")
    tokens = set(re.split(r"[^a-z0-9]+", extra)) - {""}
    return not any(t in UNSTABLE_TOKENS or re.fullmatch(r"(dp|rc|beta)\d+", t) for t in tokens)


def list_releases(include_unstable=False):
    out, page = [], 1
    while True:
        rels = gh_api(f"repos/{ARCHIVE_REPO}/releases?per_page=100&page={page}") or []
        if not rels:
            break
        for r in rels:
            info = parse_tag(r.get("tag_name", ""))
            if info and (include_unstable or is_stable(r)):
                info["stable"] = is_stable(r)
                info["assets"] = {a["name"]: a for a in r.get("assets", [])}
                out.append(info)
        page += 1
    return sorted(out, key=lambda x: x["sort"], reverse=True)


def archive_assets(rel, kind):
    """kind: boot | firmware | logical | hash."""
    names = sorted(rel["assets"])
    if kind == "hash":
        pat = re.compile(re.escape(rel["tag"]) + r"-hash\.sha256$")
    elif kind == "logical":
        pat = re.compile(re.escape(rel["tag"]) + r"-image-logical\.7z(\.\d{3})?$")
    else:
        pat = re.compile(re.escape(rel["tag"]) + rf"-image-{kind}\.7z$")
    return [rel["assets"][n] for n in names if pat.match(n)]


# ----------------------------------------------------------------------------- downloads
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, expected_sha=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and expected_sha and sha256_file(dest) == expected_sha:
        log(f"  cached  {dest.name}")
        return dest
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "nothing-flasher"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=120) as r:
        if have and r.status != 206:
            have = 0  # server ignored Range; restart
        total = have + int(r.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as f:
            done, last = have, 0.0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 1:
                    pct = f"{done * 100 // total}%" if total else f"{done >> 20} MiB"
                    print(f"\r  downloading {dest.name}: {pct}   ", end="", flush=True)
                    last = time.time()
    print()
    if expected_sha:
        got = sha256_file(part)
        if got != expected_sha:
            part.unlink()
            die(f"Checksum mismatch for {dest.name} (expected {expected_sha}, got {got}). Deleted; re-run.")
    part.replace(dest)
    log(f"  ok      {dest.name}")
    return dest


def fetch_asset(asset, folder):
    digest = (asset.get("digest") or "").removeprefix("sha256:") or None
    return download(asset["browser_download_url"], Path(folder) / asset["name"], digest)


def parse_hash_file(path):
    """Lines like '<sha256> *./boot.img' -> {'boot.img': sha}."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\*?(?:\./)?(\S+)$", line.strip())
        if m:
            out[m.group(2)] = m.group(1)
    return out


def extract_and_verify(archives, outdir, hashes, sevenz):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    first = sorted(archives)[0]  # .7z or .7z.001 (7-Zip follows the volume chain)
    r = run([sevenz, "x", "-y", f"-o{outdir}", first], check=False, mutating=False, capture=True)
    if r.returncode != 0:
        log((r.stdout or "")[-2000:])
        die(f"7-Zip failed to extract {Path(first).name}. Delete it from the cache and re-run.")
    for img in sorted(outdir.glob("*.img")):
        want = hashes.get(img.name)
        if not want:
            continue
        if sha256_file(img) != want:
            die(f"Extracted {img.name} does not match the archive's hash list. Delete {outdir} and re-run.")
    log(f"  verified images in {outdir}")


# ----------------------------------------------------------------------------- device
class Device:
    def __init__(self, adb, fastboot, serial=None):
        self.adb, self.fb, self.serial = adb, fastboot, serial
        self.mode = None  # adb | fastboot | fastbootd | None
        self.props = {}

    def a(self, *args):
        return [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)

    def f(self, *args):
        return [self.fb] + (["-s", self.serial] if self.serial else []) + list(args)

    def adb_out(self, *args):
        r = run(self.a(*args), check=False, capture=True, timeout=30, mutating=False)
        return (r.stdout or "").strip()

    def getvar(self, var):
        r = run(self.f("getvar", var), check=False, capture=True, timeout=20, mutating=False)
        m = re.search(rf"^{re.escape(var)}:\s*(\S+)", r.stdout or "", re.M)
        return m.group(1) if m else ""

    def detect(self):
        r = run([self.adb, "devices"], check=False, capture=True, timeout=20, mutating=False)
        adb_devs = [l.split("\t")[0] for l in (r.stdout or "").splitlines()
                    if l.endswith("\tdevice") and not l.startswith("emulator-")]
        r = run([self.fb, "devices"], check=False, capture=True, timeout=20, mutating=False)
        fb_devs = [l.split()[0] for l in (r.stdout or "").splitlines() if l.strip().endswith("fastboot")]
        devs = adb_devs + fb_devs
        if not devs:
            return False
        if self.serial is None:
            if len(devs) > 1:
                for i, s in enumerate(devs, 1):
                    log(f"  {i}) {s}")
                self.serial = devs[pick_number("Which device?", len(devs)) - 1]
            else:
                self.serial = devs[0]
        if self.serial in adb_devs:
            self.mode = "adb"
            self.props = {k: self.adb_out("shell", "getprop", k) for k in
                          ("ro.product.device", "ro.build.display.id", "ro.boot.slot_suffix")}
        else:
            self.mode = "fastbootd" if self.getvar("is-userspace") == "yes" else "fastboot"
        return True

    @property
    def installed_tag(self):
        d = self.props.get("ro.build.display.id", "")
        return f"Metroid_{d}" if parse_tag(f"Metroid_{d}") else None

    def battery(self):
        m = re.search(r"level:\s*(\d+)", self.adb_out("shell", "dumpsys", "battery"))
        return int(m.group(1)) if m else None

    def is_rooted(self):
        return "uid=0" in self.adb_out("shell", "su", "-c", "id")

    def wait_fastboot(self, userspace=None, secs=120):
        if DRY_RUN:
            return
        end = time.time() + secs
        while time.time() < end:
            r = run([self.fb, "devices"], check=False, capture=True, timeout=10, mutating=False)
            if self.serial in (r.stdout or ""):
                if userspace is None or (self.getvar("is-userspace") == "yes") == userspace:
                    return
            time.sleep(2)
        die("Device did not show up in fastboot. Check the cable/drivers and re-run.")

    def wait_adb(self, secs=300):
        if DRY_RUN:
            return True
        end = time.time() + secs
        while time.time() < end:
            if self.adb_out("shell", "getprop", "sys.boot_completed") == "1":
                return True
            time.sleep(3)
        return False


def is_downgrade(tag, installed):
    """True if tag's build stamp (YYMMDDHHMM) is older than installed's."""
    a, b = parse_tag(tag), parse_tag(installed)
    return bool(a and b and a["sort"] < b["sort"])


# ----------------------------------------------------------------------------- root release
def find_root_build(tag, folder):
    rel = gh_api(f"repos/{ROOT_REPO}/releases/tags/{tag}")
    if not rel:
        return None
    assets = {a["name"]: a for a in rel.get("assets", [])}
    img_name = f"{tag}-KernelSU-Next-SUSFS-boot.img"
    if img_name not in assets or "SHA256SUMS" not in assets:
        return None
    sums = fetch_asset(assets["SHA256SUMS"], folder)
    listed = parse_hash_file(sums)
    img = fetch_asset(assets[img_name], folder)
    if listed.get(img_name) != sha256_file(img):
        die(f"{img_name} does not match the release's SHA256SUMS.")
    apks = [a for n, a in assets.items() if n.endswith(".apk")]
    return {"img": img, "apks": apks, "url": rel.get("html_url")}


# ----------------------------------------------------------------------------- plan
def build_plan(mode, cur, imgdir, root_img=None, restore_init_boot=True, fw_both=False):
    """Returns list of (stage, fastboot-args, tolerate_failure). Stages: bootloader | fastbootd."""
    p, img = [], (lambda n: str(Path(imgdir) / f"{n}.img"))
    boot_src = lambda: str(root_img) if root_img else img("boot")
    if mode == "root":
        p.append(("bootloader", ["flash", f"boot_{cur}", str(root_img)], False))
        if restore_init_boot:
            p.append(("bootloader", ["flash", f"init_boot_{cur}", img("init_boot")], False))
        return p
    if mode == "boot":
        for part in BOOT_PARTS:
            p.append(("bootloader", ["flash", f"{part}_{cur}", boot_src() if part == "boot" else img(part)], False))
        return p
    # full: flash inactive slot, then switch (same as nothing_fastboot_flasher metroid)
    tgt = "b" if cur == "a" else "a"
    p.append(("bootloader", ["flash", f"vbmeta_{tgt}", img("vbmeta")], False))
    for part in BOOT_PARTS:
        p.append(("bootloader", ["flash", f"{part}_{tgt}", boot_src() if part == "boot" else img(part)], False))
    for part in LOGICAL_PARTS:
        for s in ("a", "b"):
            p.append(("fastbootd", ["delete-logical-partition", f"{part}_{s}-cow"], True))
            p.append(("fastbootd", ["delete-logical-partition", f"{part}_{s}"], True))
            p.append(("fastbootd", ["create-logical-partition", f"{part}_{s}", "1"], False))
    for part in LOGICAL_PARTS:
        p.append(("fastbootd", ["flash", f"{part}_{tgt}", img(part)], False))
    for part in VBMETA_LATE:
        p.append(("fastbootd", ["flash", f"{part}_{tgt}", img(part)], False))
    p.append(("fastbootd", ["set_active", tgt], False))
    # Upstream flashes firmware un-suffixed after set_active; fastbootd may still report the old
    # running slot as current, so name the new slot explicitly.
    for part in FIRMWARE_PARTS:
        for s in (("a", "b") if fw_both else (tgt,)):
            p.append(("fastbootd", ["flash", f"{part}_{s}", img(part)], False))
    return p


def execute_plan(dev, plan):
    stage = None
    for want, args, tolerate in plan:
        if want != stage:
            if want == "bootloader" and dev.mode != "fastboot":
                log("\n== rebooting to bootloader")
                run(dev.a("reboot", "bootloader") if dev.mode == "adb" else dev.f("reboot", "bootloader"))
                dev.wait_fastboot(userspace=False)
            elif want == "fastbootd" and dev.mode != "fastbootd":
                log("\n== rebooting to fastbootd")
                run(dev.f("reboot", "fastboot"))
                dev.wait_fastboot(userspace=True)
            dev.mode, stage = ("fastboot" if want == "bootloader" else "fastbootd"), want
        try:
            run(dev.f(*args))
        except RuntimeError as e:
            if not tolerate:
                raise
            log(f"  (ignored) {e}")


def recovery_help(cur, imgdir, backup_dir):
    log("\n--- HOW TO RECOVER ---")
    log("Your data is untouched: this tool never erases userdata. To get back to a booting phone:")
    log("  1. Hold Power + Volume Down until the bootloader (fastboot) screen appears.")
    log(f"  2. Re-run this tool and pick the same version with mode 'full', OR flash stock boot:")
    log(f"     fastboot flash boot_{cur} \"{Path(imgdir) / 'boot.img'}\"")
    if backup_dir:
        log(f"  Your pre-flash boot images are backed up in: {backup_dir}")


# ----------------------------------------------------------------------------- main flow
def backup_boot_chain(dev, folder):
    if dev.mode != "adb" or not dev.is_rooted():
        return None
    suffix = dev.props.get("ro.boot.slot_suffix", "")
    bdir = Path(folder) / "backups" / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir.mkdir(parents=True, exist_ok=True)
    log(f"\n== backing up current boot chain ({suffix}) to {bdir}")
    for part in ("boot", "init_boot", "vendor_boot"):
        tmp = f"/data/local/tmp/nf_{part}.img"
        run(dev.a("shell", "su", "-c", f"dd if=/dev/block/by-name/{part}{suffix} of={tmp} bs=4M && chmod 644 {tmp}"),
            mutating=False, capture=True)
        run(dev.a("pull", tmp, str(bdir / f"{part}{suffix}.img")), mutating=False, capture=True)
        run(dev.a("shell", "su", "-c", f"rm {tmp}"), mutating=False, capture=True, check=False)
    return bdir


def choose_release(rels, installed):
    log("\nAvailable Nothing OS builds for Phone (3):")
    for i, r in enumerate(rels[:15], 1):
        flags = [] if r.get("stable", True) else ["BETA/PRE"]
        if r["tag"] == installed:
            flags.append("INSTALLED")
        log(f"  {i:2}) {r['display']:<20} {' '.join(flags)}")
    return rels[pick_number("Pick a build", min(15, len(rels))) - 1]


def main():
    global LOG_FILE, DRY_RUN
    ap = argparse.ArgumentParser(description="Safe Nothing Phone (3) firmware flasher (never wipes data).")
    ap.add_argument("--tag", help="archive tag, e.g. Metroid_C5.0-260924-2115 (default: pick interactively)")
    ap.add_argument("--mode", choices=["full", "boot", "root", "download"],
                    help="full = update all firmware (keeps data); boot = boot images only; "
                         "root = flash KernelSU-Next+SUSFS boot only; download = fetch + verify only")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--root", dest="root", action="store_true", default=None, help="use KernelSU-Next + SUSFS boot.img")
    g.add_argument("--no-root", dest="root", action="store_false", help="use stock boot.img")
    ap.add_argument("--all-releases", action="store_true", help="also list beta/pre-release builds")
    ap.add_argument("--list", action="store_true", help="list builds and exit")
    ap.add_argument("--cache", type=Path, help="download folder (default ~/.nothing-flasher)")
    ap.add_argument("--dry-run", action="store_true", help="print every command instead of running it")
    ap.add_argument("--assume-slot", choices=["a", "b"], help="dry-run without a device: current slot to assume")
    ap.add_argument("--skip-big-downloads", action="store_true",
                    help="with --dry-run: don't download firmware/logical archives (plan only)")
    ap.add_argument("--firmware-both-slots", action="store_true", help="full mode: flash firmware to a and b")
    ap.add_argument("--self-test", action="store_true", help="run built-in safety checks and exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if args.cache:
        os.environ["NOTHING_FLASHER_HOME"] = str(args.cache)
    DRY_RUN = args.dry_run
    if os.name == "nt":
        os.system("")  # enable ANSI on Windows 10+
    (home_dir() / "logs").mkdir(parents=True, exist_ok=True)
    LOG_FILE = home_dir() / "logs" / f"flash-{_dt.datetime.now():%Y%m%d-%H%M%S}.log"
    log("=== Nothing Phone (3) safe flasher ===  (user data is never erased)")
    log(f"log: {LOG_FILE}" + ("   [DRY RUN]" if DRY_RUN else ""))

    rels = list_releases(include_unstable=args.all_releases)
    if not rels:
        die("No Metroid builds found in the archive.")
    if args.list:
        for r in rels:
            log(f"{r['tag']}{'' if r.get('stable', True) else '  (beta/pre)'}")
        return 0

    adb, fb = ensure_platform_tools()
    dev = Device(adb, fb)
    if not dev.detect():
        if not DRY_RUN:
            die("No phone found. Enable USB debugging (or boot to fastboot) and connect the cable.")
        log("No device connected; dry-run continues with assumptions.")
    if dev.mode == "adb":
        if dev.props.get("ro.product.device", "").lower() != DEVICE.lower():
            die(f"This is '{dev.props.get('ro.product.device')}', not a Nothing Phone (3) ({DEVICE}). Refusing.")
        log(f"Phone: {DEVICE}, build {dev.props.get('ro.build.display.id')}, slot {dev.props.get('ro.boot.slot_suffix')}")
    elif dev.mode in ("fastboot", "fastbootd"):
        prod = dev.getvar("product").lower()
        if prod not in FASTBOOT_PRODUCTS:
            die(f"fastboot product is '{prod}', expected a Nothing Phone (3). Refusing.")
        if dev.getvar("unlocked") != "yes":
            die("Bootloader is locked. Unlocking it wipes the phone, so this tool will not continue.")
        if not yes_no("Device is in fastboot, so the model can't be fully confirmed. Is this a Nothing Phone (3)?"):
            die("Aborted.")

    installed = dev.installed_tag
    if args.tag:
        rel = next((r for r in rels if r["tag"] == args.tag), None) or \
              next((r for r in list_releases(True) if r["tag"] == args.tag), None)
        if not rel:
            die(f"{args.tag} not found in {ARCHIVE_REPO}.")
    else:
        rel = choose_release(rels, installed)
    tag = rel["tag"]

    if not args.mode:
        log("\nWhat do you want to do?\n  1) Update/repair full firmware (keeps all data)\n"
            "  2) Flash boot images only (same build as installed)\n"
            "  3) Root only: flash KernelSU-Next + SUSFS boot (same build as installed)\n"
            "  4) Download and verify only")
    mode = args.mode or ["full", "boot", "root", "download"][pick_number("Choice", 4) - 1]

    if mode == "full" and dev.mode is not None:
        if installed and is_downgrade(tag, installed):
            die(f"{tag} is OLDER than the installed {installed}. Downgrading over your data can bootloop, "
                "and older bootloader firmware can trip anti-rollback and hard-brick the phone. Refusing.")
        if not installed and not yes_no(f"Can't read the installed build. Is {rel['display']} the same or NEWER "
                                        "than what's installed? (downgrades can brick)", False):
            die("Aborted. Only same-or-newer builds can be flashed over your data.")

    if mode in ("boot", "root") and dev.mode is not None:
        if installed and installed != tag:
            die(f"'{mode}' mode must match the installed build ({installed}); you picked {tag}. "
                "Use mode 1 (full) to change versions.")
        if not installed and not yes_no(f"Can't read the installed build in fastboot. Is it exactly {rel['display']}?"):
            die("Aborted. Boot images must match the installed build or the phone won't boot.")

    folder = home_dir() / tag
    root = args.root
    if mode == "root":
        root = True
    elif mode in ("full", "boot") and root is None:
        root = yes_no("Do you want KernelSU-Next + SUSFS root?", False)
    root_build = None
    if root:
        log(f"\n== looking for a KernelSU-Next + SUSFS build for {tag} in {ROOT_REPO}")
        root_build = find_root_build(tag, folder / "root")
        if not root_build:
            log(f"No verified root build exists for {tag} yet.")
            if mode == "root" or not yes_no("Continue with stock boot (no root)?", True):
                die("Aborted.")
        else:
            log(f"  using {root_build['img'].name}  ({root_build['url']})")

    sevenz = need_7z()
    log(f"\n== downloading {tag} into {folder}")
    hash_asset = archive_assets(rel, "hash")
    if not hash_asset:
        die("Release has no -hash.sha256 file; refusing to flash unverifiable images.")
    hashes = parse_hash_file(fetch_asset(hash_asset[0], folder / "dl"))
    imgdir = folder / "images"
    kinds = ["boot"] + (["firmware", "logical"] if mode in ("full", "download") else [])
    for kind in kinds:
        assets = archive_assets(rel, kind)
        if not assets:
            die(f"Release is missing its {kind} archive.")
        if DRY_RUN and args.skip_big_downloads and kind != "boot":
            log(f"  (dry-run) would download {', '.join(a['name'] for a in assets)}")
            continue
        files = [fetch_asset(a, folder / "dl") for a in assets]
        extract_and_verify(files, imgdir, hashes, sevenz)
    if mode == "download":
        log(f"\nDone. Verified images: {imgdir}")
        return 0

    cur = (dev.props.get("ro.boot.slot_suffix", "").lstrip("_") if dev.mode == "adb"
           else dev.getvar("current-slot") if dev.mode else "") or args.assume_slot
    if cur not in ("a", "b"):
        die("Could not read the current slot (use --assume-slot for dry runs).")
    if dev.mode == "adb":
        bat = dev.battery()
        if bat is not None and bat < MIN_BATTERY:
            die(f"Battery is {bat}%. Charge to at least {MIN_BATTERY}% first.")
    elif not DRY_RUN and not yes_no(f"Is the battery above {MIN_BATTERY}%?"):
        die("Charge the phone first.")

    restore_ib = True
    if mode == "root":
        restore_ib = yes_no("Also flash stock init_boot (needed if you used KernelSU LKM/Magisk before)?", True)
    plan = build_plan(mode, cur, imgdir, root_build["img"] if root_build else None, restore_ib,
                      args.firmware_both_slots)

    log("\n================ FLASH PLAN ================")
    log(f"Build: {tag}   mode: {mode}   root: {'KernelSU-Next + SUSFS' if root_build else 'no (stock boot)'}")
    log(f"Current slot: {cur}" + (f"   -> new slot after flashing: {'b' if cur == 'a' else 'a'}" if mode == "full" else ""))
    for stage, a, tol in plan:
        log(f"  [{stage:10}] fastboot {' '.join(a)}{'   (ok if it fails)' if tol else ''}")
    log("User data (userdata/metadata) is NOT touched. No wipe, no format, no relock.")
    if mode == "full":
        log("Note: logical partitions on both slots are recreated (as the official flasher does).\n"
            "      If the process is interrupted, re-run this tool to finish; data stays intact.")
    log("============================================")
    if not DRY_RUN and ask("Type YES to flash") != "YES":
        die("Cancelled. Nothing was flashed.")

    backup_dir = None if DRY_RUN else backup_boot_chain(dev, folder)
    if not backup_dir:
        log(f"(No root backup possible; stock images in {imgdir} are your fallback.)")
    try:
        execute_plan(dev, plan)
    except (RuntimeError, KeyboardInterrupt) as e:
        log(f"\n[x] Flashing stopped: {e}")
        recovery_help(cur if mode != "full" else ("b" if cur == "a" else "a"), imgdir, backup_dir)
        return 1

    log("\n== rebooting to Android")
    run(dev.f("reboot"))
    if root_build and root_build["apks"] and not DRY_RUN:
        log("Waiting for Android to boot (up to 5 min)...")
        if dev.wait_adb() and yes_no("Install the matching KernelSU-Next manager app now?", True):
            apks = root_build["apks"]
            for i, a in enumerate(apks, 1):
                log(f"  {i}) {a['name']}")
            apk = apks[pick_number("Which one (spoofed = random package name, harder to detect)?", len(apks)) - 1]
            run(dev.a("install", "--user", "0", str(fetch_asset(apk, folder / "root"))))
    log("\nDone. Your data was not touched.")
    return 0


# ----------------------------------------------------------------------------- self-test
def self_test():
    bad = [["fastboot", "-w"], ["fastboot", "erase", "userdata"], ["fastboot", "format:ext4", "userdata"],
           ["fastboot", "flashing", "lock"], ["fastboot", "oem", "lock"], ["fastboot", "flash", "userdata", "x.img"],
           ["fastboot", "-s", "X", "erase", "metadata"], ["fastboot", "wipe-super", "super_empty.img"],
           ["fastboot.exe", "update", "x.zip"], ["adb", "shell", "recovery", "--wipe_data"]]
    for c in bad:
        assert blocked_reason(c), f"should block: {c}"
    good = [["fastboot", "flash", "boot_a", "boot.img"], ["fastboot", "set_active", "b"],
            ["fastboot", "delete-logical-partition", "system_a-cow"], ["fastboot", "reboot", "fastboot"],
            ["adb", "reboot", "bootloader"], ["fastboot", "flash", "dtbo_b", "dtbo.img"]]
    for c in good:
        assert not blocked_reason(c), f"should allow: {c}"
    t = parse_tag("Metroid_C5.0-260924-2115")
    assert t and t["display"] == "C5.0-260924-2115" and t["sort"] == "2609242115"
    assert parse_tag("Metroid_C5.0-260924-2115_beta") is None and parse_tag("Pong_B4.1-260414-1749") is None
    assert is_downgrade("Metroid_B4.1-260814-1733", "Metroid_C5.0-260924-2115")
    assert not is_downgrade("Metroid_C5.0-260924-2115", "Metroid_C5.0-260924-2115")
    assert is_stable({"tag_name": "Metroid_B4.1-260814-1733", "name": "Metroid_B4.1-260814-1733"})
    assert not is_stable({"tag_name": "Metroid_B4.0-250917-1218", "name": "Metroid_B4.0-250917-1218_4.0-OBT"})
    assert not is_stable({"tag_name": "Metroid_C5.0-260819-1642", "name": "x", "prerelease": True})
    assert is_stable({"tag_name": "Metroid_V3.5-250829-1700", "name": "Metroid_V3.5-250829-1700_3.5"})
    plan = build_plan("full", "a", "/x")
    flat = [" ".join(a) for _, a, _ in plan]
    assert plan[0][1][:2] == ["flash", "vbmeta_b"] and "set_active b" in flat
    assert not any(blocked_reason(["fastboot"] + a) for _, a, _ in plan)
    assert ["flash", "abl_b"] in [a[:2] for _, a, _ in plan]  # firmware goes to the new slot
    assert not any(a[:2] == ["flash", "abl_a"] for _, a, _ in plan)
    assert [a for _, a, _ in build_plan("root", "b", "/x", "/r.img")][0][:2] == ["flash", "boot_b"]
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        hf = Path(d) / "h"
        hf.write_text("# c\n" + "a" * 64 + " *./boot.img\n", encoding="utf-8")
        assert parse_hash_file(hf) == {"boot.img": "a" * 64}
    print("self-test OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
