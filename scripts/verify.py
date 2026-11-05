#!/usr/bin/env python3
"""核验：pages.json 结构与对稿一致性 / 成片规格与字幕、进度条。不能通过就退出码 1。"""
import argparse, json, math, os, re, struct, subprocess, sys, wave

from PIL import Image

FPS = 30
W, H = 1080, 1440
CAP = (86, 1296, 906, 66)
BAR = (86, 1387, 930, 6)
PRES = (103, 905, 370, 357)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from assemble import mouth_curve
except Exception:
    mouth_curve = None
MODULE_FIELDS = {
    "cover": ["title"], "term": ["lines"], "code": ["lines"], "quote": ["text"],
    "uimock": ["items"], "pair": ["left", "right"], "stat": ["num", "desc"],
    "timeline": ["points"], "art": ["title"], "list": ["items"],
    "steps": ["items"], "risk": ["items"],
}
CJK = re.compile(r"[\u4e00-\u9fff]")
PUNCT = re.compile(r"[^\u4e00-\u9fff0-9A-Za-z%.]+")
FX_OK = {"lift", "zoom", "slide", "flip", "blur", "sweep"}


def plain(t):
    return re.sub(r"\*\*", "", str(t or ""))


def cjk_bigrams(text):
    s = PUNCT.sub(" ", plain(text))
    out = set()
    for seg in s.split():
        for k in range(len(seg) - 1):
            if CJK.match(seg[k]) and CJK.match(seg[k + 1]):
                out.add(seg[k:k + 2])
    return out


def ffprobe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate:format=duration",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def ink_ratio(img):
    """字幕框内近黑像素占比（文字出现时升高）。"""
    g = img.convert("L")
    px = list(g.getdata())
    return sum(1 for v in px if v < 120) / max(1, len(px))


def lcs_len(a, b):
    """最长公共子序列长度：容忍 ASR 个别错字，只判断“字有没有被念出来”。"""
    dp = [0] * (len(b) + 1)
    for ch in a:
        prev = 0
        for j, c in enumerate(b, 1):
            cur = dp[j]
            dp[j] = prev + 1 if ch == c else max(dp[j], dp[j - 1])
            prev = cur
    return dp[-1]


def env8k(path, tmpdir, tag, sr=8000, win=0.02):
    """音频 → 8kHz 单声道 → 每 20ms 一帧 RMS 包络，用于成片音轨与原配音的一致性比对。"""
    os.makedirs(tmpdir, exist_ok=True)
    wav = os.path.join(tmpdir, f"{tag}.wav")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
                    "-vn", "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", wav],
                   check=True)
    with wave.open(wav, "rb") as w:
        raw = w.readframes(w.getnframes())
    n = len(raw) // 2
    vals = struct.unpack("<%dh" % n, raw) if n else ()
    step = int(sr * win)
    env, i = [], 0
    while i + step <= n:
        seg = vals[i:i + step]
        env.append(math.sqrt(sum(v * v for v in seg) / step) / 32768.0)
        i += step
    return env


