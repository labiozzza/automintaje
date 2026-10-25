<!-- /autoplan restore point: "/Users/labiozzza/Projects/automontage/.gstack/tmp/autoplan/quality-max-autoplan-restore-20261024-232457.md" -->
# Silence trimming and editable cut timeline

## Product goal

Let users automatically find quiet spans in the selected source video, adjust the silence threshold and required duration, keep configurable audio/video handles before and after each detected span, and edit every proposed cut on the video preview before rendering. A cut is reversible: dragging either edge narrows the excluded source interval and restores the corresponding picture and sound. Users can add manual cuts and remove proposed cuts.

## User requirements

- Show average audio level and maximum peak in dB for the current video.
- Configure a silence threshold in dBFS, minimum continuous silence length in integer milliseconds, and retained padding before/after the silence in integer milliseconds.
- Show every time value to hundredth-of-a-second precision (`SS.cc` / `MM:SS.cc`) while retaining integer millisecond values internally.
- Update the previewed silence regions as settings change.
- Display every cut on the existing project preview timeline. Dragging the left/right edge edits the source-time cut boundary. Add manual cuts, delete cuts, and restore cut material by shrinking either edge.
- Render audio and video cuts together and keep subtitles, censoring, zoom and animations synchronized to the original source timeline before the cuts are applied.
- Keep source video intact; cuts are an editable decision list until render.

## Implementation plan

### Accepted source contract

- User's stated goal: remove silence with adjustable dBFS threshold, minimum silent duration, and retained timing before/after each detected pause. Show whole-track average and maximum level for the selected video.
- Show millisecond inputs as integers and timeline times to centisecond precision. Let the user see every proposed cut on the current-video preview, drag either edge to restore or remove source time, add manual cuts and delete cuts.
- Audio and video must be cut together. Keep cuts editable in source-time coordinates until render; do not alter autozoom, subtitles, animation logic or content-factory behavior.
- Execute the requested plan → review → implementation workflow autonomously. Preserve this scope; do not publish to accounts or merge changes.

### Audio analysis and preview session

- Add a bounded silence-analysis API for either a direct video upload or an existing project. Decode once to floating-point PCM at 48 kHz without downmixing; reject unsupported channel counts above 8. For each 10 ms window, compute RMS over all channel samples; whole-track average is RMS over all decoded samples, and maximum is the largest absolute sample peak. Return dBFS values, source duration and envelope. Display the floor as -120 dBFS and label mean RMS separately from peak.
- Create an immutable server-owned source snapshot before analyzing either a direct upload or project video. Resolve project files by project id only; never accept client-supplied paths. Stream-copy in bounded chunks, hash the snapshot, and bind the analysis id to that snapshot; all later preview/estimate/render work uses it. Cap each source at 2 GiB and 2 hours, and cap analysis work in the single local app process at 2 concurrent sessions, 128 MiB reserved workspace headroom, 64 MiB worker memory, and a configurable 30-minute wall-clock timeout [estimated local-app limits]. Isolate snapshot directories by worker process so cleanup cannot delete another worker's live session. Enforce request-byte limits during upload receipt, including requests without `Content-Length`; terminate the registered decoder on cancel, disconnect, timeout or server shutdown and release slots/remove partial files in `finally`.
- The analysis session owns the immutable source until atomic render handoff. On accepted render submission, atomically move/copy the snapshot into the persistent job directory before marking the session consumed; the job then owns it across queued/running status and process restart so retry/preview uses the persisted EDL plus identical source. Expired unconsumed sessions require direct-file reupload/reanalysis or project reanalysis; cleanup never deletes a job-owned source. Verify hashes while copying and before handoff; project path/size/mtime are advisory identity only, not a substitute for snapshot content hash.
- Return a bounded compact envelope `envelope_dbfs_x100: int16[]` with one signed integer hundredth-dBFS value per 10 ms bin, floor -12000, and maximum 720,000 bins. Enforce an 8 MiB maximum JSON response body; fail with an actionable limit error rather than truncating. This lets client-side threshold edits avoid re-upload/re-decode. Use bounded streaming PCM decode and incremental RMS/peak/envelope accumulation; never buffer decoded PCM for the full track. Measure worst-case decode, serialization and browser memory at configured maxima.
- The browser derives silent spans from the envelope and chosen dBFS threshold/minimum duration; padding and handle edits operate locally without re-uploading/re-decoding the file. Use integer milliseconds and 10 ms precision. Persist the normalized-master time map with the session: reset the first presentation PTS to clip-relative zero, preserve clip-relative timing through effects (no speed changes in this pipeline), and record the actual master CFR and frame PTS. VFR sources use existing CFR normalization; map requested clip-relative source times through that recorded map to the first master PTS not earlier than each boundary. Use this exact map for preview, estimates, retries and render.
- Start analysis only when the user presses “Анализировать тишину”; leave silence removal off by default so existing renders never change without opt-in. Suggested editable starting values: -42 dBFS threshold, 500 ms minimum, and 120 ms retained before/after [estimated, conservative defaults].
- After analysis, show an explicit persistent “Удалять отмеченные паузы” switch, default off. When off, source and edited-playback modes both play the source, the duration comparison says “Без изменений”, and render submission sends no effective cuts; cuts stay editable. When on, preview, duration estimate and render use the same normalized effective list, including both auto and manual cuts. Switching it off never deletes cuts; manual cuts remain usable even if analysis is unavailable.
- Show loading/error/no-audio states, average RMS and maximum peak, threshold/minimum-duration/padding controls, and detected span count/output duration. Each proposed cut also shows why it qualifies (measured quiet level and duration); label global stats as signal-level aids, not perceived-loudness scores.
- While analysis is running, show a visible progress bar and current phase. The client sends `X-Analysis-Request` and polls `/api/montage/silence/progress/{request_id}`. Upload/copy percentages use byte counts when known; probe is indeterminate; decode percentage uses decoded audio duration over the source duration; never estimate percentage from elapsed time. No-audio completes at 100% after probe.
- On `analysis_capacity` (429), show the server's `Retry-After`/`context.wait_seconds` as an updating hours/minutes/seconds countdown to the oldest current session expiry, with a note that the slot may free earlier after a render handoff. Starting analysis on a new source or reanalyzing in the same editor sends `DELETE /api/montage/silence/analyze/{analysis_id}` for its prior ready session, freeing capacity without affecting job-owned source files.

### Code surfaces

- `montage/silence_trim.py`: PCM envelope/statistics, silence-span derivation, cut-list normalization/validation, effective frame snapping, and FFmpeg cut-graph construction as separately testable functions.
- `app.py`: `/api/montage/silence/analyze` accepts exactly one source selector (streamed video upload or project id), enforces byte limits during receipt, and writes the immutable snapshot before analysis. It reports upload/probe/decode phases through `/api/montage/silence/progress/{request_id}` and releases ready sessions on `DELETE /api/montage/silence/analyze/{analysis_id}`. `/api/montage/render` accepts `silence_analysis_id` only when trimming is enabled; that mode rejects simultaneous `video`/`project_id`, validates the session and EDL, atomically transfers snapshot ownership into the job directory, then creates the job. Trimming-disabled legacy video/project requests retain the existing path and unchanged render behavior. Map parser/upload errors before session creation to the route's structured error response.
- `montage/engine.py`: apply the explicit EDL after `audio_censor` and all visual effects are baked into the master, before master retention/final/preview encodes; map cut intervals into original and output timeline metadata.
- `templates/index.html`: controls and a pointer/touch/keyboard-editable cut track beside the current project preview; slider changes derive candidate cuts from the cached envelope without re-uploading. Reuse the inline style/script conventions; no new frontend dependency or static asset is planned.
- Tests: analyzer/interval/FFmpeg CFR/VFR math in new `test_silence_trim.py`; raw-upload/project analysis, capacity, RFC 9457 validation and atomic job handoff in new `test_silence_trim_api.py`. Browser coverage is local Chrome verification of project analysis, cached-setting updates, manual editing, keyboard handles, undo, preview mode and opt-in summary; headless Playwright is unavailable in this environment (Chromium exits with SIGTRAP).
- Add `docs/silence-trimming-api.md` as an internal maintenance contract with direct-upload/project request examples, successful responses, stable error codes, retry behavior, and representative validation/expiry examples. This is not a public API or SDK.

### Internal API error contract

- The new analysis route and silence-related montage validation return HTTP status plus `application/problem+json` (RFC 9457): standard `type`, `title`, `status`, `detail`, and `instance`, plus stable extension fields `code`, `operation`, `context`, `recovery`, `side_effects`, and `request_id`. `context` includes safe actual values and relevant configured bounds; never include source paths, tokens, secrets, raw media, or PCM. Error details and codes are documented in `docs/silence-trimming-api.md`.
- Use problem details only for synchronous upload/analysis/render-request parsing and validation. Once a render job exists, render failures are job outcomes: job-status/result exposes the same stable code, safe cause, recovery and side-effect fields in its existing operation/error payload; do not misrepresent an asynchronous failure as the original request's HTTP response. `render_failed` is a job error code, not an HTTP status returned after job creation. Document both channels in the API contract.
- Define stable codes and channels: `invalid_request` (400), `source_too_large`/`analysis_resource_limit` (413), `unsupported_media`/`invalid_cut_list` (422), `source_changed` (409), `analysis_expired` (410), `analysis_capacity` (429 with `Retry-After`), `workspace_unavailable` (507), and `analysis_failed` (500). `render_failed` is a job-status error code, not an HTTP status. A video with no audio is a successful analysis response (`audio_available: false`) with no automatic candidates; it is not an exception.
- Each error states the exact operation and rejected value/limit, a safe next action, and whether side effects were `none`, `partial`, `complete`, or `unknown`. Invalid requests create no session/job; incomplete uploads and failed analyses remove partial files and create no usable session; expired/source-changed sessions create no render job; invalid EDLs fail before job creation. Capacity/disk errors are safe to retry after the stated condition changes. Unknown outcomes are never automatically retried; reconcile the session/job first. Permit at most one user-initiated retry after correcting the reported cause.
- Unexpected local failures include a request/job correlation id and a sanitized trace path in logs; return a generic top-level detail plus caller-safe `context.stage` and allowlisted `context.cause` codes that identify a recoverable cause without raw exception text: `unsupported_codec` (reselect/convert source), `media_read_failed` (reselect source), `snapshot_missing`/`snapshot_hash_mismatch` (reanalyze current source; source and cut list remain visible), `workspace_capacity` (free disk space), `probe_timeout` or `ffmpeg_timeout` (one retry after checking source/workspace), `ffmpeg_exit` (retry once after checking the source), and `render_stage_failed` (check the named stage/settings and sanitized local log). Keep trace paths, raw exception strings and sensitive details in logs only. The UI maps each code to its supported recovery action and preserves the structured code/request id for support/debugging. Tests assert status, media type, code, side-effect declaration, safe failure stage/cause, non-leaking context, and retry instructions for every introduced error path.

### UI hierarchy and states

