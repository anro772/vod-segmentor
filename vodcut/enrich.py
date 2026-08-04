"""Phase 2 orchestration: attach Riot facts, chat signals and speech to each
detected segment, then title it.

The heavy lifting moved out to focused modules — riot.py (facts),
chatstats.py (audience reaction), transcribe.py (what he said) and titles.py
(generation + validation). This file just wires them together and stays the
single entry point the CLI calls.

Pure add-on: reads output/segments.json, never touches the CV pipeline.
Alt-account games simply fail to match and are logged + skipped.
"""
import json
from pathlib import Path

from vodcut import chatstats, riot, titles, transcribe
from vodcut.riot import ExpiredKeyError, vod_start_epoch  # re-exported

__all__ = ["enrich_segments", "fun_fact", "ExpiredKeyError", "vod_start_epoch"]

MULTIKILL = {2: "DOUBLE KILL", 3: "TRIPLE KILL", 4: "QUADRA KILL", 5: "PENTAKILL"}


def fun_fact(m: dict, used: set[str] | None = None) -> str | None:
    """One short non-spoiler note for the thumbnail (no result, no KDA).

    Draws on the expanded Riot facts so that games without a headline stat stop
    all landing on the same "NNK DMG DEALT" line.
    """
    used = set() if used is None else used
    ch = m.get("challenges") or {}
    opp = m.get("opponent")
    cands: list[str] = []

    if m["damage_turrets"] >= 15000:
        cands.append(f"{round(m['damage_turrets'] / 1000)}K TURRET DMG")
    if ch.get("soloKills", 0) >= 3:
        cands.append(f"{ch['soloKills']} SOLO KILLS")
    if m.get("largestMultiKill", 0) >= 3:
        cands.append(MULTIKILL[m["largestMultiKill"]])
    if ch.get("turretPlatesTaken", 0) >= 5:
        cands.append(f"{ch['turretPlatesTaken']} PLATES TAKEN")
    if m.get("largestKillingSpree", 0) >= 6:
        cands.append(f"{m['largestKillingSpree']} KILL SPREE")
    if ch.get("outnumberedKills", 0) >= 3:
        cands.append(f"{ch['outnumberedKills']} OUTNUMBERED KILLS")
    if ch.get("survivedSingleDigitHpCount", 0) >= 2:
        cands.append("SURVIVED ON 1 HP")
    if m["duration_sec"] >= 38 * 60:
        cands.append(f"{m['duration_sec'] // 60} MINUTE GAME")
    if m["deaths"] >= 12:
        cands.append(f"{m['deaths']} DEATHS")
    if m["kills"] >= 12:
        cands.append(f"{m['kills']} KILLS")
    if opp:
        cands.append(f"VS {opp['champion'].upper()}")
    cands.append(f"{round(m['damage_champs'] / 1000)}K DMG DEALT")

    for c in cands:
        if c not in used:
            used.add(c)
            return c
    return cands[0]


def _vod_t0(manifest: dict, vod_id: str, vod_url: str | None) -> int | None:
    """Broadcast start epoch, cached in the manifest so `retitle` works offline."""
    if manifest.get("vod_start_epoch"):
        return int(manifest["vod_start_epoch"])
    try:
        return vod_start_epoch(vod_url or f"https://www.twitch.tv/videos/{vod_id}")
    except Exception as e:  # VOD expired, offline, yt-dlp failure
        print(f"[enrich] could not read VOD start time ({e})")
        # recover from a previous enrichment: game start minus its offset,
        # taking the minimum since champ select precedes the match itself
        est = [s["riot"]["start_epoch"] - s["start_sec"]
               for s in manifest["segments"] if s.get("riot")]
        if est:
            print("[enrich] estimating VOD start from previously matched games")
            return min(est)
        return None


def enrich_segments(cfg: dict, vod_id: str, vod_url: str | None = None,
                    only: list[int] | None = None) -> dict:
    out_dir = Path(cfg["paths"]["output_dir"])
    workdir = cfg["paths"]["workdir"]
    manifest = json.loads((out_dir / "segments.json").read_text(encoding="utf-8"))
    segs = manifest["segments"]

    vod_t0 = _vod_t0(manifest, vod_id, vod_url)
    if vod_t0:
        manifest["vod_start_epoch"] = vod_t0
        print(f"[enrich] vod started at epoch {vod_t0}")

    # --- facts -------------------------------------------------------------
    assigned: dict[int, dict] = {}
    if vod_t0:
        last_end = max(s["end_sec"] for s in segs)
        try:
            matches = riot.fetch_matches(cfg, workdir, vod_t0, vod_t0 + int(last_end))
            print(f"[enrich] {len(matches)} riot matches in window")
            assigned = riot.assign_matches(segs, matches, vod_t0,
                                           cfg["riot"]["match_window_min"] * 60)
        except ExpiredKeyError as e:
            print(f"{e} — continuing without enrichment; re-run with a fresh key")

    # --- audience reaction + speech ---------------------------------------
    chat_stats = chatstats.ensure(cfg, workdir, segs)
    transcripts = transcribe.ensure(cfg, workdir, Path(workdir) / "source.mp4",
                                    segs, chat_stats, only)

    # --- titles ------------------------------------------------------------
    champions = riot.champion_names(Path(workdir) / "riot")
    all_history = titles.load_history(cfg)
    # Entries for the games we are regenerating must not be deduped against —
    # otherwise a re-run always collides with its own previous output.
    keep = [h for h in all_history
            if h.get("vod_id") != vod_id or (only and h.get("index") not in only)]
    used = {h["title"] for h in keep if h.get("vod_id") == vod_id}
    used_facts: set[str] = set()   # VOD-wide, so thumbnails vary too
    new_entries: list[dict] = []

    for i, seg in enumerate(segs):
        idx = seg["index"]
        match = assigned.get(i)
        if match is None:
            seg["riot"] = None
            seg.setdefault("title", None)
            print(f"[enrich] game {idx}: no riot match (alt account or non-ranked?) — skipped")
            continue
        seg["riot"] = match
        seg["result"] = "WIN" if match["win"] else "LOSS"
        if only and idx not in only:
            if seg.get("title"):
                used.add(seg["title"])
            if seg.get("fun_fact"):
                used_facts.add(seg["fun_fact"])
            continue
        seg["fun_fact"] = fun_fact(match, used_facts)

        # keep + new_entries, so games earlier in this same VOD are compared
        # against too — otherwise "YOU CANT STOP ME" and "STOP ME IF YOU CAN"
        # both pass, since `used` only catches exact repeats.
        res = titles.make_title(cfg, match, chat_stats.get(idx),
                                transcripts.get(idx), keep + new_entries,
                                used, vod_id, idx, champions)
        seg["title"] = res["title"]
        seg["title_source"] = res["source"]
        seg["quote_used"] = res["quote_used"]
        new_entries.append(res["entry"])
        titles.save_history(cfg, keep + new_entries)  # crash-safe

        tag = "" if res["source"] == "llm" else " [template fallback]"
        print(f"[enrich] game {idx}: {match['champion']} "
              f"{match['kills']}/{match['deaths']}/{match['assists']} -> {res['title']}{tag}")
        if res["quote_used"]:
            print(f"         from quote: \"{res['quote_used'][:70]}\"")

    titles.save_history(cfg, keep + new_entries)
    (out_dir / "segments.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
