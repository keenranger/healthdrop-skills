---
name: healthdrop
description: Answer questions about the user's iPhone HealthKit data — sleep, HRV, resting heart rate, steps, workouts, and ~25 other quantity types — from their HealthDrop iCloud export. Three modes: `examine.py query` for one metric / one day / a trend (SQLite-indexed, hundreds of tokens), `examine.py` for a full multi-domain checkup with thresholds, direct manifest + day-chunk reads for ad-hoc / cross-metric questions or when Python is unavailable. Triggers on Korean and English health keywords like 수면 / sleep, HRV, 심박, 걸음 / steps, 운동 / workout, 회복 / recovery, 컨디션, 헬스 / health, 건강 검진, 종합 분석.
---

# HealthDrop skill

HealthDrop is an iPhone app that writes the user's HealthKit data to their
iCloud Drive as a small manifest plus one JSON file per UTC day. This skill
reads that export and answers questions grounded in the user's own data —
across whatever history they have, not just the last week.

## Data location

```
~/Library/Mobile Documents/iCloud~dev~keenranger~healthdrop/Documents/
├── healthdrop.json          ← manifest (a few KB)
└── days/
    ├── 2024-01-15.json      ← one chunk per UTC day
    ├── 2024-01-16.json
    └── ...
```

If `healthdrop.json` does not exist, iCloud has not synced yet. Wait a
minute or ask the user to open the HealthDrop app on iPhone (the first
export bootstraps the manifest).

## When to invoke

Anything that needs the user's own health data:

- Single metric / trend: "HRV 어때?", "어제 몇 보 걸었어?", "1년 VO2max 변화"
- Holistic: "건강 검진 해줘", "종합 분석", "전체적으로 어때?"
- Ad-hoc: "HRV 가장 낮았던 날 수면 어땠어?", "주말만 평균"

Do NOT invoke for general health-knowledge questions ("what is HRV?") that
don't need the user's data.

## Three modes — pick by question shape

| Question shape | Mode | Why |
|---|---|---|
| One metric, single stat, trend, per-day series | **A: `examine.py query`** | SQLite-indexed; ~hundreds of tokens |
| Whole-body checkup, graded read-out | **B: `examine.py`** (full report) | Engine applies thresholds + flags; one digest |
| Cross-metric correlation, arbitrary grouping, sample-level pattern, or no Python available | **C: manifest + day chunks** | LLM-side analysis on raw samples |

Between A and B when unsure: lead with A; offer to expand to a full checkup.

## Mode A — Targeted queries (`examine.py query`)

```bash
# What's recorded + window
python3 skills/healthdrop/examine.py query list

# One metric — summary, single stat, or per-day series
python3 skills/healthdrop/examine.py query metric restingHeartRate
python3 skills/healthdrop/examine.py query metric stepCount --stat avg --days 7
python3 skills/healthdrop/examine.py query metric heartRateVariabilitySDNN --stat series
python3 skills/healthdrop/examine.py query metric oxygenSaturation --from 2026-05-25 --to 2026-05-28

# All metrics for one local calendar day
python3 skills/healthdrop/examine.py query day 2026-05-27
```

- `--stat` ∈ `summary|avg|min|max|sum|count|latest|series`. Default `summary`
  = avg/min/max/latest.