- Desktop hierarchy in the existing preview card: source video first, a compact analysis/status row immediately below, timeline track next, selected-cut inspector and original/removed/remaining duration summary below the track. Keep threshold/minimum/padding under a clearly labeled “Настройки тишины” disclosure after analysis; the existing montage settings and render button remain below the preview/editor section. On mobile, use the same order, keep “Анализировать тишину” and current status directly after the video, and retain visible timeline zoom/scroll controls.
- In dense timelines, the track is an overview/seek surface until zoomed enough to target a boundary. The ordered cut list is chronological and labels each row with origin and `start–end`; selecting a row highlights the cut, opens its inspector, and brings its interval into view without changing source-time coordinates. Only the selected cut exposes draggable handles; selected identity survives zoom/pan and is cleared only when the cut is deleted or replaced. On mobile, show the selected inspector and cut list in a compact inline panel below the track, and give pan/zoom separate controls so they cannot accidentally edit cuts.
- Before analysis show “Анализировать тишину” and no fabricated zero metrics. Loading copy is “Анализируем аудио…” with a progress percentage when available and an indeterminate indicator otherwise. No audio: “В видео нет аудиодорожки. Автоматические паузы недоступны; можно добавить разрез вручную.” No candidates: “По заданным условиям пауз не найдено. Измените порог или добавьте разрез вручную.” Success shows cut count, removed duration and estimated remaining duration. Limit/error states explain whether reanalysis or source reselection is required.
- Each unselected cut block stays concise: origin (`авто`, `изменён`, `вручную`) and time span. Only the selected cut exposes handles and a detail panel. When cuts are too close for individual handles, a compact selectable cut list in the inspector disambiguates them; handle hit areas never overlap. The detail order is requested/effective interval, current kept-duration impact, detection rationale, then edit/delete actions.
- Provide two explicit playback modes named “Исходник” and “С вырезанием”. Edited playback skips each excluded interval at its effective end and resumes in sync. The “Проверить стык” action (not “listen”) starts up to 1 second before the selected cut, previews video+audio across the skip, then pauses up to 1 second after; the source/effective playhead times stay visible.
- Use one mutually exclusive playback-mode control with a visible selected mode. In “С вырезанием”, starting inside a cut seeks to its effective end; reaching a cut during playback skips it. Editing a cut while playback is active pauses playback, applies the edit, and keeps the playhead at the nearest retained source time; if both sides are equally near, choose the later retained source time. “Проверить стык” temporarily uses edited playback, then pauses and restores the previously selected mode; source and output/effective time labels remain visible throughout.
- A handle has a 44 px touch target, visible focus, `aria-valuenow`/`aria-valuetext`, 10 ms arrow increments, 100 ms Shift+arrow increments and Home/End clamping. Dragging empty track space creates a manual range only after a 100 ms / one-frame minimum gesture; otherwise it seeks. Non-drag creation uses integer-ms start/end fields. Handle movement clamps at adjacent cuts rather than silently merging them.
- Auto candidates refresh when detection settings change; adjusted/manual cuts remain. Use one reset label, “Пересчитать авторазрезы”; before applying it, confirm that adjusted/dismissed auto cuts reset while manual cuts remain. Expired analysis retains manual cuts and the original browser File can be re-uploaded for rendering; a changed source clears all old-timeline cuts with an explanation. Manual cuts remain available when analysis is unavailable.
- Threshold/minimum/padding changes update unedited auto candidates locally from the cached envelope and apply immediately; no server analysis restart occurs. Adjusted/dismissed auto cuts and manual cuts remain fixed in source time. Keep the selected cut if it survives; otherwise select the nearest remaining cut, or clear selection if none remain. Preserve the current source-time viewport. Provide one-step Undo for the most recent candidate refresh/reset or cut edit, restoring the previous candidates, selection, and viewport; the reset confirmation states exactly which auto edits/dismissals reset and that manual cuts remain.
- A persistent status model distinguishes `analyzing`, `ready`, `no-audio`, `no-candidates`, `expired`, `source-changed`, `limit`, `error`, `rendering` and `done`. Progress is indeterminate before duration is known and percentage-based when available. Duration summary updates after every effective-cut edit and is labeled estimated until render.
- Every error/expiry state presents its recovery action beside the message. `expired` retains manual cuts for preview but marks analysis-derived cuts unavailable and offers re-upload of the original browser File; `source-changed` names the newly selected source, explains that old-timeline cuts were cleared, and requires a fresh analysis before automatic cuts can render. Limit and analysis errors preserve the source and manual cuts, state whether retry/settings/source reselection is needed, and keep rendering disabled only when the required source/session is invalid. Show decode progress as indeterminate until a reliable byte or duration denominator exists; thereafter percentage means decoded source duration. Analysis can be canceled, returning to the pre-analysis state while preserving manual cuts.
- Duration summary is explicitly tied to “Удалять отмеченные паузы”: when enabled it reports source duration, normalized effective removed duration, and estimated output duration; when disabled or the effective list is empty it says “Без изменений”. Overlapping/adjacent cuts are merged before totals; invalid lists show the validation reason rather than stale output estimates. Selected-cut impact uses those same effective intervals.
- Timeline legend and encoding: retained source is neutral; auto cuts use the existing red token with a dotted pattern and “авто”; adjusted auto cuts use red diagonal hatching and “изменён”; manual cuts use a distinct outlined pattern and “вручную”; dismissed candidates appear only in a muted candidate overlay and are not counted as cuts. Selection adds a strong outline/focus ring. Requested draggable boundaries use solid handles; frame-effective boundaries use separate thin ticks and explicit effective-time labels.
- Cut colors use the current red token plus pattern/icon/text; do not rely on color alone. Timeline tick spacing follows zoom; requested boundaries stay draggable and effective frame boundaries use separate ticks/labels. Offer fit-to-view and zoom from full duration to a one-frame window; only selected handles receive the 44 px targets. Maintain 4.5:1 body contrast and existing visual tokens.

### Non-destructive timeline editor

- Render the silence controls and editable timeline in `templates/index.html`, alongside the existing project-preview video. Add an accessible silence/cut track in that visual editor, using source-video time as the stable coordinate system. Distinguish retained material, auto-detected cuts, and manual cuts.
- Each excluded interval stores `{cut_id, start_ms, end_ms, origin}` and is half-open `[start_ms, end_ms)`. Pointer/touch/keyboard boundary edits clamp to the media range and neighboring intervals, snap to centiseconds, update labels and estimated output duration, and never mutate subtitle/zoom/animation source timestamps.
- The left handle is the first source time removed; moving it right restores the earlier part. The right handle is the first source time retained; moving it left restores the later part. Manual cuts are source-time ranges; deleting one restores that entire range.
- Detection settings regenerate unedited auto cuts. Manual and boundary-adjusted cuts persist. A visible “Пересчитать авторазрезы” reset clears auto boundary edits/dismissals only, preserves manual cuts, and states what will reset.
- Preview-play seeks over excluded spans while the original source remains loaded, allowing the user to audition the proposed result around each seam. Selected cuts show requested times and the exact effective frame-aligned interval; the displayed duration estimate uses the effective frame count and explains sub-frame end behavior.
- Show a compact original/edited duration comparison and the candidate's measured level/duration so users can inspect the change before rendering.
- Pointer priority is handle, cut-block selection, then empty-track creation; crossing an adjacent cut clamps at its boundary. A click seeks; only a drag of at least 100 ms or one frame creates a range. The “Добавить разрез” action also opens integer-ms start/end fields for keyboard/non-drag creation. Deleting a selected auto span dismisses it for the current detection revision; “Сбросить правки авторазрезов” clears those dismissals/adjustments, not manual cuts. One canonical normalization contract applies in browser and server: reject malformed/non-finite/zero-length/out-of-range records and lists over 10,000 cuts; accept valid overlaps/adjacency as interval union for effective render intervals, while preserving original `cut_id`/origin membership for the inspector, counts and metadata. Drop intervals that snap to zero effective frames from the render EDL but retain them as visibly ineffective edits; an all-ineffective list yields “Без изменений” and the legacy no-cut render path.
- Send the final explicit cut list plus settings and the analysis id with the normal montage request. Persist validated source-time cuts in job state and output timeline metadata.

### Rendering and media semantics

- Apply the cut decision list once, after the existing fully effected/censored master is ready and before final/preview delivery encodes. Normalize the master to clip-relative PTS zero while preserving source presentation order/timing and record actual master frame PTS/FPS. Map each clip-relative requested boundary through the persisted source-to-master time map, then choose the first master frame PTS not earlier than each boundary; this is equivalent to `ceil(t * fps)` only for a CFR zero-based master. Treat cuts as half-open `[start,end)`, and remove every mapped frame whose PTS falls inside the request. A frame cannot be partially retained. Convert the same effective master frame boundaries to audio sample positions; pad/trim each segment by at most one sample to match video duration. Normalize/union valid intervals once, preserve member cut IDs, reset PTS and concatenate in order. The source→master map and effective intervals are shared by preview, estimate, retries and render. Do not change autozoom, subtitle generation, animation timing, or content-factory behavior.
- Apply short (5 ms) audio fades only inside retained segment edges that meet at a cut, avoiding hard-edge clicks without moving video seams or changing cut coordinates [estimated; verify on seam fixtures].
- When the effective cut list is empty, bypass all new filters/encodes and retain the existing render command path. If there is no audio, report it and do not infer silence cuts, but still allow explicit manual video-only cuts. If every frame/audio sample would be removed, reject the cut list with an actionable validation error.
- Use the existing render progress/cancel/job-process registration patterns. Persist a validated cut-list snapshot, source-to-master map, original source identity and effective interval/member-ID map in the job; previews/retries use that same snapshot. Report source duration, removed duration, final duration, requested boundaries and effective frame-snapped boundaries. Output timeline metadata maps each retained source span to its output start/end; cut/union provenance remains recoverable. Estimates and rendered output use the same snapped interval list.
- The direct-upload/project immutable snapshot limits are 2 GiB/2 hours per source, two unconsumed analysis sessions in the single local app process, 64 MiB worker memory, 128 MiB free-workspace headroom and a configurable 30-minute analysis timeout [estimated local-app safety limits]. The render route's trimmed mode requires exactly `silence_analysis_id` as the source selector; it must not accept a simultaneous file/project source or reread video bytes into memory. On handoff, job-owned media persists for queued/running work and retry/restart recovery; session TTL cleanup removes only unconsumed analysis snapshots. Fail visibly on a limit; never silently truncate uploads or cut lists.

## Acceptance criteria

1. On a fixture with known tones and silence, the analyzer reports mean/peak dBFS and finds spans at the chosen threshold/minimum duration within one 10 ms bin. Streamed decode keeps worker RSS under 64 MiB; maximum-source envelope stays at or below 720,000 signed hundredth-dBFS bins and its documented response limit.
2. Changing threshold, minimum length or padding recomputes regions/output duration without another file upload.
3. Dragging the left boundary right restores its preceding source time; dragging the right boundary left restores its following source time. Manual cut creation and deletion yield the expected retained timeline without silently losing manual edits.
4. Applying a known cut set yields the expected output duration and keeps audio/video synchronized at both cut seams; subtitles/overlays already baked into the master move with their frames. Tests cover fractional-frame boundaries, non-zero source start PTS, time-base offsets, VFR normalization and variable frame durations, checking effective frame count and corresponding audio sample indices. Preview, estimate, retry and final render use the same source-to-master map and cut-list snapshot and expose requested/effective boundaries.
5. No-cut render, no-audio input, silence at start/end, adjacent/overlapping cuts, duplicate cut provenance, cuts snapping to zero frames, short clips, full-duration silence, cancellation/timeout/disconnect, concurrent session capacity, atomic session consumption, restart recovery, expiry cleanup, same-size source replacement, request parsing/upload overflow and decoder resource exhaustion have explicit behavior and tests.
6. API tests cover direct upload and saved-project analysis, session/error contracts and trimmed job handoff. Local Chrome verification covers project analysis, cached-setting updates without another request, manual range fields, keyboard boundary editing/undo, preview mode and opt-in summary. Pointer/touch gestures and mobile layout are specified but not automated in this environment because the headless Chromium runner exits with SIGTRAP. Rendering tests mock publication; no real account uploads.
7. Seam tests use quiet speech-like audio, room tone, music and a high-level transient. Verify speech above threshold is not cut, retained-side fades introduce no click-sized peak, and output audio/video stay aligned; preview-play skips cut spans and exposes requested/effective in/out times.
8. Perform one manual local check against the selected real-world project video, recording false-cut/missed-pause observations without storing private screenshots; it is not a repeatable automated test. Do not infer that synthetic defaults are universal. Automated CFR/VFR/timebase fixtures remain deterministic and report effective master FPS.
9. During upload/copy/decode, expose a pollable phase and progress value; percentages track bytes or decoded source duration, and phases without a denominator stay indeterminate. Under two-session capacity, return `Retry-After` plus the oldest-session wait estimate and show a countdown. Resetting the selected source releases its unconsumed ready session. Verify progress polling, capacity wait, release, cancel, and session expiry without a full render.

## Constraints

- No changes to autozoom algorithms/settings, subtitle text/layout logic, animation behavior or content-factory.
- No real publication or merge.
- Do not raise bitrate to conceal seams; choose a single frame-accurate render from the existing master and measure quality/synchronization.
- A video frame is discrete: UI boundaries are shown to hundredths. For half-open cuts, both start and exclusive end map to the first master frame whose PTS is not earlier than that boundary; the entire selected end frame is removed when its PTS lies in the interval. Show the effective interval, including the fact that a frame cannot be partially retained.


