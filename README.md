# HealthDrop skill for OpenClaw

This is the OpenClaw skill that pairs with the HealthDrop iPhone app. Once
installed, the OpenClaw assistant can answer questions about your sleep,
HRV, steps, resting heart rate, workouts, and ~25 other HealthKit metrics
using the latest export sitting in your iCloud Drive. The bundled
`examine.py` (stdlib Python 3 only) gives the agent two fast modes — a
SQLite-indexed `query` for single-metric / trend questions and a full
multi-domain `checkup` digest — on top of direct manifest reads for ad-hoc
questions.

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

## Prerequisites

- HealthDrop is installed on an iPhone signed into the same Apple ID as the
  Mac running OpenClaw.
- iCloud Drive is enabled on both devices.
- You have run at least one export in HealthDrop with `iCloud Drive` as the
  selected target.
- The synced folder exists at
  `~/Library/Mobile Documents/iCloud~dev~keenranger~healthdrop/Documents/`.

## Round trip

1. Open HealthDrop on iPhone → `Export` → wait a minute for iCloud to sync.
2. Ask OpenClaw something like "이번 주 수면 어땠어?" or "How is my HRV
   trending?"
3. The skill reads `healthdrop.json` (overwritten on every export) and grounds the answer.

## macOS setup (TCC / mirror)

`~/Library/Mobile Documents/iCloud~dev~keenranger~healthdrop/` is an iOS
app's private iCloud container. macOS guards it with TCC, and processes
launched outside an interactive Terminal that has Full Disk Access — including
the OpenClaw gateway, the Codex CLI, and most agent launchers — fail to read
it with `Operation not permitted` even though the file is present.

The skill ships with a built-in `setup-mirror` flow that installs a launchd
user agent. The agent mirrors the iCloud container into `~/.healthdrop/` (a
TCC-free path); the skill auto-prefers the mirror on read, so all queries keep
working. One-time setup:

```bash
python3 examine.py setup-mirror
```

The command prints two `launchctl` lines (bootstrap + kickstart) and the
absolute path of the `python3` binary that needs Full Disk Access. Open
**System Settings → Privacy & Security → Full Disk Access**, enable that
binary, then run the printed `launchctl kickstart` once. From then on the
agent fires every 120s, copies only changed files (manifest + today's day
chunk in steady state), and writes a one-line tick summary to
`~/.healthdrop/mirror-log.txt`.

To remove later:

```bash
python3 examine.py setup-mirror --uninstall
```

This stops the agent and removes the launchd plist. The mirror directory is
intentionally kept so cached data is not lost — delete `~/.healthdrop/`
manually if you no longer want it.

### Escape hatches

- `HEALTHDROP_EXPORT_PATH=/some/readable/path/healthdrop.json` — point the
  skill at any file. Wins over both the iCloud default and the mirror.
- `python3 examine.py query list /explicit/path.json` — pass the path
  positionally. Useful for ad-hoc testing.

If you really want to read the iCloud container directly, the launchd plist
that `setup-mirror` writes shows which Python binary needs Full Disk Access;
granting FDA to the *interactive* Python (or to the OpenClaw / Codex launcher
process itself) lets you skip the mirror. The mirror flow is recommended
because it survives launcher upgrades without re-granting permissions.

## Customizing

The bundle ID in the data path (`iCloud~dev~keenranger~healthdrop`) follows
HealthDrop's iOS bundle identifier (`dev.keenranger.healthdrop`). If you fork
HealthDrop and ship under a different bundle ID, update both `app.json` and
the data path in [`SKILL.md`](SKILL.md).
