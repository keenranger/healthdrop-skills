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

## Customizing

The bundle ID in the data path (`iCloud~dev~keenranger~healthdrop`) follows
HealthDrop's iOS bundle identifier (`dev.keenranger.healthdrop`). If you fork
HealthDrop and ship under a different bundle ID, update both `app.json` and
the data path in [`SKILL.md`](SKILL.md).
