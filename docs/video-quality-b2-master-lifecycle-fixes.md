# B.2 stabilization report: master lifecycle, event loop, tests

Follow-up to `docs/video-quality-b2-report.md` and the pre-landing review. Commit: see `git log` after `9b85b64`.

## 1. Scenario check: master after the first successful publish

Scenario: render → prepare + publish to Instagram → app restart → later prepare the same
video for YouTube / Telegram.

- **Before:** the master was deleted by the publish cleanup as soon as the job reached a
  settled state (`stage == "published"` with no errors). The first publish destroyed the
  quality source needed by the next platform; a later prepare fell back to the delivery
  encode (quality loss) with only a warning.
- **Now (architecture):**
  - `_cleanup_publish_artifacts` no longer lists `master_video`/`master.mp4` among its
    candidates — cleanup routines never delete the master (app.py `_cleanup_publish_artifacts`).
  - `_protected_montage_paths` always protects the recorded master (app.py), so render
    cleanup also cannot touch it (important in inplace mode where the master is a
    `{job}_stage` stream such as `sub_words.mp4` / `censored_audio.mp4`).
  - A dedicated retention policy `app._retire_masters()` is the only component that may
    delete a master, and only for **fully settled** jobs:
    `stage == "published"` with no `publish_errors` **and** no `publish_unknown`.
    Unfinished / partially published / errored jobs always keep the master (retry and
    second-platform publishing stay on the same quality source).
  - The master is therefore never deleted *just because* one platform published; it is
    pruned only by the retention limits below.
  - Safe fallback if a master is missing/damaged is unchanged: `_prep_source_video`
    falls back to `result_video` with a `[montage-prep]`` warning on stderr.

## 2. Retention limits (tunable via env)

| Env var | Default | Meaning |
|---|---|---|
| `AUTOMONTAGE_MASTER_MAX_AGE_DAYS` | `7` | age-based prune (0 disables); master mtime vs job settle age |
| `AUTOMONTAGE_MASTER_MAX_TOTAL_BYTES` | `2 GiB` | total master volume ceiling across all jobs (0 disables); oldest removed first |
| `AUTOMONTAGE_MASTER_MIN_FREE_BYTES` | `1 GiB` | when free disk space drops below this, oldest settled masters are removed until the floor is restored (0 disables) |

Deletion order: age → low free space → total-size ceiling; always oldest-first, always
settled jobs only. A sweep summary is printed to stderr as `[master-retention] {...}`
when something was removed or an error occurred. Sweeps run (a) after a successful
publish cleanup and (b) during `_recover_montage_jobs()` at app restart, off the event
loop when a loop is running (`asyncio.to_thread` + task, guarded by a non-blocking
`threading.Lock` so concurrent sweeps do not pile up).

## 3. `AUTOMONTAGE_MASTER_MODE` resolution (montage/engine.py)

The inline master block in `render_montage` was extracted into
`montage.engine._resolve_master_export(raw_mode, final_mp4, workdir, free_bytes=None)`:
- `inplace` (default) records the fully effected pre-delivery stream (no extra encode);
- `copy` additionally writes `master.mp4` (byte-exact `shutil.copy2`), gated by a
  free-space check (`file size + 64 MiB`), falling back to `inplace` on failure or
  insufficient space;
- `none` disables master tracking (`master_video: ""`);
- unknown values fall back to `inplace` with a stderr warning.
Behavior is unchanged; the audio-censored stream stays the master source because the
master block runs *after* `audio_censor` (master = `censored_audio.mp4` when muting
intervals exist), now covered by a direct test.

## 4. Event loop (Priority 2)

All ffprobe-bearing readiness probes in async contexts now run in worker threads:

- `app.montage_result` (status payload): `_public_publish_ready` and
  `mirror_variant_ready` via `await asyncio.to_thread(...)`;
- `app.process_prepare_publish`: source resolution (`_prep_source_video`), the
  `unresolved` scan, and per-item `_prepared_video_entry` reuse checks — all off the
  event loop;
- `app.publish_montage` and `app.process_publish`: `_publish_readiness` calls are
  `await asyncio.to_thread(...)`.

Concurrency/cancel/error semantics: probes only read job dicts and stat files (no new
shared mutable state; GIL-atomic dict reads; no races introduced). Cancellation is still
checked between prepare items; a stranded ffprobe thread dies with its 120 s timeout
instead of blocking the loop. Errors inside prepared/video transforms keep the previous
`prepare_status=error` reporting; masters are untouched on error/cancel.

## 5. Direct tests added (test_montage_master_retention.py, +18)

- mode matrix: `inplace`, `copy` (byte-exact `master.mp4`), `none`, unknown value →
  `inplace`, insufficient free space for `copy` → `inplace` fallback;
- full spec scenario: publish (IG) → cleanup → restart recovery → prepare another
  platform from the master → fresh master survives the 7-day window;
- retention: age-out only for fully settled jobs (processing / publish-errors /
  publish-unknown masters survive an expired-age sweep), oldest-first total-size
  ceiling, low-free-space prune, env-disabled policy, idempotent sweeps;
- path traversal: hostile `job_id` / `master_video` from job state are flattened
  (`Path(...).name`) and can never escape `montage_work`;
- missing / damaged master preparation falls back to `result_video` with a warning;
- the recorded master comes from the censoring pipeline: a real tiny video is muted
  via the engine's exact censor command (`build_volume_filter`, `find_mute_intervals`)
  and `volumedetect` confirms ≥20 dB drop inside the mute interval; `_resolve_master_export("inplace", ...)` records the censored stream name;
- async: two jobs prepare concurrently (both succeed, masters intact), prepare error
  keeps the master and sets `prepare_status=error`, cancellation keeps the master and
  sets `prepare_status=cancelled`, and the status payload readiness works with a
  damaged master.

Existing tests updated: `test_montage_cleanup.test_publish_cleanup_releases_master_after_publish`
→ `test_publish_cleanup_keeps_master_after_publish` (first/second sequential publish keep the master).

Full suite: **213 tests OK** (`unittest`), 3 pre-existing skips. Encode params, FPS,
HDR, autozoom and content-factory code untouched; no real publications performed.

## 6. TOCTOU guard: in-flight master registry (QA F9 fix)

QA run `2026-10-23T07:29Z` (finding F9) showed: under aggressive retention
overrides a sweep can unlink a settled master **between** the preparation's
source resolution and the moment ffmpeg opens it. A plain
`prepare_status == "running"` check is not sufficient: the source prescan
(`process_prepare_publish`) runs *before* `prepare_status` is set to
`"running"`, and sweep/registration were not synchronized.

Fix (app.py):

- `_PREP_ACTIVE_MASTER_JOBS` — job ids whose master is in use by an in-flight
  platform preparation; **guarded by the same `_MASTER_RETENTION_LOCK` the
  sweep holds**, so registration and the sweep's removal decisions are
  linearized (a registration that lands before a sweep's decision protects
  the job; a finished sweep strictly precedes the next registration claim).
- `_register_prep_master(job_id)` runs via `asyncio.to_thread` at the very
  start of `process_prepare_publish` (before the source prescan) and blocks
  until any concurrently running sweep finishes — the loop is never blocked.
- `_release_prep_master(job_id)` drops the claim; the whole preparation body
  now runs inside `try/finally`, so the claim is released on success, error
  and cancellation (including cancellation between the prescan awaits).
- `_retire_masters()` snapshots the registry under its lock and every
  candidate of an in-flight job is kept with the summary note
  `(prep in flight)` — policy limits (7 days / 2 GiB / 1 GiB free) unchanged.
- No silent fallback to `result_video` while a preparation is active: the
  master cannot be deleted after the claim; the ordinary missing-master
  fallback still applies only when the master was already gone before the
  preparation claimed it.

Regression tests: `test_montage_master_concurrent_prep.py` (see below).

## 7. Known issues (open observations)

- **F10 — autozoom timeline is not bit-deterministic between identical
  renders.** Two renders of the same source with the same options produced
  different zoom timelines (4 vs 6 anchor points, anchor times shifted by
  ~0.2 s); camera anchor parameters (`eye_screen`, mode) were identical.
  Cause not proven; working hypothesis: the timeline derives from whisper
  word-timing jitter across CPU runs. Zoom/autozoom algorithms untouched —
  observation only, no product changes; re-check if determinism becomes a
  product requirement.
