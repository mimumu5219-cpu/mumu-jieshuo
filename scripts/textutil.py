"""把一页要念的原句切成字幕组（cue），保证字幕与语音逐句对应。"""
import re


def clean(t):
    return re.sub(r"\*\*", "", str(t or "")).strip()


def split_cues(text, maxlen=22):
    """按标点切成 ≤maxlen 字的短句组；过长的从句再按 maxlen 硬切。

    逗号/顿号保留在句中使用，只在整条字幕末尾去掉，避免出现
    「做路演 PPT把背景亮点风格」这种没有停顿的连读字幕。
    """
    t = clean(text)
    if not t:
        return []
    parts = [p.strip() for p in re.split(r"(?<=[。！？；，、：])", t) if p.strip()]
    out = []
    for p in parts:
        while len(p) > maxlen:
            cut = maxlen
            for k in range(maxlen, max(6, maxlen // 2), -1):
                if p[k - 1] in "，、：；":
                    cut = k
                    break
            # 不许把 WorkBuddy 这种英文单词切成两半
            while cut > 4 and cut < len(p) and p[cut - 1].isascii() \
                    and p[cut - 1].isalnum() and p[cut].isascii() and p[cut].isalnum():
                cut -= 1
            out.append(p[:cut])
            p = p[cut:].lstrip("，、：；")
        if p:
            out.append(p)
    # 太碎的合并（如「简称。」并回上一组）
    merged = []
    for c in out:
        if merged and len(c) <= 6 and len(merged[-1]) + len(c) <= maxlen:
            merged[-1] += c
        elif merged and len(merged[-1]) <= 5 and len(c) + len(merged[-1]) <= maxlen:
            merged[-1] += c
        else:
            merged.append(c)
    # 开头太碎的（「说白了，」）并到下一句，别单独闪 0.2 秒
    if len(merged) > 1 and len(merged[0]) <= 6 and len(merged[0]) + len(merged[1]) <= maxlen:
        merged[1] = merged[0] + merged[1]
        merged.pop(0)
    merged = [re.sub(r"[，、：；]$", "", m).strip() for m in merged]
    return [m for m in merged if m]


def norm(t):
    """只保留会被念出来的字，用于对齐与核验。"""
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", clean(t))