<!-- autoplan-accepted:ceo -->
- Preserve every behavior and non-goal in U-01 and U-02, including manual cuts and restoring removed spans by dragging either boundary.
- Keep analysis/display settings in integer milliseconds and present timecodes at hundredth-second precision.
- Do not change autozoom, subtitle logic, animation behavior, or content-factory.
- Implement details D-A through D-L as recorded above, preserving the user's outcomes and no-cut compatibility.
<!-- /autoplan-accepted:ceo -->

<!-- autoplan-accepted:design -->
- Preserve the stated desktop/mobile hierarchy and explicit analysis, no-audio, no-candidates, expiry, source-change, limit, error, rendering and completion states; controls remain attached to the current source preview.
- Trimming is an explicit persistent opt-in, default off; when off render and duration summary remain unchanged, while all cuts remain editable. When on, preview, estimate and render share the same normalized effective cut list.
- Provide source and edited playback with defined cut-boundary, in-playback edit, seam-check and playhead behavior; display source/effective times consistently.
- Dense timelines use overview/seek plus chronological selectable cut list; only the selected cut has handles, selection/viewport are stable, and mobile inspector/pan/zoom affordances remain usable.
- Visually distinguish retained, auto, adjusted, manual, dismissed, selected and frame-effective boundaries without relying on color alone; retain requested/effective time labels.
- Settings recalculate unedited auto cuts locally and immediately; adjusted/dismissed/manual cuts persist. Preserve selection and source-time viewport deterministically and provide one-step undo for cut edits and recalculation/reset.
- State-specific recovery actions explain what remains editable/renderable; duration summaries use normalized effective cuts and show “Без изменений” when trimming is off or empty. Analysis progress is indeterminate until decoded-duration progress is measurable, then percentage-based; cancellation preserves manual cuts.
- Keep accessibility requirements: keyboard interaction, ARIA values, 44 px targets, contrast, patterns/text labels, and narrow-screen behavior. Verify these in browser tests.
<!-- /autoplan-accepted:design -->

<!-- autoplan-accepted:dx -->
- Keep this as an end-user app feature with a private frontend/server API; do not add a public API, SDK, CLI, hosted service, or developer onboarding surface.
- Implement the RFC 9457 analysis/render validation error contract with stable codes, safe actionable context, recovery guidance, side-effect states, sanitized correlation/trace handling, and bounded retry semantics as specified above.
- Add internal API request/success/error examples at `docs/silence-trimming-api.md`; keep examples aligned with the actual route and response models.
- Verify error contracts, session retry/reanalysis behavior, non-leaking diagnostics, API examples, analyzer tests, browser flow and local media verification. Do not claim TTHW, competitive rank or adoption without relevant evidence.
<!-- /autoplan-accepted:dx -->

<!-- autoplan-accepted:eng -->
- Analyze a hashed immutable server-owned snapshot for both direct and project sources. Enforce 2 GiB/2 h, 2 global sessions, 64 MiB worker RSS, 128 MiB workspace headroom, 30-minute analysis timeout, 720,000 envelope bins, 8 MiB response ceiling, and cleanup/termination on every exit.
- Render trim-enabled jobs from `silence_analysis_id` only; atomically transfer snapshot ownership to the durable job before consuming the session. Preserve source and EDL for queued/running/retry/restart; TTL cleanup cannot remove job-owned media. Keep legacy request/render behavior unchanged when trim is off.
- Persist and reuse the clip-relative source-to-normalized-master actual-PTS map across preview, estimate, retry and render. Normalize cut ranges consistently in browser and Python as interval union while preserving cut IDs/origins; reject malformed/out-of-bounds/oversized records and mark zero-effective-frame cuts visibly ineffective.
- Keep RFC 9457 for synchronous errors only; expose asynchronous render errors in job state with stable code, safe cause, recovery, side effects and correlation. Use only allowlisted caller-safe cause codes and do not expose paths, secrets, raw exceptions, media or PCM.
- Test known-signal analysis, maximum envelope/response and worker memory, upload/parser bounds, immutable source hash/handoff/races, cancellation/timeout/cleanup/restart/expiry, overlap provenance, source/master PTS mapping, fractional frame/audio-sample alignment, VFR/timebase/non-zero start PTS, legacy no-cut compatibility, API error fields, browser gestures/accessibility and synchronized FFmpeg fixtures. Real project-video verification remains a separate local manual check.
- Implement sequentially across `montage/silence_trim.py`, `app.py`, `montage/engine.py`, `templates/index.html`, `docs/silence-trimming-api.md` and tests; no classes/services or parallel lanes. Keep all stated user behaviors and non-goals.
<!-- /autoplan-accepted:eng -->
## Review record

Autoplan plan/review sequence requested explicitly by the user. User also requested no intermediate participation; use autonomous decisions for implementation details, preserve all stated requirements, and do not change scope in response to reviewer proposals without prior authorization.

### Initial decision ledger

| ID / owner | Contract and evidence | Current | Proposed | Status | Exact authority / scope |
|---|---|---|---|---|---|
| U-01 / user | Silence threshold in dB; minimum silence and pre/post retained margins in integer ms; centisecond timeline display; average/max dB; drag either cut edge to restore media; create/delete cuts | Existing montage does not trim silence | Add silence analysis and editable source-time EDL as one feature | Approved | User's current request; these behaviors and no changes to zoom/subtitles/animations/content factory are binding |
| U-02 / user | All video/audio edits must stay synchronized; original media must remain recoverable until render | No cut EDL | Keep source-time cuts editable/non-destructive; apply identical intervals to audio and video after baked effects | Approved | User requested restoring silence by dragging cut handles |
| A-01 / assistant | Do not add adjacent features or change existing creative algorithms | None | HOLD SCOPE review; no additional product scope | Auto-selected from user's hands-off and explicit-scope request | Review posture only; does not narrow U-01 |
| D-A / assistant | Responsive silence preview while a threshold changes | Re-decode/re-upload vs cached envelope vs browser decode | One 10 ms PCM envelope and client-side candidate derivation | Auto-decided | User delegated implementation details; avoids repeated processing and large browser decode |
| D-B / assistant | Restore media by moving either edge | Destructive source trim vs editable excluded intervals | Source-time EDL applied after effected master | Auto-decided | Required to restore cut media without shifting existing subtitle/zoom/animation times |
| D-C / assistant | Avoid repeated direct-file transfer | Upload twice vs cached analysis source | Random analysis id, source identity binding and bounded TTL | Auto-decided | User delegated implementation; prevents re-upload while keeping path ownership server-side |
| D-D / assistant | Interpret silence level | Peak-only vs RMS-only vs show both | Average RMS + sample peak; RMS drives threshold | Auto-decided | Gives requested average/max readings and makes threshold behavior visible |
| D-E / assistant | Existing montage behavior when feature is unused | Trim by default vs opt-in | Analysis available; silence removal defaults off | Auto-decided | No-cut path preserves previous behavior unless user enables the feature |
| D-F / assistant | Convert editable centisecond times to discrete video edits | Arbitrary PTS vs frame-snapped seams | Keep integer ms, display hundredths, choose frames with PTS in `[start,end)` using `ceil(start*fps)` and exclusive `ceil(end*fps)`, align audio to those effective frame times | Auto-decided | Preserves half-open cut semantics and estimate/render parity; disclose that a frame is indivisible |
| D-G / assistant | Audible transients at user-selected cut seams | Hard edges vs a minimal fade within retained content | 5 ms edge fades at the two audio sides of each cut | Auto-decided | Does not move the visual seam or remove additional timeline; guard against clicks and verify against synthetic transients |
| D-H / assistant | Estimate/result consistency | Estimate requested ms while render snaps independently; persist effective cut map | Use one validated cut snapshot and frame-snap map for estimate, preview metadata, retry and render | Auto-decided | Avoids a preview that promises different boundaries from the output |
| D-I / assistant | Reuse analysis for a direct upload | Upload source again at render; keep random-ID analysis session | Keep a bounded temporary source session; verify source identity/expiry; reuse it for render | Auto-decided | Avoids a second full upload while keeping file ownership server-side |
| D-J / assistant | Detection metric meaning | Peak-only threshold; downmixed RMS; channel-preserving RMS envelope | RMS of all channel samples per window; sample peak separately; label both dBFS metrics | Auto-decided | Prevents phase cancellation from downmixing and makes average versus maximum clear |
| D-K / assistant | Preview the edited result | Show shaded ranges only; render a separate preview file | Preview-play seeks over excluded ranges in the loaded source video | Auto-decided | Lets the user audition cut order without another render or media copy |
| D-L / assistant | Recalculate after manual edits | Replace all cuts on every threshold change; keep stale proposals | Regenerate unedited auto cuts, preserve manual/adjusted cuts, and provide an explicit auto-cut reset | Auto-decided | Keeps settings responsive without silently discarding manual work |

<!-- autoplan-accepted:ceo -->
- Preserve every behavior and non-goal in U-01 and U-02, including manual cuts and restoring removed spans by dragging either boundary.
- Keep analysis/display settings in integer milliseconds and present timecodes at hundredth-second precision.
- Do not change autozoom, subtitle logic, animation behavior, or content-factory.
- Implement details D-A through D-L as recorded above, preserving the user's outcomes and no-cut compatibility.
<!-- /autoplan-accepted:ceo -->

### Initial system audit receipts

- Frontend preview already exists in `templates/index.html:1134-1177`; direct-file preview is created in `templates/index.html:1962-1977`; project preview and seek keybar are in `templates/index.html:2329-2365`.
- Montage upload currently accepts a direct video/project in `app.py:3402-3473`; montage fields are assembled in `templates/index.html:3459-3558`.
- Current engine applies SRT-based censor only in `montage/engine.py:1266-1310`, then records the fully effected master and encodes delivery/preview in `montage/engine.py:1312-1361`.
- No existing silence-trim implementation was found in tracked Python code; existing `montage/audio_censor.py:70-118` finds banned-word intervals and builds a volume mute filter, not silence cuts.
- `TODOS.md` and a feature-specific silence design doc were not found. No branch decision records were found by `gstack-decision-search --scope branch --recent 50`.
- Outside Codex review is unavailable because the installed `codex` executable is missing (`gstack-codex-probe`: `MODEL_UNUSABLE_INSTALL`); Aside search is unavailable on this host. No external coverage will be claimed.

### Reality rows

REALITY: premise-table pass the branch already has audio mute intervals, not silence removal; traced `app.py:2806-2843` and `montage/engine.py:1266-1310`
REALITY: already-done pass searched tracked code and `origin/main` history for silence cut/render flow; no implementation found; current preview/render receipts `templates/index.html:1134-1177`, `app.py:3402-3707`
REALITY: surface-check pass existing render API is `POST /api/montage/render` at `app.py:3402`; proposed analyze route is new, not represented as existing
REALITY: binding-decisions pass `gstack-decision-search --scope branch --recent 50` returned no records; U-01/U-02 remain binding; `docs/video-silence-trimming-plan.md:73-89`
REALITY: numbers pass user requires integer milliseconds and hundredth-second display; 10 ms envelope is an implementation estimate selected to match 0.01 s display precision [estimated]; `docs/video-silence-trimming-plan.md:11-12,22,24`
REALITY: deferred-asks pass no linked issue/TODOS.md or prior silence request was found; no requested behavior is deferred; `docs/video-silence-trimming-plan.md:49-63,89`
REALITY: pr-count pass no PR is requested for this task; no PR split is planned; `docs/video-silence-trimming-plan.md:61`

Search note: Aside and WebSearch are unavailable in this session; proceed using repository evidence, installed FFmpeg behavior and first-principles reasoning.

### CEO scope challenge and mode

**0A. Problem:** Long quiet pauses make users manually inspect the source, choose cut points and repeat preview/render cycles. The requested feature solves that work directly; the do-nothing cost is repeated editing in another app or accepting dead air. Silence trimming is separate from `audio_censor`, which mutes selected SRT word intervals but does not remove picture/audio time.

