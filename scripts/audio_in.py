#!/usr/bin/env python3
"""接入外部配音：整段或逐页音频 → 44.1k 单声道 wav，并写 durations.json。

默认走整段：用户按完整口播稿录一条音频，脚本按“字数估算 + 静音吸附”切页。
  python3 audio_in.py --project DIR --audio FILE
逐页模式（可选）：
  python3 audio_in.py --project DIR --audio-dir DIR
"""
import argparse, json, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textutil import norm

TAIL = 0.35
HOLD = 0.50          # 整段配音结尾留一点收尾静音


def run(cmd):
    subprocess.run(cmd, check=True)


def dur_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, check=True).stdout
    m = re.findall(r"duration=([\d.]+)", out)
    if m:
        return float(m[-1])
    return float(out.strip().splitlines()[-1])


def to_wav(src, dst):
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
         "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", dst])


def silences(path, noise="-32dB", mind=0.10):
    """检测停顿区间，用来把估算的切页点吸附到真实的句间停顿上。"""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af",
                        f"silencedetect=noise={noise}:d={mind}", "-f", "null", "-"],
                       capture_output=True, text=True)
    out, st = [], None
    for line in p.stderr.splitlines():
        if "silence_start:" in line:
            st = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and st is not None:
            out.append((st, float(line.split("silence_end:")[1].split()[0])))
            st = None
    return out


def snap(est, sils, lo, hi, tol=1.5):
    """在估算点附近找一段停顿的中点，找不到就用估算点。"""
    best = None
    for a, b in sils:
        m = (a + b) / 2
        if lo < m < hi and abs(m - est) <= tol and m > 0:
            if best is None or abs(m - est) < abs(best - est):
                best = m
    return best if best is not None else est


def cut_pages(total, weights, sils):
    """在停顿候选点上做 DP：让每页实际时长尽量贴近“本页字数 / 全片均速”。"""
    rm = sum(weights) / total
    cand = [0.0] + [round((a + b) / 2, 3) for a, b in sils
                    if 0.05 < (a + b) / 2 < total - 0.05] + [total]
    m, n = len(cand), len(weights)
    INF = float("inf")
    dp = [[INF] * m for _ in range(n + 1)]
    par = [[-1] * m for _ in range(n + 1)]
    dp[0][0] = 0.0
    for k in range(n):
        aim = weights[k] / rm
        for a in range(m):
            if dp[k][a] == INF:
                continue
            for c in range(a + 1, m):
                d = cand[c] - cand[a]
                if d < 0.35:
                    continue
                if (n - k - 1) and (total - cand[c]) < 0.35 * (n - k - 1):
                    break
                cost = (d - aim) ** 2
                if dp[k][a] + cost < dp[k + 1][c]:
                    dp[k + 1][c] = dp[k][a] + cost
                    par[k + 1][c] = a
    if dp[n][m - 1] == INF:
        return None
    cuts = [0.0] * (n + 1)
    c = m - 1
    for k in range(n, 0, -1):
        cuts[k] = cand[c]
        c = par[k][c]
    return cuts