- Range: `--days N` (anchored at the export's last day) or `--from`/`--to`
  (inclusive). Omit both for the whole window.
- Cumulative metrics (steps, energy, exercise/stand minutes, flights,
  distance) report per-day **sums**; everything else per-day **means**.
  `latest`/`min`/`max` use raw readings.
- `--json` emits a structured `healthdrop.query.*` object (numbers; for
  `metric`, the full `series`).
- First query builds a SQLite per-metric-per-day index under
  `~/.cache/healthdrop/` and reuses it until the export changes — query
  cost stays flat as history grows. The cache is derived; delete to rebuild.

Run `query list` first if unsure of the exact key. Keys are HealthKit
identifiers without the `HKQuantityTypeIdentifier` prefix (e.g.
`restingHeartRate`, `stepCount`).

## Mode B — Full checkup (`examine.py`)

```bash
python3 skills/healthdrop/examine.py
```

Reads the canonical iCloud path, computes four domains (sleep,
cardiovascular & recovery, activity & energy, body composition & gait),
applies consumer-grade thresholds, and prints a compact digest. Read the
**digest**, not the JSON — re-reading the file defeats the purpose.

Check the status header first:

- `meta.status = "no_file"` ("export not found") → iCloud hasn't synced or no
  export has run. Tell the user to run an export and retry.
- `meta.status = "parse_error"` → unreadable file; say so. Don't paste it.
- `meta.status = "permission_denied"` → macOS TCC blocks the iCloud container
  from this launcher (OpenClaw / Codex CLI / etc.). Tell the user to run
  `python3 examine.py setup-mirror` once and follow the printed launchctl +
  Full Disk Access steps; the skill auto-prefers the resulting
  `~/.healthdrop/` mirror on subsequent runs. Or, point the skill at any
  readable file via `HEALTHDROP_EXPORT_PATH=/path/to/healthdrop.json`.
- Otherwise proceed.

Flags:

- `--input PATH` overrides the iCloud path.
- `HEALTHDROP_EXPORT_PATH` env var overrides the iCloud path (wins over both
  the default and the local mirror). Useful for one-off testing.
- `--json` emits `DigestReport` (`schema: "healthdrop.digest/1"`) with
  numbers, band tokens (`green`/`amber`/`red`), and bilingual flag messages
  (`message_ko`/`message_en`).
- `--lang ko|en` sets a render hint inside the JSON only.

The digest's **Flags** section surfaces data-quality issues — stale data,
phantom workouts (~40s/~3kcal junk) excluded from activity, multi-source
nights de-duplicated, envelope-based efficiency, single-reading-only data.
Pass the relevant ones along in plain language; they explain why a number
is provisional or why a workout was ignored.

## Mode C — Direct read (manifest + day chunks)

When the question doesn't fit A or B — cross-metric, arbitrary grouping,
sample-level pattern, or Python is unavailable:

1. **Pick the right root**, in this order:
   - `$HEALTHDROP_EXPORT_PATH` if set — its parent directory is the root.
   - `~/.healthdrop/` if it exists (the macOS mirror; see SKILL.md "macOS
     setup" / README). Modes A and B auto-prefer this; direct reads must
     opt in by reading from here instead of the iCloud container.
   - Otherwise `~/Library/Mobile Documents/iCloud~dev~keenranger~healthdrop/Documents/`.
2. Read `<root>/healthdrop.json` (the manifest). Check `generatedAt`.
3. Identify which UTC days are needed:
   - "Last night?" → 1–2 chunks (may straddle UTC midnight)
   - "This month RHR?" → ~30 chunks
   - "Year of VO2max?" → ~365 chunks (cheap; sparse metric)
4. Read each `<root>/days/YYYY-MM-DD.json` one at a time. Use the manifest's
   `sampleCount` / `sizeBytes` per entry to estimate cost before deciding
   the window.
5. Stream: accumulate stats per chunk, then drop it. Never hold every chunk
   in memory.

**Always start by reading the manifest.** Never `ls days/` or read every
chunk eagerly — that defeats the whole chunked design.

Reading the iCloud path when the mirror exists is a bug: same TCC
restriction that triggers `meta.status="permission_denied"` for Modes A/B
will hit Mode C too. The mirror is the single point of truth on macOS
once setup-mirror has been run.

### Local time vs UTC

Chunks are bucketed by **UTC date of `startDate`**. Each sample's
`startDate` keeps its original ISO-8601 timestamp with UTC offset, so you
can re-bucket into local time. "Last night" often needs two adjacent UTC
chunks.

### Cost discipline

Dense metrics (`heartRate`, `respiratoryRate`, `walkingHeartRateAverage`,
`oxygenSaturation`) can produce hundreds of thousands of samples over
multi-year history. Downsample or aggregate before quoting trends — never
enumerate every sample.

## Schema (HealthDrop v4)

```ts
type Manifest = {
  schemaVersion: 4;
  generatedAt: string;             // ISO 8601
  app: { version: string };
  days: ManifestDayEntry[];        // sorted ascending
  anchors: Record<string, string>; // opaque; ignore
};

type ManifestDayEntry = {
  date: string;        // YYYY-MM-DD, UTC
  path: string;        // "days/2026-05-28.json"
  sha256: string;
  sampleCount: number;
  sizeBytes: number;
};

type DayChunk = {
  schemaVersion: 4;
  date: string;                            // YYYY-MM-DD, UTC
  metrics: Record<string, SamplePoint[]>;
  sleep: SleepInterval[];
  workouts: WorkoutSummary[];
};

type SamplePoint = {
  startDate: string;   // ISO 8601 with UTC offset
  endDate: string;
  value: number;
  unit: string;        // raw HealthKit unit ('count', 'count/min', 'ms', 'kg', ...)
  source?: string;
};

type SleepInterval = {
  startDate: string;
  endDate: string;
  stage: 'inBed' | 'asleepUnspecified' | 'awake' | 'core' | 'deep' | 'rem';
  source?: string;
};

type WorkoutSummary = {
  startDate: string;
  endDate: string;
  activityType: string;   // raw HealthKit identifier as string
  durationSec: number;
  totalEnergyKcal?: number;
  totalDistanceMeters?: number;
  source?: string;
};
```

### Metric keys (HealthDrop v4)

`metrics` is keyed by the HealthKit identifier minus the
`HKQuantityTypeIdentifier` prefix. Treat unknown keys as just another
`SamplePoint[]`; absent keys mean the user hasn't recorded that metric.

- **Activity ring**: `stepCount`, `activeEnergyBurned`, `basalEnergyBurned`,
  `appleExerciseTime`, `appleStandTime`, `flightsClimbed`
- **Distance**: `distanceWalkingRunning`, `distanceCycling`,
  `distanceSwimming`
- **Cardiovascular**: `heartRate`, `restingHeartRate`,
  `walkingHeartRateAverage`, `heartRateVariabilitySDNN`,
  `oxygenSaturation`, `vo2Max`, `respiratoryRate`
- **Body composition**: `bodyMass`, `bodyMassIndex`, `bodyFatPercentage`,
  `leanBodyMass`
- **Temperature**: `appleSleepingWristTemperature`, `bodyTemperature`
- **Walking analysis**: `walkingSpeed`, `walkingStepLength`,
  `walkingAsymmetryPercentage`, `walkingDoubleSupportPercentage`

### Unit quirks

- Any metric with `unit: "%"` is stored as a **0–1 fraction**
  (`oxygenSaturation 0.97` = 97%, `bodyFatPercentage 0.159` = 15.9%).
  Multiply by 100 for display. `examine.py` already handles this; Mode C
  reads must convert manually.
- Cumulative metrics (steps, energy, exercise minutes, flights, distance)
  are summed per local day, then averaged across days — never averaged per
  raw sample. `examine.py query --stat avg` already does this.

## Answering style

- **Language matches the user** (Korean → Korean, English → English). The
  default digest is English; for a Korean answer, translate numbers into
  Korean prose. `--json` localizes only flag messages.
- **Number first, interpretation second.** Example:
  > "지난 7일 평균 수면 6시간 43분, HRV 평균 45ms. 안정 심박 82bpm으로 회복
  > 주의 신호."
- **Cite freshness.** Always mention `generatedAt`. If stale (24–72h) or very
  stale (>72h), say so and suggest a re-export before trusting trends.
- **Bands.** `[green]`/`[amber]`/`[red]` map to 양호 / 주의 / 경고.
  `Overall condition` maps as `good` 양호 / `watch` 주의(경미) / `mixed`
  주의(복합) / `attention` 경고(점검 필요) / `insufficient_data` 데이터 부족.
  If `data completeness` is below 100%, qualify the verdict as based on
  limited data.
- **Lead the overall with the worst driver.** The engine uses a transparent
  worst-of rollup, not a fake 0–100 score.
- **Prefer averages over single points.** 3-day or 7-day for short-arc
  questions; 30-day or monthly for months/year. Single nights/days are noisy.
- **Never invent samples.** If a chunk you need is missing, say so and
  suggest a fresh export.
- **No clinical claims.** Consumer wearable data, not medical diagnosis.
  Strongest nudge is a soft "if the pattern persists, consider checking with
  a clinician" (KO: "패턴이 지속되면 전문가 상담을 고려해 보세요").

### Empty / missing metrics

Many users have no `vo2Max`, no cycling/swimming distance, only a single
body-composition reading. Say "no X data in this window" (not "0") and
suggest recording + re-exporting. The engine reports those domains as "no
data" and lists them rather than printing zero.

## Privacy

Private to the user. Emit aggregates, digests, and summary numbers — never
raw `metrics`/`sleep`/`workouts` arrays, never the file body. Do not log
the data or transmit it anywhere. `examine.py` itself prints only
aggregates and sends nothing.

## Fallback

If `examine.py` fails for a reason other than no-file/parse-error, fall
back to Mode C (raw manifest + day-chunk read), applying the same
freshness, privacy, and non-clinical rules by hand.
