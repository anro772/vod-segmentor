# thebausffs VOD Splitter — Build Workflow

A spec for a Claude Code session. Drop this in the repo root. Either @-reference it
or rename it to `CLAUDE.md` so it loads automatically. Read the whole file before
writing any code.

---

## 1. What this project is

I upload VODs for the League of Legends streamer **thebausffs**. I want a tool that
takes a Twitch VOD URL, finds every individual game inside that multi-hour broadcast,
and cuts each game out into its own video file.

The streamer keeps a **fixed stream overlay** and the game's own UI is identical every
match, so the visual states (champ select, loading screen, in-game, endgame) look the
same every time. That is the whole reason this is tractable: we detect games by
**computer vision on the frames**, not by any external stats API. No Riot API, no
op.gg, no keys, no rate limits for the core pipeline.

The four visual states, in order per game:
`champ select -> loading screen -> in-game -> endgame (Victory/Defeat)`

I will provide **reference screenshots** of each of these four states from an actual
VOD. Those become the detection templates. Do not start writing the detector until you
have them (see section 9).

---

## 2. Scope

**Phase 1 (build this now):** reliable detection of game boundaries and clean splitting
into one video per game. Nothing else. Everything below Phase 1 in section 8 is
documented only so you understand the destination — do not build it yet.

The Phase 1 deliverable: given a VOD URL plus the reference templates, produce one
`.mp4` per real game, cut from the start marker to the endgame screen, at 1080p60, at a
reasonable file size, plus a manifest describing what was found. Verified against at
least one real full VOD with a human spot-check.

---

## 3. Environment and stack

- **Language:** Python 3.11+. Use a local `venv`, pin deps in `requirements.txt`.
- **Core deps:** `yt-dlp`, `opencv-python`, `numpy`. `ffmpeg` and `ffprobe` must be on
  PATH (system binaries, called via `subprocess`).
- **OS:** Windows (my machine). AMD RX 7900XTX available, so hardware encode via
  `h264_amf` is an option, but default to software x264 (see section 6).
- Keep everything modular and CLI-driven. No GUI.

---

## 4. Pipeline overview

Five stages, each an independently testable function/module:

1. **acquire** — download the VOD locally once with yt-dlp.
2. **sample** — extract low-frequency frames across the whole VOD (one decode pass).
3. **classify** — label each sampled frame as one of the four states or `OTHER`.
4. **segment** — run a state machine over the label timeline to produce game boundaries.
5. **cut** — re-encode each confirmed game into its own file with ffmpeg.

Between stage 4 and 5 there is a **human review gate** (section 7).

---

## 5. Detection design (the important part)

### 5.1 Frame sampling
- Download once: `yt-dlp -f "best" -o "<workdir>/source.mp4" "<vod_url>"`. This grabs the
  1080p60 source. A 6h VOD is large (~15-25 GB); that's acceptable for Phase 1, delete
  the source after if `keep_source` is false.
- Extract one frame every `sample_interval_sec` (default **5s**) in a single ffmpeg pass:
  `ffmpeg -i source.mp4 -vf "fps=1/5,scale=1280:720" -q:v 3 frames/%06d.jpg`
  Numbering maps back to time: `frame_time = index * sample_interval_sec`.
- Work at a canonical **1280x720** for classification (fast, and UI anchors are still
  clearly distinguishable). Templates must be captured/normalized to this same scale.

### 5.2 Per-frame classification
For each state, define one or more **ROI (region of interest) crops** taken from the
reference screenshots — a stable region that uniquely identifies that state. Match each
ROI template against the same region of the frame using
`cv2.matchTemplate(..., cv2.TM_CCOEFF_NORMED)` and take the peak correlation score.

Assign the frame to the state with the highest score **if** that score clears the
state's threshold; otherwise label it `OTHER` (starting-soon screen, desktop, just
chatting, queue, etc.).

