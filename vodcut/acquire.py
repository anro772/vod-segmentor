import re
import subprocess
import sys
from pathlib import Path


def vod_id_from_url(url: str) -> str:
    m = re.search(r"videos/(\d+)", url)
    return m.group(1) if m else "unknown"


def acquire(url: str, workdir: str) -> Path:
    """Download the VOD once with yt-dlp. Returns path to source.mp4."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    out = wd / "source.mp4"
    if out.exists() and out.stat().st_size > 0:
        print(f"[acquire] {out} already exists, skipping download")
        return out
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "best", "-o", str(out), url]
    print("[acquire]", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out
