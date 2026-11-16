# nothing-susfs

Automatic KernelSU-Next + SUSFS GKI kernel builds for the **Nothing Phone (3)** (`Metroid`), plus a safe,
cross-platform flasher.

- **Auto-release workflow**: for each stable Nothing OS release it rebuilds the kernel from the **exact stock
  kernel commit** with the stock config, so the phone's stock vendor modules still load, repacks the stock
  `boot.img`, and publishes a ready-to-flash `boot.img` as a GitHub release.
- **Flasher** (`flasher/nothing_flash.py`): Python, standard library only. Updates or repairs Nothing OS without
  wiping data, and installs the matching root `boot.img` from this repo's releases.

> **Status:** the workflow has not run on GitHub yet; its first run is the real test. Releases are built
> automatically and are **not boot-tested**. See [Disclaimer](#disclaimer).

## Quick start (flasher)

### Prerequisites

| Need | Notes |
|---|---|
| Python 3.9+ | macOS/Linux: `python3`. Windows: `py -3` or `python`. |
| adb + fastboot (platform-tools) | Uses yours if on `PATH`; otherwise offers to download Google's platform-tools into `~/.nothing-flasher/`. |
| 7-Zip | macOS: `brew install sevenzip`. Linux: `sudo apt install 7zip` (or `p7zip-full`). Windows: `winget install 7zip.7zip`. Looks for `7zz`, `7z`, `7za`. |
| Unlocked bootloader | Unlocking wipes the phone and is done once, by you. The tool never unlocks or relocks. |
| Windows / Linux extras | Windows: [Google USB driver](https://developer.android.com/studio/run/win-usb). Linux: [udev rules](https://developer.android.com/studio/run/device#setting-up-udev). |

Connect the phone with USB debugging on (or boot it to fastboot), then:

```bash
python3 flasher/nothing_flash.py            # interactive menu
```

### Options

Exactly as printed by `python3 flasher/nothing_flash.py --help`:

```
usage: nothing_flash.py [-h] [--tag TAG] [--mode {full,boot,root,download}]
                        [--root | --no-root] [--all-releases] [--list]
                        [--cache CACHE] [--dry-run] [--assume-slot {a,b}]
                        [--skip-big-downloads] [--firmware-both-slots]
                        [--self-test]

  --tag TAG             archive tag, e.g. Metroid_C5.0-260924-2115 (default: pick interactively)
  --mode {full,boot,root,download}
                        full = update all firmware (keeps data); boot = boot images only;
                        root = flash KernelSU-Next+SUSFS boot only; download = fetch + verify only
  --root                use KernelSU-Next + SUSFS boot.img
  --no-root             use stock boot.img
  --all-releases        also list beta/pre-release builds
  --list                list builds and exit
  --cache CACHE         download folder (default ~/.nothing-flasher)
  --dry-run             print every command instead of running it
  --assume-slot {a,b}   dry-run without a device: current slot to assume
  --skip-big-downloads  with --dry-run: don't download firmware/logical archives (plan only)
  --firmware-both-slots full mode: flash firmware to a and b
  --self-test           run built-in safety checks and exit
```

Also: `NOTHING_FLASHER_HOME` sets the cache folder (same as `--cache`); `GITHUB_TOKEN` (or `GH_TOKEN`) avoids
GitHub API rate limits.

### Interactive menu

1. Lists the 15 newest stable builds from [spike0en/nothing_archive](https://github.com/spike0en/nothing_archive),
   marking the one installed (`--all-releases` adds betas). You pick one.
2. Asks what to do:

| Menu | `--mode` | What it does |
|---|---|---|
| 1 | `full` | **Full update, keeps data.** Flashes vbmeta, boot images, logical partitions (system, vendor, ...) and firmware to the *inactive* slot, then switches to it (same partition steps as the official Metroid flasher). Logical partitions on both slots are recreated. Firmware goes to the new slot only, or to both with `--firmware-both-slots`. |
| 2 | `boot` | Flashes `boot`, `init_boot`, `vendor_boot`, `dtbo`, `recovery` to the current slot. Build must equal the installed one. |
| 3 | `root` | Flashes only the KernelSU-Next + SUSFS `boot` to the current slot. Build must equal the installed one. Asks whether to also flash stock `init_boot` (default yes; needed if Magisk or KernelSU LKM was used before). |
| 4 | `download` | Downloads and verifies images, flashes nothing. |

3. In `full`/`boot` mode it asks whether you want root (`--root` / `--no-root` skip the question). If no verified
   root build exists for that tag it offers to continue with stock boot (never in `root` mode).
4. Prints the full flash plan and waits for you to type `YES`.
5. Reboots. If a root build was used it offers to install the manager APK from the release.

### Examples

```bash
python3 flasher/nothing_flash.py --list
python3 flasher/nothing_flash.py --tag Metroid_C5.0-260924-2115 --mode full --root
python3 flasher/nothing_flash.py --mode root --dry-run
python3 flasher/nothing_flash.py --mode full --dry-run --assume-slot a --skip-big-downloads   # no phone needed
python3 flasher/nothing_flash.py --self-test
```

`--dry-run` prints every command that would change the phone (flash, reboot, backup) and does not run it. It still
downloads files and, if a phone is connected, runs read-only `adb`/`fastboot` queries. `--self-test` runs the
built-in checks of the command denylist, tag parsing, downgrade logic, and the flash plan; it needs no phone or
network.

Downloads are cached in `~/.nothing-flasher/<build>/`; logs go to `~/.nothing-flasher/logs/`.

## Safety

What the code enforces (see `flasher/nothing_flash.py`):

- **Command denylist.** Every `fastboot`/`adb` command is checked before it runs and refused if it contains
  `-w`, `--wipe`, `erase`, `format`, `wipe-super`, `flashall`, `update`, a flash to `userdata`/`metadata`/`data`/
  `cache`, `flashing lock` / `oem lock`, or a factory-reset/wipe-data argument. There is no wipe and no relock
  path. `--self-test` verifies this.
- **Model check.** Over adb the device must report `Metroid`. In fastboot the product must be `metroid`/`sun`, the
  bootloader must report unlocked, and you must confirm it is a Phone (3).
- **Downgrade refusal (`full` mode).** A build older than the installed one is refused (older firmware can trip
  anti-rollback). If the installed build can't be read, it asks you to confirm the target is same or newer.
- **Build match (`boot`/`root` modes).** The chosen build must equal the installed one; if it can't be read, it
  asks you to confirm.
- **Verification.** Archive downloads are checked against GitHub's digest and the archive's `-hash.sha256`;
  extracted images are re-hashed; the root `boot.img` is checked against the release's `SHA256SUMS`. A missing hash
  file aborts.
- **Battery.** Over adb, refuses below 30%. In fastboot it asks you.
- **Backup.** If the phone is rooted and reachable over adb, `boot`, `init_boot`, `vendor_boot` of the current slot
  are pulled to `~/.nothing-flasher/<build>/backups/<timestamp>/` first.
- **Typed `YES`** before anything is flashed (not asked in `--dry-run`).
- **Recovery help** printed if flashing stops partway.

What is **not** guaranteed:

- It is not a sandbox. The denylist only covers the commands this tool itself issues; it does not make a bad image,
  a cable drop, or a power loss safe.
- "Keeps data" means the tool never erases `userdata`/`metadata`. An interrupted `full` update can leave the phone
  unbootable until you re-run it. Back up what matters first.
- Firmware (bootloader-level) partitions are flashed in `full` mode. The downgrade check relies on the build stamp
  in the tag and on you answering honestly when the installed build is unreadable.
- Backups happen only on a rooted phone over adb; otherwise your fallback is the stock images in the cache.
- The platform-tools download from `dl.google.com` has no published checksum (HTTPS only).
- Root builds are not boot-tested (see below). The bootloader-unlocked check only runs in fastboot mode; over adb
  a locked bootloader simply makes fastboot fail.
- The flasher has been checked with `--self-test` and dry runs, not against every firmware release.

## Auto-release workflow

`.github/workflows/auto-release.yml`, "Auto-build KernelSU-Next + SUSFS (Nothing Phone 3)".

**Triggers**
- Daily cron at 04:23 UTC.
- `workflow_dispatch` with inputs `archive_tag` (blank = newest stable, e.g. `Metroid_C5.0-260924-2115`) and
  `force` (rebuild even if that tag already has a release).
- Only one run at a time (`concurrency` group, no cancel). GitHub runs cron and manual dispatch only from the
  default branch, so it takes effect once merged.

**`detect` job** (`.github/scripts/metroid_detect.py`)
- Picks the newest stable Metroid build in spike0en/nothing_archive (no prerelease/beta/OBT), or `archive_tag`.
- Stops with nothing to do if this repo already has a release for that tag (unless `force`).
- Downloads the stock `boot.img`, verifies its hash, reads the kernel version banner, derives the GKI commit,
  sublevel, monthly branch, page size and build timestamp, and checks the commit's `SUBLEVEL` matches.

**`build` job** (180 min timeout)
- Uses a pinned build engine, `sr2echa/GKI_KernelSU_SUSFS@ab898f9...` (fork of WildKernels'), pinned magiskboot
  from the official Magisk v31.1 APK (SHA-256 checked). KernelSU-Next (`dev-susfs` branch), WildKernels patches and
  AnyKernel3 are resolved at build time, not pinned.
- Builds with SUSFS + Ptrace fix + Unicode fix only. Everything else (NoMount, DroidSpaces, networking, NTSync,
  BBG, BPF, Perf) stays off.
- Fails instead of publishing unless: `kernel/common` is exactly the stock commit; the built kernel's version string
  and build timestamp equal stock; `CONFIG_KSU=y` and `CONFIG_KSU_SUSFS=y` (when the `.config` is found; if not, a
  warning is logged and this check is skipped); `FANOTIFY`, `TMPFS_XATTR`, `TMPFS_POSIX_ACL`, `SYSVIPC` are not `=y`;
  the repacked image keeps its AVB footer and contains the new kernel.
- The KernelSU-Next manager APK is best effort; a missing APK does not block the release.

**Release** (tag = archive tag, e.g. `Metroid_C5.0-260924-2115`; an existing release with that tag is replaced):
`<tag>-KernelSU-Next-SUSFS-boot.img`, `<tag>-KernelSU-Next-SUSFS-AnyKernel3.zip`, manager APK(s) if found,
`build-info.json` (kernel release, GKI commit, SUSFS/KernelSU-Next versions, `boot_tested: false`) and `SHA256SUMS`.

**Not yet proven.** The workflow has never run on GitHub. Its first run will likely expose problems. Releases are
auto-built and **not boot-tested**: treat each one as untested until you have booted it.

## Why the build is strict

Enabling extra kernel options (`FANOTIFY`, `TMPFS_XATTR`, `TMPFS_POSIX_ACL`, `SYSVIPC`, DroidSpaces/networking
add-ons) changes struct layouts and therefore symbol CRCs. The phone's stock vendor drivers then refuse to load,
and boot hangs on the static logo. The kernel must be built from the exact stock commit with the stock config,
and a kernel from a different Nothing OS build will not work either.

## Manual flashing

Only use the `boot.img` that matches your installed build exactly (`Settings > About`, or the tag). Verify it with
the release's `SHA256SUMS`.

1. `adb reboot bootloader`
2. Make sure `init_boot` is stock (no Magisk / KernelSU LKM patch). If you used LKM before, flash the stock
   `init_boot` for your build to the active slot.
3. One-time test, nothing written: `fastboot boot <tag>-KernelSU-Next-SUSFS-boot.img`. If it hangs, hold
   Power + Volume Down to get back.
4. If it booted, flash it: `fastboot getvar current-slot`, then
   `fastboot flash boot_<slot> <tag>-KernelSU-Next-SUSFS-boot.img`.
5. Install the KernelSU-Next manager APK from the same release.

Or let the flasher do it: `python3 flasher/nothing_flash.py --tag <tag> --mode root`.

## Recovery

Your data is not erased by anything here. To get a booting phone again:

1. Hold Power + Volume Down until the bootloader screen appears.
2. Flash the stock `boot.img` for your build back to the same slot:
   `fastboot flash boot_<slot> boot.img` (the stock images come from nothing_archive; `./fetch_stock.sh [TAG]`
   downloads, verifies and extracts the boot-chain images to `stock/<TAG>/`, and needs `curl`, `jq`, 7-Zip).
3. Or re-run the flasher with `--mode boot --no-root` (same build), or `--mode full` to repair the whole build.

The flasher also prints these steps when it stops partway, and keeps backups of your previous boot images when it
could make them.

## Troubleshooting

| Symptom | Try |
|---|---|
| Stuck on the static logo | Wrong build, or the kernel config differs from stock. Recover as above. |
| "No verified root build exists for ..." | No release for that tag yet (workflow not run, or the build failed). Check the Releases page and Actions tab. |
| "7-Zip is required" | Install 7-Zip (see prerequisites). |
| "No phone found" | Enable USB debugging, check cable and drivers/udev, or boot to fastboot. |
| "Refusing" a downgrade / "must match the installed build" | Pick the installed build for `boot`/`root`; use `full` to change versions, and only to same-or-newer. |
| "Checksum mismatch" | The bad file is deleted; re-run. |
| "GitHub API rate limit hit" | Set `GITHUB_TOKEN` and retry. |
| Download interrupted | Re-run; partial downloads resume. |

## Disclaimer

An unlocked bootloader and root weaken device security and can brick or bootloop the phone, lose data, or break
banking/DRM apps. You flash at your own risk. Releases here are unofficial, auto-built and untested on your device.
This project is not affiliated with or endorsed by Nothing Technology.

## Credits

- [spike0en/nothing_archive](https://github.com/spike0en/nothing_archive) and
  [spike0en/nothing_fastboot_flasher](https://github.com/spike0en/nothing_fastboot_flasher): firmware archive and
  the partition flashing logic the flasher mirrors.
- [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS): the build engine this
  workflow is based on.
- [KernelSU-Next](https://github.com/KernelSU-Next/KernelSU-Next): root solution.
- SUSFS by simonpunk.
- magiskboot from [Magisk](https://github.com/topjohnwu/Magisk) by topjohnwu: boot image repacking.
