# vod-segmentor

Turns a full thebausffs Twitch VOD into ready-to-upload YouTube games:
one video per game with synced Twitch chat burned in, plus a title, a
thumbnail, and a metadata file. Full original spec: `workflow.md`.

## Setup (once)

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy config.example.yaml config.yaml     # then paste your Riot dev key into it
```
`ffmpeg`/`ffprobe` must be on PATH. For the chat overlay, download
TwitchDownloaderCLI (Windows x64 zip) from
https://github.com/lay295/TwitchDownloader/releases into
`tools\TwitchDownloaderCLI\`, then run
`tools\TwitchDownloaderCLI\TwitchDownloaderCLI.exe ffmpeg --download` once.

For titles, install [Ollama](https://ollama.com) and pull the model:
```powershell
ollama pull qwen2.5:7b-instruct
```
Optional — with `titles.provider: none` the tool falls back to the built-in
phrase templates and needs no LLM at all. The speech model (faster-whisper
`small.en`, ~460 MB) downloads itself on first use.

### AMD GPUs: use the Vulkan backend, not ROCm

**Required on this machine (RX 7900 XTX).** Ollama defaults to its ROCm/HIP
backend, which hard-faults on RDNA3 under Windows — it crashed the GPU three
times here with `ROCm error: context is destroyed` / `Exception 0xc0000005`,
taking the whole desktop with it. It is *not* an out-of-memory problem; the
card was correctly detected with 24 GiB free. Force Vulkan instead:

```powershell
setx OLLAMA_VULKAN 1          # makes the Vulkan backend available
setx OLLAMA_LLM_LIBRARY vulkan # ...and actually selects it over ROCm
```
**Both are needed.** `OLLAMA_VULKAN=1` on its own only makes Vulkan *available*;
Ollama still picks ROCm when it sees an AMD card. These are already set on this
machine. Restart Ollama once after setting them, then confirm the backend —
it must say `library=Vulkan`, not `library=ROCm`:
```powershell
Select-String -Path "$env:LOCALAPPDATA\Ollama\server.log" -Pattern "inference compute"
```
Measured after the switch: 29/29 layers offloaded, ~4.2 GB model buffer,
224 MB KV cache, ~8s per game, zero crashes across 24 games.

Two related settings in `config.yaml` matter for the same reason: `num_ctx`
(4096) and `num_predict` (400). Left to itself Ollama sizes the context from
total VRAM — 32768 on a 24 GB card — which allocates gigabytes of KV cache for
prompts that are barely 2k tokens. Don't raise these without a reason.

If titles still crash the GPU, set `titles.num_gpu: 0` in `config.yaml` to run
the model on CPU. It costs roughly a minute per game and is completely safe.

## Before you run a new VOD (30 second checklist)

1. **Fresh Riot dev key** in `config.yaml` → `riot.api_key`. Expires every 24h;
   regenerate at https://developer.riotgames.com. Only needed the *first* time
   you enrich a given VOD — after that its match data is cached to disk and
   `retitle` works forever without a key.
2. **Ollama running.** `produce` calls it once per game. If it isn't running
   you still get videos, thumbnails and metadata, just templated titles — and
   `retitle <VODID>` fixes them later in seconds.
3. That's it. Same two commands as always.

## The whole workflow (2 commands)

```powershell
# 1. Download + analyze the VOD (CV detection of every game)
venv\Scripts\python vodcut.py detect "https://www.twitch.tv/videos/<VODID>"
```
Prints a table of detected games (start/end, in-game minutes, win/loss).
Review it plus the boundary snapshots in `output\<VODID>\previews\` before
cutting. Times/durations off? See Troubleshooting.

```powershell
# 2. Produce everything: videos + chat overlay + titles + thumbnails
venv\Scripts\python vodcut.py produce "https://www.twitch.tv/videos/<VODID>"
```
This automatically:
- downloads the Twitch chat replay and renders it once as a transparent
  overlay with 7TV/BTTV/FFZ emotes (TwitchDownloaderCLI, `tools/`)
- matches every game to Riot Match-V5 — champion, KDA, **lane opponent, solo
  kills, turret plates, gold curve, comeback/int/splitpush flags** (needs a
  live API key, see below; cached to disk afterwards)
- finds the chat hype spikes and **transcribes what Baus said** during each game
- generates a title from those facts + his actual quotes via a local LLM, then
  hard-validates it (no spoilers, no invented stats, never repeats a past title)
  and an action thumbnail (face cutout + zoomed fight frame)
- cuts + encodes each game with the chat composited in

Result per game: `output\<VODID>\games\game_NN\`
- `BAUS <CHAMP> <PHRASE>.mp4`  (video, chat burned in)
- `title.txt`     (title incl. emoji)
- `thumbnail.jpg` (1280x720)
- `meta.json`     (segment + riot stats, incl. result — spoilers live here only)

`--only 1,3` on `produce` processes a subset (e.g. after fixing one game).

## How titles are made

Titles used to come from 16 hardcoded phrases, so 24 uploads produced only 13
distinct ones, two of them identical. Now each title is built from what
actually happened in that specific game:

1. **Facts** (`vodcut/riot.py`) — Match-V5 including the `challenges` block,
   **the lane opponent** (champion + their KDA + gold diff), team objectives and
   the timeline gold curve. Reduced to plain-English "story" lines like
   *"he solo killed his lane opponent several times"*.
2. **Chat** (`vodcut/chatstats.py`) — message-rate spikes in `chat.json` mark
   the moments that mattered. Used for *timing and hype only*; chat words are
   never quoted, since they're emote names and spoilers.
3. **Speech** (`vodcut/transcribe.py`) — faster-whisper over the game audio,
   aimed by those spikes. Quotes are filtered to ones actually about the match
   (a talk about Maroon 5 is dropped) and free of Whisper garbles.
4. **Generation + ranking** (`vodcut/titles.py`) — the LLM writes 8 candidates;
   every one is hard-validated, then the survivors are **scored** and the best
   is chosen. Scoring rewards titles built from a real quote and penalises
   generic phrasing, echoes of the story hints, and stray numbers.

The validator is the important part — the model is never trusted. A candidate
is rejected for: lowercase, wrong length, wrong/missing emoji, revealing the
result, **profanity**, chat slang, sensitive topics, a number not present in the
facts, an invented or garbled word (checked with `wordfreq`), naming a champion
who wasn't in the game (Riot's Data Dragon list), a near-miss champion spelling
(`rapidfuzz`), or resembling any previous title. If everything fails it falls
back to the old phrase templates, so the pipeline never breaks.

`output\title_history.json` records every title ever produced and is what stops
repeats **across uploads**. Do not delete it.

## Secondary commands (piecemeal / debugging)

```powershell
venv\Scripts\python vodcut.py cut <VODID> [--only 1,3]   # cut videos only (chat included)
venv\Scripts\python vodcut.py enrich "<url>"             # titles/thumbnails only, no cutting
venv\Scripts\python vodcut.py reclassify <VODID>         # re-run CV from cached frames
                                                         # (fast, no re-download; for tuning)
