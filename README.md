# HealthDrop skill for OpenClaw

This is the OpenClaw skill that pairs with the HealthDrop iPhone app. Once
installed, the OpenClaw assistant can answer questions about your sleep,
HRV, steps, resting heart rate, workouts, and ~25 other HealthKit metrics
using the latest export sitting in your iCloud Drive. The bundled
`examine.py` (stdlib Python 3 only) gives the agent two fast modes — a
SQLite-indexed `query` for single-metric / trend questions and a full
multi-domain `checkup` digest — on top of direct manifest reads for ad-hoc
questions.

## Prerequisites — install the macOS helper first

This skill reads HealthDrop's iCloud export, but on macOS that container is TCC-protected and can't be read by skills/claws directly (see [Why a separate helper?](#why-a-separate-helper) below). Install the HealthDrop Helper first so it can re-export the data to a TCC-free path:

1. Install HealthDrop Helper via Homebrew cask:
   ```bash
   brew install --cask keenranger/tap/healthdrop-helper
   ```
   Or download the notarized `.dmg` from the [latest release](https://github.com/keenranger/healthdrop/releases/latest). If you took the `.dmg` path, open it once before the next step so Gatekeeper clears the first-launch dialog:
   ```bash
   open -a "HealthDropHelper"
   ```
   (The cask install does this automatically; only the `.dmg` path needs it.)

2. Run `healthdrop install` to register the launchd agent and trigger the first sync:
   ```bash
   healthdrop install
   ```
   This is a one-time setup. After it succeeds, the helper runs in the background via launchd on a periodic tick plus an `NSMetadataQuery` subscription, so you never need to run it again unless you reinstall or upgrade.

   **`.dmg` path note:** the `healthdrop` command lands on `PATH` via the cask's `binary` stanza, so cask users can run it directly. If you installed via the `.dmg` instead, either invoke the binary directly:
   ```bash
   /Applications/HealthDropHelper.app/Contents/MacOS/HealthDropHelper install
   ```
   or create the symlink once so subsequent calls match the cask form:
   ```bash
   sudo ln -s /Applications/HealthDropHelper.app/Contents/MacOS/HealthDropHelper /usr/local/bin/healthdrop
   healthdrop install
   ```
   `open -n -a "HealthDropHelper" --args install` works but hides stdout/stderr and the subcommand's exit code, so the direct binary path or the symlink is what you want for a CLI install. Subsequent `status` / `sync` / `uninstall` commands use the same form.

3. Then install/activate this skill in OpenClaw:
   ```bash
   openclaw skills install git:keenranger/healthdrop-skills@main
   ```

Also required on the iPhone side:

- HealthDrop is installed on an iPhone signed into the same Apple ID as the Mac running OpenClaw.
- iCloud Drive is enabled on both devices.
- You have run at least one export in HealthDrop with `iCloud Drive` as the selected target.

Once everything is set up, every HealthDrop export from your iPhone propagates automatically: iPhone → iCloud → helper → `~/.healthdrop/` → this skill.

## Why a separate helper?

HealthDrop on iOS writes its export to an app-private iCloud container at `~/Library/Mobile Documents/iCloud~dev~keenranger~healthdrop/Documents/` on the Mac. macOS guards that location with TCC (Transparency, Consent, and Control), and TCC attributes file access to the *responsible* process — the launching binary, not the binary actually doing the read. Skills and claws are launched by Claude Desktop, ChatGPT Atlas, Cursor, or OpenClaw, so any process they spawn inherits *that* launcher's TCC attribution. Those launchers are denied iCloud-container reads, and the failure is silent: `os.path.exists()` returns `True`, `os.access(..., R_OK)` returns `False`, and reads either fail with `Operation not permitted` or come back empty. Granting Full Disk Access to every potential launcher is also the wrong fix — it's a blanket grant of read access to the entire user home, far broader than the export needs.

The HealthDrop Helper is a signed, notarized macOS `.app` that holds the matching `com.apple.developer.icloud-container-identifiers` entitlement for `iCloud.dev.keenranger.healthdrop`. It reads the container via the official ubiquity APIs (`URLForUbiquityContainerIdentifier` + `NSFileCoordinator` + `startDownloadingUbiquitousItem`, so evicted chunks pull back on demand) and re-exports the manifest plus day chunks to `~/.healthdrop/`. That path is TCC-free, so any skill, claw, or future tool can read it as plain files with no entitlement plumbing and no per-launcher Full Disk Access grant. This skill points at `~/.healthdrop/` instead of the iCloud container for exactly that reason.

## Install (one-liner)

```bash
openclaw skills install git:keenranger/healthdrop-skills@main
```

The OpenClaw CLI clones the repo, reads `SKILL.md` frontmatter for the slug
(`healthdrop`), and copies it into your workspace. Run again after `main`
updates to pull a new version.

## Install (manual)

Symlink the folder so changes to the repo propagate without re-installing:

```bash
git clone https://github.com/keenranger/healthdrop-skills ~/src/healthdrop-skills
ln -s ~/src/healthdrop-skills ~/.openclaw/workspace/skills/healthdrop
```

Or copy it once (from inside the cloned repo):

```bash
cp -R . ~/.openclaw/workspace/skills/healthdrop
```

Restart your OpenClaw agent so it picks up the new skill.

## Round trip

1. Open HealthDrop on iPhone → `Export` → wait a minute for iCloud to sync.
2. Ask OpenClaw something like "이번 주 수면 어땠어?" or "How is my HRV
   trending?"
3. The skill reads `healthdrop.json` (overwritten on every export) and grounds the answer.

## Troubleshooting: skill returns empty data

If the skill responds with "no export found" or stale numbers, the helper probably isn't keeping `~/.healthdrop/` fresh. Check in order:

- **Inspect `~/.healthdrop/`.** It should contain `healthdrop.json` plus a `days/` directory. If the directory is missing, or `healthdrop.json`'s `mtime` is older than 24 hours (and you've exported more recently from your iPhone), the helper isn't running.
  ```bash
  ls -la ~/.healthdrop/
  stat -f "%Sm" ~/.healthdrop/healthdrop.json
  ```
- **Ask the helper directly.**
  ```bash
  healthdrop status
  ```
  This reports the launchd agent state, last sync time, and whether the iCloud container is reachable. If the agent isn't loaded, the command will tell you.
- **Re-run install or sync.** If `healthdrop status` reports the agent isn't running, re-run install (it's idempotent):
  ```bash
  healthdrop install
  ```
  Or kick off a one-shot mirror if you just want to refresh the data:
  ```bash
  healthdrop sync
  ```
  If that doesn't recover, reinstall the cask and re-run install:
  ```bash
  brew reinstall --cask keenranger/tap/healthdrop-helper
  healthdrop install
  ```
- **Confirm the iPhone side.** If `~/.healthdrop/` is fresh but the data still looks stale, the export itself may be old — open HealthDrop on the iPhone, run an export, and give iCloud a minute to sync before re-asking the skill.

## Customizing

The bundle ID in the data path (`iCloud~dev~keenranger~healthdrop`) follows
HealthDrop's iOS bundle identifier (`dev.keenranger.healthdrop`). If you fork
HealthDrop and ship under a different bundle ID, update both `app.json` and
the data path in [`SKILL.md`](SKILL.md).

## See also

- HealthDrop iOS app: [keenranger/healthdrop](https://github.com/keenranger/healthdrop)
- Helper target tracking issue: [keenranger/healthdrop#14](https://github.com/keenranger/healthdrop/issues/14)
- Helper distribution tracking issue: [keenranger/healthdrop#17](https://github.com/keenranger/healthdrop/issues/17)
- Architecture decision record: [ADR-001](https://github.com/keenranger/healthdrop/blob/main/docs/decisions/ADR-001-helper-cask-decoupled-from-skill-installer.md)
