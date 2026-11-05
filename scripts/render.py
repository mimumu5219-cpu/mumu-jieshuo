#!/usr/bin/env python3
"""无头 Chrome 逐页截图：入场动画帧 + 1 张定格页（1080×1440），并量回字幕逐字坐标。"""
import argparse, html, json, os, re, subprocess, sys, tempfile, threading
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
FPS = 30
SETTLE = 1.5          # 动画全部结束后的定格时刻

_tl = threading.local()


def find_chrome():
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise SystemExit("找不到 Chrome，请设置环境变量 CHROME 指向浏览器可执行文件")


def profile():
    if not hasattr(_tl, "dir"):
        _tl.dir = tempfile.mkdtemp(prefix="wbchrome_")
    return _tl.dir


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(33)
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def shot(chrome, idx_html, page_no, t, out, cue=None, cap=None):
    q = f"file://{idx_html}?p={page_no}&shot=1&t={t:.4f}"
    if cue:
        q += f"&c={cue}"
    if cap is not None:
        q += f"&cap={cap}"
    url = q
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile()}",
           "--window-size=1080,1440", "--force-device-scale-factor=1",
           "--virtual-time-budget=2500", "--default-background-color=00000000",
           f"--screenshot={out}", url]
    subprocess.run(cmd, check=True, capture_output=True)
    if not os.path.exists(out):
        raise SystemExit(f"截图失败：{out}")
    w, h = png_size(out)
    if (w, h) != (1080, 1440):
        raise SystemExit(f"截图尺寸不对：{w}×{h}（期望 1080×1440）")


def measure(chrome, idx_html, page_no, total, cue=0):
    """dump-dom 读回 ?measure=1 里逐字的真实 x/宽度（相对字幕框左边），用于整字擦出对齐。"""
    url = f"file://{idx_html}?p={page_no}&measure=1&c={cue}"
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile()}",
           "--window-size=1080,1440", "--force-device-scale-factor=1",
           "--virtual-time-budget=2000", "--dump-dom", url]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    pat = re.compile(r'data-c="([^"]*)"[^>]*?data-x="(-?[\d.]+)"[^>]*?data-w="([\d.]+)"')
    chars = [{"c": html.unescape(c), "x": float(x), "w": float(w)}
             for c, x, w in pat.findall(out)]
    if not chars:
        print(f"  第 {page_no} 页第 {cue+1} 句字幕坐标未量到，assemble 将回退估算", file=sys.stderr)
        return f"{page_no - 1}:{cue}", None
    return f"{page_no - 1}:{cue}", {
        "text": "".join(d["c"] for d in chars),
        "page": page_no, "cue": cue, "total": total,
        "chars": chars,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--only", type=int, default=0, help="只渲染第 N 页（1 起，调试用）")
    ap.add_argument("--frames", type=int, default=30, help="每页入场动画帧数（30fps）")
    ap.add_argument("--no-anim", action="store_true")
    ap.add_argument("--no-measure", action="store_true", help="不采集字幕逐字坐标")
    args = ap.parse_args()
    proj = os.path.abspath(args.project)

    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        spec = json.load(f)
    total = len(spec["pages"])

    chrome = find_chrome()
    rdir = os.path.join(proj, "render")
    idx = os.path.join(rdir, "index.html")
    if not os.path.exists(idx):
        raise SystemExit("先运行 prepare.py")

    cues = {}
    cf = os.path.join(rdir, "cues.json")
    if os.path.exists(cf):
        with open(cf, encoding="utf-8") as f:
            cues = json.load(f)

    pages = [args.only - 1] if args.only else list(range(total))
    tasks = []
    for i in pages:
        adir = os.path.join(rdir, "anim", f"page_{i:02d}")
        os.makedirs(adir, exist_ok=True)
        # 清掉旧帧，避免帧数变化后拼错
        for fn in os.listdir(adir):
            if fn.startswith("f_") and fn.endswith(".png"):
                os.remove(os.path.join(adir, fn))
        if not args.no_anim:
            for k in range(args.frames):
                # 定格页不带字幕，字幕由成片阶段的语速条（capstrip）叠加
                tasks.append((i, k / FPS, os.path.join(adir, f"f_{k:03d}.png"), None, 0))
        tasks.append((i, SETTLE, os.path.join(rdir, f"page_{i:02d}.png"), None, 0))
        for j in range(len(cues.get(str(i)) or [])):
            tasks.append((i, SETTLE, os.path.join(rdir, f"capstrip_{i:02d}_{j}.png"), j, 1))

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda a: shot(chrome, idx, a[0] + 1, a[1], a[2], a[3], a[4]), tasks))

    # 把每句字幕从整页截图里裁出来（固定位置），成片时按语音时间叠加
    nstrip = 0
    for i in pages:
        for j in range(len(cues.get(str(i)) or [])):
            src = os.path.join(rdir, f"capstrip_{i:02d}_{j}.png")
            if not os.path.exists(src):
                continue
            Image.open(src).convert("RGB").crop((86, 1296, 86 + 906, 1296 + 66)).save(src)
            nstrip += 1

    print(f"截图完成：{len(pages)} 页，{len(tasks)} 张（含 {0 if args.no_anim else args.frames} 帧/页入场、{nstrip} 条字幕条）")

    if not args.no_measure:
        layout = {}
        jobs = [(i, j) for i in pages for j in range(len(cues.get(str(i)) or [1]))]
        with ThreadPoolExecutor(max_workers=4) as ex:
            for key, m in ex.map(lambda a: measure(chrome, idx, a[0] + 1, total, a[1]), jobs):
                if m:
                    layout[key] = m
        with open(os.path.join(rdir, "caption_layout.json"), "w", encoding="utf-8") as f:
            json.dump(layout, f, ensure_ascii=False, indent=1)
        print(f"字幕坐标：{len(layout)} 条已量回 → render/caption_layout.json")


if __name__ == "__main__":
    main()