def build_cues(pages, durs, cues, sils=None, ctimes=None):
    """切分字幕时间窗：有 ASR 逐字时刻就按真实发声，没有才按字数估算 + 停顿时吸附。

    ctimes 是与「整段口播稿」逐字对齐、并且已经换算到成片时间轴（含每页 TAIL 留白）的
    起音时刻，长度 = 各页 read 的字数之和。base 是本页第一个字在 ctimes 里的下标，
    缺了它每页都会去取 ctimes[0]，字幕就会全部提前到片头——这是之前“没对齐”的根因。
    """
    timed, acc, base = [], 0.0, 0
    if ctimes:
        ctimes = list(ctimes)
        for k in range(1, len(ctimes)):
            if ctimes[k] < ctimes[k - 1]:
                ctimes[k] = ctimes[k - 1]
    for i, p in enumerate(pages):
        texts = [t for t in (cues.get(str(i)) or []) if t]
        if not texts:
            texts = [re.sub(r"\*\*", "", p.get("caption") or "").strip()]
        tot, dur = sum(len(norm(t)) for t in texts), durs[i]["dur"]
        nch = len(norm(p.get("read") or p.get("caption") or ""))
        bounds, prev, run = [acc], acc, 0
        for k in range(1, len(texts)):
            run += len(norm(texts[k - 1]))
            est = acc + dur * run / max(1, tot)
            lo, hi = prev + 0.2, acc + dur - 0.2
            if ctimes and base + run < len(ctimes):
                t = ctimes[base + run]
            elif sils:
                tol = max(0.12, min(0.45, (est - prev) / 2))
                t = snap(est, sils, lo, hi, tol)
            else:
                t = est
            t = min(max(t, lo), hi)
            bounds.append(t)
            prev = t
        bounds.append(acc + dur)
        for j, txt in enumerate(texts):
            item = {"page": i, "j": j, "start": round(bounds[j], 3),
                    "end": round(bounds[j + 1], 3), "text": txt}
            if ctimes and base + run0(j, texts) < len(ctimes):
                # 这句第一个字的真实起音时刻，供 verify --stage sync 检查字幕有没有超前/滞后
                item["src_t"] = round(ctimes[base + run0(j, texts)], 3)
            timed.append(item)
        base += nch
        acc += dur + durs[i].get("tail", TAIL)
    return timed


