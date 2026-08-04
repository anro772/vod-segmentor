"""Title generation: facts + quotes -> local LLM -> hard validation.

The old titler drew from 16 hardcoded strings, so 24 uploads produced 13
distinct phrases and two exact duplicates. This replaces the phrase pool with
generation grounded in what actually happened, but keeps the pool as the
fallback so the pipeline never depends on Ollama being up.

The validator is the load-bearing part, not the prompt. A 14B model at
temperature 0.9 will occasionally write lowercase, invent a statistic, or leak
the result; every candidate is therefore checked mechanically and rejected on
any violation. Generation is cheap, so we simply ask for several and keep the
first that survives.
"""
import json
import re
import time
import urllib.error
import urllib.request
import zlib
from datetime import date
from pathlib import Path

PREFIX = "BAUS"
MIN_LEN, MAX_LEN = 28, 70

EMOJI = ["😈", "💀", "😤", "😳", "😨", "🤯", "🤡", "😱", "😭", "🥶", "🔥", "👀"]
_EMOJI_SET = set(EMOJI)

# Nothing here may ever appear in a title: the result is a spoiler and lives
# only in meta.json. Chat and the transcript both say "gg" constantly.
SPOILERS = ("WIN", "WINS", "WINNING", "WON", "LOSS", "LOSE", "LOSES", "LOSING",
            "LOST", "VICTORY", "DEFEAT", "GG", "EZ", "FF", "SURRENDER",
            "SURRENDERS", "THROWS", "THROWN", "COMEBACK")

# He swears constantly on stream, so the transcript is full of it and the model
# will happily lift it verbatim. Profanity in a YouTube title risks the video's
# monetisation, so it is a hard reject — and such quotes are filtered out
# before the model ever sees them. better_profanity carries the maintained
# list; these are extras it misses plus the ones worth catching as bare words.
PROFANITY = ("FUCK", "FUCKS", "FUCKING", "FUCKED", "SHIT", "SHITS", "SHITTY",
             "BITCH", "BASTARD", "CUNT", "DICK", "ASSHOLE", "RETARD",
             "RETARDED", "NIGGA", "WHORE", "SLUT", "PUSSY", "COCK")

try:
    from better_profanity import profanity as _profanity
    _profanity.load_censor_words()
except ImportError:  # optional dep; the literal list above still applies
    _profanity = None

try:
    from wordfreq import zipf_frequency as _zipf
except ImportError:
    _zipf = None

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:
    _fuzz = None

# Twitch emote / chat slang: real words in his speech, but wrong in a title.
CHAT_SLANG = ("KAPPA", "POG", "POGGERS", "POGCHAMP", "LUL", "OMEGALUL",
              "KEKW", "MONKAS", "SADGE", "COPIUM", "MALDING", "PEPE",
              "PEPEGA", "JAM", "AWOO", "WIDEPEEPO", "CATJAM")

# Live streams wander into things that must never end up on a public video
# title, and they can be perfectly on-topic ("...because you play Volibear").
# Blocked both when mining quotes and when validating a finished title.
SENSITIVE = ("PEDOPHILE", "PEDO", "RAPE", "RAPED", "RAPIST", "NAZI", "HITLER",
             "SUICIDE", "KYS", "CANCER", "AUTISTIC", "AUTISM", "RETARD",
             "TERRORIST", "SLAVE", "RACIST", "MOLEST", "ABUSE")

# Shown to the model to convey tone. Single source of truth so they can also be
# rejected: given a thin fact sheet the model will otherwise just copy one out
# verbatim, which is how "I'D NEVER THOUGHT I'D SAY THIS" shipped as a title.
EXAMPLES_GOOD = [
    "BAUS SION I'D NEVER THOUGHT I'D SAY THIS 😳",
    "BAUS JAX THIS DIVE STRATEGY IS INSANE 😈",
    "BAUS IRELIA HAS NO ANSWER FOR VLADIMIR 😨",
    "BAUS SKARNER TOOK 14 PLATES AND KEPT GOING 😤",
]
EXAMPLES_BAD = [
    "BAUS SION THIS GAME IS PURE CHAOS 💀",
    "BAUS JAX THIS GAME TESTS EVERY LIMIT 😨",
]
_EXAMPLE_TAILS = {" ".join(t.split()[2:]).upper()
                  for t in EXAMPLES_GOOD + EXAMPLES_BAD}

