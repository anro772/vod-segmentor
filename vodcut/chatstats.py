"""Per-game chat analysis from the chat replay we already download.

work/<VODID>/chat.json is fetched by chat.py for the overlay render and then
never read again — but it is 30k timestamped audience reactions, and message
rate spikes mark exactly the moments worth talking about. That gives us, for
free, the timestamps to aim the transcriber at.

Chat *text* is only ever used in aggregate (top terms) as a hint about mood.
It is never quoted into a title: chat is spoiler-happy ("gg", "throw") and
frequently toxic. What was actually said comes from the transcript.
"""
import json
import re
from collections import Counter
from pathlib import Path

import ijson

# Emote-name and chat-slang noise that says nothing about *this* game.
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "it", "its", "to", "of", "in",
    "for", "on", "at", "he", "she", "they", "him", "her", "his", "you", "your",
    "i", "im", "me", "my", "we", "this", "that", "was", "are", "be", "so", "if",
    "not", "no", "yes", "just", "like", "what", "why", "how", "who", "when",
    "can", "will", "do", "does", "did", "get", "got", "go", "one", "all", "up",
    "out", "now", "then", "there", "here", "have", "has", "had", "with", "from",
    "baus", "bausen", "thebausffs", "lol", "xd", "lmao",
}

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,23}")


def _iter_comments(chat_json: Path):
    with open(chat_json, "rb") as f:
        for c in ijson.items(f, "comments.item"):
            off = c.get("content_offset_seconds")
            body = (c.get("message") or {}).get("body") or ""
            if off is not None:
                yield int(off), body


def _merge_adjacent(buckets: list[tuple[int, int]], bucket_sec: int) -> list[tuple[int, int]]:
    """Collapse neighbouring hot buckets into one moment.

    Real hype lasts longer than one bucket — the raw data shows t=15830 and
    t=15840 firing together — so without this the top-N is three slices of the
    same twenty seconds instead of three different moments.
    """
    merged: list[list[int]] = []
    for t, n in sorted(buckets):
        if merged and t - merged[-1][1] <= bucket_sec:
            merged[-1][1] = t
            merged[-1][2] += n
        else:
            merged.append([t, t, n])
    return [(a + (b - a) // 2, n) for a, b, n in merged]


def compute(chat_json: Path, segments: list[dict], cfg: dict) -> dict[int, dict]:
    """{segment index -> {spikes, top_terms, hype_score, messages}}.

    One pass over the file (~0.2s for 118MB with ijson's C backend).
    """
    ccfg = cfg.get("chat_stats", {})
    bucket = int(ccfg.get("bucket_sec", 10))
    min_ratio = float(ccfg.get("min_spike_ratio", 3.0))
    max_spikes = int(ccfg.get("max_spikes", 5))

    spans = {s["index"]: (s["start_sec"], s["end_sec"]) for s in segments}
    counts: dict[int, Counter] = {i: Counter() for i in spans}
    words: dict[int, list[tuple[int, str]]] = {i: [] for i in spans}
    total = 0

    for off, body in _iter_comments(chat_json):
        total += 1
        for idx, (a, b) in spans.items():
            if a <= off <= b:
                counts[idx][off // bucket] += 1
                if body:
                    words[idx].append((off, body))
                break

    vod_rate = total / max(max(b for _, b in spans.values()), 1)
    out: dict[int, dict] = {}
    for idx, (a, b) in spans.items():
        c = counts[idx]
        keys = range(a // bucket, b // bucket + 1)
        series = [c.get(k, 0) for k in keys]
        if not series:
            out[idx] = {"spikes": [], "top_terms": [], "hype_score": 0.0, "messages": 0}
            continue
        median = max(sorted(series)[len(series) // 2], 1)
        hot = [(k * bucket, c[k]) for k in keys if c.get(k, 0) >= median * min_ratio]
        spikes = sorted(_merge_adjacent(hot, bucket), key=lambda x: -x[1])[:max_spikes]

        # terms are drawn from inside the spike windows only — that is where
        # the reaction is, the rest of the game is baseline chatter
        hot_words: Counter = Counter()
        for t, msg in words[idx]:
            if any(abs(t - st) <= bucket * 3 for st, _ in spikes):
                for tok in _TOKEN.findall(msg):
                    if tok.lower() not in STOPWORDS and len(tok) > 2:
                        hot_words[tok] += 1

        msgs = sum(series)
        out[idx] = {
            "spikes": [{"t": t, "count": n, "ratio": round(n / median, 1)}
                       for t, n in spikes],
            "top_terms": [{"term": w, "n": n} for w, n in hot_words.most_common(12)],
            "hype_score": round((msgs / max(b - a, 1)) / max(vod_rate, 0.001), 2),
            "messages": msgs,
        }
    return out


def ensure(cfg: dict, workdir: str, segments: list[dict], refresh: bool = False) -> dict[int, dict]:
    """Cached wrapper — chat_stats.json next to the chat replay."""
    wd = Path(workdir)
    cache = wd / "chat_stats.json"
    if cache.exists() and not refresh:
        return {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    chat_json = wd / "chat.json"
    if not chat_json.exists():
        print("[chatstats] no chat.json — skipping chat signals")
        return {}
    stats = compute(chat_json, segments, cfg)
    cache.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(f"[chatstats] {len(stats)} games analysed -> {cache.name}")
    return stats
