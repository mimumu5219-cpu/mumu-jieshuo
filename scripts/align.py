"""用 ASR 把音频逐字对到口播稿，得到每条字幕的真实发声时刻。"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from textutil import norm

SIZE = os.environ.get("WB_ASR_MODEL", "small")


def asr_char_times(audio):
    """转写并把每个字的时间插值到词内 → [(字, 起, 止)]。"""
    from faster_whisper import WhisperModel
    m = WhisperModel(SIZE, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(audio, language="zh", word_timestamps=True,
                           beam_size=5, vad_filter=False)
    out = []
    for s in segs:
        for w in (s.words or []):
            t = norm(w.word)
            if not t:
                continue
            n = len(t)
            for k, ch in enumerate(t):
                out.append((ch, w.start + (w.end - w.start) * k / n,
                            w.start + (w.end - w.start) * (k + 1) / n))
    return out


def align_chars(script, asr):
    """字符级编辑距离对齐，返回每个原稿字对应的 ASR 序号（缺失时用邻居插值）。"""
    a = [c for c in norm(script)]
    n, m = len(a), len(asr)
    if not n or not m:
        return [], 0
    INF = 10 ** 9
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    bt = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
        bt[i][0] = 1
    for j in range(m + 1):
        dp[0][j] = j
        bt[0][j] = 2
    for i in range(1, n + 1):
        ai = a[i - 1]
        row, prow = dp[i], dp[i - 1]
        brow = bt[i]
        for j in range(1, m + 1):
            sub = prow[j - 1] + (0 if ai == asr[j - 1][0] else 1)
            dele = prow[j] + 1
            ins = row[j - 1] + 1
            best = min(sub, dele, ins)
            row[j] = best
            brow[j] = (0 if best == sub else (1 if best == dele else 2))
    mapping = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        op = bt[i][j]
        if i > 0 and j > 0 and op == 0:
            if a[i - 1] == asr[j - 1][0]:
                mapping[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif i > 0 and (op == 1 or j == 0):
            i -= 1
        else:
            j -= 1
    matched = sum(1 for v in mapping if v is not None)
    # 删除位置用左右最近的有效映射插值
    prev, nxt = None, None
    for k in range(n):
        if mapping[k] is not None:
            prev = mapping[k]
        elif prev is not None:
            mapping[k] = prev
    for k in range(n - 1, -1, -1):
        if mapping[k] is not None:
            nxt = mapping[k]
        elif nxt is not None:
            mapping[k] = nxt
    return [v if v is not None else 0 for v in mapping], matched / max(1, n)


def char_times(audio, script):
    """原稿每个字的起始时刻（秒）。"""
    asr = asr_char_times(audio)
    mapping, score = align_chars(script, asr)
    return [(asr[k][1] if k < len(asr) else 0.0) for k in mapping], score


def char_spans(audio, script):
    """原稿每个字的 (起, 止) 时刻（秒）。切页必须用「止」，不能用「起」。"""
    asr = asr_char_times(audio)
    mapping, score = align_chars(script, asr)
    return [((asr[k][1], asr[k][2]) if k < len(asr) else (0.0, 0.0))
            for k in mapping], score


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--audio", required=True)
    args = ap.parse_args()
    proj = os.path.abspath(args.project)
    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        pages = json.load(f)["pages"]
    script = "".join(p.get("read") or p.get("caption") or "" for p in pages)
    times, score = char_times(args.audio, script)
    print(f"逐字对齐：{len(times)} 字，匹配率 {score*100:.0f}%")
    out = os.path.join(proj, "char_times.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"times": times, "score": score}, f, indent=1)
    print("→", out)


if __name__ == "__main__":
    main()