# Flags are internal identifiers; shown raw the model writes them straight into
# titles ("OUTNUMBERED KILLER"). Describe the situation instead — and keep every
# description free of the result, since win/loss is never a spoiler we leak.
FLAG_TEXT = {
    "comeback": "the game swung hard after a huge kill deficit",
    "stomped_lane": "he crushed his lane opponent early",
    "lost_lane_early": "he fell far behind in lane",
    "lost_lane_won_game": "he fell behind in lane but the game turned around",
    "int_game": "he kept dying over and over",
    "barely_died": "he almost never died",
    "carried_teamfights": "he was involved in nearly every kill his team got",
    "splitpush_game": "he ignored the fights and pushed the side lanes",
    "very_long_game": "the game dragged on far past normal length",
    "very_short_game": "the game ended unusually quickly",
    "ended_in_surrender": "the game ended on a surrender vote",
    "solo_killed_lane": "he solo killed his lane opponent several times",
    "multikill": "he landed a big multikill",
    "clutch_survivals": "he survived fights on almost no health",
    "outnumbered_killer": "he won fights while outnumbered",
    "plate_farmer": "he took a pile of turret plates early",
    "big_killing_spree": "he went on a long killing spree",
    "open_nexus": "his nexus was left wide open",
    "destroyed_lane_opponent": "his lane opponent was completely shut down",
    "lane_opponent_fed": "his lane opponent got very strong",
}


def _is_real_word(w: str) -> bool:
    if _zipf is not None:
        return _zipf(w.lower(), "en") >= MIN_WORD_ZIPF
    return w in COMMON

_WORD = re.compile(r"[A-Z][A-Z']*")
_NUM = re.compile(r"\d{2,}")
_ALNUM_MIX = re.compile(r"[A-Z]+\d+[A-Z]+|\d+[A-Z]{3,}")

# Minimum wordfreq zipf score for a word to count as real English. Measured:
# afraid 4.70, gonna 5.29, reckless 3.82 vs plages 1.33, orelia 0.00,
# teamfights 0.00. Anything below this must instead appear in the transcript or
# the facts, which is what lets genuine jargon ("splitpush") through.
MIN_WORD_ZIPF = 2.0