**0B. Existing leverage:** Reuse the upload/project model (`app.py`), existing preview video/keybar and pointer-edit patterns (`templates/index.html`), progress/cancel/process registration (`montage/engine.py`), and unittest/Playwright conventions. Put signal math and EDL normalization in one testable `montage/silence_trim.py`; do not create another render pipeline.

**0C. Dream state:** A user sees the source video, its quiet regions, overall average/peak level, and the expected kept duration before rendering. They adjust threshold and margins, drag either edge to restore or remove more content, add/delete a cut, preview the source time, then render one synchronized output whose cuts are traceable to original timestamps.

**0D. Architecture alternatives, auto-decided under the user's explicit no-participation delegation:**

| Choice | Alternatives | Decision | Reason |
|---|---|---|---|
| D-A analysis feedback | Re-run `silencedetect` and re-upload on each slider change; browser-decode the full file; decode once to a compact PCM envelope | Decode once and derive candidates from the envelope | Complete real-time settings preview without repeated media transfer/decode; avoids browser full-file memory pressure |
| D-B edit representation | Destructively trim the source early; store excluded source-time intervals as an EDL | Store explicit source-time cuts, render them after the effected master | Edge dragging can restore source material; source SRT/zoom/animation times remain unchanged |
| D-C analysis media lifetime | Re-upload the file when montage starts; keep a random-ID temporary analysis session | Keep a bounded TTL session and reuse its source for render | Avoids uploading large direct-file videos twice; source identity and TTL are validated |
| D-D audio level | Peak only; RMS only; show mean RMS and sample peak separately, threshold against per-window RMS | Mean RMS + sample peak, RMS threshold on 10 ms bins | Users get average and maximum dB readings while short clicks do not suppress an otherwise quiet span |
| D-E unchanged behavior | Trim by default; opt in per montage | Show analysis, but default trimming off | Existing jobs and saved projects retain current output unless the user enables trimming |
| D-F cut seams | Arbitrary output timestamps; snap render seams to nearest source/output frame while retaining centisecond display | Store ms integers, display centiseconds, snap actual video seams to frames and expose effective time | Video frames are discrete; audio/video remain aligned and UI labels stay precise |

**Review mode:** HOLD SCOPE. The user's feature list is the ceiling; no unrelated cleanup, audio normalization, waveform editing or platform changes are added. Office Hours was skipped because the user supplied detailed product requirements and explicitly delegated intermediate decisions.

### CEO temporal interrogation

- **First hour:** Probe exact source profile/duration/audio stream; define the dBFS envelope and a known tone/silence fixture; verify the frame and PTS behavior at cut points.
- **Core logic:** Implement analysis session lifecycle and EDL validation; exercise quiet spans at start/end, no audio, overlapping intervals, padding larger than a span, full-duration silence and cancellation.
- **Integration:** Preserve source timeline for subtitles/zoom/animations/censoring; apply one synchronized trim/concat after all effects; verify output/master duration and track alignment.
- **Polish/tests:** Complete direct-upload and saved-project UI flows, pointer/touch/keyboard boundary edits, manual range creation/removal, stale analysis-session feedback, centisecond labels and mobile layout.
- **HOLD SCOPE complexity check:** Planned touched paths are `app.py`, `montage/engine.py`, `montage/progress_tracker.py`, new `montage/silence_trim.py`, `templates/index.html`, `docs/silence-trimming-api.md`, new `test_silence_trim.py`, new `test_silence_trim_api.py`, and this plan/report (9 files total including the plan); no new service or class hierarchy. Keep signal/EDL logic in one module and session state in existing app/job patterns.

### Plan review scope decisions

Every question covered by the user's request is already approved. Reviewer recommendations that alter the explicit cut behavior or the stated non-goals remain rejected unless specifically accepted by the user. Implementation details above are delegated, reversible choices; keep a decision trail and surface unresolved external/data-loss risks rather than inventing approval.

### CEO review findings and dispositions

| Finding | Severity | Disposition | Evidence / rationale |
|---|---|---|---|
| Target user/outcome lacks independent product research | High | Preserve the direct request; define functional acceptance evidence and make no adoption/time-saved claim. | User directly identified the problem; no user study was requested and no external research provider is available. |
| Detection settings and manual editor scope are broad | High | Keep threshold, minimum duration, margins, manual cuts and boundary restoration. All are explicit requirements U-01/U-02. | No scope reduction without user authorization. |
| Average/maximum dB measurements were ambiguous | High | Define mean as RMS over all channel samples, peak as max absolute sample, both in dBFS; threshold uses per-window RMS and UI labels distinguish them. | Prevents phase-cancellation from downmixing and avoids implying peak equals perceived loudness. |
| Handle/recalculation/delete semantics were ambiguous | Medium | Define left/right edge directions, retain adjusted/manual cuts when settings change, and provide an explicit auto-cut reset. Delete removes a cut; no independent undo stack is promised. | Makes restoration behavior concrete without silently losing manual work. |
| Frame snapping and output estimation could diverge | Medium | Snap cut starts inward with ceil and ends inward with floor; normalize once and share effective intervals for estimate, preview, retries and render. Show snapped times. | Prevents removal of material outside the selected interval and keeps duration estimates consistent. |
| Simpler detection-only alternatives were not compared | Medium | Keep decision D-A: compare repeated `silencedetect`/re-upload, browser full-file decode and cached PCM envelope. Retain the envelope plus full editor required by the user. | A reduced detector-only flow would omit explicit manual-edit requirements. |
| Competitive differentiation is unverified | Medium | Make no market claim; test the product-specific promise that cuts stay integrated with existing subtitles, animation and montage output. | CapCut-style editing was requested as an interaction reference; external research is unavailable. |
| Session identity and retry consistency were underspecified | Medium | D-H/D-I bind the analysis session to the selected source, persist the final EDL and effective frame snaps, and reuse that snapshot for preview, estimate, retries and rendering. | Prevents stale analysis and mismatched estimates/outputs. |

### Design review findings and dispositions

Native reviewer input `36cb3aa71a85e7b10dc2bf0aec7627b8d8bb8852b26bd8edf9c91f23ff251de0` was completed in-host. Codex outside review is unavailable (`MODEL_UNUSABLE_INSTALL`); no outside coverage is claimed. Structural findings were auto-resolved while preserving U-01/U-02:

| Finding | Severity | Disposition | Plan location |
|---|---|---|---|
| Trimming default-off state and effect on render were ambiguous | High | Added persistent “Удалять отмеченные паузы”, default off; specified both playback modes, estimates, render submission and retention of editable cuts. | UI hierarchy and states; analysis/preview session |
| Playback modes and seam-check interaction were ambiguous | High | Defined one selected mode control, behavior when playhead enters a cut, pause/update on edit, and return to prior mode after seam check. | UI hierarchy and states |
| Dense cut navigation could make handles unusable | High | Track becomes overview/seek until zoomed; chronological list selects/highlights and brings cut into view; only selected cut has handles; mobile inspector is compact and pan/zoom controls are separate. | UI hierarchy and states |
| Visual states for retained/auto/adjusted/manual/dismissed/effective cuts were underspecified | Medium | Added explicit patterns, labels, selection ring, and separate requested/effective boundary marks. | UI hierarchy and states |
| Detection refresh could move context or replace edits without recovery | Medium | Defined local immediate recalculation, persistence of edited/manual cuts, deterministic selection/viewport behavior, and one-step undo for refresh/reset/edit. | UI hierarchy and states |
| Expiry/source-change/error recovery was vague | Medium | Defined visible recovery actions and exact preservation/clearing behavior for manual and analysis-derived cuts. | UI hierarchy and states |
| Duration comparison might imply disabled cuts affect output | Medium | Bound totals to opt-in, normalized effective cuts, and specified “Без изменений” for disabled/empty lists. | UI hierarchy and states |
| Narrow-screen inspector/list behavior was underspecified | Medium | Defined inline compact inspector/list and separate pan/zoom controls while preserving editor order. | UI hierarchy and states |
| Progress percentage semantics/cancel were unclear | Low | Percentage now means decoded source duration after a reliable denominator; before then indeterminate; cancellation returns to pre-analysis while retaining manual cuts. | UI hierarchy and states |

The design fixes add interaction specificity but do not change the user's requested behaviors or exclusions. No further design review round is planned; continue with DX and Engineering review.

<!-- autoplan-accepted:design -->
- Preserve the stated desktop/mobile hierarchy and explicit analysis, no-audio, no-candidates, expiry, source-change, limit, error, rendering and completion states; controls remain attached to the current source preview.
- Trimming is an explicit persistent opt-in, default off; when off render and duration summary remain unchanged, while all cuts remain editable. When on, preview, estimate and render share the same normalized effective cut list.
- Provide source and edited playback with defined cut-boundary, in-playback edit, seam-check and playhead behavior; display source/effective times consistently.
- Dense timelines use overview/seek plus chronological selectable cut list; only the selected cut has handles, selection/viewport are stable, and mobile inspector/pan/zoom affordances remain usable.
- Visually distinguish retained, auto, adjusted, manual, dismissed, selected and frame-effective boundaries without relying on color alone; retain requested/effective time labels.
- Settings recalculate unedited auto cuts locally and immediately; adjusted/dismissed/manual cuts persist. Preserve selection and source-time viewport deterministically and provide one-step undo for cut edits and recalculation/reset.
- State-specific recovery actions explain what remains editable/renderable; duration summaries use normalized effective cuts and show “Без изменений” when trimming is off or empty. Analysis progress is indeterminate until decoded-duration progress is measurable, then percentage-based; cancellation preserves manual cuts.
- Keep accessibility requirements: keyboard interaction, ARIA values, 44 px targets, contrast, patterns/text labels, and narrow-screen behavior. Verify these in browser tests.
<!-- /autoplan-accepted:design -->

### DX review findings and dispositions

DX scope was triggered by internal API/action terminology. The feature is an end-user workflow in a local app, not a public API/SDK product; DX review is limited to the internal frontend/server contract and future repository maintenance. Native reviewer input `aa91141a9eed2199762bdb644dcb7801335526db274868be051bef2ed6f44f90` completed. Codex is unavailable: `gstack-codex-probe role-ready exec` returned `MODEL_UNUSABLE_INSTALL` because `codex` is missing. No outside or competitive benchmark coverage is claimed.

| Finding | Severity | Disposition | Evidence / rationale |
|---|---|---|---|
| Analysis API failure contract lacks actionable errors | High | Added RFC 9457 responses with stable codes, safe actual-value/bounds context, recovery instructions, side-effect states, request correlation, and explicit retry rules. | New upload, decode, limit, session and source-identity paths are introduced by this feature. |
| Retry behavior for expired sessions, invalid cuts, and source changes was underspecified | High | Defined when no session/job exists, when reanalysis/re-upload is required, when a retry is safe, and that unknown outcomes must be reconciled before retry. | Prevents duplicate work and tells UI/agents the next safe action. |
| Safe diagnostic context and trace handling were not specified | High | Include request/job id and sanitized trace path in local logs; response context excludes local paths, tokens, secrets, media and PCM. | Needed for recoverable errors without disclosing private source data. |
| Internal endpoint examples/docs were absent | Medium | Add `docs/silence-trimming-api.md` with internal request/response/error examples. Do not expose a public API, SDK or CLI. | A maintenance contract is useful for this new frontend/server boundary; no external developer product is implied. |
| Broad TTHW, community and external onboarding measures do not fit this change | Informational | Keep them N/A; verify helper/API tests and local media workflow instead. No external research or adoption claims. | The target developer is the local repository maintainer, not an API customer. |

Pre-fix DX scores from the single native review: API ergonomics 6/10; error handling 4/10; documentation/examples 4/10; in-app discoverability/control 8/10; testability 8/10; product boundary 9/10. TTHW and external developer onboarding are N/A. The introduced API error/documentation gaps are addressed in the current plan; implementation must verify the contract. Optional standalone maintenance-workflow expansion is deferred as unnecessary beyond the requested feature/API docs.

