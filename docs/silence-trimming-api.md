# Silence trimming API (internal)

These endpoints connect the local montage UI to the analysis and render workers. They are not a public API, SDK, or CLI.

## Analyze a direct upload

```http
POST /api/montage/silence/analyze
Content-Type: application/octet-stream
X-Filename: %D0%BA%D0%BB%D0%B8%D0%BF.mp4
X-Analysis-Request: <client-generated UUID>

<raw video bytes>
```

The request body is streamed to a server-owned immutable snapshot. The server enforces a 2 GiB maximum while receiving the body, including when `Content-Length` is absent. `X-Filename` is URL-decoded and reduced to a safe basename; it is metadata only.

## Analyze a saved project

```http
POST /api/montage/silence/analyze
Content-Type: application/json
X-Analysis-Request: <client-generated UUID>

{"project_id":"p_1234abcd"}
```

The project id is resolved through the server-side project registry. Client-supplied paths are never accepted. Both upload and project analysis produce the same kind of immutable snapshot/session.

## Successful response

```json
{
  "analysis_id": "<random opaque id>",
  "request_id": "<client-generated UUID>",
  "source_filename": "clip.mp4",
  "source_duration_ms": 12000,
  "fps": "30000/1001",
  "audio_available": true,
  "sample_rate_hz": 48000,
  "channels": 2,
  "bin_ms": 10,
  "mean_rms_dbfs": -24.13,
  "max_peak_dbfs": -2.41,
  "audio_duration_ms": 12000,
  "envelope_dbfs_x100": [-2210, -1987, -12000],
  "expires_at_ms": 1790000000000
}
```

`envelope_dbfs_x100` has one signed integer hundredth-dBFS value per 10 ms bin. The floor is `-12000` (-120 dBFS). The maximum is 720,000 bins and the encoded response body may not exceed 8 MiB. RMS is accumulated across all channel samples without downmixing; maximum peak is the largest absolute sample.

If the source has no audio stream, analysis still succeeds with `audio_available:false`, null RMS/peak, `channels:0`, and an empty envelope. Manual video-only cuts remain available.

Threshold, minimum duration, and padding are applied in the browser to the returned envelope. They do not create another upload or decode.

## Read progress while analysis is running

The client sends a random UUID in `X-Analysis-Request` and polls it while the POST is pending:

```http
GET /api/montage/silence/progress/<client-generated-UUID>
```

```json
{"request_id":"<uuid>","status":"running","phase":"decoding","percent":43.5,"detail":"Обработано 4.3 из 10.0 с","wait_seconds":null}
```

Phases are `resolving`, `receiving`, `copying`, `probing`, `decoding`, `complete`, `capacity`, `error`, `cancelled`, and `released`. `percent` is null when the operation has no reliable denominator (for example, initial probe or a chunked upload without known size); during decode it measures decoded duration, not an invented time estimate. The UI should use an indeterminate bar in those phases and a determinate bar otherwise. Progress ids are short-lived diagnostics, not analysis tokens.

If capacity is full, the 429 problem response includes `context.wait_seconds`, `context.next_expiry_ms`, and the matching `Retry-After` header. The estimate is until the oldest active session expires; it can become free earlier if its render consumes the session.

## Release an unconsumed session

When the user selects a different source or explicitly starts a fresh analysis in the same editor, release the previous ready session:

```http
DELETE /api/montage/silence/analyze/<analysis_id>
```

The server deletes only an unconsumed analysis snapshot. A session already moved to a render job cannot be deleted through this route; job-owned media follows the job retention/recovery lifecycle.

## Submit a trimmed render

The normal `POST /api/montage/render` multipart form includes:

```text
silence_trim_enabled=true
silence_analysis_id=<id from analysis response>
silence_cuts_json=[{"cut_id":"manual-1","start_ms":1500,"end_ms":2200,"origin":"manual"}]
```

The trimmed request must not include a `video` or `project_id` source selector. SRT/transcription, overlay, and montage settings remain normal form fields. The server validates the source session and cut list, then atomically transfers the immutable snapshot to the persistent job before creating the render task. The job stores the cut snapshot, source hash, master time map and output timeline metadata. The session id is not retained in the job after transfer.