# Retained only as a fallback for when wordfreq is unavailable.
COMMON = frozenset("""
ABOUT above across actually after again against ahead alive alone along already
also always among anger angry another answer anyone anything around away awful
back bad barely because become before began behind being believe below best
better between beyond biggest blind both break breaks bring brings broke brutal
build built call called came cannot cant care carry casually caught chance
change chaos choice class clean clear close comes coming complete could crazy
crime cursed damage danger dare dead deal death decide decided deep defend
delete different dive dives doing done doubt down drop each early easily
edge else empty end ends enemy enough entire even ever every exact exactly
face fact fail fails fall falls far fast faster fastest fault fear feel
felt fewer fight fighting final find finds first five follow forever forget
forgot free from front full game games gave getting give given goes going
gone good grief hand happen happened happens hard harder hardest have having
head hear heart help here hero hide high hold holding home hope hour house
huge hundred hurt idea impossible inside insane instead into just keep keeping
kept kill killed kills kind knew know known last late later lead learn least
leave left less lesson level life like line little live long longer look
looking lose lost loud love luck lucky made make makes making many matter
maybe mean meant meta might mind minute minutes miss missed mistake moment
money more most move moves much must myself near nearly need needs never
next nobody none nothing notice now number often once only open opinion
other others over pain part pass past patience people perfect person pick
place plan play played playing point position possible power press pretty
problem pull punish pure push quick quickly quiet quit rage rather reach
read ready real really reason refuse remember respect rest right risk roll
room rule rules run running safe said same save saved says second seconds
seen sense serious shame shape share short should show side sign simple
since single situation skill slow small some someone something sometimes
soon sorry sound space speak spent stand start started stay step still
stop stopped strange strategy street strong stuck stupid such sudden suffer
supposed sure surprise survive take taken takes talk teach team tell than
that their them then there these they thing things think thinking third
this those though thought three through throw time times tired today
together told took total tough toward tower towers track tried tries true
trust truth turn turns twice type ugly under understand until upon usually
very wait waiting walk want wanted wants warn wasted watch watching water
weak wear week weird well went what whatever when where whether which
while whole whom why wild will wish with within without woke wonder word
work worked working world worse worst worth would wrong yesterday yourself
ability afraid almost alright anymore apart asleep awake aware beat beaten
blame brave breathe calm careful chase cheap clever climb coming confident
control cool correct crash cruel decision deserve destroy disaster dream
drive easy effort embarrassing energy escape excuse expect experience explain
famous finish forced forward funny further future gamble greatest guess happy
hate heavy honest hopeless human ignore imagine impressive improve incredible
intense interesting learned legend listen lonely lucky magic main major master
memory mercy message miracle mission monster nasty nervous night noise normal
obvious offer opponent opposite order ordinary panic patient pause peace
perhaps permanent plate plates please pointless practice prepare pressure
pride prison promise proof proper protect proud prove punch purpose quality
question range rare react reality reckless record recover regret relax relief
remain remove repeat replace rescue result return reveal reverse ridiculous
ruin rush sacrifice scared scary search secret seek serious settle severe
shadow shake shock shoot shout sight silence silly similar simply sleep slip
smart smash smile solid solve speed spend spirit split spot spread stack
stage stare steal steady stick storm story straight stream stress stretch
strike struggle stubborn stuff stumble style succeed suggest summon super
support suppose surface survive suspect swear sweet swing switch tactic
talent target taste technique terrible thick threat tight tiny tolerate
trade tragic trap trick trigger trouble twist ultimate unable unfair unique
universe unknown unless unlucky unusual upset urgent useful useless usual
value vanish various victim vision voice wake warrior weakness weapon
weather weight whatever whisper wisdom wise witness wonder worry wound
gonna wanna gotta kinda sorta yeah nope aint dunno lemme gimme cmon
""".split())
COMMON = frozenset(w.upper() for w in COMMON)


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return (0x1F300 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or o == 0x2B50


def _emojis(s: str) -> list[str]:
    return [c for c in s if _is_emoji(c)]


# ---------------------------------------------------------------- history

def load_history(cfg: dict) -> list[dict]:
    p = Path(cfg["titles"]["history_file"])
    if not p.is_absolute():
        p = Path(cfg["paths"]["root"]) / p
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("[titles] history file unreadable, starting fresh")
        return []


def save_history(cfg: dict, entries: list[dict]) -> None:
    p = Path(cfg["titles"]["history_file"])
    if not p.is_absolute():
        p = Path(cfg["paths"]["root"]) / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def _tail(title: str, champion: str) -> str:
    """The part after `BAUS <CHAMP> ` — what actually has to be original."""
    head = f"{PREFIX} {champion.upper()} "
    return title[len(head):] if title.startswith(head) else title


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.upper()) if len(w) > 2}


def _too_similar(tail: str, history: list[dict], threshold: float = 0.5) -> str | None:
    """Jaccard over content words. 0.5 is deliberately strict: sharing half the
    vocabulary is exactly the "THIS GAME IS ..." sameness we are trying to kill,
    and over-rejection is cheap (we generate several candidates and retry)."""
    mine = _content_words(tail)
    if not mine:
        return None
    for h in history:
        other = _tail(h.get("title", ""), h.get("champion", ""))
        theirs = _content_words(other)
        if not theirs:
            continue
        if len(mine & theirs) / len(mine | theirs) >= threshold:
            return h["title"]
        # rapidfuzz also catches reorderings and partial overlaps that a plain
        # set comparison scores as different ("X WONT END" vs "WONT EVER END X")
        if _fuzz is not None and _fuzz.token_set_ratio(tail, other) >= 90:
            return h["title"]
    return None


# ---------------------------------------------------------------- validation

