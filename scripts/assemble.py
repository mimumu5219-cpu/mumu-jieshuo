#!/usr/bin/env python3
"""合成成片：入场动画帧 + 定格页 + 配音 + 主播窗(口型) + 逐字擦出字幕 + 进度条 → mp4。"""
import argparse, glob, json, math, os, re, struct, subprocess, sys, wave

from PIL import Image, ImageDraw

TAIL = 0.35            # 每页配音后的留白
FADE = 0.12            # 切页白闪
FPS = 30
W, H = 1080, 1440

# 版式坐标（与 templates/card.html 严格对应，单位：舞台绝对像素）
MEDIA = (122, 374, 834, 424)
PRES = (103, 905, 370, 357)
BAR = (86, 1387, 930, 6)
CAP = (86, 1296, 906, 66)


def make_mask(path, w, h, r):
    ss = 4
    m = Image.new("L", (w * ss, h * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=255)
    m.resize((w, h), Image.LANCZOS).save(path)


def make_fill(path, w, h, r, color=(0, 209, 255, 255)):
    ss = 4
    m = Image.new("L", (w * ss, h * ss), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=255)
    img = Image.new("RGBA", (w, h), color)
    img.putalpha(m.resize((w, h), Image.LANCZOS))
    img.save(path)


def make_cover(path, page_png):
    x, y, w, h = BAR
    Image.open(page_png).convert("RGB").crop((x, y, x + w, y + h)).save(path)


def make_gutter(path, page_png):
    """卡片右侧的静态背景竖条：字幕遮片右移会溢出卡片，用它盖回来。"""
    x0 = 1016
    Image.open(page_png).convert("RGB").crop((x0, 0, W, H)).save(path)


def mouth_curve(path, nframes):
    """把音轨算成每帧开口量 0–1：RMS → 归一 → 上下门限 → 惯性平滑。"""
    with wave.open(path, "rb") as w:
        sr, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if nch != 1 or sw != 2:
        return [0.0] * nframes
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    win = int(sr / FPS)
    env = []
    for k in range(nframes):
        seg = vals[k * win:(k + 1) * win]
        if not seg:
            env.append(0.0)
            continue
        env.append(math.sqrt(sum(v * v for v in seg) / len(seg)) / 32768.0)
    ranked = sorted(env)
    p95 = ranked[max(0, int(len(ranked) * 0.95) - 1)] or 1e-6
    out, prev = [], 0.0
    for v in env:
        v = min(1.0, v / p95)
        v = 0.0 if v < 0.2 else (v - 0.2) / 0.8
        prev = prev * 0.55 + v * 0.45
        out.append(prev)
    return out


def make_mouths(dirpath, curve, body=(29, 22, 23), w=18, hmax=15):
    """逐帧画一张小嘴 PNG，frames 用 %04d 命名，供 ffmpeg image2 直接读。"""
    os.makedirs(dirpath, exist_ok=True)
    ss = 4
    cw, ch = 30, 26
    for k, o in enumerate(curve):
        h = 2.0 + (hmax - 2.0) * (o ** 0.85)
        wide = w * (0.9 + 0.18 * o)
        img = Image.new("RGBA", (cw * ss, ch * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, top = cw * ss / 2, 2 * ss
        d.ellipse([cx - wide * ss / 2, top, cx + wide * ss / 2, top + h * ss],
                  fill=body + (255,))
        img.resize((cw, ch), Image.LANCZOS).save(
            os.path.join(dirpath, "m_%04d.png" % k))


def plain(t):
    return re.sub(r"\*\*", "", str(t or ""))


def caption_size(text):
    n = len(plain(text))
    if n <= 16:
        return 42
    if n <= 20:
        return 39
    if n <= 26:
        return 34
    if n <= 32:
        return 30
    return 26


def caption_width(text, size):
    w = 0.0
    for ch in plain(text):
        if ch == " ":
            w += 0.32
        elif ch.isascii() and (ch.isalnum() or ch in ".,:;%+-/"):
            w += 0.56
        elif ch.isascii():
            w += 0.32
        else:
            w += 1.0
    return w * size


def ffprobe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, check=True).stdout
    m = re.findall(r"duration=([\d.]+)", out)
    if m:
        return float(m[-1])
    return float(out.strip().splitlines()[-1])


def run(cmd):
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    proj = os.path.abspath(args.project)

    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        spec = json.load(f)
    with open(os.path.join(proj, "durations.json"), encoding="utf-8") as f:
        durs = json.load(f)
    pages = spec["pages"]
    meta = spec.get("meta", {})
    if len(durs) != len(pages):
        raise SystemExit("durations.json 与 pages.json 页数不一致，请重跑 tts.py")

    page_dur = [d["dur"] + d.get("tail", TAIL) for d in durs]
    total = sum(page_dur)
    starts, acc = [], 0.0
    for d in page_dur:
        starts.append(acc)
        acc += d

    adir = os.path.join(proj, "audio")
    rdir = os.path.join(proj, "render")
    ast = os.path.join(proj, "assets")
    sdir = os.path.join(proj, "seg")
    for d in (ast, sdir):
        os.makedirs(d, exist_ok=True)

    make_mask(os.path.join(ast, "mask_presenter.png"), PRES[2], PRES[3], 27)
    make_mask(os.path.join(ast, "mask_media.png"), MEDIA[2], MEDIA[3], 16)
    make_fill(os.path.join(ast, "bar_fill.png"), BAR[2], BAR[3], 3)
    make_cover(os.path.join(ast, "bar_cover.png"), os.path.join(rdir, "page_00.png"))
    make_gutter(os.path.join(ast, "gutter.png"), os.path.join(rdir, "page_00.png"))

    # ---------- 1. 每页视频段：入场动画帧 + 定格页 ----------
    for i, dur in enumerate(page_dur):
        frames = sorted(glob.glob(os.path.join(rdir, "anim", f"page_{i:02d}", "f_*.png")))
        settled = os.path.join(rdir, f"page_{i:02d}.png")
        seg = os.path.join(sdir, f"seg_{i:02d}.mp4")
        if frames:
            hold = max(0.05, dur - len(frames) / FPS)
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-framerate", str(FPS), "-i",
                   os.path.join(rdir, "anim", f"page_{i:02d}", "f_%03d.png"),
                   "-loop", "1", "-framerate", str(FPS), "-t", f"{hold:.3f}", "-i", settled,
                   "-filter_complex",
                   f"[0:v]fade=t=in:st=0:d={FADE}:color=white,format=yuv420p[a];"
                   f"[1:v]scale={W}:{H},setsar=1,format=yuv420p[b];"
                   f"[a][b]concat=n=2:v=1:a=0[v]",
                   "-map", "[v]", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "16", "-pix_fmt", "yuv420p", "-g", str(FPS * 2), seg]
        else:
            cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                   "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", settled,
                   "-vf", f"fade=t=in:st=0:d={FADE}:color=white,format=yuv420p",
                   "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "16", "-pix_fmt", "yuv420p", "-g", str(FPS * 2), seg]
        run(cmd)

    lst = os.path.join(sdir, "list.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for i in range(len(pages)):
            f.write(f"file 'seg_{i:02d}.mp4'\n")
    video = os.path.join(sdir, "video.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
         "-safe", "0", "-i", lst, "-c", "copy", video])

    # ---------- 2. 音轨 ----------
    track = os.path.join(sdir, "track.wav")
    full = os.path.join(adir, "full.wav")
    if os.path.exists(full):
        # 整段配音不切碎：直接用完整音轨，页边界只换画面。
        # 这样任何切点都不可能吞掉句尾的字，也不会出现“静音后突然补一个字”。
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", full,
             "-af", f"atrim=0:{total:.3f},apad=pad_dur=0.1",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1", track])
    else:
        inputs, fc, labels = [], [], []
        for i, dur in enumerate(page_dur):
            inputs += ["-i", os.path.join(adir, f"page_{i:02d}.wav")]
            fc.append(f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=mono,"
                      f"apad=pad_dur={durs[i].get('tail', TAIL)},atrim=0:{dur:.3f}[a{i}]")
            labels.append(f"[a{i}]")
        fc.append("".join(labels) + f"concat=n={len(pages)}:v=0:a=1[aout]")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + inputs +
            ["-filter_complex", ";".join(fc), "-map", "[aout]",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1", track])

    # ---------- 3. 主合成：主播 / 素材视频 / 逐字字幕 / 进度条 ----------
    inputs = ["-i", video, "-i", track]
    fc = ["[0:v]format=yuv420p[base]"]
    cur = "base"
    idx = 2

    presenter = meta.get("presenter") or {"type": "none"}
    if presenter.get("type") in ("video", "image") and presenter.get("src"):
        src = os.path.join(proj, presenter["src"])
        if presenter["type"] == "video":
            inputs += ["-stream_loop", "-1", "-i", src]
        else:
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}", "-i", src]
        fc.append(f"[{idx}:v]scale={PRES[2]}:{PRES[3]}:force_original_aspect_ratio=increase,"
                  f"crop={PRES[2]}:{PRES[3]},setsar=1,fps={FPS},format=rgba[praw]")
        idx += 1
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}",
                   "-i", os.path.join(ast, "mask_presenter.png")]
        fc.append(f"[{idx}:v]format=gray[pmask]")
        idx += 1
        fc.append("[praw][pmask]alphamerge[pr]")
        fc.append(f"[{cur}][pr]overlay=x={PRES[0]}:y={PRES[1]}:shortest=0[vpr]")
        cur = "vpr"

        # 口型：静态形象没有真 viseme，用音轨能量驱动一张小嘴逐帧开合，贴到嘴部坐标。
        # 坐标由 pages.json 的 meta.presenter.mouth 给（相对主播窗的比例），默认取画面中部。
        lip = presenter.get("lipsync", presenter.get("type") == "image")
        if lip:
            mpos = presenter.get("mouth") or [0.478, 0.415]
            mx = int(PRES[0] + mpos[0] * PRES[2])
            my = int(PRES[1] + mpos[1] * PRES[3])
            nframes = int(round(total * FPS)) + 1
            mdir = os.path.join(sdir, "mouth")
            make_mouths(mdir, mouth_curve(track, nframes))
            inputs += ["-framerate", str(FPS), "-i", os.path.join(mdir, "m_%04d.png")]
            fc.append(f"[{idx}:v]scale=30:26,format=rgba[mo]")
            idx += 1
            fc.append(f"[{cur}][mo]overlay=x={mx - 15}:y={my - 2}:shortest=0[vlip]")
            cur = "vlip"

    for i, p in enumerate(pages):
        media = p.get("media") or {}
        if media.get("type") != "video" or not media.get("src"):
            continue
        dur = page_dur[i]
        # 3 秒左右的短片：播一遍，播完用最后一帧停住（循环会看到明显的跳回片头）
        inputs += ["-i", os.path.join(proj, media["src"])]
        # 短片只在“本页开始”那一刻起播：把时间轴整体平移到 starts[i]，
        # 否则片头就开始跑，等这一页轮到它时早就播完/定格了。
        fc.append(f"[{idx}:v]scale={MEDIA[2]}:{MEDIA[3]}:force_original_aspect_ratio=increase,"
                  f"crop={MEDIA[2]}:{MEDIA[3]},setsar=1,fps={FPS},"
                  f"tpad=stop_mode=clone:stop_duration={dur + 0.5:.3f},"
                  f"format=rgba,"
                  f"fade=t=in:st=0:d=0.22:alpha=1,"
                  f"setpts=PTS+{starts[i]:.3f}/TB[md{i}]")
        idx += 1
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}",
                   "-i", os.path.join(ast, "mask_media.png")]
        fc.append(f"[{idx}:v]format=gray[mmask{i}]")
        idx += 1
        fc.append(f"[md{i}][mmask{i}]alphamerge[mdm{i}]")
        t0, t1 = starts[i], starts[i] + dur
        fc.append(f"[{cur}][mdm{i}]overlay=x={MEDIA[0]}:y={MEDIA[1]}:"
                  f"enable='between(t,{t0:.3f},{t1:.3f})'[vmd{i}]")
        cur = f"vmd{i}"

    # 字幕与语音逐句对应：每条字幕条按 cues_timed.json 的时间窗叠加，
    # 窗口来自音频停顿实测，所以字幕出现/切换的时刻就是语音念到这句的时刻。
    layout = {}
    clp = os.path.join(rdir, "caption_layout.json")
    if os.path.exists(clp):
        with open(clp, encoding="utf-8") as f:
            layout = json.load(f)
    timed = []
    ctp = os.path.join(proj, "cues_timed.json")
    if os.path.exists(ctp):
        with open(ctp, encoding="utf-8") as f:
            timed = json.load(f)
    else:
        timed = [{"page": i, "j": 0, "start": starts[i], "end": starts[i] + page_dur[i],
                  "text": plain(p.get("caption") or "")} for i, p in enumerate(pages)]

    cap_color = Image.open(os.path.join(rdir, "page_00.png")).convert("RGB").getpixel((100, 1320))
    last_j = {}
    for q in timed:
        last_j[q["page"]] = max(last_j.get(q["page"], 0), q.get("j", 0))
    for q in timed:
        i, j = q["page"], q.get("j", 0)
        strip = os.path.join(rdir, f"capstrip_{i:02d}_{j}.png")
        if not os.path.exists(strip) or not plain(q["text"]).strip():
            continue
        t0 = max(starts[i], q["start"])
        page_end = starts[i] + page_dur[i]
        t1 = page_end if j == last_j[i] else min(page_end, max(q["end"], q["start"] + 0.3))
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}", "-i", strip]
        fc.append(f"[{idx}:v]fade=t=in:st={t0:.3f}:d=0.10:alpha=1,format=rgba[sq{idx}]")
        sidx = idx
        idx += 1
        fc.append(f"[{cur}][sq{sidx}]overlay=x={CAP[0]}:y={CAP[1]}:"
                  f"enable='between(t,{t0:.3f},{t1:.3f})'[vs{q['page']}_{j}]")
        cur = f"vs{q['page']}_{j}"

        # 逐字擦出：白卡同色遮片按整字边界右移
        chars = list(plain(q["text"]))
        n = len(chars)
        if not n:
            continue
        meas = (layout.get(f"{i}:{j}") or {}).get("chars") or []
        if len(meas) == n:
            xs = [CAP[0] + d["x"] for d in meas]
            left = xs[0]
            right = max(CAP[0] + d["x"] + d["w"] for d in meas)
        else:
            size = caption_size(q["text"])
            est = min(CAP[2], caption_width(q["text"], size))
            unit = [caption_width(ch, size) for ch in chars]
            cum = [0.0]
            for uw in unit:
                cum.append(cum[-1] + uw)
            if cum[-1] > 0:
                unit = [u * (min(est, cum[-1]) / cum[-1]) for u in unit]
                cum = [0.0]
                for uw in unit:
                    cum.append(cum[-1] + uw)
            left = CAP[0] + (CAP[2] - est) / 2
            xs = [left + c for c in cum[:-1]]
            right = left + cum[-1]

        cw = int(right - left) + 12
        cover = os.path.join(ast, f"cap_cover_{i:02d}_{j}.png")
        Image.new("RGB", (cw, CAP[3]), cap_color).save(cover)
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}", "-i", cover]
        fc.append(f"[{idx}:v]format=rgba[cc{idx}]")
        cidx = idx
        idx += 1

        ws = t0 + 0.03
        # 快擦：0.18s 内把整句擦完，字幕几乎和语音同刻到位（慢擦会让人觉得字幕落后于声音）
        rd = min(0.18, max(0.12, (t1 - ws) * 0.3))
        expr = f"{right + 6:.1f}"
        for k in range(n - 1, -1, -1):
            tk = ws + (k + 1) / n * rd
            expr = f"if(lt(t,{tk:.3f}),{xs[k]:.1f},{expr})"
        fc.append(f"[{cur}][cc{cidx}]overlay=x='{expr}':"
                  f"y={CAP[1]}:enable='between(t,{t0:.3f},{t1:.3f})'[vc{i}_{j}]")
        cur = f"vc{i}_{j}"

    # 进度条
    inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}",
               "-i", os.path.join(ast, "bar_fill.png")]
    fc.append(f"[{cur}][{idx}:v]overlay=x={BAR[0]}:y={BAR[1]}[vbar]")
    idx += 1
    inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}",
               "-i", os.path.join(ast, "bar_cover.png")]
    fc.append(f"[{idx}:v]format=rgba[bcov]")
    idx += 1
    fc.append(f"[vbar][bcov]overlay=x='{BAR[0]}+{BAR[2]}*min(t/{total:.3f},1)':"
              f"y={BAR[1]}[vbar2]")
    cur = "vbar2"

    # 右侧静态竖条：把字幕遮片溢出卡片的部分盖回背景
    inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.3f}",
               "-i", os.path.join(ast, "gutter.png")]
    fc.append(f"[{cur}][{idx}:v]overlay=x=1016:y=0[vout]")
    idx += 1

    out = args.out or os.path.join(proj, "out.mp4")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"] + inputs + [
        "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "1:a",
        "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-t", f"{total:.3f}", out]

    with open(os.path.join(proj, "ffmpeg_cmd.txt"), "w", encoding="utf-8") as f:
        f.write(" ".join(cmd))

    print(f"合成 {len(pages)} 页 / 总时长 {total:.1f}s → {out}")
    run(cmd)
    print("完成。")


if __name__ == "__main__":
    main()
