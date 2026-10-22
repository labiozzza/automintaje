# Stage A diagnostic artifacts

- `experiment_pixfmt.py` — isolated comparison of yuv420p vs yuv444p at CRF 18 (SSIM vs source).
- `experiment_passes.py` — isolated measurement of SSIM degradation across 1..5 encode passes.
- `qa_probe_example.json` — sample ffprobe output from `montage.quality_audit`.
- `qa_metric_example.json` — sample PSNR/SSIM output.

Results (2026-10-22, source: montage_work/9a7cf622/project_video.MOV, HEVC yuv420p 1280x720 30fps):

pix_fmt comparison (CRF 18):
- yuv420p: SSIM 0.983193, 9.93 MB
- yuv444p: SSIM 0.989768, 10.00 MB

Multi-pass accumulation (CRF 18, yuv420p):
- 1 pass: 0.983193
- 2 passes: 0.978091
- 3 passes: 0.974547
- 4 passes: 0.971681
- 5 passes: 0.969272
