"""Phase 3: thumbnail = gameplay still + face cutout + title text.

Cutouts are 1280x720 transparent PNGs named left*/right* by which side the
face occupies; text goes on the opposite side.
"""
import random
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

SIZE = (1280, 720)

# zoom width as a fraction of the source frame; for a 16:9 source the height
# fraction equals the width fraction. Position is champion-centered at grab
# time, clamped away from the HUD / facecam / minimap edges.
ZOOM_W = 0.56
ZOOM_X0 = (0.05, 1 - ZOOM_W - 0.05)   # clamp range for crop origin x
ZOOM_Y0 = (0.03, 0.28)                # bottom edge stays above facecam/HUD

# gameplay area used for scoring/bar detection (excludes HUD, facecam, minimap)
PLAY = (0.08, 0.05, 0.80, 0.72)

# champion floating healthbars, HSV ranges (OpenCV hue 0-179):
# green = the streamer's champ, red = enemies, blue/yellow = teammates
BAR_COLORS = {
    "green": [((45, 120, 120), (75, 255, 255))],
    "red": [((0, 120, 100), (10, 255, 255)), ((168, 120, 100), (179, 255, 255))],
    "blue": [((95, 120, 120), (125, 255, 255))],
    "yellow": [((20, 120, 120), (35, 255, 255))],
}