<!-- autoplan-accepted:dx -->
- Keep this as an end-user app feature with a private frontend/server API; do not add a public API, SDK, CLI, hosted service, or developer onboarding surface.
- Implement the RFC 9457 analysis/render validation error contract with stable codes, safe actionable context, recovery guidance, side-effect states, sanitized correlation/trace handling, and bounded retry semantics as specified above.
- Add internal API request/success/error examples at `docs/silence-trimming-api.md`; keep examples aligned with the actual route and response models.
- Verify error contracts, session retry/reanalysis behavior, non-leaking diagnostics, API examples, analyzer tests, browser flow and local media verification. Do not claim TTHW, competitive rank or adoption without relevant evidence.
<!-- /autoplan-accepted:dx -->

### Engineering review findings and dispositions

The native engineering review identified system-boundary and test gaps in the current proposal. Codex outside coverage is unavailable: the probe returned `MODEL_UNUSABLE_INSTALL` because the `codex` executable is missing. The findings below are accepted as implementation constraints; they do not change the user's feature scope.

| Finding | Severity | Disposition |
|---|---|---|
| Project source TOCTOU race; metadata-only identity could diverge from analyzed bytes | High | Snapshot project and direct sources into immutable server-owned files before analysis; hash snapshots and verify copy/handoff, then transfer ownership atomically to the persistent render job. |
| Source-time cuts and normalized master PTS lacked an exact shared map | High | Reset normalized master to clip-relative zero, persist source-to-master PTS/FPS mapping and derive frame snapping from actual master PTS. All preview/estimate/retry/render use this map and one effective interval list. |
| Existing multipart route materializes direct uploads in memory and source/session handoff was vague | High | Trimmed render requests use `silence_analysis_id` only, reject competing source selectors, atomically transfer the immutable snapshot to the job, and preserve legacy route behavior when trim is off. Stream and enforce byte limits during receipt, including unknown content length. |
| Resource caps omitted worker-wide decode/memory/time bounds and project-session concurrency | High | Stream incremental PCM; enforce the two-session limit across upload and project analyses within the single local app process, plus per-worker memory, source, envelope, workspace and wall-clock caps; terminate/reap decoder and release capacity on cancel/disconnect/timeout. Isolate snapshot directories by process so one worker cannot delete another worker's snapshot. |
| Session expiry could conflict with queued/running job retry and restart recovery | Medium | Session TTL applies only before handoff. Persistent job owns source and EDL across queue/run/restart; cleanup removes only unconsumed sessions and orphaned job data. |
| Browser/API overlap normalization and provenance behavior were inconsistent | Medium | Define one deterministic union contract, reject malformed/non-finite/zero/out-of-range/oversized lists, preserve member cut IDs/origins through union, and share browser/Python golden vectors. Zero-effective-frame edits stay visible but do not enter render; all-ineffective equals no-cut. |
| Synchronous API errors and asynchronous render job failures shared an unclear error contract | Medium | RFC 9457 applies only before job creation; render failure is a job status/result code with the same safe fields, not an HTTP failure retroactively applied to the request. |
| Worst-case envelope and JSON/browser-memory footprint lacked a hard bound | Medium | Cap 10 ms bins at 720,000 and JSON analysis responses at 8 MiB, never truncate; assert maximum-source encoding size and browser memory. |
| Test list omitted concurrency/handoff/restart, start-PTS/timebase/VFR and upload-overflow contracts; selected real clip check is not deterministic | Medium | Add deterministic tests for source identity races, atomic handoff, cancellation/cleanup/restart, decode resource limits, parser/upload limits, timebase/non-zero start PTS, VFR/variable durations and frame-boundary math. Keep real-project-video evaluation a documented local manual check, not an automated regression assertion. |

The plan retains one pure media/EDL module and existing route/engine/UI boundaries. No parallel implementation lanes are warranted; shared plan/code/test/API semantics need a single sequential integration path.

<!-- autoplan-accepted:eng -->
- Analyze a hashed immutable server-owned snapshot for both direct and project sources. Enforce 2 GiB/2 h, 2 sessions per local process, 64 MiB worker RSS, 128 MiB workspace headroom, 30-minute analysis timeout, 720,000 envelope bins, 8 MiB response ceiling, and cleanup/termination on every exit; isolate process-owned snapshot namespaces and require single-worker/sticky routing.
- Render trim-enabled jobs from `silence_analysis_id` only; atomically transfer snapshot ownership to the durable job before consuming the session. Preserve source and EDL for queued/running/retry/restart; TTL cleanup cannot remove job-owned media. Keep legacy request/render behavior unchanged when trim is off.
- Persist and reuse the clip-relative source-to-normalized-master actual-PTS map across preview, estimate, retry and render. Normalize cut ranges consistently in browser and Python as interval union while preserving cut IDs/origins; reject malformed/out-of-bounds/oversized records and mark zero-effective-frame cuts visibly ineffective.
- Keep RFC 9457 for synchronous errors only; expose asynchronous render errors in job state with stable code, safe cause, recovery, side effects and correlation. Use only allowlisted caller-safe cause codes and do not expose paths, secrets, raw exceptions, media or PCM.
- Test known-signal analysis, maximum envelope/response and worker memory, upload/parser bounds, immutable source hash/handoff/races, cancellation/timeout/cleanup/restart/expiry, overlap provenance, source/master PTS mapping, fractional frame/audio-sample alignment, VFR/timebase/non-zero start PTS, legacy no-cut compatibility, API error fields, browser gestures/accessibility and synchronized FFmpeg fixtures. Real project-video verification remains a separate local manual check.
- Implement sequentially across `montage/silence_trim.py`, `app.py`, `montage/engine.py`, `templates/index.html`, `docs/silence-trimming-api.md` and tests; no classes/services or parallel lanes. Keep all stated user behaviors and non-goals.
<!-- /autoplan-accepted:eng -->

Native CEO review input `2b798c59ddaf90d176de4d5bfa1ba58d01adb5ea33921b108d4267f328425a0b` had its findings reconciled here. A final read of the completed plan used input `2aadbac7f1f1cfd877daef4625ba91f6dd526745e7c50d0b399461e98db64306`; its remaining concerns and dispositions follow. Earlier inward-rounding shorthand is superseded by the precise half-open PTS rule below.

- Scope/readiness findings: preserve the user's full manual-editor request; keep removal opt-in, suggestions inspectable and restoration reversible. Do not claim user adoption/time saved; evidence is functional fixtures and the user's supplied workflow.
- Metric finding: define channel aggregation, whole-track RMS and sample peak in dBFS; threshold is per-window RMS and labels state that peak is not perceived loudness.
- Interaction finding: document left/right handle directions, auto/manual edit retention, explicit auto-detection reset, and deletion behavior; no separate undo stack is promised.
- Snap/render finding: starts ceil and ends floor on the normalized CFR master; convert those same seconds inward to audio sample boundaries. Estimates, preview, retry and final render use the same effective intervals.
- Workflow/session findings: previews show cut in/out and effective times, can audition the skipped-cut result; analysis is source-bound/expiring and the validated EDL is persisted for retries.
- Competitive finding: no market claim; the product-specific value tested is integration with existing effects and a reversible cut workflow.
- Manual-cut deferral is not adopted because manual cuts and edge restoration are explicit user requirements. No other scope change is accepted.

Final CEO findings on input `2aadbac7f1f1cfd877daef4625ba91f6dd526745e7c50d0b399461e98db64306`:
- Keep the full user-requested editor rather than reducing to detection-only. Defaults remain estimated, opt-in and reviewable; no adoption/time-saved claim is made.
- Bound memory independently of upload size: streaming PCM, 48 kHz max eight channels, two-hour duration, 2 GiB per direct file, two sessions and TTL; test/measure runtime and cleanup. Limits are explicit safety guards, not validated product defaults.
- Product quality criteria include known speech/noise/music/transients, a no-cut legacy bypass, source-time restoration, seam preview, and exact estimate/render cut-list parity.
- Frame indices use half-open `[start,end)`: start=`ceil(start_ms*fps/1000)`, exclusive end=`ceil(end_ms*fps/1000)`. Audio samples map to effective frame times. This supersedes the earlier floor-at-end draft and is covered by fractional-frame tests.
- Manual cut creation and edge editing remain because they are direct user requirements. The review's suggested staged scope is not adopted.

External Codex coverage is unavailable (`MODEL_UNUSABLE_INSTALL`); this is a single in-host perspective.

Final CEO pass input `9ff7fa02457e34bba489b77b2bb842d2aa63cbc3613a5731b7c09382052e2b0a` confirms the core request and raises these residual risks:
- The user asked for the full editor; keep manual cuts and handle edits, but make analysis opt-in, show every candidate, and do not claim universal defaults or measured time-savings.
- Treat dBFS/padding/session limits as estimated configuration and exercise the actual project video plus synthetic speech/music/noise/transient fixtures before saying defaults are validated.
- Keep both requested/effective timestamps and a reversible, no-cut render path; cut synchronization and audio seam quality remain release assertions.
- No external competitor/user study claims are made. The intended value is integration with the existing montage/effects workflow.

### CEO deep-review sections

#### 1. Architecture review

```text
Browser file/project
  -> silence analyze endpoint -> validated source session -> PCM envelope/stats
  -> source video preview + local threshold/padding -> candidate cuts + manual EDL
  -> montage request (analysis id, source-time EDL, regular montage settings)
  -> existing rotate/zoom/subtitles/overlays/censor pipeline
  -> effected master -> [non-empty EDL only: synchronized video/audio trim+concat]
  -> existing final/preview outputs -> existing publication preparation
```

Four boundary paths:
- Happy: source identity validates, PCM stream produces envelope, user reviews/edits cuts, render consumes the same source-bound EDL and returns aligned output.
- Nil: project id/file/audio missing; analysis returns a visible no-audio/no-source state; manual video-only cuts require an actual source, while auto detections are disabled without audio.
- Empty: audio exists but no spans meet threshold/minimum; show average/peak and zero proposed cuts; empty EDL bypasses the new render filter/encode path.
- Error: upload/decode/session/FFmpeg failure; cleanup partial temp media, preserve source/job, show stage-specific error and permit reanalysis/retry.

Session state machine: `new -> receiving -> analyzing -> ready -> consumed-by-render` with terminal `failed`, `expired`, and `cancelled`. Only ready sessions can be consumed. The random id maps to an immutable temp upload or validated project identity; no request-supplied path is used. A user may edit the local cut list without mutating the server analysis. Montage submission snapshots settings and cuts atomically; edits made afterward cannot mutate the running job. The persisted job EDL is the retry source of truth.

Coupling is justified: preview, estimate and render share one cut-normalization/snap contract. New dependency remains FFmpeg, already required by this project; no package or database is added. Load grows linearly with media duration for one PCM decode and with interval count for envelope/UI data; the first pressure point is long concurrent direct-upload sessions. Bound them and stream audio to avoid keeping PCM in RAM. Single failure point is local FFmpeg decode/concat; fail visibly without deleting source or publishing partial output. Rollback is opt-out; no schema migration.

#### 2. Error & rescue map

| Codepath | Failure | Rescue / user result | Test |
|---|---|---|---|
| Analysis upload | size/duration/session cap, disk full, disconnect | remove partial file; return actionable size/space/network error; no session id | mock write/short upload and limit boundary |
| Probe/PCM decode | missing audio, corrupt stream, FFmpeg non-zero/timeout | clean process/temp; display “Аудиодорожка не найдена” or “Не удалось проанализировать”; no false cuts | no-audio, invalid container, timeout/cancel |
| Envelope/detector | NaN, invalid range, empty envelope, threshold boundary | validate finite bounded values; empty result is valid; non-finite rejected | NaN/Inf, exact equality, empty/boundary tests |
| Analysis token | missing/expired/source-mutated/project deleted | reject before job creation; tell user to reanalyze | unknown id, TTL, project stat/hash change |
| Cut EDL | malformed JSON, too many, out-of-range, overlap, all media removed | 4xx with field/reason; normalize only documented adjacent ranges; never silently trim further | malformed/range/overlap/full-cut tests |
| Render filters | bad concat graph, encoder error, cancellation | register/terminate FFmpeg, remove partial file, leave original media and job state; show render-stage failure | injected FFmpeg failure and cancel tests |

No catch-all should turn invalid input into “no cuts”; failures are typed and visible. Log job/session id, source duration and stage, but never PCM/audio payload.

#### 3. Security & threat model

