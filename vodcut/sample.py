import subprocess
from pathlib import Path


def sample(source: str | Path, workdir: str, interval_sec: int, width: int, height: int) -> Path:
    """Extract one frame every interval_sec at canonical resolution.

    frames/%06d.jpg, 1-indexed by ffmpeg: time = (index - 1) * interval_sec
    (fps=1/N grabs a frame at the start of each N-second bucket).
    """
    frames = Path(workdir) / "frames"
    if frames.exists() and any(frames.glob("*.jpg")):
        print(f"[sample] {frames} already populated, skipping")
        return frames
    frames.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-i", str(source),
        "-vf", f"fps=1/{interval_sec},scale={width}:{height}",
        "-q:v", "3",
        str(frames / "%06d.jpg"),
    ]
    print("[sample]", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return frames
