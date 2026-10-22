"""Isolated experiment: measure SSIM after N encode passes (Stage A)."""
import json, subprocess, tempfile, os, shutil, re
from pathlib import Path

src = Path("montage_work/9a7cf622/project_video.MOV")
if not src.exists():
    raise SystemExit("sample video missing")
tmp = Path(tempfile.mkdtemp(prefix="exp_passes_"))
ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
cur = src
results = {}
for n in range(1, 6):
    out = tmp / f"pass_{n}.mp4"
    cmd = [ffmpeg, "-y", "-i", str(cur), "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    m = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(src), "-i", str(out),
                        "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-"],
                       capture_output=True, text=True)
    allv = None
    for line in reversed(m.stderr.splitlines()):
        if "All:" in line:
            mm = re.search(r"All:\s*([0-9.]+)", line)
            if mm: allv = float(mm.group(1)); break
    results[f"{n}_passes"] = {"ssim_all": allv, "size_bytes": out.stat().st_size}
    cur = out
print(json.dumps(results, indent=2))
(tmp / "passes.json").write_text(json.dumps(results, indent=2))
