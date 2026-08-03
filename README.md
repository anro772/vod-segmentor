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
- matches every game to Riot Match-V5 (champion, KDA, result — needs a live
  API key, see below)
- generates a channel-style title (no spoilers) and an action thumbnail
  (face cutout + zoomed fight frame with his green healthbar guaranteed)
- cuts + encodes each game with the chat composited in

Result per game: `output\<VODID>\games\game_NN\`
- `BAUS <CHAMP> <PHRASE>.mp4`  (video, chat burned in)
- `title.txt`     (title incl. emoji)
- `thumbnail.jpg` (1280x720)
- `meta.json`     (segment + riot stats, incl. result — spoilers live here only)

`--only 1,3` on `produce` processes a subset (e.g. after fixing one game).

## Secondary commands (piecemeal / debugging)

```powershell
venv\Scripts\python vodcut.py cut <VODID> [--only 1,3]   # cut videos only (chat included)
venv\Scripts\python vodcut.py enrich "<url>"             # titles/thumbnails only, no cutting
venv\Scripts\python vodcut.py reclassify <VODID>         # re-run CV from cached frames
                                                         # (fast, no re-download; for tuning)
```

## Riot API key (expires every 24h!)

`produce`/`enrich` need a live dev key in `config.yaml` -> `riot.api_key`.
When it expires you get a clear "dev key has expired" error — regenerate at
https://developer.riotgames.com and paste it in. Games on alt accounts don't
match; they still get cut, just with a generic title and no stats note.

## Config highlights (`config.yaml`)

- `chat:` overlay size/position/font; `enabled: false` to disable
- `encode:` `qp` (26 default; lower = better quality, bigger files), `encoder`
  (`amf` = GPU fast, `x264` = CPU)
- `segment:` `min_game_minutes` (10) filters remakes/dodges
- `classify.thresholds` + `templates/v1/` — detection tuning, see below

## Folders

- `work\<VODID>\`   source.mp4, sampled frames, chat.json/chat.mov,
                    classification.json (intermediates; safe to delete after)
- `output\<VODID>\` segments.json, previews\, games\
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
- Twitch VODs expire (~60 days for partners) — run `detect` before then.
