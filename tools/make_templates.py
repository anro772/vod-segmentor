"""Generate detection templates from the reference screenshots in refs/.

Scales each ref to the canonical 1280x720, crops the ROIs defined below
(fractional coordinates), and writes the crops + rois.json to templates/v1/.

Re-run whenever the overlay changes (bump the templates dir version).
"""
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
CANON = (1280, 720)

# state -> list of anchors: (name, ref_image, x0, y0, x1, y1) as fractions
# sources are real sampled VOD frames (1280x720), not the hand screenshots —
# the screenshots had a slightly different aspect/scale and hurt match scores
ROIS = {
    "CHAMP_SELECT": [
        ("cs_header", "vod_champ_select.png", 0.225, 0.012, 0.440, 0.048),
    ],
    "LOADING": [
        ("load_crest", "vod_loading.png", 0.460, 0.050, 0.530, 0.095),
    ],
    # per-anchor thresholds (4th tuple slot below via THRESH): measured on VOD
    # 2832004308 — stats icons: in-game 0.86+, non-game <=0.25; topright digits
    # vary so it hovers ~0.6; minimap terrain is often covered by icons/fog.
    "IN_GAME": [
        ("ig_topright", "vod_ingame.png", 0.810, 0.000, 0.995, 0.028),
        ("ig_minimap_terrain", "vod_ingame.png", 0.820, 0.680, 0.970, 0.930),
        ("ig_stats_icons", "vod_ingame.png", 0.238, 0.900, 0.262, 0.985),
    ],
    # endgame client has several tab layouts (PROGRESSION/SCOREBOARD vs
    # STATS/GRAPHS/RUNES) — the VICTORY/DEFEAT banner is the constant; any
    # single anchor clearing threshold marks the state, win/loss = which
    # banner scored higher.
    "ENDGAME": [
        ("eg_tabs", "vod_endgame.png", 0.008, 0.085, 0.140, 0.110),
        ("eg_victory", "vod_endgame.png", 0.030, 0.012, 0.125, 0.050),
        ("eg_defeat", "vod_endgame_defeat.png", 0.030, 0.012, 0.125, 0.050),
    ],
}

# per-anchor score thresholds; anchors not listed use the state threshold from config
ANCHOR_THRESH = {
    "ig_stats_icons": 0.70,
    "ig_topright": 0.45,
    "ig_minimap_terrain": 0.30,
}


def main() -> None:
    out_dir = ROOT / "templates" / "v1"
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"canonical": list(CANON), "states": {}}

    for state, anchors in ROIS.items():
        meta["states"][state] = []
        for name, ref, x0, y0, x1, y1 in anchors:
            img = cv2.imread(str(ROOT / "refs" / ref))
            if img is None:
                sys.exit(f"missing reference image refs/{ref}")
            img = cv2.resize(img, CANON, interpolation=cv2.INTER_AREA)
            px0, py0 = int(x0 * CANON[0]), int(y0 * CANON[1])
            px1, py1 = int(x1 * CANON[0]), int(y1 * CANON[1])
            crop = img[py0:py1, px0:px1]
            cv2.imwrite(str(out_dir / f"{name}.png"), crop)
            entry = {"name": name, "file": f"{name}.png", "roi": [x0, y0, x1, y1]}
            if name in ANCHOR_THRESH:
                entry["threshold"] = ANCHOR_THRESH[name]
            meta["states"][state].append(entry)
            print(f"{state}: {name} -> {px1 - px0}x{py1 - py0}px")

    (out_dir / "rois.json").write_text(json.dumps(meta, indent=2))
    print(f"wrote {out_dir / 'rois.json'}")


if __name__ == "__main__":
    main()
