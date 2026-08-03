"""Phase 2: match detected segments to Riot Match-V5 games, build titles.

Pure add-on: reads output/segments.json, never touches the CV pipeline.
Alt-account games simply fail to match and are logged + skipped.
"""
import json
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

API = "https://{routing}.api.riotgames.com"


def _get(url: str, key: str):
    req = urllib.request.Request(url, headers={"X-Riot-Token": key,
                                               "User-Agent": "vod-segmentor/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 5))
                print(f"[enrich] rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            if e.code == 403:
                sys.exit("[enrich] 403 from Riot API — the dev key has expired, regenerate it")
            raise
    raise RuntimeError("rate limit retries exhausted")


def vod_start_epoch(url: str) -> int:
    """Broadcast start time of the VOD via yt-dlp metadata (no download)."""
    out = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", url],
                         capture_output=True, text=True, check=True).stdout
    return int(json.loads(out)["timestamp"])


def fetch_matches(cfg: dict, start_epoch: int, end_epoch: int) -> list[dict]:
    r = cfg["riot"]
    key = r["api_key"]
    base = API.format(routing=r["routing"])
    acct = _get(f"{base}/riot/account/v1/accounts/by-riot-id/{r['game_name']}/{r['tag_line']}", key)
    ids = _get(f"{base}/lol/match/v5/matches/by-puuid/{acct['puuid']}/ids"
               f"?startTime={start_epoch - 3600}&endTime={end_epoch + 3600}&count=100", key)
    matches = []
    for mid in ids:
        m = _get(f"{base}/lol/match/v5/matches/{mid}", key)
        info = m["info"]
        me = next(p for p in info["participants"] if p["puuid"] == acct["puuid"])
        matches.append({
            "match_id": mid,
            "start_epoch": info["gameStartTimestamp"] // 1000,
            "duration_sec": info["gameDuration"],
            "queue_id": info["queueId"],
            "champion": me["championName"],
            "win": me["win"],
            "kills": me["kills"], "deaths": me["deaths"], "assists": me["assists"],
            "damage_champs": me["totalDamageDealtToChampions"],
            "damage_turrets": me["damageDealtToTurrets"],
            "cs": me["totalMinionsKilled"] + me["neutralMinionsKilled"],
            "gold": me["goldEarned"],
        })
    return matches


# channel-style title phrases; first matching profile wins, phrase picked
# deterministically per match so re-runs don't shuffle titles.
# IMPORTANT: no spoilers — phrases must never reveal win/loss or exact KDA.
PHRASES = [
    # (predicate, [phrases])
    (lambda m: m["damage_turrets"] >= 15000, [
        "TURRETS ARE OPTIONAL THIS GAME 😈",
        "NOBODY CAN SAVE THESE TOWERS 💀",
        "THE TOWERS PAY THE PRICE AGAIN 😤",
    ]),
    (lambda m: m["deaths"] >= 12, [
        "THIS GAME BREAKS EVERY RULE 😳",
        "THIS GAME IS PURE CHAOS 💀",
        "THIS GAME TESTS EVERY LIMIT 😨",
    ]),
    (lambda m: m["duration_sec"] >= 38 * 60, [
        "THIS GAME IS A 40 MINUTE PRISON 💀",
        "THIS GAME JUST WONT END EVER 🤯",
    ]),
    (lambda m: m["kills"] >= 12, [
        "THIS GAME IS PURE CARRY MODE 😤",
        "THIS GUY IS UNSTOPPABLE TODAY 😳",
    ]),
    (lambda m: m["deaths"] > 0 and (m["kills"] + m["assists"]) / m["deaths"] >= 5, [
        "THIS GUY JUST WONT DIE EVER 🤯",
    ]),
    (lambda m: True, [
        "THIS GAME NEEDS PERFECT DECISIONS 😈",
        "THIS GAME IS OUT OF HIS CONTROL 🤡",
        "THIS GAME GOES DOWN TO THE WIRE 😨",
        "THIS GAME HAS NO SAFE MOMENT 😳",
        "EVERY FIGHT DECIDES EVERYTHING HERE 🤯",
        "THIS GAME IS ON ANOTHER LEVEL 💀",
    ]),
]


def fun_fact(m: dict) -> str | None:
    """One short non-spoiler note for the thumbnail (no result, no KDA)."""
    if m["damage_turrets"] >= 15000:
        return f"{round(m['damage_turrets'] / 1000)}K TURRET DMG"
    if m["duration_sec"] >= 38 * 60:
        return f"{m['duration_sec'] // 60} MINUTE GAME"
    if m["deaths"] >= 12:
        return f"{m['deaths']} DEATHS"
    if m["kills"] >= 12:
        return f"{m['kills']} KILLS"
    return f"{round(m['damage_champs'] / 1000)}K DMG DEALT"


def make_title(m: dict, used: set[str] | None = None) -> str:
    """Channel-style title. Phrases in `used` (earlier games of the same VOD)
    are skipped — collisions rotate through the pool, then fall through to
    the next matching category, so titles within a VOD don't repeat."""
    used = set() if used is None else used
    fallback = None
    for pred, phrases in PHRASES:
        if not pred(m):
            continue
        start = zlib.crc32(m["match_id"].encode()) % len(phrases)
        if fallback is None:
            fallback = phrases[start]
        for i in range(len(phrases)):
            pick = phrases[(start + i) % len(phrases)]
            if pick not in used:
                used.add(pick)
                return f"BAUS {m['champion'].upper()} {pick}"
    # every phrase of every matching pool already used (very long VOD)
    return f"BAUS {m['champion'].upper()} {fallback}"


def enrich_segments(cfg: dict, vod_url: str) -> dict:
    out_dir = Path(cfg["paths"]["output_dir"])
    manifest = json.loads((out_dir / "segments.json").read_text())
    vod_t0 = vod_start_epoch(vod_url)
    print(f"[enrich] vod started at epoch {vod_t0}")
    last_end = max(s["end_sec"] for s in manifest["segments"])
    matches = fetch_matches(cfg, vod_t0, vod_t0 + int(last_end))
    print(f"[enrich] {len(matches)} riot matches in window")

    window = cfg["riot"]["match_window_min"] * 60
    used_phrases: set[str] = set()
    for seg in manifest["segments"]:
        seg_start = vod_t0 + seg["start_sec"]
        best = min(matches, key=lambda m: abs(m["start_epoch"] - seg_start), default=None)
        if best and abs(best["start_epoch"] - seg_start) <= window:
            seg["riot"] = best
            seg["title"] = make_title(best, used_phrases)
            seg["result"] = "WIN" if best["win"] else "LOSS"
            print(f"[enrich] game {seg['index']}: {best['champion']} "
                  f"{best['kills']}/{best['deaths']}/{best['assists']} -> {seg['title']}")
        else:
            seg["riot"] = None
            seg["title"] = None
            print(f"[enrich] game {seg['index']}: no riot match (alt account or non-ranked?) — skipped")

    (out_dir / "segments.json").write_text(json.dumps(manifest, indent=2))
    return manifest