Suggested anchors (finalize against my screenshots):
- **CHAMP_SELECT:** the client's pick/ban layout (side pick columns or the ban bar).
  Choose a region that is pure LoL client, not covered by the webcam/overlay.
- **LOADING:** the row of 10 champion portraits with loading bars. Very distinctive and
  consistent — likely the most reliable start marker.
- **IN_GAME:** minimap region (bottom-right) plus the ability/item bar (bottom-center),
  and/or a static element of his overlay that is only present in-game. The top-center
  game clock is a strong secondary signal and is OCR-friendly if you want to add it
  later.
- **ENDGAME:** the VICTORY/DEFEAT banner region. Also record win vs loss here (color
  palette differs strongly, or OCR the word) — it's free to capture now even though
  titles are a later phase, and it's a rock-solid end marker.

Keep templates in a versioned `templates/` folder. If his overlay ever gets redesigned,
we re-capture templates, nothing else changes.

### 5.3 Segmentation state machine
Input: an ordered list of `(frame_time, state, score)`.

- **Debounce:** require `min_consecutive` (default **2**) matching samples before
  accepting a state change. Filters single-frame false positives (alt-tabs, transitions).
- **Game assembly:** a game cluster is a roughly contiguous run of
  `CHAMP_SELECT -> LOADING -> IN_GAME` ending in `ENDGAME`.
  - `game_start` = first `CHAMP_SELECT` sample of the cluster (or first `LOADING` if
    `start_marker = loading`, or if champ select wasn't captured).
  - `game_end` = the `ENDGAME` detection. **Fallback:** if `ENDGAME` is missed, close the
    segment at the last `IN_GAME` sample and log a warning.
- **Filter non-games:** discard any cluster whose `IN_GAME` run is shorter than
  `min_game_minutes` (default **10**). Kills dodges, spectates, and remakes.
- **Padding:** apply `lead_pad_sec` (default 5) before the start and `tail_pad_sec`
  (default 8) after the end.

Handle gracefully and log, do not crash on: missing champ select (stream started
mid-select), missed endgame, back-to-back games with no gap, brief desktop/alt-tab
frames mid-game, and non-Summoner's-Rift modes (Arena etc.) that don't match templates.

### 5.4 Optional refinements (only if v1 accuracy needs it)
- **Adaptive skip:** once `IN_GAME` is confirmed, jump the cursor forward
  `min_game_minutes` before resuming the `ENDGAME` search. Speeds up long VODs.
- **Boundary tightening:** re-sample at 1s granularity in a small window around each
  coarse boundary to pin exact cut points. Generous padding usually makes this unneeded.

---

## 6. Cutting and encoding

Cut from the local `source.mp4` using the boundaries from `segments.json`. Re-encode
(don't stream-copy) so cuts are frame-accurate and files are smaller.

Default (software, quality/size balance):
```
ffmpeg -ss <start> -to <end> -i source.mp4 \
  -c:v libx264 -preset medium -crf 21 -pix_fmt yuv420p \
  -c:a copy -movflags +faststart <out>.mp4
```
- **CRF 21** is visually clean at 1080p60 and roughly halves size vs the Twitch source.
  Higher CRF = smaller, lower = higher quality. Make it a config value.
- **Preserve 60fps** — do not add an fps filter that drops frames.
- **Audio:** Twitch VOD audio is AAC, so `-c:a copy` is lossless and instant. DMCA-muted
  stretches stay silent, which is fine.

Optional (hardware, faster, slightly larger for equal quality) for the 7900XTX:
`-c:v h264_amf -rc cqp -qp_i 20 -qp_p 20` (verify the exact AMF flags available in the
installed ffmpeg build). Expose encoder choice via config.

Optional (fast, lossless, keyframe-imprecise, bigger files): `-c copy`. Only if I ever
say I don't care about size or exact cut points.

---

## 7. Human review gate and CLI

Two commands. Do **not** auto-cut without a confirmation step.

- `python vodcut.py detect "<vod_url>" [--config config.yaml]`
  Runs acquire -> sample -> classify -> segment. Writes:
  - `work/classification.json` — every sample's state + scores (for threshold tuning).
  - `output/segments.json` — the final games: index, start/end seconds, start/end
    timecodes, detected win/loss, and paths to a **preview frame** saved at each start
    and each end boundary.
  - Prints a summary table so I can sanity-check counts and durations.
  I eyeball the preview frames, then:
- `python vodcut.py cut [--segments output/segments.json] [--only 1,3,5]`
  Cuts the confirmed (or selected) segments. `--only` lets me cut a subset.

A combined `--auto` flag on `detect` may run both, but the split is the default.

---

## 8. Config

`config.yaml` (or module constants) — everything tunable lives here:
- paths: `workdir`, `output_dir`, `templates_dir`
- `sample_interval_sec` (5), `canonical_res` (1280x720)
- per-state match `thresholds`
- `start_marker`: `champ_select` | `loading` (default `champ_select`)
- `lead_pad_sec` (5), `tail_pad_sec` (8), `min_game_minutes` (10), `min_consecutive` (2)
- encode: `encoder` (`x264` | `amf`), `crf` (21), `audio` (`copy`)
- `keep_source` (false), `keep_frames` (false)

---

## 9. How to proceed (first session)

1. **Ask me for:** (a) one sample VOD URL, and (b) the four reference screenshots
   (champ select, loading, in-game, endgame). You cannot build classification without
   the templates — do not guess or stub them with placeholders and move on.
2. Scaffold the repo: `venv`, `requirements.txt`, `config.yaml`, module skeletons for
   the five stages, and the two-command CLI.
3. Build and verify stage by stage against the real sample VOD:
   acquire -> sample -> classify (tune thresholds using `classification.json`) ->
   segment -> review gate -> cut. Prove each stage before moving on.
4. Save intermediate artifacts (frames, JSONs, boundary previews) so I can debug and so
   thresholds are tunable without re-running everything.
5. Keep functions pure and independently callable. No stage should require re-downloading.

---

## 10. Naming (Phase 1 minimal)

Phase 1 output names can stay simple and get enriched later:
`game_<NN>_<vodid>_<start-timecode>.mp4`
Rich naming (date / champ / win-loss / fact / title) is Phase 2+, below.

---

## Roadmap (NOT in scope now — context only)

Documented so you understand the direction. Do not build these in the Phase 1 session.

- **Phase 2 — Titles.** Generate uppercase titles per game from: champion (already
  readable via OCR on the loading-screen portrait/name the detector sees), win/loss
  (already captured at endgame), and a standout stat. The champ and result come free from
  CV; if I want deeper stats (damage, tower damage, KDA) for a title fact, pull them from
  the **Riot Match-V5 API** by PUUID — that's the origin the stat sites all read from, so
  we'd go straight to it, not through op.gg.
- **Phase 3 — Thumbnails.** Composite 4-5 cropped Baus face shots over a gameplay still
  from that game. His webcam sits at a fixed overlay position, so face crops are a trivial
  fixed-region crop from any in-game frame. Simple layout, nothing fancy.
- **Phase 4 — Organization.** One tidy folder or manifest per game carrying date, champ,
  win/loss, one fact, the title, and the thumbnail, ready for me to manually pick and
  upload to the YouTube channel.
- **Optional — Orchestration.** Only after the core is solid, an outer wrapper (a simple
  scheduler/script, or n8n if I want the dashboard and connectors) can chain
  detect -> cut -> titles -> thumbnails -> upload. The orchestrator only ever *calls* this
  tool; it never replaces the CV core.

---

## Known gotchas

- Twitch VODs expire (14 days most channels, 60 for partners), so run before then.
- Non-1080p VODs: normalize resolution before matching.
- Overlay redesigns invalidate templates — re-capture into a new `templates/` version.
- Non-Rift modes won't match; skip and log rather than mis-cutting.
- Very short games / remakes are filtered by `min_game_minutes`.
