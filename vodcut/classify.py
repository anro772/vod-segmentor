"""Label each sampled frame as CHAMP_SELECT / LOADING / IN_GAME / ENDGAME / OTHER
via template matching of game-invariant UI chrome regions."""
import json
from pathlib import Path

import cv2
import numpy as np

STATES = ["CHAMP_SELECT", "LOADING", "IN_GAME", "ENDGAME"]


def load_templates(templates_dir: str | Path) -> dict:
    tdir = Path(templates_dir)
    meta = json.loads((tdir / "rois.json").read_text())
    for anchors in meta["states"].values():
        for a in anchors:
            a["img"] = cv2.imread(str(tdir / a["file"]))
            if a["img"] is None:
                raise FileNotFoundError(tdir / a["file"])
    return meta


def _match_anchor(frame: np.ndarray, anchor: dict, pad: float) -> float:
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = anchor["roi"]
    px0 = max(0, int((x0 - pad) * w))
    py0 = max(0, int((y0 - pad) * h))
    px1 = min(w, int((x1 + pad) * w))
    py1 = min(h, int((y1 + pad) * h))
    window = frame[py0:py1, px0:px1]
    tpl = anchor["img"]
    if window.shape[0] < tpl.shape[0] or window.shape[1] < tpl.shape[1]:
        return 0.0
    res = cv2.matchTemplate(window, tpl, cv2.TM_CCOEFF_NORMED)
    return float(res.max())


def classify_frame(frame: np.ndarray, meta: dict, cfg: dict) -> tuple[str, dict]:
    """Returns (state, scores). scores maps anchor name -> peak correlation."""
    pad = cfg["roi_search_pad"]
    thresholds = cfg["thresholds"]
    scores: dict[str, float] = {}
    state_score: dict[str, float] = {}

    for state in STATES:
        anchors = meta["states"][state]
        vals, hits = [], 0
        for a in anchors:
            s = _match_anchor(frame, a, pad)
            scores[a["name"]] = round(s, 4)
            vals.append(s)
            if s >= a.get("threshold", thresholds[state]):
                hits += 1
        if state == "IN_GAME":
            state_score[state] = float(np.mean(sorted(vals)[-2:]))
            if hits < cfg["ingame_min_anchors"]:
                state_score[state] = 0.0
        else:
            state_score[state] = max(vals) if hits else 0.0

    best = max(state_score, key=state_score.get)
    if state_score[best] <= 0.0:
        return "OTHER", scores
    return best, scores


def classify_all(frames_dir: str | Path, workdir: str, meta: dict, cfg: dict,
                 interval_sec: int) -> list[dict]:
    frames = sorted(Path(frames_dir).glob("*.jpg"))
    results = []
    for i, fp in enumerate(frames):
        img = cv2.imread(str(fp))
        if img is None:
            continue
        state, scores = classify_frame(img, meta, cfg)
        t = (int(fp.stem) - 1) * interval_sec
        results.append({"frame": fp.name, "time": t, "state": state, "scores": scores})
        if i % 200 == 0:
            print(f"[classify] {i}/{len(frames)} t={t}s -> {state}")
    out = Path(workdir) / "classification.json"
    out.write_text(json.dumps(results, indent=1))
    print(f"[classify] wrote {out} ({len(results)} samples)")
    return results
