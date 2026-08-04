"""Riot Match-V5 client: fetch, cache and distil one game's worth of facts.

Split out of enrich.py so that title generation has something specific to talk
about. The old code read 12 fields; a match doc carries ~200 plus a 125-field
`challenges` block, and the other nine participants (i.e. the lane opponent)
were never looked at, which is why every title sounded the same.

Everything is cached under work/<VODID>/riot/. The dev key expires every 24h,
so a cached VOD must stay re-titleable without any key at all.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://{routing}.api.riotgames.com"


class ExpiredKeyError(RuntimeError):
    """Riot dev key rejected (expired) — enrichment is skipped, cuts proceed."""


class NoCacheError(RuntimeError):
    """Nothing cached and no usable key — caller decides whether that's fatal."""


# Curated story hooks. Deliberately not all 125 challenges: a model handed 125
# numbers picks one at random, a model handed 15 meaningful ones picks the point.
CHALLENGE_KEYS = (
    "soloKills", "quickSoloKills", "outnumberedKills", "killsNearEnemyTurret",
    "survivedSingleDigitHpCount", "skillshotsDodged", "turretPlatesTaken",
    "laneMinionsFirst10Minutes", "maxLevelLeadLaneOpponent",
    "earlyLaningPhaseGoldExpAdvantage", "maxKillDeficit", "killParticipation",
    "damagePerMinute", "takedownsFirstXMinutes", "epicMonsterSteals",
    "hadOpenNexus", "dancedWithRiftHerald",
)

PARTICIPANT_KEYS = (
    "largestKillingSpree", "largestMultiKill", "doubleKills", "tripleKills",
    "quadraKills", "pentaKills", "firstBloodKill", "firstTowerKill",
    "totalTimeSpentDead", "longestTimeSpentLiving", "damageSelfMitigated",
    "totalDamageTaken", "turretKills", "champLevel", "gameEndedInSurrender",
)


DDRAGON = "https://ddragon.leagueoflegends.com"


def champion_names(cache_dir: str | Path) -> set[str]:
    """Every champion name, from Riot's Data Dragon CDN (no API key needed).

    Used to stop a title naming a champion who wasn't in the game, and — via
    fuzzy matching — to catch misspellings like "ORELIA" for Irelia.
    Cached to disk; falls back to an empty set offline, which simply disables
    those two checks rather than breaking titling.
    """
    cache = Path(cache_dir) / "champions.json"
    if cache.exists():
        return set(json.loads(cache.read_text(encoding="utf-8")))
    try:
        with urllib.request.urlopen(f"{DDRAGON}/api/versions.json", timeout=20) as r:
            version = json.loads(r.read())[0]
        with urllib.request.urlopen(
                f"{DDRAGON}/cdn/{version}/data/en_US/champion.json", timeout=20) as r:
            data = json.loads(r.read())["data"]
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError) as e:
        print(f"[riot] could not fetch champion list ({e}); name checks disabled")
        return set()
    names = set()
    for c in data.values():
        names.add(c["id"])    # Belveth
        names.add(c["name"])  # Bel'Veth
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(sorted(names)), encoding="utf-8")
    print(f"[riot] cached {len(names)} champion names (patch {version})")
    return names


def _get(url: str, key: str):
    req = urllib.request.Request(url, headers={"X-Riot-Token": key,
                                               "User-Agent": "vod-segmentor/1.0"})
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 5))
                print(f"[riot] rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                raise ExpiredKeyError(
                    f"[riot] {e.code} from Riot API — the dev key has expired, regenerate it")
            raise
    raise RuntimeError("rate limit retries exhausted")


def vod_start_epoch(url: str) -> int:
    """Broadcast start time of the VOD via yt-dlp metadata (no download)."""
    out = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--no-warnings", url],
                         capture_output=True, text=True, check=True).stdout
    return int(json.loads(out)["timestamp"])


