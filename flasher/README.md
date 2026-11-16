# Nothing Phone (3) safe flasher

One script for updating or repairing Nothing OS on the Phone (3) (`Metroid`) **without losing data**, and for
switching on KernelSU-Next + SUSFS root built for exactly your firmware.

```bash
python3 flasher/nothing_flash.py          # interactive menu
```

Works on macOS, Linux and Windows (Python 3.9+, standard library only).

## What it does
1. Finds your phone over adb or fastboot, and refuses anything that isn't a Phone (3) with an unlocked bootloader.
2. Lists Nothing OS builds from [nothing_archive](https://github.com/spike0en/nothing_archive) (stable only;
   `--all-releases` shows betas), marking the one you have installed.
3. Downloads only what's needed to `~/.nothing-flasher/<build>/`, checks every file against GitHub's checksum and
   the archive's `-hash.sha256`, and extracts with 7-Zip.
4. Asks whether you want **KernelSU-Next + SUSFS root**. If yes, it fetches the matching
   `<build>-KernelSU-Next-SUSFS-boot.img` from this repo's releases and verifies it against `SHA256SUMS`.
5. Shows exactly which commands it will run, then waits for you to type `YES`.

| Mode | Use it for |
|---|---|
| 1 Full firmware | Updating to a new build or repairing (dirty flash, keeps data). Same partition steps as the official [Metroid flasher](https://github.com/spike0en/nothing_fastboot_flasher/tree/metroid): flashes the inactive slot, then switches to it. |
| 2 Boot images only | Restoring stock boot/init_boot/vendor_boot/dtbo/recovery for the build you already have. |
| 3 Root only | Flashing the KernelSU-Next + SUSFS boot.img for the build you already have, and optionally stock init_boot (needed if you used Magisk or KernelSU LKM). |
| 4 Download only | Fetching and verifying images. |

## Safety
- Never wipes: `-w`, `erase`, `format`, `wipe-super`, flashing `userdata`/`metadata` and bootloader relocking are
  blocked in code (`--self-test` checks this).
- Modes 2 and 3 refuse to run unless the build you picked is the one installed (a mismatched kernel won't boot).
- Checks battery ≥ 30%. If the phone is rooted, it backs up your current boot, init_boot and vendor_boot to
  `~/.nothing-flasher/<build>/backups/` first.
- `--dry-run` prints every command without touching the phone. Every run is logged to `~/.nothing-flasher/logs/`.
- If anything fails, it prints how to recover, and your data is still intact.

## Needs
- **adb/fastboot**: uses yours if they're on PATH, otherwise offers to download Google's platform-tools.
- **7-Zip**: `brew install sevenzip` · `sudo apt install 7zip` · `winget install 7zip.7zip`
- Windows: [Google USB driver](https://developer.android.com/studio/run/win-usb). Linux: [udev rules](https://developer.android.com/studio/run/device#setting-up-udev).

## Examples
```bash
python3 flasher/nothing_flash.py --list
python3 flasher/nothing_flash.py --tag Metroid_C5.0-260924-2115 --mode full --root
python3 flasher/nothing_flash.py --mode root --dry-run
```
Set `GITHUB_TOKEN` if you hit GitHub API rate limits.
