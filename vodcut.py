"""thebausffs VOD splitter — Phase 1 CLI.

  python vodcut.py detect "<vod_url>" [--config config.yaml] [--auto]
  python vodcut.py cut [--segments output/segments.json] [--only 1,3,5]
"""
import argparse
import json
from pathlib import Path

from vodcut.config import load_config
from vodcut.acquire import acquire, vod_id_from_url
from vodcut.sample import sample
from vodcut.classify import load_templates, classify_all
from vodcut.segment import build_segments
from vodcut.cut import cut_segments


def _scope_to_vod(cfg: dict, vod_id: str) -> None:
    """Keep each VOD's intermediates and outputs in their own subfolders."""
    cfg["paths"]["workdir"] = str(Path(cfg["paths"]["workdir"]) / vod_id)
    cfg["paths"]["output_dir"] = str(Path(cfg["paths"]["output_dir"]) / vod_id)


def cmd_detect(args):
    cfg = load_config(args.config)
    _scope_to_vod(cfg, vod_id_from_url(args.url))
    paths, smp = cfg["paths"], cfg["sampling"]
    source = acquire(args.url, paths["workdir"])
    frames = sample(source, paths["workdir"], smp["sample_interval_sec"],
                    smp["canonical_width"], smp["canonical_height"])
    meta = load_templates(paths["templates_dir"])
    samples = classify_all(frames, paths["workdir"], meta, cfg["classify"],
                           smp["sample_interval_sec"])
    segs = build_segments(samples, cfg, frames, paths["output_dir"],
                          vod_id_from_url(args.url))
    if args.auto and segs:
        cut_segments(Path(paths["output_dir"]) / "segments.json", source,
                     paths["output_dir"], cfg["encode"])
    elif segs:
        print(f"\nReview {paths['output_dir']}\\previews\\*.jpg, then run:"
              f"\n  python vodcut.py produce \"{args.url}\"")


def _safe_name(title: str) -> str:
    keep = "".join(c for c in title if c.isalnum() or c in " -_").strip()
    return " ".join(keep.split()) or "game"


def cmd_produce(args):
    """One shot: riot data + titles, then per game: video + thumbnail + title
    into output/games/game_NN/. Run after `detect` (review gate)."""
    from vodcut.enrich import enrich_segments
    from vodcut.thumbnail import make_thumbnail
    from vodcut.cut import cut_one
    from vodcut.chat import ensure_chat
    cfg = load_config(args.config)
    vod_id = vod_id_from_url(args.url)
    _scope_to_vod(cfg, vod_id)
    out_dir = Path(cfg["paths"]["output_dir"])
    source = Path(cfg["paths"]["workdir"]) / "source.mp4"
    only = [int(x) for x in args.only.split(",")] if args.only else None
    chat = ensure_chat(cfg, vod_id, cfg["paths"]["workdir"])
    chat_pos = (cfg["chat"]["x"], cfg["chat"]["y"])
    manifest = enrich_segments(cfg, args.url)
    for seg in manifest["segments"]:
        if only and seg["index"] not in only:
            continue
        gdir = out_dir / "games" / f"game_{seg['index']:02d}"
        gdir.mkdir(parents=True, exist_ok=True)
        title = seg.get("title") or f"game_{seg['index']:02d}"
        (gdir / "title.txt").write_text(title, encoding="utf-8")
        (gdir / "meta.json").write_text(json.dumps(seg, indent=2))
        make_thumbnail(source, seg, cfg, gdir / "thumbnail.jpg")
        cut_one(seg, source, gdir / f"{_safe_name(title)}.mp4", cfg["encode"],
                chat, chat_pos)
        print(f"[produce] game {seg['index']} ready: {gdir}")


def cmd_enrich(args):
    """Phase 2+3: riot match data, titles, thumbnails -> per-game folders."""
    from vodcut.enrich import enrich_segments
    from vodcut.thumbnail import make_thumbnail
    cfg = load_config(args.config)
    _scope_to_vod(cfg, vod_id_from_url(args.url))
    out_dir = Path(cfg["paths"]["output_dir"])
    source = Path(cfg["paths"]["workdir"]) / "source.mp4"
    manifest = enrich_segments(cfg, args.url)
    for seg in manifest["segments"]:
        gdir = out_dir / "games" / f"game_{seg['index']:02d}"
        gdir.mkdir(parents=True, exist_ok=True)
        if seg.get("title"):
            (gdir / "title.txt").write_text(seg["title"], encoding="utf-8")
        (gdir / "meta.json").write_text(json.dumps(seg, indent=2))
        if source.exists():
            make_thumbnail(source, seg, cfg, gdir / "thumbnail.jpg")
            print(f"[enrich] game {seg['index']}: {gdir}")
        else:
            print(f"[enrich] game {seg['index']}: no source.mp4, skipped thumbnail")


def cmd_cut(args):
    from vodcut.chat import ensure_chat
    cfg = load_config(args.config)
    _scope_to_vod(cfg, args.vod_id)
    source = Path(cfg["paths"]["workdir"]) / "source.mp4"
    only = [int(x) for x in args.only.split(",")] if args.only else None
    chat = ensure_chat(cfg, args.vod_id, cfg["paths"]["workdir"])
    cut_segments(args.segments or Path(cfg["paths"]["output_dir"]) / "segments.json",
                 source, cfg["paths"]["output_dir"], cfg["encode"], only,
                 chat, (cfg["chat"]["x"], cfg["chat"]["y"]))


def cmd_reclassify(args):
    """Re-run classify+segment from existing frames (no download/decode)."""
    cfg = load_config(args.config)
    _scope_to_vod(cfg, args.vod_id)
    paths, smp = cfg["paths"], cfg["sampling"]
    frames = Path(paths["workdir"]) / "frames"
    meta = load_templates(paths["templates_dir"])
    samples = classify_all(frames, paths["workdir"], meta, cfg["classify"],
                           smp["sample_interval_sec"])
    build_segments(samples, cfg, frames, paths["output_dir"], args.vod_id)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect")
    d.add_argument("url")
    d.add_argument("--auto", action="store_true")
    d.set_defaults(func=cmd_detect)

    c = sub.add_parser("cut")
    c.add_argument("vod_id", help="twitch vod id (the number in the url)")
    c.add_argument("--segments")
    c.add_argument("--only")
    c.set_defaults(func=cmd_cut)

    pr = sub.add_parser("produce")
    pr.add_argument("url")
    pr.add_argument("--only")
    pr.set_defaults(func=cmd_produce)

    e = sub.add_parser("enrich")
    e.add_argument("url")
    e.set_defaults(func=cmd_enrich)

    r = sub.add_parser("reclassify")
    r.add_argument("vod_id", help="twitch vod id (the number in the url)")
    r.set_defaults(func=cmd_reclassify)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
