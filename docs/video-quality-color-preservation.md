# Color preservation in the montage video pipeline

## Root cause

The color shift came from mismatched pixel conversions and labels, not from bitrate:

1. OpenCV's `VideoCapture` decoded tagged BT.709 frames to BGR using its implicit FFmpeg conversion path; this did not consistently honor the stream matrix/range.
2. OpenCV then sent full-range BGR to FFmpeg as rawvideo. The encoder's implicit RGB→YUV conversion used a default matrix/range, while the output was sometimes labeled BT.709/limited. The labels could therefore disagree with the encoded pixels. The issue also affected full-range input.
3. Instagram, YouTube and Telegram remux paths applied H.264/HEVC metadata bitstream filters that replaced the source VUI with BT.709/limited despite making no pixel conversion.
4. The HDR tone-map filter converted the matrix but did not explicitly convert the BT.2020 primaries to BT.709.

## Changes

- Added `montage/color_pipeline.py` to probe pixel format, bit depth, primaries, transfer, matrix, range, profile/level and HDR side data. Unknown source labels remain unknown; SD/HD raster conventions are used only to choose a necessary pixel-conversion matrix, not to invent primaries/transfer tags.
- Replaced OpenCV's implicit video decode in the zoom, subtitle and timeline-animation renderers with FFmpeg BGR decode using the probed matrix/range. Existing OpenCV frame transformations and their settings are unchanged.
- Every OpenCV→FFmpeg path now explicitly converts full-range BGR to limited-range YUV using the selected matrix, then labels the resulting YUV representation accurately. Full-range inputs are pixel-converted to limited range; they are not simply relabeled.
- Stream-copy/remux no longer writes color metadata. The source VUI and HDR side data are left intact.
- SDR non-BT.709 platform re-encodes use zscale to convert pixels before writing BT.709 labels. HDR PQ/HLG conversion explicitly reads BT.2020 primaries/matrix and writes BT.709 primaries, transfer, matrix and limited range after tone mapping. HDR metadata reaching an SDR finalize or platform encode path is rejected.
- Cache profile is now `b3-3-color-preserving`, invalidating prepared videos generated with the previous color handling.

## Before/after measurements

RGB comparisons use decoded frames in a common BGR representation, without subtitles, overlays, zoom or geometry changes. The color-chart fixtures are 720×1280 and contain saturated patches plus mid/dark gray. `MAE` is mean absolute difference per channel, 0–255.

| Input | Previous implicit path | Corrected path | Difference reduction |
|---|---:|---:|---:|
| SDR BT.709 limited chart | 5.794 MAE | 0.274 MAE | 95.3% |
| SDR BT.709 full-range chart | 7.215 MAE | 0.621 MAE | 91.4% |
| User source, `IMG_9072.mov`, first 12 frames after orientation normalization | 2.928 MAE | 0.026 MAE | 99.1% |
| Synthetic 10-bit PQ chart, after tone-map then OpenCV round-trip | — | 0.168 MAE against the tone-mapped SDR reference | — |

The user source reports H.265, 8-bit `yuv420p`, BT.709 primaries/transfer/matrix and limited range. No camera-captured HDR source was present in the workspace. Synthetic PQ and HLG test clips exercised the HDR conversion path; both yielded SDR BT.709 outputs. Their tone-map aesthetic is not claimed to match real camera HDR.

### Saved comparable frames

These chart frames show source and corrected output in the same decoded RGB space; HDR frames are both after PQ→SDR tone mapping, before/after the OpenCV round-trip.

- SDR BT.709 limited: [before](video-quality-color-acceptance/bt709-limited-before.png) · [after](video-quality-color-acceptance/bt709-limited-after.png)
- SDR BT.709 full range: [before](video-quality-color-acceptance/bt709-full-range-before.png) · [after](video-quality-color-acceptance/bt709-full-range-after.png)
- Synthetic PQ→BT.709: [before OpenCV](video-quality-color-acceptance/pq-to-bt709-before-opencv.png) · [after OpenCV](video-quality-color-acceptance/pq-to-bt709-after-opencv.png)

No actual user frames were checked into the repository; the private user-source measurement is recorded above.

## Verification

- Targeted: `python3.9 -m unittest test_video_color_pipeline test_montage_platform_export test_publish_prepare test_subtitle_highlighting test_montage_unit` — **34 passed**.
- Color tests verify limited/full-range round trips, full-range remux metadata preservation, truthful handling of unknown tags, SDR BT.2020 pixel conversion, and synthetic PQ/HLG tone mapping.
- General suite: `python3.9 -m unittest discover -p 'test_*.py'` — **242 run, 239 passed, 3 skipped**.
- No real publishing, merge, zoom-setting, subtitle-logic, animation-logic or content-factory changes were made.