def run0(j, texts):
    """第 j 句之前（本页内）累计字数。"""
    return sum(len(norm(t)) for t in texts[:j])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--audio-dir", default=None)
    ap.add_argument("--audio", default=None)
    args = ap.parse_args()
    proj = os.path.abspath(args.project)
    adir = os.path.join(proj, "audio")
    os.makedirs(adir, exist_ok=True)

    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        pages = json.load(f)["pages"]
    n = len(pages)
    cues = {}
    cf = os.path.join(proj, "render", "cues.json")
    if os.path.exists(cf):
        with open(cf, encoding="utf-8") as f:
            cues = json.load(f)
    sils_for_cues = None

    if args.audio_dir:
        srcs = sorted(f for f in os.listdir(args.audio_dir)
                      if f.lower().endswith((".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg")))
        if len(srcs) != n:
            raise SystemExit(f"逐页音频数量 {len(srcs)} 与页数 {n} 不一致：{srcs}")
        durs = []
        for i, f in enumerate(srcs):
            dst = os.path.join(adir, f"page_{i:02d}.wav")
            to_wav(os.path.join(args.audio_dir, f), dst)
            durs.append({"page": i + 1, "dur": dur_of(dst), "src": f})
        print(f"逐页音频：{n} 段 → audio/page_XX.wav")
    elif args.audio:
        total_audio = dur_of(args.audio)
        # 用户给的是完整口播稿（不分页），所以按页文本长度估算切点，
        # 再把每个切点吸附到最近的句间停顿上，避免切在字中间。
        weights = [max(6, len(re.sub(r"\*\*", "", p.get("read") or p.get("say") or p.get("caption") or "")))
                   for p in pages]
        cum, acc = [0.0], 0.0
        for w in weights:
            acc += w
            cum.append(acc)
        ests = [total_audio * c / cum[-1] for c in cum[:-1]]
        sils = silences(args.audio)
        sils_for_cues = sils
        # 有 ASR 就逐字对齐：切点 = 每个字真正被念出来的时刻，不靠估算
        script = "".join(p.get("read") or p.get("caption") or "" for p in pages)
        ctimes, score = None, 0.0
        try:
            from align import char_spans
            spans, score = char_spans(args.audio, script)
            ctimes = [s for s, _ in spans]
            print(f"逐字对齐：匹配率 {score*100:.0f}%")
        except Exception as e:
            print(f"  未启用逐字对齐（{e.__class__.__name__}），退回停顿吸附", file=sys.stderr)
            spans = None
            ctimes = None
        if ctimes:
            idx = 0
            cuts = [0.0]
            snapped = 0
            for p in pages[:-1]:
                idx += len(norm(p.get("read") or p.get("caption") or ""))
                # 关键：用上一页末字的「结束时刻」prev_end，而不是末字的起点。
                # 按起点切会把整个末字推到下一页开头（中间隔着 0.35s 留白），
                # 听起来就是“最后一个字被吞掉了”。
                prev_end = spans[idx - 1][1] if idx >= 1 else 0.0
                nxt = spans[idx][0] if idx < len(spans) else total_audio
                gap = nxt - prev_end
                lo = max(cuts[-1] + 0.4, prev_end)
                hi = min(total_audio - 0.2, max(nxt, prev_end))
                # 有真实停顿就切在停顿中点（最自然）；否则切在末字结束处，两页首尾相接
                best = None
                if gap > 0.05:
                    for a, b in sils:
                        m = (a + b) / 2
                        if prev_end + 0.02 < m <= nxt and lo < m < hi:
                            if best is None or abs(m - prev_end) < abs(best - prev_end):
                                best = m
                if best is not None:
                    cut = best
                    snapped += 1
                else:
                    cut = min(max(prev_end, lo), total_audio - 0.2)
                cuts.append(cut)
            cuts.append(total_audio)
        elif cut_pages(total_audio, weights, sils) is not None:
            cuts = cut_pages(total_audio, weights, sils)
            snapped = n - 1
        else:
            cuts = [0.0]
            snapped = 0
            for i in range(1, len(ests)):
                est = ests[i]
                good = snap(est, sils, cuts[-1] + 0.6,
                            (ests[i + 1] if i + 1 < len(ests) else total_audio) - 0.4)
                if abs(good - est) > 0.02:
                    snapped += 1
                cuts.append(good)
            cuts.append(total_audio)
        cue_times = ctimes
        # 整段配音不切碎：成片直接用完整音轨，页边界只是“视觉换页”。
        # 之前的做法是把音频按页切断再拼，切点稍早就会把末字推到下一页开头，
        # 听起来像“最后一个字被吞掉了”；不做切割就不可能出现这个问题。
        cuts[-1] = total_audio + HOLD
        durs = []
        for i in range(n):
            seg = max(0.3, cuts[i + 1] - cuts[i])
            dst = os.path.join(adir, f"page_{i:02d}.wav")
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", f"{cuts[i]:.3f}", "-t", f"{seg:.3f}", "-i", args.audio,
                 "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", dst])
            durs.append({"page": i + 1, "dur": round(seg, 3), "tail": 0.0,
                         "src": os.path.basename(args.audio)})
        # 完整音轨（末尾补 0.5s 静音，让最后一句有收尾）
        dst_track = os.path.join(adir, "full.wav")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", args.audio,
             "-af", f"apad=pad_dur={HOLD}", "-ac", "1", "-ar", "44100",
             "-c:a", "pcm_s16le", dst_track])
        print(f"整段音频不切碎，页边界 {n-1} 个（{snapped} 个落在停顿上）")

        # 字幕时刻仍是整段音频时间轴；页边界不插留白，所以不需要再偏移。
        if locals().get("cue_times"):
            raw = cue_times
            vtimes, idx = [], 0
            for i, p in enumerate(pages):
                nch = len(norm(p.get("read") or p.get("caption") or ""))
                v0 = cuts[i]
                for k in range(idx, min(idx + nch, len(raw))):
                    vtimes.append(v0 + max(0.0, raw[k] - cuts[i]))
                idx += nch
            cue_times = vtimes
    else:
        raise SystemExit("给 --audio-dir 或 --audio")

    with open(os.path.join(proj, "durations.json"), "w", encoding="utf-8") as f:
        json.dump(durs, f, ensure_ascii=False, indent=1)
    timed = build_cues(pages, durs, cues, sils_for_cues, locals().get("cue_times"))
    with open(os.path.join(proj, "cues_timed.json"), "w", encoding="utf-8") as f:
        json.dump(timed, f, ensure_ascii=False, indent=1)
    print("字幕排期：%d 句 → cues_timed.json" % len(timed))
    print("总时长 %.1fs → durations.json" % sum(d["dur"] + d.get("tail", TAIL) for d in durs))


if __name__ == "__main__":
    main()