def _allowed_numbers(sheet: dict) -> set[int]:
    """Every number the model is permitted to write, incl. rounded-K forms."""
    ok: set[int] = set()

    def add(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        n = abs(v)
        ok.add(int(n))
        ok.add(round(n))
        if n >= 1000:
            ok.add(int(n // 1000))
            ok.add(round(n / 1000))
        if n < 1:
            ok.add(int(n * 100))
            ok.add(round(n * 100))

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        else:
            add(o)

    walk(sheet)
    return ok


def build_vocab(sheet: dict, transcript: list[dict] | None) -> set[str]:
    """Every word the title is allowed to use: ordinary English, plus anything
    he actually said this game, plus the champion/stat names in the facts."""
    vocab = set(COMMON)

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            vocab.update(_WORD.findall(o.upper()))

    walk(sheet)
    for line in transcript or []:
        vocab.update(_WORD.findall(line["text"].upper()))
    return vocab


def validate(title: str, champion: str, sheet: dict, history: list[dict],
             used: set[str], similarity: float = 0.5,
             vocab: set[str] | None = None,
             champions: set[str] | None = None) -> str | None:
    """None if the title is acceptable, else a short reason for rejection."""
    head = f"{PREFIX} {champion.upper()} "
    if not title.startswith(head):
        return f"must start with '{head.strip()}'"
    tail = _tail(title, champion)
    if not tail.strip():
        return "nothing after the champion name"
    if not (MIN_LEN <= len(title) <= MAX_LEN):
        return f"length {len(title)} outside {MIN_LEN}-{MAX_LEN}"
    if any(c.islower() for c in title):
        return "contains lowercase"

    ems = _emojis(title)
    if len(ems) != 1:
        return f"needs exactly one emoji, found {len(ems)}"
    if ems[0] not in _EMOJI_SET:
        return f"emoji {ems[0]} not in the allowed set"
    if not title.rstrip().endswith(ems[0]):
        return "emoji must be the last character"

    words = set(_WORD.findall(title.upper()))
    bad = words & set(SPOILERS)
    if bad:
        return f"spoiler word {sorted(bad)[0]}"
    bad = words & set(PROFANITY)
    if bad:
        return f"profanity {sorted(bad)[0]}"
    if _profanity is not None and _profanity.contains_profanity(title.lower()):
        return "profanity (better_profanity)"
    bad = words & set(CHAT_SLANG)
    if bad:
        return f"chat slang {sorted(bad)[0]}"
    bad = words & set(SENSITIVE)
    if bad:
        return f"sensitive topic {sorted(bad)[0]}"
    if tail.upper() in _EXAMPLE_TAILS:
        return "copied verbatim from the prompt's examples"


    if _ALNUM_MIX.search(title):
        return "letters and digits jammed together (emote or username)"
    stray = set(re.findall(r"[^A-Z0-9 '?!,.\-]", title)) - set(_emojis(title))
    if stray:
        return f"stray character {''.join(sorted(stray))!r}"
    # every digit must be traceable, not just multi-digit ones — "DIED LIKE 5
    # TIMES" invented a count that no field supported
    for tok in re.findall(r"\d+", title):
        if int(tok) not in _allowed_numbers(sheet):
            return f"number {tok} is not in the facts"

    # A word is acceptable if it is real English *or* he actually said it this
    # game (jargon like "splitpush" scores 0.00 in wordfreq but is legitimate).
    for w in _WORD.findall(title.upper()):
        if len(w) > 14:
            return f"implausible word {w}"
        if len(w) < 5:
            continue
        bare = w.replace("'", "")
        if _is_real_word(bare) or _is_real_word(w):
            continue
        if vocab is not None and (w in vocab or bare in vocab):
            continue
        return f"invented or garbled word {w}"

    if champions:
        allowed = {champion.upper()}
        opp = (sheet.get("lane_opponent") or {}).get("champion")
        if opp:
            allowed.add(opp.upper())
        upper_champs = {c.upper() for c in champions}
        for w in words:
            if len(w) < 4 or w in allowed:
                continue
            if w in upper_champs:
                return f"names {w}, who was not in this game"
            # near-miss spellings ("ORELIA" for Irelia) survive the word check
            # when Whisper mistranscribed the name into the transcript
            if _fuzz is not None and not _is_real_word(w):
                best = max((_fuzz.ratio(w, c) for c in upper_champs), default=0)
                if best >= 85:
                    return f"{w} looks like a misspelled champion name"

    if title in used:
        return "already used in this VOD"
    if any(h.get("title") == title for h in history):
        return "exact match in title history"
    clash = _too_similar(tail, history, similarity)
    if clash:
        return f"too similar to '{clash}'"
    return None


# ---------------------------------------------------------------- quotes

_MARKERS = ("I ", "IM ", "I'M ", "MY ", "WE ", "THIS IS", "THATS", "THAT'S",
            "NEVER", "ALWAYS", "INSANE", "CRAZY", "ACTUALLY", "LITERALLY",
            "WHAT ", "HOW ", "WHY ", "GUYS", "BRO", "HONESTLY", "SUPPOSED",
            "CANT", "CAN'T", "SHOULD", "WORST", "BEST", "TROLL", "GRIEF")

# He talks about music, tournaments and chat for long stretches. A quote has to
# actually be about the match, or we end up with "MADE ME RECOGNIZE MAROON FIVE"
# on a League video.
GAME_TERMS = frozenset("""
lane laning top mid bot jungle jungler support adc tower towers turret turrets
plate plates inhib inhibitor nexus baron drake dragon herald grubs minion
minions wave waves cannon cs farm gank ganked ganking dive dove diving fight
fights fighting teamfight kill kills killed die died dying death deaths
flash ult ulti ultimate ability abilities passive tp teleport recall back
ward wards vision item items build buy gold level levels xp exp respawn
push pushing split splitpush shove freeze proxy engage disengage peel poke
trade trades allin health hp mana cooldown armor mr damage tank bruiser
carry scaling snowball comp team enemy enemies ally allies queue elo rank
""".split())

_CALLOUT = re.compile(
    r"^(ok|okay|yeah|yes|no|nope|back|recall|flash|ult|ward|wait|come on|"
    r"let's go|lets go|hello|hi|thanks|thank you|nice|gg)[\s.!?]*$", re.I)


def _is_garbled(text: str, champions: set[str] | None) -> bool:
    """True if Whisper mangled a word in here ("use Udu for this wave").

    Offering such a line invites the model to put the garble in a title, where
    it survives validation because the transcript legitimises it.
    """
    known = {c.upper() for c in (champions or set())}
    for w in _WORD.findall(text.upper()):
        if len(w) < 3 or w in known:
            continue
        if not _is_real_word(w.replace("'", "")):
            return True
    return False


def _about_the_game(text: str, champions: set[str] | None) -> bool:
    words = {w.lower() for w in _WORD.findall(text.upper())}
    if words & GAME_TERMS:
        return True
    return bool(words & {c.lower() for c in (champions or set())})


def mine_quotes(transcript: list[dict], spikes: list[dict], limit: int = 15,
                champions: set[str] | None = None) -> list[dict]:
    """Lines worth putting in front of the model.

    Ranked by proximity to a chat reaction first — the audience is a better
    judge of what mattered than any heuristic — then by how much the line
    sounds like an opinion rather than a mechanical callout.
    """
    hot = [s["t"] for s in spikes]
    scored, seen = [], set()
    for line in transcript:
        text = " ".join(line["text"].split())
        if not text or _CALLOUT.match(text):
            continue
        words = text.split()
        if not (4 <= len(words) <= 18):
            continue
        upper = text.upper()
        if any(re.search(rf"\b{s}\b", upper) for s in ("GG", "EZ", "FF")):
            continue  # spoiler-adjacent, don't even offer it
        if any(re.search(rf"\b{p}\b", upper) for p in PROFANITY):
            continue  # unusable in a YouTube title; don't tempt the model
        if any(re.search(rf"\b{s}\b", upper) for s in SENSITIVE):
            continue
        if not _about_the_game(text, champions):
            continue  # music/tournament tangents are not League content
        if _is_garbled(text, champions):
            continue
        key = upper[:40]
        if key in seen:
            continue
        seen.add(key)

        score = sum(2 for m in _MARKERS if m in upper)
        if hot:
            nearest = min(abs(line["start"] - t) for t in hot)
            if nearest <= 30:
                score += 6
            elif nearest <= 90:
                score += 3
        if 6 <= len(words) <= 12:
            score += 2
        scored.append((score, line["start"], text))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"t": int(t), "text": txt} for _, t, txt in scored[:limit]]


# ---------------------------------------------------------------- fact sheet

def build_sheet(match: dict, chat: dict, quotes: list[dict]) -> dict:
    """Compact, zero-free facts. A sheet padded with nulls and 0s invites the
    model to write about nothing in particular."""
    opp = match.get("opponent")
    sheet: dict = {
        "champion": match["champion"],
        "role": match.get("role"),
        "kda": f"{match['kills']}/{match['deaths']}/{match['assists']}",
        "duration_min": round(match["duration_sec"] / 60),
        # Described, not labelled: handed the raw flag names the model wrote
        # them straight into titles ("OUTNUMBERED KILLER", "CARRIED TEAMFIGHTS").
        "story": [FLAG_TEXT.get(f, f.replace("_", " "))
                  for f in match.get("flags", [])],
    }
    if opp:
        sheet["lane_opponent"] = {
            "champion": opp["champion"],
            "kda": f"{opp['kills']}/{opp['deaths']}/{opp['assists']}",
            "gold_diff": opp["gold_diff"],
        }
    stats = {
        "turret_damage": match["damage_turrets"],
        "damage_to_champions": match["damage_champs"],
        "cs": match["cs"],
        "solo_kills": match.get("challenges", {}).get("soloKills"),
        "turret_plates": match.get("challenges", {}).get("turretPlatesTaken"),
        "outnumbered_kills": match.get("challenges", {}).get("outnumberedKills"),
        "survived_on_low_hp": match.get("challenges", {}).get("survivedSingleDigitHpCount"),
        "biggest_killing_spree": match.get("largestKillingSpree"),
        "biggest_multikill": match.get("largestMultiKill"),
        "gold_diff_at_15": match.get("timeline", {}).get("gold_diff_at_15"),
    }
    sheet["stats"] = {k: v for k, v in stats.items() if v}
    if chat:
        # Deliberately NOT top_terms. Those are emote names and usernames, and
        # handing them to the model got "SAME5MAROONSONGS" written into a real
        # title. Chat tells us how hyped a game was and where; the words people
        # typed are not title material.
        sheet["chat"] = {"hype_vs_average": chat.get("hype_score")}
    if quotes:
        sheet["quotes"] = [q["text"] for q in quotes]
    return sheet


# ---------------------------------------------------------------- generation

SYSTEM = """You write YouTube titles for a channel that uploads League of Legends games from the streamer thebausffs ("Baus"). He is a top laner famous for reckless splitpushing, inting Sion, and refusing to play safe.

Write titles that sound like an excited viewer or like Baus himself talking. NOT like a statistics readout.

HARD RULES. A title breaking any of these is thrown away:
1. Start with exactly: BAUS <CHAMPION>
2. ALL CAPS. No lowercase letters anywhere.
3. Total length between 28 and 70 characters.
4. End with exactly one emoji from this set: {emoji}
5. NEVER reveal or hint at the result. These words are banned: {spoilers}
6. Only use numbers that literally appear in FACTS. Never invent a statistic.
7. Do not reuse or rephrase anything under RECENT TITLES.
8. Every word must be ordinary English or a name that appears in FACTS/QUOTES.
   Never invent words, never misspell a champion name, and never use emote
   names, usernames or chat slang. If a quote looks garbled, do not use it.

WHAT MAKES A TITLE GOOD:
- BEST BY FAR: something he actually SAID. Take a line from QUOTES and tighten
  it into a title. If QUOTES has anything usable, use it. Keep his voice.
- Next best: the lane matchup, or one specific weird situation.
- Worst: a generic line that could describe any game.
- The "story" entries in FACTS are background for YOU. Do not copy their
  wording into the title — they are descriptions, not phrases.
- Vary the sentence shape. Do NOT start every title with "THIS GAME".

Examples of the right FEEL. Never copy these — they describe other games:
{good}
Examples of the wrong feel (too generic, could be any game):
{bad}

Reply with JSON only, no commentary:
{{"titles": ["...", "..."]}}
Give {n} distinct candidates, most specific first."""


def _ollama(cfg: dict, system: str, user: str) -> list[str]:
    t = cfg["titles"]
    # num_ctx matters a lot on AMD: left to itself Ollama sizes the context from
    # total VRAM (32768 on a 24GB card), which allocates gigabytes of KV cache
    # for prompts that are barely 2k tokens and was implicated in hard GPU
    # faults here. Pin it to something that actually fits the job.
    options = {"temperature": float(t.get("temperature", 0.9)),
               "top_p": 0.95,
               "num_ctx": int(t.get("num_ctx", 4096)),
               "num_predict": int(t.get("num_predict", 400))}
    if t.get("num_gpu") is not None:
        options["num_gpu"] = int(t["num_gpu"])  # 0 = CPU only
    body = json.dumps({
        "model": t["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "format": "json",
        "keep_alive": t.get("keep_alive", "10m"),
        "options": options,
    }).encode()
    req = urllib.request.Request(
        t.get("host", "http://localhost:11434").rstrip("/") + "/api/chat",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=int(t.get("timeout_sec", 180))) as r:
        doc = json.loads(r.read())
    content = doc.get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return []
    titles = parsed.get("titles") if isinstance(parsed, dict) else parsed
    if isinstance(titles, str):
        titles = [titles]
    return [str(x).strip() for x in (titles or []) if str(x).strip()]


_GENERIC = ("THIS GAME IS", "THIS GAME HAS", "THIS GAME NEEDS", "THIS GUY",
            "THIS GAME GOES", "THIS GAME TESTS", "THIS GAME BREAKS",
            "EVERY FIGHT", "ON ANOTHER LEVEL", "OUT OF HIS CONTROL")


def score(title: str, champion: str, sheet: dict,
          used_emoji: set[str] | None = None) -> float:
    """Rank valid candidates. Taking the first that passed validation kept
    picking flat lines when a better one sat two entries down.

    Rewards titles built from something he actually said; penalises ones that
    merely echo the hints we supplied or fall back to stock phrasing.
    """
    tail = _tail(title, champion)
    words = _content_words(tail)
    s = 0.0

    quotes = sheet.get("quotes") or []
    best_q = max((len(words & _content_words(q)) for q in quotes), default=0)
    s += min(best_q, 4) * 2.0          # grounded in real speech — what we want

    # echoing the story hints back at us is not writing
    for hint in sheet.get("story") or []:
        overlap = len(words & _content_words(hint))
        if overlap >= 3:
            s -= 4.0
        elif overlap == 2:
            s -= 1.5

    upper = tail.upper()
    if any(g in upper for g in _GENERIC):
        s -= 4.0
    if upper.startswith("THIS GAME"):
        s -= 2.0
    if re.search(r"\d", title):
        s -= 1.0                        # numbers are where hallucination lives
    if 38 <= len(title) <= 66:
        s += 1.0
    ems = _emojis(title)
    if used_emoji and ems and ems[0] in used_emoji:
        s -= 1.5                        # spread the emoji across a VOD's uploads

    # Words that only exist because the transcript contains them are usually
    # Whisper mangling something ("BORK UDDER" for Botrk and Ludens). They pass
    # validation legitimately, but a title full of them reads as nonsense.
    if _zipf is not None:
        s -= 2.0 * sum(1 for w in words
                       if _zipf(w.replace("'", "").lower(), "en") < 2.5)
    s += min(len(words), 7) * 0.25      # a bit of substance over three words
    return s


def _call_with_retry(cfg: dict, system: str, user: str):
    """(candidates, error). Survives a runner crash.

    When the GPU backend faults, Ollama kills the runner and the HTTP call dies
    with a connection reset — but the server itself stays up and reloads the
    model on the next request. Retrying a few seconds later usually succeeds,
    so a transient backend crash should not silently demote a whole VOD to
    template titles the way it did before.
    """
    tries = int(cfg["titles"].get("retries", 3))
    last = None
    for i in range(tries):
        try:
            return _ollama(cfg, system, user), None
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as e:
            last = e
            if i < tries - 1:
                wait = 5 * (i + 1)
                print(f"[titles] ollama call failed ({type(e).__name__}: {e}); "
                      f"retrying in {wait}s")
                time.sleep(wait)
    return [], last


def generate(cfg: dict, sheet: dict, champion: str, history: list[dict],
             used: set[str], vocab: set[str] | None = None,
             champions: set[str] | None = None
             ) -> tuple[str | None, list[str]]:
    """(accepted title, rejection log). One retry with the failures fed back."""
    t = cfg["titles"]
    n = int(t.get("candidates", 6))
    recent = [h["title"] for h in history[-int(t.get("history_lookback", 40)):]]
    system = SYSTEM.format(
        emoji=" ".join(EMOJI), spoilers=", ".join(SPOILERS), n=n,
        good="\n".join(f"  {t}" for t in EXAMPLES_GOOD),
        bad="\n".join(f"  {t}" for t in EXAMPLES_BAD))
    payload = {"FACTS": sheet, "RECENT TITLES": recent}
    user = json.dumps(payload, ensure_ascii=False, indent=1)
    rejected: list[str] = []

    for attempt in (1, 2):
        cands, err = _call_with_retry(cfg, system, user)
        if err is not None:
            rejected.append(f"ollama unreachable: {err}")
            return None, rejected
        passing = []
        for c in cands:
            why = validate(c, champion, sheet, history, used,
                           float(t.get("similarity_max", 0.5)), vocab, champions)
            if why is None:
                passing.append(c)
            else:
                rejected.append(f"{c!r}: {why}")
        if passing:
            seen_em = {e for tl in used for e in _emojis(tl)}
            return max(passing,
                       key=lambda c: score(c, champion, sheet, seen_em)), rejected
        if attempt == 1:
            user = (json.dumps(payload, ensure_ascii=False, indent=1)
                    + "\n\nYour previous attempt was fully rejected:\n"
                    + "\n".join(f"- {r}" for r in rejected[-n:])
                    + "\nFix every issue and try completely different wording.")
    return None, rejected


# ---------------------------------------------------------------- fallback

# Kept verbatim from the original enrich.py: when Ollama is down or every
# candidate fails validation, the pipeline still produces a usable title.
PHRASES = [
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


def template_title(m: dict, used: set[str], history: list[dict]) -> str:
    """Deterministic per match_id, now also avoiding the persisted history."""
    seen = used | {h["title"] for h in history}
    fallback = None
    for pred, phrases in PHRASES:
        if not pred(m):
            continue
        start = zlib.crc32(m["match_id"].encode()) % len(phrases)
        for i in range(len(phrases)):
            pick = phrases[(start + i) % len(phrases)]
            title = f"{PREFIX} {m['champion'].upper()} {pick}"
            if fallback is None:
                fallback = title
            if title not in seen:
                return title
    return fallback or f"{PREFIX} {m['champion'].upper()} {PHRASES[-1][1][0]}"


# ---------------------------------------------------------------- entry point

def make_title(cfg: dict, match: dict, chat: dict, transcript: list[dict],
               history: list[dict], used: set[str], vod_id: str, index: int,
               champions: set[str] | None = None) -> dict:
    """-> {title, source, sheet, quote_used, rejected}."""
    tcfg = cfg.get("titles", {})
    spikes = (chat or {}).get("spikes", [])
    quotes = mine_quotes(transcript or [], spikes, champions=champions)
    sheet = build_sheet(match, chat, quotes)
    title, rejected = None, []

    if tcfg.get("provider", "ollama") == "ollama":
        vocab = build_vocab(sheet, transcript)
        title, rejected = generate(cfg, sheet, match["champion"], history,
                                   used, vocab, champions)
        if title is None:
            print(f"[titles] game {index:02d}: {len(rejected)} candidate(s) rejected, "
                  f"falling back to template")
            for r in rejected[:3]:
                print(f"         - {r}")

    source = "llm"
    if title is None:
        if not tcfg.get("fallback_to_templates", True):
            raise RuntimeError(f"no valid title for game {index} and fallback disabled")
        title = template_title(match, used, history)
        source = "template"

    used.add(title)
    # only claim a quote inspired the title when the overlap is real — a loose
    # 2-word match attributed titles to quotes they had nothing to do with
    tw = _content_words(_tail(title, match["champion"]))
    quote_used = next((q["text"] for q in quotes
                       if len(_content_words(q["text"]) & tw) >= max(3, len(tw) // 2)),
                      None)
    return {
        "title": title,
        "source": source,
        "sheet": sheet,
        "quote_used": quote_used,
        "rejected": rejected,
        "entry": {"vod_id": vod_id, "index": index, "date": date.today().isoformat(),
                  "champion": match["champion"], "title": title, "source": source,
                  "quote_used": quote_used},
    }
