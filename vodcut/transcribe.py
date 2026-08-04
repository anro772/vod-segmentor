"""What Baus actually said, per game, via faster-whisper.

Stats tell you a game had 24k turret damage; they cannot tell you he said
"I'd never thought I'd say this". Titles that sound like a person wrote them
need the speech, so this is the one stage that reads the audio.

Cost control: `mode: spikes` transcribes only a window around each chat hype
peak (~6 min per game instead of ~30) because chatstats already knows where
the interesting moments are. `mode: full` does the whole game when that turns
out to be affordable — see the benchmark in the plan.

Everything is cached per game and resumable; audio never touches the video
stream (`-vn`), so slicing a 15GB source is cheap.
"""
import json
import subprocess
import tempfile
import time
from pathlib import Path

# Bias the decoder toward the vocabulary of this specific stream. Whisper is
# markedly better at accented English and jargon when primed like this.
GLOSSARY = (
    "League of Legends stream commentary. Baus, thebausffs, inting, int, "
    "Sion, Irelia, Jax, Skarner, Volibear, Vladimir, Anivia, Jayce, Darius, "
    "top lane, teleport, baron, dragon, herald, turret plates, gank, roam, "
    "splitpush, flash, ult, ward, minions, jungler, lethal tempo, tower dive."
)

_MODEL = None


def _model(cfg: dict):
    """Load once — model init costs far more than a single transcription."""
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        t = cfg.get("transcribe", {})
        name = t.get("model", "small.en")
        print(f"[transcribe] loading faster-whisper '{name}' "
              f"({t.get('compute_type', 'int8')}, {t.get('threads', 8)} threads)...")
        _MODEL = WhisperModel(name, device=t.get("device", "cpu"),
                              compute_type=t.get("compute_type", "int8"),
                              cpu_threads=int(t.get("threads", 8)))
    return _MODEL


def _windows(seg: dict, spikes: list[dict], cfg: dict) -> list[tuple[int, int]]:
    """VOD-relative [start, end) spans of audio worth transcribing."""
    t = cfg.get("transcribe", {})
    mode = t.get("mode", "spikes")
    a, b = int(seg["start_sec"]), int(seg["end_sec"])
    if mode == "full" or not spikes:
        if mode != "full":
            # no chat signal — sample the back half, where fights live
            mid = a + (b - a) // 2
            step = max((b - mid) // 3, 60)
            raw = [(x, x + 120) for x in range(mid, b, step)][:3]
        else:
            return [(a, b)]
    else:
        pad = int(t.get("spike_pad_sec", 60))
        raw = [(s["t"] - pad, s["t"] + pad) for s in spikes]

    spans = []
    for lo, hi in sorted(raw):
        lo, hi = max(lo, a), min(hi, b)
        if hi - lo < 10:
            continue
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])
    return [(int(x), int(y)) for x, y in spans]


def _extract_audio(source: Path, start: int, end: int, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-ss", str(start), "-t", str(end - start), "-i", str(source),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)],
        check=True)


def transcribe_game(cfg: dict, source: Path, seg: dict,
                    spikes: list[dict]) -> list[dict]:
    """[{start, end, text}] in VOD-relative seconds."""
    spans = _windows(seg, spikes, cfg)
    audio_sec = sum(e - s for s, e in spans)
    model = _model(cfg)
    out: list[dict] = []
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "clip.wav"
        for lo, hi in spans:
            _extract_audio(source, lo, hi, wav)
            segments, _ = model.transcribe(
                str(wav), language="en", initial_prompt=GLOSSARY,
                vad_filter=True, condition_on_previous_text=False)
            for s in segments:
                text = s.text.strip()
                if text:
                    out.append({"start": round(lo + s.start, 1),
                                "end": round(lo + s.end, 1), "text": text})
    elapsed = max(time.time() - t0, 0.01)
    print(f"[transcribe] game {seg['index']:02d}: {audio_sec}s audio in "
          f"{elapsed:.0f}s ({audio_sec / elapsed:.1f}x realtime), "
          f"{len(out)} lines")
    return out


def ensure(cfg: dict, workdir: str, source: Path, segments: list[dict],
           chat_stats: dict, only: list[int] | None = None) -> dict[int, list[dict]]:
    """Cached per-game transcripts. Resumable: cached games are skipped."""
    tcfg = cfg.get("transcribe", {})
    out_dir = Path(workdir) / "transcripts"
    result: dict[int, list[dict]] = {}
    if not tcfg.get("enabled", True):
        return result

    pending = [s for s in segments if not only or s["index"] in only]
    missing = [s for s in pending
               if not (out_dir / f"game_{s['index']:02d}.json").exists()]
    if missing and not source.exists():
        print(f"[transcribe] {source} missing — skipping speech for "
              f"{len(missing)} game(s)")

    for seg in pending:
        cache = out_dir / f"game_{seg['index']:02d}.json"
        if cache.exists():
            result[seg["index"]] = json.loads(cache.read_text(encoding="utf-8"))
            continue
        if not source.exists():
            continue
        spikes = (chat_stats.get(seg["index"]) or {}).get("spikes", [])
        lines = transcribe_game(cfg, source, seg, spikes)
        out_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(lines, indent=2), encoding="utf-8")
        result[seg["index"]] = lines
    return result