Threats: token guessing/cross-session source use, path traversal via project/analysis ids, oversized uploads, resource exhaustion with many long analyses, stale project files, malformed EDL, HTML injection in labels. Mitigations: unguessable server-generated id; whitelist/regex id and dictionary lookup, canonical project lookup; stream to server-generated names under a fixed work root; cap bytes, duration, channels, concurrent sessions and TTL; SHA-256 source fingerprint created during direct upload or project analysis and checked before render; validate all numbers/ranges/counts; render user strings with existing `esc()` before DOM insertion. No external transfer, credentials or new dependency. Existing app is local-only and does not claim multi-tenant authorization; the new endpoint must not accept arbitrary file paths.

#### 4. Data flow & interaction edge cases

```text
INPUT file/project -> source validation + bounded temp session
 -> streamed PCM -> 10 ms per-window dBFS envelope + mean/peak stats
 -> local threshold/minimum/padding detector -> source-time candidates
 -> user edits (auto/manual/adjusted/dismissed) -> normalized EDL snapshot
 -> API validation -> existing effect pipeline -> master CFR frame/time map
 -> inward-rounded video frame intervals + matching audio sample intervals
 -> trim/concat -> output metadata (requested/effective intervals, durations)
```

Analysis requests carry a monotonically increasing frontend generation id and `AbortController`; selecting a second file cancels/ignores the first response. Settings recalculate locally; manual/adjusted cuts remain. User edits after submitting affect only the next job. If analysis expires or the file changes, render refuses before job start and prompts reanalysis. Double submit is disabled; drag cancellation releases pointer capture and restores the last valid boundary. At least one retained frame/sample is required. Adjacent cuts merge; overlaps normalize by union; invalid zero-length/negative intervals are rejected. Empty source, no audio, silence at start/end, minimum-long full silence, one-frame intervals, VFR source, project deletion and browser navigation have explicit tests/states.

#### 5. Code quality review

The new audio math/EDL functions belong in one `montage/silence_trim.py`; app remains responsible for API/source sessions and engine for applying the EDL. Do not add a new rendering service/class hierarchy. Keep cut normalization pure so UI/API/render agree. Avoid a second bespoke audio parser in `audio_censor.py`; censorship is SRT-based and remains unchanged. Keep media FFmpeg arguments as arrays, not shell strings. Bounded branching: separate parsing, detection, validation and rendering functions; no function should own the full upload→render lifecycle.

#### 6. Test review

| Flow | Test type | Success assertion | Failure/edge assertion | no_claim |
|---|---|---|---|---|
| PCM stats/envelope | unit | known channel samples yield mean RMS, peak and 10 ms bins | silent, clipped, NaN, multichannel, partial final bin | synthetic signals do not represent every microphone/noise floor |
| silence detector | unit | threshold/minimum/padding produce exact cut ms | threshold equality, short spans, negative inputs, edge padding, adjacent intervals | unit test does not establish best user defaults |
| cut editing model | unit/browser | left/right restore, manual add/delete, recalculation preserves manual cuts | overlapping cuts, min-size, clear/reset, stale response | DOM interaction test does not measure time saved |
| analysis session API | integration | direct upload/project return same metrics and source-bound token | large upload, missing audio, expired/mutated token, disk/FFmpeg failure | local test does not prove adversarial remote auth; app stays local |
| cut render | FFmpeg integration | output duration and A/V seam timestamps match snapped EDL | no-cut branch unchanged, no-audio manual cut, all-cut rejected, cancellation | synthetic media does not prove every codec/PTS variant |
| result/retry | app integration | stored EDL/source id reused; preview and final metadata agree | stale session/replaced project rejected before rendering | does not invoke real publishing |

Friday-night confidence test: generate audio with known quiet spans, quiet speech-like sections, tones, music and transients; run detector, edit boundaries, render, and measure actual removed/kept time and A/V sync. Hostile QA: set an interval over the entire clip, submit NaN/huge/negative values, race two source uploads, expire/replace analysis source and cancel during PCM/concat. Chaos: terminate FFmpeg during analysis and cut render; assert temp cleanup, job error visibility and source retention. Pyramid: pure interval math majority, a few FFmpeg/API integration tests, focused browser flows. No real publisher calls.

#### 7. Performance

Stream PCM into fixed-size windows; no full decoded audio buffer. At the 2-hour cap, 10 ms bins yield 720,000 integer samples (about 1.37 MiB as signed 16-bit before JSON overhead; raw JSON is larger) [estimated from 7200 seconds × 100 bins]. Slider edits scan this envelope only. The video upload is stored once and reused via analysis id. Cap session bytes/duration/concurrency and expire sessions on a janitor/request; analyze in a worker thread/process with cancellation/timeout so FastAPI's event loop remains responsive. Time a short/typical/maximum clip; report measured wall time and peak memory during implementation. Render adds one encode only for a non-empty cut list; bitrate is unchanged from existing quality policy.

#### 8. Observability & debugging

Use existing operation progress/stage details and job logs. Emit structured entry/success/failure details with job id, analysis id prefix, audio presence, duration, mean/peak dBFS, threshold/minimum/padding, detected/edited cut count, removed ms, effective frame/audio sample boundaries and output duration. Never log audio samples or full user content. User sees analysis status, zero-candidate/no-audio/errors and render stage; logs let a report be reconstructed. No separate dashboard/process report: none consumes it in this local app.

#### 9. Deployment & rollback

No DB migration; feature off by default. Deploy `app.py`, new module, engine and template together, restart Uvicorn. Smoke test direct file and existing project analysis, no-cut legacy render, one known silence edit, manual edge restore and no-audio manual cut; publisher endpoints remain mocked/not called. Rollback by reverting the one feature change and using existing render path; analysis-session temp files expire/clean safely. No real publication or merge.

#### 10. Long-term trajectory

Reversibility 4/5: EDL and source are retained, but rendered output is a new file; reverting means rerender from source. Main maintenance risk is keeping browser and Python interval normalization/frame snapping identical; shared test vectors lock this contract. Avoid turning the tool into a general multi-track editor, waveform DAW or audio restoration system.

#### 11. Design & UX review

```text
Choose source -> Analyze (loading / error / no audio)
 -> stats + threshold/minimum/padding -> source preview + editable cut track
 -> adjust/add/delete (manual edits retained) -> preview-play skips cuts
 -> montage options -> render progress -> result summary (source/removed/output time)
```

| Feature | Loading | Empty/no audio | Error | Success/partial |
|---|---|---|---|---|
| Analyze | visible progress | “Аудио нет”; manual video cuts remain | reanalyze/retry | average RMS, peak, candidates and duration |
| Cut track | skeleton/disabled handles | full retained track, no cut blocks | expired/mutated source asks for reanalysis | cut blocks, left/right handles, requested and effective times |
| Render | existing progress | empty EDL uses old path | invalid/full cut rejected visibly | result reports removed and output duration |

Put stats and controls directly above the cut track next to the existing preview. Distinguish removed blocks with color plus text/legend, not color alone. Handles are keyboard focusable and expose `aria-valuetext`; hit targets support touch. Fit-to-view with horizontal timeline zoom/scroll for longer clips. Use the existing visual system; no new design tokens. If a user's interval is shorter than one frame, display that it snaps to zero and is not applied.

### CEO Review Sections 1–11 (HOLD SCOPE)

#### 1. Architecture

Existing route: `app.py:start_montage` (`app.py:3402+`) accepts direct video/project; `process_montage` (`app.py:2775+`) calls `montage/engine.py:render_montage`. Existing preview UI is `templates/index.html:1134-1177`; direct uploads create a browser object URL in `templates/index.html:1962-1977`. Existing audio censorship is SRT-driven (`montage/engine.py:1266-1310`), not silence removal.

```text
Direct file / saved project
  -> POST /api/montage/silence/analyze (file or project_id)
  -> source validation -> server-owned analysis session -> streaming PCM decode
  -> dBFS envelope + mean RMS + sample peak -> browser cut candidates
  -> threshold/min-duration/padding -> editable source-time EDL
  -> POST /api/montage/render (analysis_id + EDL + SRT + current options)
  -> validated source copied into job -> existing rotation/zoom/subtitle/overlay/censor
  -> effected master -> if EDL nonempty: audio+video trim/PTS reset/concat -> master
  -> final/preview -> existing optional mirrored variant -> existing publisher prep
```

State machine: `no-source -> analyzing -> ready -> editing -> submitted -> consumed`; failures are explicit `analysis-error`, `expired`, or `render-error` states. A token not in the server session registry or whose source identity changed cannot transition to `submitted`. The output remains the existing montage job; no database or new service is proposed. The direct upload is temporarily stored under a random token and reused by render. A project source is looked up by project id and copied only after validating the recorded identity. The renderer uses the existing process registration/cancellation path.

Nil path: no audio returns a clear “audio unavailable” state; manual video cuts remain available. Empty path: no detected intervals means the existing render path runs without new FFmpeg commands. Error path: decode/analysis or invalid session fails visibly before montage; cut-graph/render failure fails the montage without marking it complete. Security boundary: API accepts upload bytes or an existing project id only, never a client path. Temporary files stay under `MONTAGE_WORK` and expire. Rollback: trim toggle defaults off and an empty cut list takes the original path; disabling it restores behavior without a schema migration. No 10x/100x service scaling claim: cost is one linear PCM pass plus the existing render.

#### 2. Error & Rescue Map

| Codepath | Failure | Handling | User sees / test |
|---|---|---|---|
| `silence_trim` PCM decode | missing audio, corrupt stream, FFmpeg failure | return typed analysis error; no session | audio unavailable/error; synthetic no-audio and corrupt-fixture tests |
| Analysis upload session | disk full, over limit, timeout, expiry | clean partial file; release registry entry; never accept partial analysis | upload/analysis error or reanalyze prompt; size, expiry and injected write-failure tests |
| dB/interval derivation | NaN/Inf/invalid threshold, empty envelope, no detected spans | validate finite values and integer ms; empty list is valid | field-level validation; empty and boundary tests |
| `start_montage` source binding | unknown token, source changed, project deleted | reject before job start; do not fall back to stale/unrelated upload | “Видео изменилось, анализируйте заново”; mismatch tests |
| EDL validator/renderer | out-of-range/overlap/full cut/zero retained duration | normalize documented adjacency; reject invalid or all-removed list; register ffmpeg process for cancel | clear 4xx or render error; unit, API and cancel tests |
| audio/video concat | filter graph/codec failure, unequal segment streams | capture stderr tail, fail job and clean partial outputs | stage failure, never success; audio/video seam fixture |
| UI upload/drag | stale async response, duplicate submit, pointer cancel/navigation | request generation id/AbortController, disabled submit, pointer capture/release; keep local EDL until explicit reset | stale result ignored, user can retry; browser tests |

#### 3. Security & Threat Model

- New surface is the analysis endpoint and optional `silence_analysis_id` render form field. IDs are cryptographically random bearer tokens; lookup is registry-only and path components are never constructed from client input. Project ids use the existing project lookup; both direct and project inputs are streamed/copied into a server-generated immutable snapshot and hashed before analysis.
- Bound request bytes (including chunked/no-length uploads), source duration, audio channels, active sessions, decoder threads, wall time, response bins/bytes and workspace headroom. Decode PCM incrementally to 10 ms buckets rather than retaining PCM/audio in memory. Clean partial files on every exception/cancel/disconnect and remove orphaned/expired analysis directories.
- The token binds to an immutable server snapshot, not a mutable project path. Verify the snapshot hash at atomic job handoff, move ownership into the persistent job directory before consuming the session, and preserve job-owned media for queue/run/retry/restart. No audio samples, local paths, tokens or user captions are written to general logs.
- Validate dB values as finite and in allowed range; ms fields are finite integers with bounds; cut list is a JSON array with a maximum count, sorted/normalized or rejected, within video duration. Invalid JSON/HTML labels are escaped in the DOM; no user text is interpolated into HTML.
- No credentials, external uploads or new third-party dependencies. The current app is local-network scoped; this feature does not claim multi-tenant auth. Temp-session exposure is constrained by unpredictable tokens and source binding.

#### 4. Data Flow & Interaction Edge Cases

```text
  video/project -> bounded immutable snapshot + SHA-256 -> PCM decode -> envelope/stats -> UI candidate spans
  -> user EDL edits -> range/type/count validation -> job state snapshot
  -> master cut filter -> audio/video PTS reset -> concat -> duration/metadata
```

