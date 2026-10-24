# Video quality work — final acceptance report

## Scope delivered

- Platform preparation now probes codec, profile/level, pixel format, dimensions, FPS, container, audio stream and size before choosing a compatible remux or an encode.
- Compatible H.264/HEVC masters are remuxed with stream copy and fast-start metadata. SDR BT.709 VUI is added with the H.264/HEVC metadata bitstream filter without re-encoding.
- Incompatible platform outputs are encoded from the retained montage master. Instagram's size-limit retry also starts from that master, never from an already prepared output.
- Instagram preserves source rates from 23–60 FPS, including fractional rates. Outside that range its CFR fallback selects a supported rate. YouTube and Telegram do not apply an artificial size/bitrate cap beyond Telegram's existing 45 MiB target.
- Prepared-output cache signatures include export profile `b3-2` as well as source identity, invalidating earlier platform files when export behavior changes.
- Re-encoded SDR outputs carry BT.709 primaries, transfer, matrix and limited-range signaling. HDR sources tagged PQ/HLG are tone-mapped to BT.709 before the 8-bit/OpenCV stages; failure to tone-map stops the render rather than continuing with HDR pixels.

## Verification

- Full suite: `python3.9 -m unittest` — **236 run (233 passed, 3 skipped)**.
- Platform-export tests cover IG FPS preservation/clamping, incompatible profile fallback, copy/remux behavior, YouTube HEVC remux, audio compatibility, Telegram probe constraints, cache invalidation, and the Instagram oversize retry using the master as its input.
- Final offline acceptance montage used the existing short vertical fixture with autozoom, word subtitles and the banned-word audio censor enabled. Render completed; the resulting master is 720×1280, H.264, yuv420p, 25/1 FPS, BT.709 matrix, limited range. `silencedetect` found the expected censor interval at **7.8722–7.9410 s** (68.8 ms).
- Instagram, YouTube and Telegram preparation completed from that master without any real publication. Final outputs were respectively **4.13 MB**, **5.31 MB**, and **42.35 MB**. All three are MP4, 720×1280, 25/1 FPS, H.264 High/yuv420p + AAC, with BT.709 primaries/transfer/matrix and limited range. IG and YouTube encode paths use 48 kHz audio; Telegram preserves the source 44.1 kHz AAC audio.
- SSIM against the retained master: Instagram **0.9910**, YouTube **0.9928**, Telegram **0.9998**. Telegram used its existing size-target encode because the master advertises the nonstandard H.264 High 4:4:4 Predictive profile despite yuv420p; IG and YouTube likewise re-encoded to the standard High profile.
- Fast-path remux smoke tests with a standard compatible 25 FPS H.264/AAC source and an HEVC YouTube source preserved their video streams. Synthetic HDR-tagged input exercised the PQ detection and tone-map route; the generated SDR intermediate is BT.709 H.264/yuv420p. This is wiring verification only: no camera-captured HDR sample was available, so HDR visual quality is **not claimed as verified**.
- `yuv420p` remains the compatibility default. `yuv444p` was not promoted: this acceptance did not produce comparative measurements justifying a default change.

## Manual test

1. Open the local web app at **http://127.0.0.1:8000**.
2. Upload a vertical MP4/MOV and select autozoom, word subtitles, and banned-word muting.
3. Run the montage and download the result. Confirm the subtitle placement, zoom framing, and muted phrase.
4. Use **Prepare for Instagram**, **Prepare for YouTube**, and **Prepare for Telegram**. Inspect each local file's duration, FPS, codecs, audio sample rate, and BT.709 tags with `ffprobe`; do not press a publish action for this local acceptance.

## Limits

- No upload or publication was performed.
- HDR visual tone mapping remains unverified against camera-captured PQ/HLG footage.
- Variable-frame-rate camera footage was not part of this acceptance fixture. Copy/remux paths preserve input timestamps; encode paths normalize CFR at the probed source rate and resample audio for synchronization.
