"""Twitch chat overlay via TwitchDownloaderCLI: download the chat replay
once per VOD, render it once as a compact h264 pair (picture + grayscale
alpha mask via --generate-mask), and let cut_one recombine them with
ffmpeg's alphamerge at cut time. This is ~100x smaller than a ProRes-alpha
render at identical visual quality. Chat timestamps are VOD-relative, so
applying the same -ss/-to to both files keeps every cut perfectly in sync."""
import subprocess
from pathlib import Path

from vodcut.config import ROOT

TDCLI = ROOT / "tools" / "TwitchDownloaderCLI" / "TwitchDownloaderCLI.exe"
TD_FFMPEG = ROOT / "tools" / "TwitchDownloaderCLI" / "ffmpeg.exe"


def ensure_chat(cfg: dict, vod_id: str, workdir: str) -> tuple[Path, Path] | None:
    """Returns (chat.mp4, chat_mask.mp4) for the full VOD, creating if needed."""
    ccfg = cfg["chat"]
    if not ccfg.get("enabled", True):
        return None
    if not TDCLI.exists():
        print("[chat] TwitchDownloaderCLI not found, skipping chat overlay")
        return None
    wd = Path(workdir)
    chat_json = wd / "chat.json"
    chat_mp4 = wd / "chat.mp4"
    chat_mask = wd / "chat_mask.mp4"
    if not chat_json.exists():
        print("[chat] downloading chat replay...")
        subprocess.run([str(TDCLI), "chatdownload", "--id", vod_id,
                        "-o", str(chat_json), "-E", "--collision", "overwrite"],
                       check=True)
    if not (chat_mp4.exists() and chat_mask.exists()):
        print("[chat] rendering chat overlay + alpha mask (full VOD)...")
        subprocess.run([str(TDCLI), "chatrender", "-i", str(chat_json),
                        "-o", str(chat_mp4),
                        "--ffmpeg-path", str(TD_FFMPEG),
                        "-w", str(ccfg["width"]), "-h", str(ccfg["height"]),
                        "--font-size", str(ccfg["font_size"]),
                        "--background-color", "#00000000",
                        "--outline", "--outline-size", str(ccfg["outline_size"]),
                        "--framerate", "30", "--generate-mask",
                        "--collision", "overwrite"],
                       check=True)
    # clean up the giant ProRes render from the previous pipeline version
    old = wd / "chat.mov"
    if old.exists():
        print(f"[chat] removing obsolete {old.name} "
              f"({old.stat().st_size / 2**30:.1f} GB)")
        old.unlink()
    return chat_mp4, chat_mask
