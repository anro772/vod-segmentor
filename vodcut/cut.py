import json
import subprocess
from pathlib import Path


def cut_one(seg: dict, source: str | Path, out: Path, encode_cfg: dict,
            chat: Path | None = None, chat_pos: tuple[int, int] = (365, 783)) -> Path:
    if encode_cfg["encoder"] == "amf":
            qp = str(encode_cfg.get("qp", 24))
            vcodec = ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                      "-qp_i", qp, "-qp_p", qp]
    else:
        vcodec = ["-c:v", "libx264", "-preset", encode_cfg["preset"],
                  "-crf", str(encode_cfg["crf"])]
    span = ["-ss", str(seg["start_sec"]), "-to", str(seg["end_sec"])]
    if chat:
        # same -ss/-to on the chat render keeps it frame-synced to the game
        inputs = [*span, "-i", str(source), *span, "-i", str(chat),
                  "-filter_complex",
                  f"[0:v][1:v]overlay={chat_pos[0]}:{chat_pos[1]}[v]",
                  "-map", "[v]", "-map", "0:a"]
    else:
        inputs = [*span, "-i", str(source)]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
           *inputs,
           *vcodec, "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", str(out)]
    print(f"[cut] game {seg['index']}: {seg['start_tc']} -> {seg['end_tc']} -> {out.name}")
    subprocess.run(cmd, check=True)
    return out


def cut_segments(segments_path: str | Path, source: str | Path, output_dir: str,
                 encode_cfg: dict, only: list[int] | None = None,
                 chat: Path | None = None,
                 chat_pos: tuple[int, int] = (365, 783)) -> list[Path]:
    manifest = json.loads(Path(segments_path).read_text())
    vod_id = manifest["vod_id"]
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for seg in manifest["segments"]:
        if only and seg["index"] not in only:
            continue
        out = out_dir / f"game_{seg['index']:02d}_{vod_id}_{seg['start_tc']}.mp4"
        done.append(cut_one(seg, source, out, encode_cfg, chat, chat_pos))
    return done
