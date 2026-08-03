"""Sanity check: each reference screenshot must classify as its own state."""
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vodcut.config import load_config
from vodcut.classify import load_templates, classify_frame

cfg = load_config()
meta = load_templates(cfg["paths"]["templates_dir"])
w, h = cfg["sampling"]["canonical_width"], cfg["sampling"]["canonical_height"]

expected = {
    "vod_champ_select.png": "CHAMP_SELECT",
    "vod_loading.png": "LOADING",
    "vod_ingame.png": "IN_GAME",
    "vod_endgame.png": "ENDGAME",
}
ok = True
for ref, want in expected.items():
    img = cv2.resize(cv2.imread(str(ROOT / "refs" / ref)), (w, h))
    state, scores = classify_frame(img, meta, cfg["classify"])
    flag = "OK " if state == want else "FAIL"
    if state != want:
        ok = False
    print(f"{flag} {ref}: {state}  {scores}")
sys.exit(0 if ok else 1)