def _find_bars(frame: np.ndarray) -> dict[str, list[tuple[float, float]]]:
    """Champion healthbar centers (frame-fraction coords) by color: wide,
    short, brightly saturated horizontal strips in the gameplay area."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = (int(PLAY[0] * w), int(PLAY[1] * h),
                      int(PLAY[2] * w), int(PLAY[3] * h))
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    scale = w / 1280  # bar size limits below are calibrated at 1280x720
    found: dict[str, list[tuple[float, float]]] = {}
    for color, ranges in BAR_COLORS.items():
        mask = np.zeros(hsv.shape[:2], np.uint8)
        for lo, hi in ranges:
            mask |= cv2.inRange(hsv, np.array(lo), np.array(hi))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 7), np.uint8))
        centers = []
        for c in cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                  cv2.CHAIN_APPROX_SIMPLE)[0]:
            bx, by, bw, bh = cv2.boundingRect(c)
            # champion bars are ~85x9px at 720p; minion bars (~40x4px) must
            # not pass or they drag the crop toward minion waves. A real bar
            # is a solid filled rectangle — chat/announcement text in the
            # same colors is sparse and fails the fill check.
            fill = mask[by:by + bh, bx:bx + bw].mean() / 255
            if (55 * scale <= bw <= 115 * scale and 4 * scale <= bh <= 14 * scale
                    and bw >= 4 * bh and fill >= 0.55):
                centers.append(((x0 + bx + bw / 2) / w, (y0 + by + bh / 2) / h))
        found[color] = centers
    return found


def _action_score(frame: np.ndarray) -> tuple[float, dict]:
    """Fight-ness of a frame. Base: vivid red/orange (damage numbers, spell
    effects) in the gameplay area. Bonus if the streamer's champ (green bar)
    and at least one enemy (red bar) are both on screen. Dead/gray screens
    score negative; shop/menus have little red."""
    h, w = frame.shape[:2]
    play = frame[int(PLAY[1] * h):int(PLAY[3] * h), int(PLAY[0] * w):int(PLAY[2] * w)]
    hsv = cv2.cvtColor(play, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    if sat.mean() < 40:  # death screen desaturates the world
        return -1.0, {}
    red = (((hue < 12) | (hue > 168)) & (sat > 140) & (val > 120))
    bars = _find_bars(frame)
    score = float(red.mean())
    # champion presence dominates: red effects alone (e.g. hitting a turret
    # with nobody around) must not beat an actual fight between champions
    if bars["green"]:
        score += 0.06
        if bars["red"]:
            score += 0.04
    return score, bars


def pick_action_time(frames_dir: Path, seg: dict, interval: int,
                     window: tuple[float, float] = (0.5, 0.9)
                     ) -> tuple[float, dict]:
    """Best 'mid-fight' sampled timestamp in the later part of the game,
    plus the healthbar centers found in that frame."""
    dur = seg["end_sec"] - seg["start_sec"]
    t0 = seg["start_sec"] + dur * window[0]
    t1 = seg["start_sec"] + dur * window[1]
    # preference tiers: [0] green + another champion (red/blue/yellow bar)
    # on screen, [1] green only, [2] anything. A lone champ on an empty
    # screen makes for a dull thumbnail, so company beats raw action score.
    tiers = [(None, -2.0), (None, -2.0),
             ((seg["start_sec"] + dur * 0.6, {}), -2.0)]
    for t in range(int(t0), int(t1), interval):
        fp = frames_dir / f"{int(t // interval) + 1:06d}.jpg"
        img = cv2.imread(str(fp))
        if img is None:
            continue
        s, bars = _action_score(img)
        others = bars.get("red", []) or bars.get("blue", []) or bars.get("yellow", [])
        if bars.get("green") and others:
            tier = 0
        elif bars.get("green"):
            tier = 1
        else:
            tier = 2
        if s > tiers[tier][1]:
            tiers[tier] = ((t, bars), s)
    return next(entry for entry, _ in tiers if entry)


def _zoom_origin(bars: dict, face_side: str) -> tuple[float, float]:
    """Crop origin so the champions land in the half of the thumbnail NOT
    covered by the face cutout: the streamer's green bar weighs most,
    everyone else pulls the frame toward the fight."""
    pts, weights = [], []
    for color, wgt in (("green", 3.0), ("red", 1.0), ("blue", 0.5), ("yellow", 0.5)):
        for cx, cy in bars.get(color, []):
            pts.append((cx, cy))
            weights.append(wgt)
    if pts:
        cx = np.average([p[0] for p in pts], weights=weights)
        cy = np.average([p[1] for p in pts], weights=weights) + 0.06  # bar sits above champ
    else:
        cx, cy = 0.44, 0.36  # gameplay-area center
    # face covers ~half the canvas: aim the champs at 72% (face left) or
    # 28% (face right) of the crop width instead of dead center
    target = 0.72 if face_side == "left" else 0.28
    x0 = min(max(cx - ZOOM_W * target, ZOOM_X0[0]), ZOOM_X0[1])
    y0 = min(max(cy - ZOOM_W / 2, ZOOM_Y0[0]), ZOOM_Y0[1])

    # hard guarantee: the streamer's green bar must sit inside the visible
    # (non-face) part of the crop — clamping above can push it out
    if bars.get("green"):
        gx, gy = min(bars["green"], key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        vis_x = (0.50, 0.93) if face_side == "left" else (0.07, 0.50)
        if not (x0 + vis_x[0] * ZOOM_W <= gx <= x0 + vis_x[1] * ZOOM_W):
            x0 = gx - ZOOM_W * (0.72 if face_side == "left" else 0.28)
        if not (y0 + 0.06 * ZOOM_W <= gy <= y0 + 0.60 * ZOOM_W):
            y0 = gy - ZOOM_W * 0.25
        x0 = min(max(x0, ZOOM_X0[0]), ZOOM_X0[1])
        y0 = min(max(y0, ZOOM_Y0[0]), ZOOM_Y0[1])
    return float(x0), float(y0)


def _grab_still(source: Path, at_sec: float, dst: Path, zoom: bool) -> None:
    if zoom:
        x0, y0 = zoom
        vf = (f"crop=iw*{ZOOM_W}:ih*{ZOOM_W}:iw*{x0:.4f}:ih*{y0:.4f},"
              f"scale={SIZE[0]}:{SIZE[1]}")
    else:
        vf = f"scale={SIZE[0]}:{SIZE[1]}"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(at_sec), "-i", str(source),
                    "-frames:v", "1", "-vf", vf,
                    str(dst)], check=True)


def _draw_note(img: Image.Image, note: str, side: str, font_path: str) -> None:
    """One small non-spoiler note on the side the face does NOT occupy."""
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, 72)
    w = draw.textlength(note, font=font)
    x = (SIZE[0] - w) - 50 if side == "left" else 50
    draw.text((x, 150), note, font=font, fill="white",
              stroke_width=6, stroke_fill="black")


def make_thumbnail(source: Path, seg: dict, cfg: dict, out: Path) -> Path:
    tcfg = cfg["thumbnails"]
    cutouts = sorted(Path(cfg["paths"]["root"], tcfg["cutouts_dir"]).glob("*.png"))
    cut = random.choice(cutouts)
    side = "left" if cut.name.startswith("left") else "right"

    frames_dir = Path(cfg["paths"]["workdir"]) / "frames"
    if frames_dir.exists():
        at, bars = pick_action_time(frames_dir, seg,
                                    cfg["sampling"]["sample_interval_sec"])
        zoom = _zoom_origin(bars, side)
    else:  # frames were cleaned up — fall back to a fixed point, no zoom
        at = seg["start_sec"] + (seg["end_sec"] - seg["start_sec"]) * tcfg["frame_at"]
        zoom = None
    still_tmp = out.with_suffix(".still.png")
    _grab_still(source, at, still_tmp, zoom)

    img = Image.open(still_tmp).convert("RGBA")
    img.alpha_composite(Image.open(cut).convert("RGBA").resize(SIZE))
    riot = seg.get("riot")
    if riot:
        from vodcut.enrich import fun_fact
        note = fun_fact(riot)
        if note:
            _draw_note(img, note, side, tcfg["font"])

    img.convert("RGB").save(out, quality=92)
    still_tmp.unlink()
    return out
