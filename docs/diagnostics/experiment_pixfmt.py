"""Isolated experiment: compare yuv420p vs yuv444p encoding quality (Stage A).

Encodes the same source with both pix_fmts at CRF 18 and measures SSIM
against the source.  Does NOT modify production code.
"""
import json, subprocess, tempfile, os, shutil
from pathlib import Path

src = Path("montage_work/9a7cf622/project_video.MOV")
if not src.exists():
    raise SystemExit("sample video missing")
tmp = Path(tempfile.mkdtemp(prefix="exp_pixfmt_"))
ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
results = {}
for pix in ("yuv420p", "yuv444p"):
    out = tmp / f"out_{pix}.mp4"
    cmd = [ffmpeg, "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", pix, "-c:a", "copy", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    # measure ssim
    m = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(src), "-i", str(out),
                        "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    allv = None
    for line in reversed(m.stderr.splitlines()):
        if "All:" in line:
            mm = re.search(r"All:\s*([0-9.]+)", line)
            if mm:
                allv = float(mm.group(1)); break
    results[pix] = {"ssim_all": allv, "size_bytes": out.stat().st_size}
print(json.dumps(results, indent=2))
(tmp / "results.json").write_text(json.dumps(results, indent=2))