def best_lag(a, b, span=20):
    """在 ±span 帧内找最佳偏移，返回 (lag, 相关系数)。lag>0 表示 b 相对 a 滞后。"""
    bl, bc = 0, -1.0
    for lag in range(-span, span + 1):
        if lag >= 0:
            x, y = a[:len(a) - lag], b[lag:]
        else:
            x, y = a[-lag:], b[:len(b) + lag]
        m = min(len(x), len(y))
        if m < 30:
            continue
        x, y = x[:m], y[:m]
        mx, my = sum(x) / m, sum(y) / m
        num = sum((p - mx) * (q - my) for p, q in zip(x, y))
        dx = math.sqrt(sum((p - mx) ** 2 for p in x))
        dy = math.sqrt(sum((q - my) ** 2 for q in y))
        c = num / dx / dy if dx and dy else 0.0
        if c > bc:
            bl, bc = lag, c
    return bl, bc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--stage", required=True, choices=["pages", "video", "sync", "audio"])
    ap.add_argument("--script", default=None, help="口播稿 txt，用于核验字幕是否偏离原稿")
    args = ap.parse_args()
    proj = os.path.abspath(args.project)
    fails, warns = [], []

    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        spec = json.load(f)
    pages = spec.get("pages") or []
    meta = spec.get("meta") or {}

    if args.stage == "pages":
        if not meta.get("title"):
            fails.append("meta.title（左上角大字标题）为空")
        if not 3 <= len(pages) <= 16:
            warns.append(f"页数 {len(pages)}，建议 5–14 页")
        seen = {}
        for i, p in enumerate(pages):
            tag = f"第{i+1}页"
            cap = plain(p.get("caption"))
            if not cap.strip():
                fails.append(f"{tag} 缺字幕")
            n = len(cap)
            if n > 30:
                fails.append(f"{tag} 字幕 {n} 字，超过 30")
            elif n > 22:
                warns.append(f"{tag} 字幕 {n} 字，偏长（建议 ≤22）")
            if not p.get("topic"):
                fails.append(f"{tag} 缺 topic（右侧主题）")
            if len(str(p.get("topic", ""))) > 12:
                warns.append(f"{tag} topic 超过 12 字")
            if cap in seen:
                warns.append(f"{tag} 字幕与第{seen[cap]+1}页重复")
            seen.setdefault(cap, i)
            media = p.get("media") or {}
            if media:
                if media.get("type") not in ("image", "video"):
                    fails.append(f"{tag} media.type 只能是 image / video")
                src = media.get("src")
                if not src or not os.path.exists(os.path.join(proj, src)):
                    fails.append(f"{tag} 素材不存在：{src}")
            else:
                mod = p.get("module")
                if mod not in MODULE_FIELDS:
                    fails.append(f"{tag} module 未知：{mod}")
                else:
                    d = p.get("data") or {}
                    for k in MODULE_FIELDS[mod]:
                        if not d.get(k):
                            fails.append(f"{tag} {mod} 缺字段 data.{k}")

            if not p.get("read"):
                warns.append(f"{tag} 缺 read（这页要念的原句），整段音频切页会不齐")

            fx = str(p.get("fx") or "")
            parts = [t.strip() for t in fx.split("+") if t.strip()]
            if not parts:
                warns.append(f"{tag} 缺 fx（页内动效），将用模块默认值")
            else:
                bad = [t for t in parts if t not in FX_OK]
                if bad:
                    fails.append(f"{tag} fx 未知：{'、'.join(bad)}")
                if len(parts) > 2:
                    warns.append(f"{tag} fx 叠加过多：{fx}")

        sweeps = sum(1 for p in pages if "sweep" in str(p.get("fx") or ""))
        if sweeps > 2:
            warns.append(f"sweep 用了 {sweeps} 页，建议 ≤2 页")
        for i in range(1, len(pages)):
            a, b = str(pages[i - 1].get("fx") or ""), str(pages[i].get("fx") or "")
            if a and a == b:
                warns.append(f"第{i}页与第{i+1}页 fx 都是 {a}，建议换一种")

        # 主播必须对口型：形象图默认 lipsync，且有合法嘴位
        pr = meta.get("presenter") or {}
        if pr.get("type") == "image":
            if pr.get("lipsync") is False:
                fails.append("主播是形象图但 lipsync 被关了：要求必须对口型")
            m = pr.get("mouth")
            if not (isinstance(m, (list, tuple)) and len(m) == 2):
                fails.append("形象图主播缺 meta.presenter.mouth（嘴位比例）")
            else:
                mx, my = m
                if not (0.2 <= mx <= 0.8 and 0.2 <= my <= 0.8):
                    warns.append(f"mouth 比例 {list(m)} 偏离画面中部，大概率贴偏")
            if pr.get("src") and not os.path.exists(os.path.join(proj, pr["src"])):
                fails.append(f"主播素材不存在：{pr['src']}")

        if args.script and os.path.exists(args.script):
            with open(args.script, encoding="utf-8") as f:
                src_text = f.read()
            src_bi = cjk_bigrams(src_text)
            all_bi = set()
            bad = []
            for i, p in enumerate(pages):
                bi = cjk_bigrams(p.get("caption"))
                all_bi |= bi
                miss = bi - src_bi
                if bi and len(miss) / len(bi) > 0.3:
                    bad.append(f"第{i+1}页字幕有 {len(miss)}/{len(bi)} 个词不在口播稿：{'、'.join(sorted(miss)[:4])}")
            cover = len(all_bi & src_bi) / max(1, len(all_bi))
            print(f"对稿覆盖：{cover*100:.0f}%（字幕用词能在口播稿里找到的比例）")
            if cover < 0.6:
                fails.append(f"字幕与口播稿偏离过大（覆盖 {cover*100:.0f}%）")
            warns += bad
        else:
            print("（未提供 --script，跳过对稿核验）")

    elif args.stage == "audio":
        # 硬校验：成片音轨必须与原始配音同源（整段不切碎）。
        # 波形一致就不可能出现“末字被切走”；ASR 复听只作参考，
        # 同音字（稿→告）会误报，不能当失败判据。
        import wave
        out = os.path.join(proj, "out.mp4")
        src = os.path.join(proj, "voice", "full.wav")
        if os.path.exists(out) and os.path.exists(src):
            a = env8k(src, os.path.join(proj, "verify"), "src8k")
            b = env8k(out, os.path.join(proj, "verify"), "out8k")
            lag, corr = best_lag(a, b, span=20)   # ±0.4s，20 帧 = 0.02s
            print(f"音轨一致性：相关 {corr:.3f}（偏移 {lag*0.02:+.2f}s）")
            if len(b) + 8 < len(a):
                fails.append(f"成片音轨比原配音短 {(len(a)-len(b))*0.02:.2f}s")
            if corr < 0.985:
                fails.append(f"成片音轨与原始配音不一致（相关 {corr:.3f}，疑似被切碎）")
        else:
            warns.append("缺 voice/full.wav 或 out.mp4，跳过音轨一致性校验")

        from faster_whisper import WhisperModel
        from textutil import norm
        model = WhisperModel(os.environ.get("WB_ASR_MODEL", "small"),
                             device="cpu", compute_type="int8")
        print("  页  末三字  首三字  识别末尾")
        out = os.path.join(proj, "out.mp4")
        durs = json.load(open(os.path.join(proj, "durations.json"), encoding="utf-8"))
        starts, acc = [], 0.0
        for d in durs:
            starts.append(acc)
            acc += d["dur"] + d.get("tail", 0.35)
        for i, p in enumerate(pages):
            if os.path.exists(out):
                # 直接复听成片里这一页的时间窗，而不是切好的分页 wav
                wav = os.path.join(proj, "verify", f"audio_p{i:02d}.wav")
                os.makedirs(os.path.dirname(wav), exist_ok=True)
                subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                                "-ss", f"{starts[i]:.3f}", "-t", f"{durs[i]['dur']:.3f}",
                                "-i", out, "-vn", "-ac", "1", "-ar", "16000", wav], check=True)
            else:
                wav = os.path.join(proj, "audio", f"page_{i:02d}.wav")
            if not os.path.exists(wav):
                fails.append(f"第{i+1}页缺音频：{wav}")
                continue
            segs, _ = model.transcribe(wav, language="zh", beam_size=5, vad_filter=False)
            txt = norm("".join(s.text for s in segs)).lower()
            exp = norm(p.get("read") or p.get("caption") or "").lower()
            if not exp:
                continue
            tail, head = exp[-3:], exp[:3]
            ht = lcs_len(tail, txt[-14:])
            hh = lcs_len(head, txt[:14])
            print(f"  {i+1:>2}   {ht}/{len(tail)}      {hh}/{len(head)}     …{txt[-16:]}")
            if ht < min(2, len(tail)):
                warns.append(f"第{i+1}页末三字识别 {ht}/3（同音字可能误报）：读稿「{exp[-6:]}」/ 识别「{txt[-8:]}」")
            if hh < min(2, len(head)):
                warns.append(f"第{i+1}页开头可疑：读稿开头「{exp[:6]}」/ 识别开头「{txt[:8]}」")

    elif args.stage == "sync":
        ctp = os.path.join(proj, "cues_timed.json")
        if not os.path.exists(ctp):
            raise SystemExit("缺 cues_timed.json：先跑 prepare.py 和 audio_in.py")
        with open(ctp, encoding="utf-8") as f:
            cues = json.load(f)
        sp = args.script or os.path.join(proj, "口播稿.txt")
        src = open(sp, encoding="utf-8").read() if os.path.exists(sp) else ""
        cnorm = PUNCT.sub("", "".join(c["text"] for c in cues))
        snorm = PUNCT.sub("", src)
        print(f"字幕 {len(cues)} 句 / {len(cnorm)} 字；口播稿 {len(snorm)} 字")
        if src:
            if cnorm == snorm:
                print("✓ 字幕全文与口播稿逐字一致")
            elif snorm.startswith(cnorm):
                print(f"✓ 字幕覆盖口播稿前 {len(cnorm)} 字")
            else:
                k = 0
                while k < min(len(cnorm), len(snorm)) and cnorm[k] == snorm[k]:
                    k += 1
                fails.append(
                    f"字幕与口播稿第 {k+1} 字起不一致"
                    f"（字幕 {cnorm[max(0,k-6):k+8]} / 原稿 {snorm[max(0,k-6):k+8]}）")
        prev = 0.0
        print("  页/句   起—止         字  语速  漂移  字幕")
        for c in cues:
            d = c["end"] - c["start"]
            n = len(PUNCT.sub("", c["text"]))
            if c["start"] < prev - 0.02:
                fails.append(f"第{c['page']+1}页第{c['j']+1}句与上一句重叠")
            rate = n / max(0.001, d)
            if rate > 9.0 or rate < 2.6:
                warns.append(f"第{c['page']+1}页第{c['j']+1}句语速异常 {rate:.1f} 字/秒（{d:.2f}s）")
            drift = ""
            # src_t 来自 ASR 逐字对齐：这句第一个字真正被念出来的时刻。
            # 字幕比它早 0.06s（切页预卷）属正常；超过 0.25s 就是字幕脱离语音。
            if isinstance(c.get("src_t"), (int, float)):
                dft = c["start"] - c["src_t"]
                drift = f"{dft:+.2f}"
                if abs(dft + 0.06) > 0.35:
                    fails.append(
                        f"第{c['page']+1}页第{c['j']+1}句字幕与语音漂移 {dft:+.2f}s（>0.35s）")
                elif abs(dft + 0.06) > 0.20:
                    warns.append(
                        f"第{c['page']+1}页第{c['j']+1}句字幕与语音漂移 {dft:+.2f}s")
            print(f"  {c['page']+1:>2}/{c['j']+1}  {c['start']:6.2f}–{c['end']:6.2f}  {n:>3}  {rate:4.1f}  {drift:>5}  {c['text']}")
            prev = c["end"]

    else:  # video
        out = os.path.join(proj, "out.mp4")
        if not os.path.exists(out):
            fails.append("没有 out.mp4")
        else:
            info = ffprobe(out)
            vs = [s for s in info["streams"] if s["codec_type"] == "video"][0]
            dur = float(info["format"]["duration"])
            if (vs["width"], vs["height"]) != (W, H):
                fails.append(f"分辨率 {vs['width']}×{vs['height']}，应为 {W}×{H}")
            if vs.get("r_frame_rate") != "30/1":
                warns.append(f"帧率 {vs.get('r_frame_rate')}")
            if not [s for s in info["streams"] if s["codec_type"] == "audio"]:
                fails.append("没有音轨")
            with open(os.path.join(proj, "durations.json"), encoding="utf-8") as f:
                durs = json.load(f)
            expect = sum(d["dur"] + d.get("tail", 0.35) for d in durs)
            if abs(dur - expect) > 0.8:
                fails.append(f"时长 {dur:.2f}s，按配音应为 {expect:.2f}s")

            with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
                spec = json.load(f)
            starts, acc = [], 0.0
            for d in durs:
                starts.append(acc)
                acc += d["dur"] + 0.35

            tmp = os.path.join(proj, "verify")
            os.makedirs(tmp, exist_ok=True)
            thumbs = []
            for i, p in enumerate(spec["pages"]):
                end = starts[i] + durs[i]["dur"] + 0.2
                for label, t in (("start", starts[i] + 0.45), ("end", end)):
                    png = os.path.join(tmp, f"p{i:02d}_{label}.png")
                    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                                    "-ss", f"{t:.3f}", "-i", out, "-frames:v", "1", png],
                                   check=True)
                    im = Image.open(png).convert("RGB")
                    ratio = ink_ratio(im.crop((CAP[0], CAP[1], CAP[0] + CAP[2], CAP[1] + CAP[3])))
                    # 页首 0.45s 内字幕必须已经出现（快擦完），否则就是字幕落后于语音
                    if label == "start" and ratio < 0.008:
                        fails.append(f"第{i+1}页开头字幕没有及时出现（墨迹 {ratio*100:.1f}%）")
                    if label == "end" and ratio < 0.015:
                        fails.append(f"第{i+1}页末字幕没有出现（墨迹 {ratio*100:.2f}%）")
                    if label == "end":
                        thumbs.append(im.resize((270, 360), Image.LANCZOS))

            if thumbs:
                sheet = Image.new("RGB", (270 * len(thumbs), 360), "#000")
                for k, im in enumerate(thumbs):
                    sheet.paste(im, (270 * k, 0))
                sheet.save(os.path.join(tmp, "contact_sheet.png"))
                print(f"抽帧总览：verify/contact_sheet.png（{len(thumbs)} 页末帧）")

            # 进度条末帧应铺到卡片右缘
            png = os.path.join(tmp, "last.png")
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-sseof", "-0.2", "-i", out, "-frames:v", "1", png], check=True)
            im = Image.open(png).convert("RGB")
            hit = sum(1 for x in range(990, 1014)
                      if (lambda c: c[2] > 150 and c[1] > 120 and c[0] < 120)(
                          im.getpixel((x, BAR[1] + BAR[3] // 2))))
            if hit < 8:
                warns.append(f"末帧进度条没有贴到卡片右缘（采样 {hit}/24）")

            # 口型：取“开口最大”和“最闭嘴”两帧，比嘴部暗像素是否真的变化
            pr = (spec.get("meta") or {}).get("presenter") or {}
            track = os.path.join(proj, "seg", "track.wav")
            if (pr.get("type") == "image" and pr.get("lipsync", True) is not False
                    and mouth_curve and os.path.exists(track)):
                m = pr.get("mouth") or [0.462, 0.416]
                mx = int(PRES[0] + m[0] * PRES[2])
                my = int(PRES[1] + m[1] * PRES[3])
                curve = mouth_curve(track, int(dur * FPS) + 1)
                span = curve[:max(1, int(durs[0]["dur"] * FPS))]
                if len(span) > 4:
                    hi = max(range(len(span)), key=lambda k: span[k])
                    lo = min(range(len(span)), key=lambda k: span[k])
                    counts = {}
                    for tag, k in (("open", hi), ("closed", lo)):
                        png = os.path.join(tmp, f"lip_{tag}.png")
                        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                                        "-ss", f"{k / FPS:.3f}", "-i", out,
                                        "-vf", f"crop=56:40:{mx - 28}:{my - 10}",
                                        "-frames:v", "1", png], check=True)
                        g = Image.open(png).convert("L")
                        counts[tag] = sum(1 for v in g.getdata() if v < 45)
                    gain = counts["open"] - counts["closed"]
                    print(f"口型对照：张口暗像素 {counts['open']} / 闭嘴 {counts['closed']}（差 {gain}）")
                    if gain < 40:
                        fails.append(f"口型没跟上音频：张口帧嘴部几乎没变化（差 {gain}）")

    for w in warns:
        print(f"  ! {w}")
    for f in fails:
        print(f"  ✗ {f}")
    if fails:
        print(f"核验未通过：{len(fails)} 项必须修，{len(warns)} 项建议")
        sys.exit(1)
    print(f"核验通过（{len(warns)} 项建议）")


if __name__ == "__main__":
    main()
