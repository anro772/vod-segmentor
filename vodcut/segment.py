"""State machine: classification timeline -> game segments."""
import json
import shutil
from pathlib import Path


def _tc(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}-{sec % 3600 // 60:02d}-{sec % 60:02d}"


def _debounced_runs(samples: list[dict], min_consecutive: int, gap_sec: float) -> list[dict]:
    """Contiguous IN_GAME runs, merging across OTHER gaps <= gap_sec."""
    runs = []
    cur = None
    streak = 0
    for s in samples:
        if s["state"] == "IN_GAME":
            streak += 1
            if streak >= min_consecutive:
                start_t = s["time"] if streak == min_consecutive else None
                if cur is None:
                    # backdate run start to the first sample of the streak
                    cur = {"start": s["time"] - (min_consecutive - 1) * _interval(samples),
                           "end": s["time"]}
                else:
                    cur["end"] = s["time"]
        else:
            streak = 0
            if cur is not None and s["time"] - cur["end"] > gap_sec:
                runs.append(cur)
                cur = None
    if cur is not None:
        runs.append(cur)
    return runs


def _interval(samples: list[dict]) -> int:
    return samples[1]["time"] - samples[0]["time"] if len(samples) > 1 else 5


def build_segments(samples: list[dict], cfg: dict, frames_dir: str | Path,
                   output_dir: str, vod_id: str) -> list[dict]:
    scfg = cfg["segment"]
    runs = _debounced_runs(samples, scfg["min_consecutive"], scfg["max_ingame_gap_sec"])

    segments = []
    prev_end = 0.0
    for run in runs:
        dur_min = (run["end"] - run["start"]) / 60
        if dur_min < scfg["min_game_minutes"]:
            print(f"[segment] dropping short IN_GAME run at {_tc(run['start'])} ({dur_min:.1f} min)")
            continue

        # start: earliest champ select / loading sample in the lookback window,
        # after the previous game ended
        lb_from = max(prev_end, run["start"] - scfg["start_lookback_sec"])
        start_states = {"champ_select": ("CHAMP_SELECT", "LOADING"),
                        "loading": ("LOADING",)}[scfg["start_marker"]]
        pre = [s for s in samples
               if lb_from <= s["time"] < run["start"] and s["state"] in start_states]
        if pre:
            start = pre[0]["time"]
            start_via = pre[0]["state"]
        else:
            start = run["start"] - scfg["start_backoff_sec"]
            start_via = "IN_GAME-backoff"

        # end: first ENDGAME sample within gap window after run end
        post = [s for s in samples
                if run["end"] < s["time"] <= run["end"] + scfg["max_ingame_gap_sec"] * 2
                and s["state"] == "ENDGAME"]
        if post:
            end = post[-1]["time"]
            end_via = "ENDGAME"
            sc = post[0]["scores"]
            result = "WIN" if sc.get("eg_victory", 0) > sc.get("eg_defeat", 0) else "LOSS"
        else:
            end = run["end"]
            end_via = "last-IN_GAME (ENDGAME missed)"
            result = "UNKNOWN"
            print(f"[segment] WARNING: no ENDGAME after run ending {_tc(run['end'])}, using fallback")

        start = max(prev_end, start - scfg["lead_pad_sec"])
        end = end + scfg["tail_pad_sec"]
        prev_end = end

        segments.append({
            "index": len(segments) + 1,
            "start_sec": start, "end_sec": end,
            "start_tc": _tc(start), "end_tc": _tc(end),
            "ingame_minutes": round(dur_min, 1),
            "result": result, "start_via": start_via, "end_via": end_via,
        })

    # preview frames at each boundary
    out = Path(output_dir)
    prev_dir = out / "previews"
    prev_dir.mkdir(parents=True, exist_ok=True)
    interval = _interval(samples)
    frames_dir = Path(frames_dir)
    for seg in segments:
        for which in ("start", "end"):
            idx = int(seg[f"{which}_sec"] // interval) + 1
            src = frames_dir / f"{idx:06d}.jpg"
            dst = prev_dir / f"game_{seg['index']:02d}_{which}.jpg"
            if src.exists():
                shutil.copy(src, dst)
                seg[f"{which}_preview"] = str(dst)

    manifest = {"vod_id": vod_id, "segments": segments}
    out.mkdir(parents=True, exist_ok=True)
    (out / "segments.json").write_text(json.dumps(manifest, indent=2))

    print(f"\n{'#':>2} {'start':>9} {'end':>9} {'in-game':>8} {'result':>8}  via")
    for s in segments:
        print(f"{s['index']:>2} {s['start_tc']:>9} {s['end_tc']:>9} "
              f"{s['ingame_minutes']:>6}m {s['result']:>8}  {s['start_via']} -> {s['end_via']}")
    print(f"\n[segment] wrote {out / 'segments.json'}")
    return segments
