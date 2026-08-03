"""Twitch chat overlay via TwitchDownloaderCLI: download the chat replay
once per VOD, render it once as a transparent (ProRes 4444) strip, and let
cut_one composite it. Chat timestamps are VOD-relative, so applying the same
-ss/-to to the chat render keeps it perfectly in sync with every cut."""
import subprocess
from pathlib import Path

from vodcut.config import ROOT

TDCLI = ROOT / "tools" / "TwitchDownloaderCLI" / "TwitchDownloaderCLI.exe"
TD_FFMPEG = ROOT / "tools" / "TwitchDownloaderCLI" / "ffmpeg.exe"


def ensure_chat(cfg: dict, vod_id: str, workdir: str) -> Path | None:
    """Returns the full-VOD transparent chat render, creating it if needed."""
    ccfg = cfg["chat"]
    if not ccfg.get("enabled", True):
        return None
    if not TDCLI.exists():
        print("[chat] TwitchDownloaderCLI not found, skipping chat overlay")
        return None
    wd = Path(workdir)
    chat_json = wd / "chat.json"
    chat_mov = wd / "chat.mov"
    if not chat_json.exists():
        print("[chat] downloading chat replay...")
        subprocess.run([str(TDCLI), "chatdownload", "--id", vod_id,
                        "-o", str(chat_json), "-E", "--collision", "overwrite"],
                       check=True)
    if not chat_mov.exists():
        print("[chat] rendering transparent chat overlay (full VOD)...")
        subprocess.run([str(TDCLI), "chatrender", "-i", str(chat_json),
                        "-o", str(chat_mov),
                        "--ffmpeg-path", str(TD_FFMPEG),
                        "-w", str(ccfg["width"]), "-h", str(ccfg["height"]),
                        "--font-size", str(ccfg["font_size"]),
                        "--background-color", "#00000000",
                        "--outline", "--outline-size", str(ccfg["outline_size"]),
                        "--framerate", "30", "--collision", "overwrite",
                        '--output-args=-c:v prores_ks -profile:v 4444 '
                        '-pix_fmt yuva444p10le "{save_path}"'],
                       check=True)
    return chat_mov