| Interaction | Edge case | Handling |
|---|---|---|
| Select file / project | file changes while analysis is pending | abort old request and increment generation; ignore stale response; clear prior EDL |
| Threshold/padding change | 0ms, negative, extreme, non-finite | numeric bounds and integer-ms normalization; recalc locally from the same envelope |
| Automatic detection | no audio, all silence, no spans, start/end silence | no-audio allows manual cuts; no spans means no-op; full removal rejected; edges clamp to `[0,duration]` |
| Cut manipulation | edge crosses other cut, cut < minimum, deletes silence | enforce interval invariant; merge overlaps; deleted auto-cut stays dismissed for current detection revision; reset action is explicit |
| Async session | source replaced/project deleted, token expires, duplicate render | recheck identity/TTL; fail before render with reanalyze action; disable duplicate submit |
| Render | cancel while reading/encoding, ffmpeg partial file | terminate registered process, remove partial output, preserve original source and validated EDL |

Ordering invariant: one analysis token binds one source identity and one envelope. Settings changes do not re-upload or mutate server source. Montage submission snapshots EDL/options atomically before queuing; browser edits after submit do not mutate the running job. Retry uses that persisted snapshot. A different video selection invalidates the old analysis id. Tests should force request completion in both orders (old/new analysis) and ensure only the newest source token reaches render.

#### 5. Code Quality

Reuse `app.py` project/workdir and job lifecycle, `montage/engine.py` process registration/progress and master path, and existing preview/keybar/pointer patterns in `templates/index.html`. Keep signal maths/EDL normalization in one small `montage/silence_trim.py`; do not add a second montage pipeline or package. Keep input validation in API boundary and a pure deterministic interval normalizer in the module. The no-cut branch must stay visibly separate from trim FFmpeg commands. Avoid editing `camera_trajectory.py`, subtitle generators, animation renderer and content-factory.

#### 6. Test Review

```text
PCM math (unit) ─┬─> silence detector edge cases (unit)
                 ├─> API/session binding and cleanup (integration)
                 ├─> EDL normalize/snap and A/V concat (media integration)
                 └─> Browser analyzer/editor states (Playwright)
```

Required assertions and limits:
- Envelope tests use known amplitudes/durations/channels. Assert mean RMS and peak dBFS independently; `no_claim`: synthetic PCM does not model room acoustics or microphone noise.
- Detection tests cover exact threshold equality, shorter/longer-than-minimum spans, padding clamped at media edges, adjacent/overlapping candidates, no-audio and full-silence. Assert intervals to one envelope bin; `no_claim`: sample fixtures do not establish the best universal threshold.
- Timeline tests assert both handle directions, add/delete manual ranges, recalculation preserving manual edits, source-duration bounds, frame/audio sample snapping, and estimate/output duration equality; `no_claim`: DOM tests do not measure perceived editing speed.
- Media integration creates synthetic video+audio, cuts known intervals, probes output streams/durations and correlates audio/video markers at both seams. Check empty EDL bypass and canceled/failed output cleanup; `no_claim`: synthetic media does not prove all camera codecs/PTS behave identically.
- API tests cover direct upload and saved project, expiry/mutation/token mismatch, too-large input, and no real publisher. Browser tests cover async source replacement, pointer/touch/keyboard, accessible labels and narrow viewport.

Test pyramid: many interval/unit tests, fewer FFmpeg/API integration tests, two browser-flow tests. Tests use deterministic PCM/files, not wall-clock sleeps or account credentials. No LLM tests are needed.

#### 7. Performance

One streaming decode builds `duration × 100` envelope values; sliders scan that compact envelope instead of reopening media. A 60-minute source yields 360,000 bins; at an integer hundredth-dB representation the payload is a few MB, not decoded PCM. Direct and project sources are copied once into an immutable analysis snapshot, then moved into the job for render. TTL cleanup and a two-session global cap bound temporary storage. No external service or DB connections are added. The render performs the existing montage plus one post-master EDL encode only when effective cuts are nonempty. The 30-minute timeout, 64 MiB worker target and response cap remain estimates until measured on short, typical and maximum allowed clips.

#### 8. Observability & Debuggability

Use existing job operation logs/progress. Add structured fields for analysis id, source identity prefix, duration, mean/peak dBFS, chosen threshold/minimum/padding, candidate count, accepted/removed ms, effective snapped boundaries, and final duration; never log PCM or source bytes. Stage states: analyzing, ready/editing, applying cuts, finalizing. Errors identify which stage and whether reanalysis is needed. Existing progress UI is the operational monitor; no standalone dashboard/process artifact is introduced.

#### 9. Deployment & Rollback

No DB migration. Deploy Python module/API and template together. The default-off trim toggle preserves existing user output. Rollback is to disable trimming and revert the route/template/engine code; existing job files remain readable. Smoke checks: direct-upload analysis, project analysis, no-cut render, a known cut render, and reanalysis after an expired session. Do not invoke publish or real accounts. Server restart is required because `app.py` changes; restart invalidates unconsumed analysis ids, while job-owned source/EDL remain available for render recovery.

#### 10. Long-Term Trajectory

Reversibility 4/5: source remains intact and EDL is in job metadata; removing the post-master stage restores original timing. Main debt risk is the temporary analysis-session manager and the dual source-time/effective-frame metadata; keep those in one module and document both coordinate systems. No claim that silence trimming is a general-purpose DAW/editor timeline.

#### 11. Design & UX

| Feature | Loading | Empty/no audio | Error | Success/partial |
|---|---|---|---|---|
| Analyze | spinner + progress label | show no-audio message; manual cuts remain | actionable reanalyze/retry | average/peak dBFS, detected count and durations |
| Cut track | skeleton over current preview | full source track, no cut blocks | source/session mismatch asks to reanalyze | colored excluded spans, start/end handles and centisecond/effective-frame labels |
| Render | existing montage progress | no-cut follows current path | invalid/all-cut list rejected before encode | result duration/source removed summary and playable output |

Flow: `select/upload video -> preview current source -> analyze -> adjust threshold/duration/padding -> drag/add/delete cut ranges -> audition skip preview -> start montage -> render result`. Keep current preview controls and input hierarchy; place dB stats/controls directly above the cut track. Use distinct cut color with text/icon, keyboard focusable handles and aria-valuetext, touch hit targets, horizontal scroll/fit-to-view for long videos and responsive layout. No new color/font system.

No unresolved scope issue is accepted from the CEO review: manual cuts and controls are explicit user requirements. Remaining unverified items are media-specific detection quality and performance; required synthetic and local-clip evidence is listed above. External Codex coverage is unavailable; the CEO perspective was in-host only.

### Final Review Outputs

#### What already exists

- Existing source/project upload and job creation: `app.py:start_montage` (`app.py:3402-3473`); the current direct upload reads the whole video into bytes before writing (`app.py:3471-3473`).
- Existing pipeline: SRT censorship in `montage/engine.py:1266-1310`, followed by the effected master and delivery encodes (`montage/engine.py:1312-1361`).
- Existing UI preview/keybar and pointer conventions: `templates/index.html:1134-1177`, `1962-1977`, `2329-2365`.
- Existing audio censoring is SRT-based (`montage/audio_censor.py:70-118`); it does not detect or remove silence.
- Python 3.9/unittest and project conventions are in `CLAUDE.md`/`AGENTS.md`. No `DESIGN.md`, `TODOS.md`, or feature-specific silence implementation exists. No schema migration or new dependency is proposed.

#### NOT in scope

- Public API/SDK/CLI, hosted service, new deployment platform, multi-track audio editor, waveform editing, audio normalization, autozoom/subtitle/animation/content-factory changes, real publication or merge.
- External competitive/adoption/TTHW claims. The interface is a private internal route in a local end-user app; no meaningful external developer onboarding clock exists.
- TODO additions: no independent deferred feature is required; all user-requested work remains in this implementation.

#### Design Review Completion Summary

| Dimension | Initial | After plan fixes | Remaining gap |
|---|---:|---:|---|
| Information architecture | 7/10 | 9/10 | None material; verify in browser implementation. |
| Interaction states | 6/10 | 9/10 | Runtime appearance awaits implementation. |
| Journey/emotional arc | 7/10 | 8/10 | No user research; no adoption claim. |
| Specificity / AI slop | 6/10 | 8/10 | No generated visual mockup was approved. |
| Design-system alignment | 6/10 | 7/10 | No DESIGN.md exists; reuse existing project tokens. |
| Responsive/accessibility | 6/10 | 9/10 | Runtime keyboard/touch/contrast verification remains. |
| Unresolved decisions | — | 0 high/critical | Native reviewer left one tie-break; later retained time chosen. |

Overall design score is 7/10 (the lowest rated dimension). Native design reviewer completed two substantive reads; Codex outside review unavailable (`MODEL_UNUSABLE_INSTALL`); cross-model litmus consensus N/A. No approved mockups. The UI scope is an app editor, not a landing page.

#### Developer Persona and Journey

**Target developer:** the maintainer of this local Python/FFmpeg application. They add a feature to an existing codebase, run unittest and browser/media tests, and need to preserve current upload, render, and retry behavior. Tolerance for ambiguous media lifecycle, frame timing, or silent error behavior is low. The target is an internal maintainer, not an API customer.

**Developer perspective (inference, not observed interview):** “I already have a working montage pipeline and I need silence removal to fit into it without changing old output. I look for the route, source ownership, and render handoff first. If analysis buffers the whole upload or only works for a fresh request, I need that stated before I wire up the UI. When a render fails, I need to know whether a job exists, whether the source is still available, and whether retry is safe. I can validate interval math with synthetic media, but I still need a local check on the selected project video. I want one deterministic source-to-master time map so preview and output do not disagree. The API examples and targeted test commands should make it possible to maintain the feature without reverse-engineering the browser.”

| Stage | Maintainer action | Friction / status |
|---|---|---|
| Discover | Find the feature plan and current source/render paths | Existing files cited; no public product discovery surface is in scope. |
| Install | Use the repository's existing Python 3.9/FFmpeg environment | No new dependency or setup step planned; verify exact local prerequisites during implementation. |
| Hello World | Run a deterministic analyzer test, then a local preview | TTHW N/A as external developer onboarding; command/test path to be documented in API/maintenance docs. |
| Real usage | Exercise direct upload and saved project, edit cuts, render | Full source ownership and timeline map specified; local media check is manual. |
| Debug | Read stable problem code, safe cause, recovery and request/job id | Specified in API contract; verify actual emitted response and job status. |
| Upgrade | Deploy app/module/template together, no migration | Existing behavior stays default-off; verify old no-cut render path. |

First-time failure model: a maintainer will first discover the feature from this plan, then inspect `app.py` and `montage/engine.py`; there is no standalone public installation flow. The highest-risk confusion is whether a temporary analysis token still owns the source after render submission. The plan now gives the job a durable immutable snapshot and explicitly separates synchronous API errors from asynchronous job failures.

#### DX Review Scorecard

| Dimension | Initial | After plan fixes | Evidence / status |
|---|---:|---:|---|
| Getting started / TTHW | N/A | N/A | No external developer product or hello-world workflow. |
| API ergonomics | 6/10 | 8/10 | One private analysis route and explicit trimmed render source contract; examples planned. |
| Error messages | 4/10 | 8/10 | RFC 9457 synchronous errors, safe cause/recovery/side effects; job errors separate. |
| Documentation | 4/10 | 8/10 | Internal endpoint examples and error contract planned in `docs/silence-trimming-api.md`. |
| Upgrade path | 8/10 | 8/10 | Opt-in, no schema migration, legacy no-cut path preserved. |
| Developer environment | 8/10 | 8/10 | No new dependencies or runtime platform; test commands and real-media check still need execution. |
| Community | N/A | N/A | Local end-user app, not a developer product. |
| DX measurement | 8/10 | 8/10 | Regression fixtures and local checks defined; no external onboarding instrumentation proposed. |

DX overall: 8/10 for plan completeness after fixes. TTHW: N/A → N/A. Product type: end-user local application with a private internal API. Native DX reviewer completed four evaluations; final verification found no High/Critical gaps. Codex outside review unavailable; cross-model consensus N/A. The internal API documentation is an implementation task, not evidence that it already exists.

