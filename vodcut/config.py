from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_config(path: str | Path = "config.yaml") -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("workdir", "output_dir", "templates_dir"):
        cfg["paths"][key] = str((ROOT / cfg["paths"][key]).resolve())
    cfg["paths"]["root"] = str(ROOT)
    return cfg