def _cached(path: Path, fetch):
    """Read `path` if present, else call `fetch()` and persist the result."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if fetch is None:
        raise NoCacheError(f"{path.name} not cached and no live key available")
    doc = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return doc


def _lane_opponent(participants: list[dict], me: dict) -> dict | None:
    """The enemy in the same role. Easily the strongest single story hook and
    the one thing the old titler was structurally blind to."""
    pos = me.get("teamPosition") or me.get("individualPosition") or ""
    if not pos or pos == "Invalid":
        return None
    for p in participants:
        if p["teamId"] == me["teamId"]:
            continue
        if (p.get("teamPosition") or p.get("individualPosition") or "") == pos:
            return {
                "champion": p["championName"],
                "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
                "gold": p["goldEarned"],
                "damage_champs": p["totalDamageDealtToChampions"],
                "cs": p["totalMinionsKilled"] + p["neutralMinionsKilled"],
                "gold_diff": me["goldEarned"] - p["goldEarned"],
                "cs_diff": (me["totalMinionsKilled"] + me["neutralMinionsKilled"])
                           - (p["totalMinionsKilled"] + p["neutralMinionsKilled"]),
            }
    return None


def _objectives(info: dict, me: dict) -> dict:
    for t in info.get("teams", []):
        if t.get("teamId") != me["teamId"]:
            continue
        obj = t.get("objectives", {})
        return {k: {"first": v.get("first"), "kills": v.get("kills")}
                for k, v in obj.items() if v.get("kills") or v.get("first")}
    return {}


def _timeline_facts(tl: dict, my_pid: int, opp_pid: int | None) -> dict:
    """Gold curve vs the lane opponent + first blood time. This is what makes
    arc-shaped facts ("down 4k at 15") possible at all."""
    frames = tl.get("info", {}).get("frames", [])
    out: dict = {}
    for minute in (10, 15, 20):
        if minute >= len(frames):
            continue
        pf = frames[minute].get("participantFrames", {})
        mine = pf.get(str(my_pid), {}).get("totalGold")
        if mine is None:
            continue
        out[f"gold_at_{minute}"] = mine
        if opp_pid is not None:
            theirs = pf.get(str(opp_pid), {}).get("totalGold")
            if theirs is not None:
                out[f"gold_diff_at_{minute}"] = mine - theirs
    for f in frames:
        for e in f.get("events", []):
            if e.get("type") == "CHAMPION_KILL":
                out["first_blood_sec"] = e["timestamp"] // 1000
                out["first_blood_was_mine"] = e.get("killerId") == my_pid
                return out
    return out


def narrative_flags(m: dict) -> list[str]:
    """Pre-chewed story shapes. The model gets these rather than raw numbers,
    because "comeback" is a title and "maxKillDeficit: 7" is not."""
    ch, tl = m.get("challenges", {}), m.get("timeline", {})
    opp = m.get("opponent")
    gd15 = tl.get("gold_diff_at_15")
    kp = ch.get("killParticipation") or 0
    flags = []

    if m["win"] and ch.get("maxKillDeficit", 0) >= 5:
        flags.append("comeback")
    if gd15 is not None and gd15 >= 2500:
        flags.append("stomped_lane")
    if gd15 is not None and gd15 <= -2000:
        flags.append("lost_lane_early")
        if m["win"]:
            flags.append("lost_lane_won_game")
    if m["deaths"] >= 12:
        flags.append("int_game")
    if m["deaths"] <= 3:
        flags.append("barely_died")
    if kp >= 0.6:
        flags.append("carried_teamfights")
    if m["damage_turrets"] >= 15000 and kp < 0.5:
        flags.append("splitpush_game")
    if m["duration_sec"] >= 38 * 60:
        flags.append("very_long_game")
    if m["duration_sec"] <= 20 * 60:
        flags.append("very_short_game")
    if m.get("gameEndedInSurrender"):
        flags.append("ended_in_surrender")
    if ch.get("soloKills", 0) >= 3:
        flags.append("solo_killed_lane")
    if m.get("largestMultiKill", 0) >= 3:
        flags.append("multikill")
    if ch.get("survivedSingleDigitHpCount", 0) >= 2:
        flags.append("clutch_survivals")
    if ch.get("outnumberedKills", 0) >= 3:
        flags.append("outnumbered_killer")
    if ch.get("turretPlatesTaken", 0) >= 4:
        flags.append("plate_farmer")
    if m.get("largestKillingSpree", 0) >= 6:
        flags.append("big_killing_spree")
    if ch.get("hadOpenNexus"):
        flags.append("open_nexus")
    if opp and opp["deaths"] >= 8 and opp["kills"] <= 3:
        flags.append("destroyed_lane_opponent")
    if opp and opp["kills"] >= 10 and m["deaths"] >= 8:
        flags.append("lane_opponent_fed")
    return flags


def _extract(raw: dict, puuid: str, tl: dict | None) -> dict:
    info = raw["info"]
    me = next(p for p in info["participants"] if p["puuid"] == puuid)
    opp = _lane_opponent(info["participants"], me)
    opp_pid = None
    if opp:
        opp_pid = next((p["participantId"] for p in info["participants"]
                        if p["teamId"] != me["teamId"]
                        and p["championName"] == opp["champion"]), None)

    m = {
        # legacy keys — meta.json consumers, fun_fact and thumbnail.py rely on these
        "match_id": raw["metadata"]["matchId"],
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
        # expansion
        "patch": info.get("gameVersion"),
        "role": me.get("teamPosition") or me.get("individualPosition"),
        "vision_score": me.get("visionScore"),
        "challenges": {k: me.get("challenges", {}).get(k)
                       for k in CHALLENGE_KEYS if me.get("challenges", {}).get(k)},
        "opponent": opp,
        "objectives": _objectives(info, me),
        "timeline": _timeline_facts(tl, me["participantId"], opp_pid) if tl else {},
    }
    for k in PARTICIPANT_KEYS:
        if me.get(k):
            m[k] = me[k]
    m["flags"] = narrative_flags(m)
    return m


def fetch_matches(cfg: dict, workdir: str, start_epoch: int, end_epoch: int) -> list[dict]:
    """Every match the account played in the VOD window, cached to disk.

    Raises ExpiredKeyError only when the cache can't answer; a fully cached VOD
    needs no key at all.
    """
    r = cfg["riot"]
    key = r.get("api_key") or ""
    base = API.format(routing=r["routing"])
    cache = Path(workdir) / "riot"
    live = bool(key) and not key.startswith("RGAPI-YOUR")

    def _live(url):
        if not live:
            return None
        return lambda: _get(url, key)

    idx_path = cache / "_index.json"
    if idx_path.exists():
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    else:
        if not live:
            raise ExpiredKeyError("[riot] no cached match index and no usable api_key")
        acct = _get(f"{base}/riot/account/v1/accounts/by-riot-id/"
                    f"{r['game_name']}/{r['tag_line']}", key)
        ids = _get(f"{base}/lol/match/v5/matches/by-puuid/{acct['puuid']}/ids"
                   f"?startTime={start_epoch - 3600}&endTime={end_epoch + 3600}&count=100", key)
        idx = {"puuid": acct["puuid"], "ids": ids}
        cache.mkdir(parents=True, exist_ok=True)
        idx_path.write_text(json.dumps(idx), encoding="utf-8")

    want_tl = r.get("fetch_timeline", True)
    matches, misses = [], 0
    for mid in idx["ids"]:
        try:
            raw = _cached(cache / f"{mid}.json",
                          _live(f"{base}/lol/match/v5/matches/{mid}"))
            tl = None
            if want_tl:
                try:
                    tl = _cached(cache / f"{mid}_timeline.json",
                                 _live(f"{base}/lol/match/v5/matches/{mid}/timeline"))
                except (NoCacheError, urllib.error.HTTPError):
                    tl = None  # timeline is a bonus, never a blocker
            matches.append(_extract(raw, idx["puuid"], tl))
        except NoCacheError:
            misses += 1
    if misses:
        print(f"[riot] {misses} match(es) not cached and no live key — skipped")
    if not matches and misses:
        raise ExpiredKeyError("[riot] nothing cached for this VOD and no usable api_key")
    return matches


def assign_matches(segments: list[dict], matches: list[dict],
                   vod_t0: int, window_sec: int) -> dict[int, dict]:
    """Greedy nearest-in-time pairing, each match consumed at most once.

    The old code took the argmin per segment independently, so back-to-back
    games could both claim the same match. Returns {segment_index -> match}.
    """
    pairs = []
    for si, seg in enumerate(segments):
        seg_start = vod_t0 + seg["start_sec"]
        for mi, m in enumerate(matches):
            delta = abs(m["start_epoch"] - seg_start)
            if delta <= window_sec:
                pairs.append((delta, si, mi))
    pairs.sort()
    taken_seg: set[int] = set()
    taken_match: set[int] = set()
    out: dict[int, dict] = {}
    for _, si, mi in pairs:
        if si in taken_seg or mi in taken_match:
            continue
        taken_seg.add(si)
        taken_match.add(mi)
        out[si] = matches[mi]
    return out