venv\Scripts\python vodcut.py retitle <VODID> [--only 3] [--rename]
```

`retitle` is the title-tuning loop: it reuses the cached Riot JSON, chat stats
and transcripts, so it needs no download, **no live Riot key**, and no
re-encode — about 8 seconds per game. Don't like a batch of titles? Just run it
again; each run re-rolls them and the history stops it repeating itself.

It always rewrites `title.txt` and `meta.json`. By default it only *reports*
which `.mp4` filenames no longer match; add `--rename` to actually rename them.
That's deliberate — a video you already uploaded shouldn't silently change name.

## Riot API key (expires every 24h!)

`produce`/`enrich` need a live dev key in `config.yaml` -> `riot.api_key` the
**first** time they run on a given VOD. When it expires you get a clear "dev
key has expired" error — regenerate at https://developer.riotgames.com and
paste it in.

After that first run the match data is cached in `work\<VODID>\riot\`, so
`retitle` — and any later `enrich` of the same VOD — works with an expired or
missing key. You only need a fresh key when you pick up a *new* VOD. If the key
is dead and nothing is cached, the pipeline says so and still cuts the videos,
just with template titles and no stats.

Games on alt accounts don't match; they still get cut, just with a generic
title and no stats note.

## Config highlights (`config.yaml`)

- `chat:` overlay size/position/font; `enabled: false` to disable
- `encode:` `qp` (26 default; lower = better quality, bigger files), `encoder`
  (`amf` = GPU fast, `x264` = CPU)
- `segment:` `min_game_minutes` (10) filters remakes/dodges
- `classify.thresholds` + `templates/v1/` — detection tuning, see below
- `titles:` `model` (any Ollama model), `candidates` (6 tries per game),
  `similarity_max` (0.5 — lower = stricter about resembling past titles),
  `provider: none` to disable the LLM entirely
- `transcribe:` `mode: full` (whole game) or `spikes` (only around chat peaks,
  ~5x cheaper), `model` (`small.en`; `distil-large-v3` if accuracy lacks),
  `enabled: false` to skip speech
- `riot:` `cache: true` keeps every match doc on disk, `fetch_timeline: true`
  adds the gold curve

## Folders

- `work\<VODID>\`   intermediates; safe to delete once you've uploaded
- `output\<VODID>\` segments.json, previews\, games\
- `output\title_history.json`  every title ever generated — this is what stops
                    titles repeating across uploads. Don't delete it.

### Disk footprint per VOD

Only `source.mp4`, the chat renders and the finished games are large. Measured
on a 6h VOD (`work\2836155423`):

| File | Size | |
|---|---|---|
| `source.mp4` | 15.8 GB | the Twitch download |
| `frames\` | 576 MB | 4290 sampled jpgs for CV detection |
| `chat.mp4` + `chat_mask.mp4` | 448 MB | rendered overlay |
| `chat.json` | 118 MB | chat replay |
| `riot\` | **11 MB** | cached match + timeline JSON |
| `classification.json` | 1.3 MB | per-frame CV scores |
| `transcripts\` | **0.1 MB** | what he said, all 11 games |
| `chat_stats.json` | **0.01 MB** | hype spikes |

The titling work adds about **11 MB per VOD** — essentially nothing. It writes
no audio files (clips go to a temp dir and are deleted) and no model files
beyond Ollama's own store. The finished videos in `output\<VODID>\games\` are
unchanged in size (~13 GB for 11 games).

The one cost that isn't disk is **time**: `produce` now transcribes each game
before titling, roughly 30 minutes of CPU for a 6h VOD. It runs on the CPU
while the GPU encodes, so it overlaps with work you were already waiting on.
Set `transcribe.enabled: false` to skip it (titles then use stats only).
- `refs\` + `tools\make_templates.py` -> `templates\v1\`  detection templates
- `cutouts\`        transparent 1280x720 face PNGs (left*/right* = face side)
- `tools\TwitchDownloaderCLI\`  chat downloader/renderer (+ its own ffmpeg)

## Troubleshooting

- **Weird detection table** (missing games, fragmented runs): inspect
  `work\<VODID>\classification.json` scores, adjust thresholds, then
  `reclassify <VODID>` — no re-download needed.
- **Overlay redesign** (his stream layout changes): capture new reference
  frames into `refs\`, adjust `tools\make_templates.py`, re-run it, then
  `reclassify`. Same story if chat position needs moving: `chat.x/y`.
- **Templates/thumbnails need his champ visible**: detection keys on the
  in-game HUD, thumbnails on the green healthbar — both are overlay-independent
  game UI, so patches rarely break them.
- **Titles feel repetitive or generic**: `retitle <VODID>` re-rolls them in
  seconds. If many say `[template fallback]`, every LLM candidate failed
  validation — the printed rejection reasons say exactly why. Raise
  `candidates`, or lower `similarity_max` to push harder for novelty.
- **Titles ignore what he said**: check `work\<VODID>\transcripts\game_NN.json`
  exists and reads sensibly. Without `source.mp4` there is no speech and titles
  fall back to stats only (still varied, just less personal). Poor accuracy on
  jargon? Switch `transcribe.model` to `distil-large-v3`.
- **A title contains a garbled word**: Whisper mistranscribed it and it entered
  the allowed vocabulary legitimately. `retitle` again; the scorer penalises
  rare words, so it usually picks a cleaner candidate the second time.
- **GPU freezes or the screen blanks during titling**: you are on the ROCm
  backend. See the AMD section above — this is the single most important
  setting on this machine. `titles.num_gpu: 0` is the safe fallback.
- **`ollama unreachable`**: start Ollama, or set `titles.provider: none` to use
  the phrase templates. A crashed runner is retried 3x before giving up, and
  the pipeline never blocks on the LLM.
- Twitch VODs expire (~60 days for partners) — run `detect` before then.