Cut records use integer milliseconds and a half-open interval `[start_ms,end_ms)`. `cut_id` must be unique; `origin` is `auto`, `edited`, or `manual`. The server rejects malformed, zero-length, non-finite, out-of-range, or more-than-10,000 records. Overlapping/adjacent valid intervals are unioned for rendering while preserving all member ids/origins. Ranges that snap to zero master frames remain visible but do not enter the render EDL. If no effective frames remain to cut, the legacy no-cut path is used. If every frame would be removed, request validation fails before job creation.

When trimming is disabled, omit the analysis id and preserve the existing video/project request path and output behavior.

## Synchronous errors

Before a render job exists, errors use `application/problem+json` (RFC 9457):

```json
{
  "type":"urn:automontage:silence:analysis_expired",
  "title":"Analysis Expired",
  "status":410,
  "detail":"The analysis session expired or is no longer available.",
  "instance":"/api/montage/render",
  "code":"analysis_expired",
  "operation":"session_lookup",
  "context":{},
  "recovery":"Reanalyze the source and retry.",
  "side_effects":"none",
  "request_id":"<correlation id>"
}
```

Stable codes/statuses:

| Code | HTTP | Meaning / recovery |
|---|---:|---|
| `invalid_request` | 400 | Unsupported/malformed request; correct fields and retry. |
| `source_too_large` | 413 | Source, duration, envelope, or response exceeds a configured bound; choose a smaller/shorter source. |
| `unsupported_media` | 422 | Missing video, unsupported channel count/codec, or decode failure; reselect or convert the media. |
| `invalid_cut_list` | 422 | Invalid EDL; correct integer source-time ranges and retry before a job is created. |
| `source_changed` | 409 | Project/source identity changed or snapshot verification failed; reselect and analyze again. |
| `analysis_expired` | 410 | Unconsumed session expired or server restarted; reanalyze. |
| `analysis_capacity` | 429 | Two analysis sessions are active; wait for `Retry-After` and retry. |
| `analysis_resource_limit` | 413 | Decoder exceeded its RSS bound; choose a smaller source or fewer channels. |
| `workspace_unavailable` | 507 | Required disk headroom is unavailable; free space and retry. |
| `analysis_failed` | 500 | Unexpected analysis failure; inspect the safe cause and retry once after checking source/workspace. |

`context` contains only safe bounds/values and allowlisted cause codes. It never contains local source paths, tokens, PCM, media bytes, or raw exception text. `side_effects` is `none`, `partial`, `complete`, or `unknown`. Partial upload/analysis files are removed on failure. Unknown outcomes are reconciled before retry; the API never retries them automatically.

Caller-safe `context.cause` values include `unsupported_codec`, `media_read_failed`, `snapshot_missing`, `snapshot_hash_mismatch`, `workspace_capacity`, `probe_timeout`, `ffmpeg_timeout`, `ffmpeg_exit`, and `render_stage_failed`. The UI maps these identifiers to supported recovery text; raw exception strings and trace paths stay in local logs.

## Asynchronous render failures

Once a job is created, a later FFmpeg failure is an asynchronous `render_failed` job outcome, not an HTTP response to the original request. Job status/result reports the stable code, safe cause, recovery instructions, side-effect state, and correlation id. The job-owned source and EDL remain available for retry/restart recovery.

## Lifecycle and limits

- Source size ≤ 2 GiB; source duration ≤ 2 hours; audio channels 1–8.
- At most two unconsumed analysis sessions per local app process, 128 MiB workspace headroom, a 30-minute decode timeout, 720,000 envelope bins and an 8 MiB JSON response. Each process uses an isolated snapshot namespace so cleanup in one worker cannot delete another worker's files. The local app must run as a single worker because the in-memory analysis-token registry is process-local; a multi-worker deployment requires sticky routing/shared session state before it is supported.
- Unconsumed analysis sessions expire after two hours. The source snapshot moves to the job directory on accepted render submission; expiry cleanup never deletes job-owned media.
- Session registry is process-local. A server restart invalidates unconsumed analysis ids; the source must be reanalyzed. Existing jobs retain their own source snapshot and EDL.
- Cancellation, disconnect, timeout, decode failure and render failure terminate tracked processes and clean temporary/partial artifacts.