#### Engineering Review Completion Summary

- Scope challenge: accepted as-is; one new pure media/EDL module, existing API/job and render boundaries, existing template; no new classes/services. Sequential implementation is required because source ownership, EDL contract and UI/render normalization share interfaces.
- Architecture review findings: 3 high-risk boundaries were tightened (immutable source hash/handoff, actual PTS map, trimmed render API selector); process/resource caps and source ownership are explicit.
- Code-quality findings: one browser/server interval-union contract with provenance and zero-effective-frame semantics; synchronous problem responses are distinct from asynchronous job failures.
- Test review: add unit, API/session, FFmpeg and browser tests as enumerated in acceptance criteria; include source races, restart/expiry, bounds, PTS/timebase/VFR and cancellation. QA Test Plan artifact: `/Users/labiozzza/.gstack/projects/labiozzza-automintaje/labiozzza-quality-max-eng-review-test-plan-20261025.md`.
- Performance: stream PCM and file copies; cap worker memory, analysis sessions, duration, response bytes and timeout. All limits/defaults remain estimates until measured.
- Failure modes: no known silent failure is accepted in the plan; source/session/job ownership and cleanup are test requirements.
- Outside voice: Codex unavailable, exact evidence `MODEL_UNUSABLE_INSTALL`; no outside coverage. Final native verification: no remaining High/Critical gaps.

Reality rows (engineering review):
- `premise-table`: the feature does not already exist; existing audio censor is SRT-based (`montage/audio_censor.py:70-118`); proposed source/session and error bounds are labeled estimates (`docs/video-silence-trimming-plan.md:30-31,88-90`).
- `already-done`: fetched `origin/main`; no silence-trim implementation/commit appeared in named route/engine/template/test paths since 2026-10-24.
- `surface-check`: existing `POST /api/montage/render` at `app.py:3402`; proposed analysis route is new.
- `binding-decisions`: branch decision search returned no records; user requirements U-01/U-02 and non-goals remain binding.
- `numbers`: 720,000 bins = 7,200 seconds × 100 bins/second; file/duration/session/memory/time/response limits are estimated, not measured.
- `deferred-asks`: no linked issue/TODOS.md found; no user-requested behavior is deferred.
- `pr-count`: no PR requested; one local change set is planned; no PR split.

`PLAN_REALITY: phase=eng rows=7/7 missing=none verdict=complete` — verified by `gstack-plan-reality check --phase eng docs/video-silence-trimming-plan.md`.

#### Design Litmus Scorecard

| Check | Native in-host | Codex | Consensus / evidence |
|---|---|---|---|
| Product/brand clear in first screen | Product identity is existing local montage app | N/A | N/A: no outside voice; brand specification unavailable. |
| One visual anchor | Source video preview | N/A | N/A: grounded in existing preview path. |
| Scannable by headings | Yes, preview → status → timeline → inspector | N/A | N/A: hierarchy is specified in plan. |
| Each section has one job | Yes, analysis, timeline edit, inspector, montage settings separated | N/A | N/A: plan evidence only, no rendered pixels. |
| Cards necessary | Reuse existing preview card; no new decorative grid | N/A | N/A: existing layout. |
| Motion improves hierarchy | N/A, no new decorative motion planned | N/A | N/A. |
| Works without decorative shadows | N/A, no new shadow treatment specified | N/A | N/A. |

Hard rejections: none identified from the text plan. Pixel/mockup quality is unverified; no visual mockup was produced or approved.

#### Competitive DX Benchmark

| Product | Start → useful result | Time / evidence | DX choice |
|---|---|---|---|
| This application | Existing local checkout → silence analysis and editable cut preview | Time unmeasured; internal app feature, no first-time external developer start state | Reuse current local setup and preview; add internal API examples and focused tests |
| External peers | N/A | Aside/WebSearch unavailable; no external benchmark claimed | No competitive positioning claim |

TTHW target/current: N/A. The product is not a developer API/SDK/CLI and there is no external “hello world” flow. A future maintainer's local test time can be measured after implementation, but is not represented as a current observation.

#### First-Time Maintainer Confusion Check

| Time | Maintainer action | Predicted question | Plan response |
|---|---|---|---|
| T+0:00 | Opens feature plan and checks route | “Where does this enter the current renderer?” | Existing `app.py:start_montage`, new analyze route and render handoff named. |
| T+0:30 | Reads upload/session contract | “Which file is analyzed and which file is rendered?” | Immutable server snapshot, hash, source id and durable job handoff specified. |
| T+1:00 | Reads frame math | “How do VFR and non-zero PTS map to this cut?” | Persisted source→normalized-master PTS map and VFR fixtures specified. |
| T+2:00 | Checks errors/retries | “Does the HTTP request fail, or does an existing job fail?” | Synchronous Problem Details vs asynchronous job status separated. |
| T+3:00 | Runs test plan | “Do these tests cover the seams and lifecycle?” | Unit/API/FFmpeg/browser matrices plus local manual project-video check specified. |

This is a plan-based prediction, not an observed onboarding study.

#### DX Implementation Checklist

- [ ] Internal request/success/error examples are accurate and copy-pasteable.
- [ ] Analysis route names and fields are consistent with the render form.
- [ ] Defaults are explicit, editable, and trimming remains opt-in.
- [ ] Every synchronous API error has stable code, safe context and recovery.
- [ ] Every asynchronous render failure is visible in job status/result.
- [ ] Retry instructions match source/session/job side effects and limits.
- [ ] No secrets, source paths, PCM or raw exception text are exposed.
- [ ] Upload/source ownership is obvious from route and API documentation.
- [ ] Analysis id expiry and reanalysis steps are documented.
- [ ] Cut-list normalization and provenance are documented with examples.
- [ ] CFR/VFR/source-time mapping is documented.
- [ ] Targeted unit, API, FFmpeg and browser test commands are recorded during implementation.
- [ ] Existing no-trim request behavior remains compatible.
- [ ] No new dependency or external service is introduced.
- [ ] Restart and retry behavior is described and tested.
- [ ] Real project-video validation is labeled manual and non-repeatable.

#### Cross-Phase Themes

1. Preserve original source-time intent while ensuring preview, estimate, retry and render use identical effective cuts.
2. Make the opt-in/no-cut path explicit so existing montage output does not change accidentally.
3. Bound source ownership, worker resources, lifecycle cleanup and recovery; never turn a failure into a silent no-op.

#### Local media verification (2026-10-25)

- Investigation of the reported `source_changed` render error found two app processes listening on port 8000 and a `snapshot missing` handoff log. The analysis registry was process-local while cleanup shared one directory, so one worker could delete another worker's live snapshot. Snapshots now live in process-specific PID+nonce namespaces, and cleanup removes only the current namespace or dead-worker namespaces; the main app was restarted as a single worker. A same-size mutation regression test confirms the hash mismatch now returns safe `409 source_changed` before job creation.
- A 28.9 MB synthetic 5-minute source produced visible progress: upload `1.77%`, probe indeterminate, then decoded-duration progress `0% → 34.2% → 68.0%`, followed by ready/100%. The temporary analysis session was deleted after the check. Capacity tests return `Retry-After` equal to the oldest active session's expiry estimate; the UI countdown formats hours/minutes/seconds.
- Same-size snapshot mutation was simulated at render handoff: the API returned `409 source_changed` with the safe cause `snapshot_hash_mismatch`, created no job, and logged only truncated expected/actual digest prefixes. The UI now explains that the cut list remains available and analysis should be rerun; it releases the invalid session.
- In the live local Chrome UI, analyzed the selected saved project (32.535 s): mean RMS `-30.39 dBFS`, max sample peak `-6.47 dBFS`, and two default-threshold candidates. Changing threshold to `-60 dBFS` recomputed to zero candidates without another analysis request.
- Manually added a 500 ms cut, enabled the opt-in toggle, confirmed the estimated duration changed, switched to edited playback, and verified keyboard boundary movement plus Undo. A synthetic pointer drag verified boundary clamping; responsive layout was checked at a 500 px viewport. No publish action was triggered and no screenshots were stored. Touch interaction was not exercised because headless Playwright exits with SIGTRAP.
- Exercised `_apply_silence_edl` against that saved project with the two detected source-time spans: output was usable, contained video+audio, removed `1,833 ms`, and reported `30,702 ms` output duration. Final probe showed 921 CFR video frames at 30 fps (30.700 s) and 30.701 s audio/container duration, a 1 ms muxed A/V delta. This was a helper-level real-source check, not a full montage render; subjective false-cut/missed-pause listening remains unverified.
- The existing server on port 8000 served the pre-change route until restart. It was restarted to the new app revision after confirming only completed jobs; the reviewed browser smoke used the running local app and made no render/publication request.

#### Implementation Tasks

- [x] **T1 (P1, human: ~1 day / CC: ~30 min)** — `montage/silence_trim.py`, `app.py` — immutable source sessions, streamed bounded analysis, compact envelope, monitored decoder RSS, pollable upload/probe/decode progress, capacity wait estimates, ready-session release, expiry and structured errors. Known-signal, upload/project, progress endpoint, capacity wait, release, worker-RSS parsing and expiry tests pass.
- [x] **T2 (P1, human: ~1 day / CC: ~30 min)** — `montage/engine.py`, `montage/progress_tracker.py`, `app.py` — atomic source handoff, shared target CFR/PTS map, canonical EDL union, synchronized A/V cut/concat and durable job metadata. CFR, target-FPS, VFR, video-only, no-cut and real-source helper checks pass.
- [x] **T3 (P1, human: ~1 day / CC: ~30 min)** — `templates/index.html`, `docs/silence-trimming-api.md` — opt-in editor, determinate/indeterminate progress bar, analysis stages, capacity countdown, ready-session release on source reset, accessible cut list/handles/playback, API examples and recovery guidance. Manual Chrome check covered project analysis, progress completion, cached settings, manual cut, keyboard/undo, preview mode and narrow layout.
- [x] **T4 (P1, human: ~1 day / CC: ~30 min)** — `test_silence_trim.py`, `test_silence_trim_api.py` — unit/API/progress/capacity/FFmpeg seam/source-handoff/expiry/regression tests and local project-video helper check. Full unittest suite: 273 passed, 3 skipped; no publication calls.

Plan/phase reviewers did not write per-phase JSONL task artifacts, so the machine aggregator returned: `_No actionable tasks emitted from any phase._` The four implementation tasks above were the manually reconciled build plan; all four are now complete.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|---|---|---|---:|---|---|
| CEO Review | `/plan-ceo-review` | Scope and strategy | multiple in-plan passes | issues addressed in plan | user scope preserved; no outside voice |
| Outside Review | Codex CLI probe | Independent second opinion | 1 attempt per applicable phase | unavailable | `MODEL_UNUSABLE_INSTALL`; no cross-model confirmation |
| Eng Review | `/plan-eng-review` | Architecture and tests | 4 native attempts, 3 complete | findings addressed; final no High/Critical | see engineering summary |
| Design Review | `/plan-design-review` | UI/UX | 3 native attempts, 2 complete | findings addressed; final no High/Critical | overall plan score 7/10 |
| DX Review | `/plan-devex-review` | Internal API/maintainer experience | 4 native evaluations | findings addressed; final no High/Critical | overall plan score 8/10; TTHW N/A |

**OUTSIDE COVERAGE:** Codex unavailable for CEO, Design, DX and Engineering because the installed CLI is missing (`MODEL_UNUSABLE_INSTALL`). Outside coverage is not complete.

**VERDICT:** The owner approved `plan1a` (gate revision 1). Implementation is complete and the full unittest suite passes. No push, merge, publication, or PR was performed.

**VERIFICATION LIMITS:** The full 2 GiB/2-hour upload and 30-minute timeout boundaries were not exercised at maximum scale. Headless Playwright could not run because its Chromium process exits with `SIGTRAP`; the browser workflow was manually checked in the running Chrome instance. Touch-specific device input and a full montage-job render on the private project were not exercised; the actual source was cut with the EDL helper and synthetic A/V render fixtures cover the pipeline semantics.

**UNRESOLVED DECISIONS:**
- None. Approval recorded as `plan1a` at gate revision 1.
