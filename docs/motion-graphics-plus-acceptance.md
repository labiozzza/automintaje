# Motion Graphics Plus acceptance

This checklist covers the optional Motion Graphics Plus workflow. Its output is a
new immutable montage job; it must not modify the source montage. Publishing
continues through the existing publication queue and modal.

## Automated checks

| Check | Result |
| --- | --- |
| `python3.9 -m unittest test_motion -v` | Pass (8 tests) |
| `python3.9 -m unittest discover` | Pass (335 tests, 4 skipped) |
| `AUTOMONTAGE_RUN_REMOTION_SMOKE=1 python3.9 -m unittest test_motion_remotion -v` | Pass; transparent overlay composited into a publishable video |
| `npm run typecheck` in `motion/assets/remotion` | Pass |
| `python3.9 -m py_compile app.py motion/*.py test_motion.py` | Pass |
| Import `app` with project Python 3.9 | Pass |
| Parse inline scripts in `templates/index.html` | Pass |
| `git diff --check` | Pass |

## Manual product acceptance

Run these checks against local test media and mock publication providers. Do not
publish to real accounts as part of acceptance.

- [ ] Open Motion from a ready montage; source, transcript/timing and analysis
  remain tied to the selected source job.
- [ ] Confirm the source snapshot survives deletion/cleanup of the source job
  and rejects a changed fingerprint.
- [ ] Select an available OpenCode model, generate a plan, and verify progress,
  cancel, session recovery and actionable errors.
- [ ] Review scenes individually; accept, reject and revise a scene. Confirm
  revision history is preserved and edits to a source montage do not occur.
- [ ] Preview accepted scenes, render an approved revision, reload the app and
  confirm the output job and video are recoverable.
- [ ] Open publish from Motion and confirm it uses the shared modal, the correct
  video, per-job draft and idempotency state, and normal queue preparation.
- [ ] Request a mirrored version when the immutable source inputs are present;
  confirm it is built from a mirrored base montage with the approved Motion
  plan reapplied. Confirm unavailable inputs produce a clear explanation.
- [ ] Verify ordinary montage preview, publishing, and mirror behavior remain
  unchanged for non-Motion jobs.

## Current limits

- The automated renderer smoke test uses local generated media. A full
  real-media Motion Director session and interactive browser pass have not yet
  been run.
- HDR sources are rejected by the current Motion renderer; SDR is supported.
- The installed Remotion dependency tree reports a `zod` version warning
  (installed `4.6.7`, expected `4.5.4`) despite the package override.
