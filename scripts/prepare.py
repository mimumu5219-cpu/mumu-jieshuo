#!/python3
"""把 pages.json 准备成可截图的网页：拷贝模板、生成 pages.js、视频首帧封面。"""
import argparse, json, os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textutil import split_cues, clean

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(SKILL, "templates", "card.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    args = ap.parse_args()
    proj = os.path.abspath(args.project)

    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        spec = json.load(f)
    pages = spec["pages"]
    meta = spec.setdefault("meta", {})
    sub_cues = {}

    rdir = os.path.join(proj, "render")
    os.makedirs(rdir, exist_ok=True)
    shutil.copyfile(TPL, os.path.join(rdir, "index.html"))

    presenter = meta.get("presenter") or {"type": "none"}
    if presenter.get("type") in ("image", "video") and presenter.get("src"):
        presenter["src"] = "../" + presenter["src"].lstrip("./")

    for i, p in enumerate(pages):
        media = p.get("media") or {}
        typ = media.get("type")
        src = media.get("src")
        if typ in ("image", "video") and src:
            src = "../" + src.lstrip("./")
            if typ == "video":
                poster_rel = f"poster_{i:02d}.png"
                poster_abs = os.path.join(rdir, poster_rel)
                subprocess.run(
                    ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-i", os.path.join(proj, media["src"]), "-frames:v", "1", poster_abs],
                    check=True)
                media = {"type": "poster", "src": poster_rel}
            else:
                media = {"type": "image", "src": src}
            p["media"] = media

        caption = str(p.get("caption") or "")
        # 字幕按“这页要念的原句”切成短句组，逐句跟着语音切换
        cues = split_cues(p.get("read") or caption or "")
        if not cues:
            cues = [clean(caption)] if clean(caption) else []
        p["sub"] = cues
        sub_cues[str(i)] = cues
        for c in cues:
            if len(c) > 24:
                print(f"  ⚠ 第 {i+1} 页有一句字幕偏长（{len(c)} 字）：{c}", file=sys.stderr)

    payload = {"meta": {**meta, "presenter": presenter}, "pages": pages}
    with open(os.path.join(rdir, "pages.js"), "w", encoding="utf-8") as f:
        f.write("window.WB = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    with open(os.path.join(rdir, "cues.json"), "w", encoding="utf-8") as f:
        json.dump(sub_cues, f, ensure_ascii=False, indent=1)
    n_cue = sum(len(v) for v in sub_cues.values())
    print(f"准备完成：{len(pages)} 页 / {n_cue} 条字幕 → {os.path.join(rdir,'index.html')}")


if __name__ == "__main__":
    main()
