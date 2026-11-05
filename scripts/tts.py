#!/usr/bin/env python3
"""把 pages.json 的每页文案合成配音（macOS say），并输出时长表。"""
import argparse, json, os, re, subprocess


def strip_markup(t: str) -> str:
    return re.sub(r"\*\*", "", str(t or "")).replace("\n", " ").strip()


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:
        pass
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", path],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise RuntimeError("无法读取音频时长: " + path)
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", type=int, default=None)
    args = ap.parse_args()

    proj = os.path.abspath(args.project)
    with open(os.path.join(proj, "pages.json"), encoding="utf-8") as f:
        spec = json.load(f)
    meta = spec.get("meta", {})
    voice = args.voice or meta.get("voice") or "Tingting"
    rate = args.rate or int(meta.get("rate") or 180)

    adir = os.path.join(proj, "audio")
    os.makedirs(adir, exist_ok=True)

    rows = []
    for i, p in enumerate(spec["pages"]):
        text = strip_markup(p.get("say") or p.get("caption") or "")
        if not text:
            raise SystemExit(f"第 {i+1} 页没有文案")
        aiff = os.path.join(adir, f"page_{i:02d}.aiff")
        wav = os.path.join(adir, f"page_{i:02d}.wav")
        subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", aiff, text], check=True)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", aiff,
             "-ar", "44100", "-ac", "1", wav], check=True)
        os.remove(aiff)
        rows.append({"i": i, "file": os.path.relpath(wav, proj), "dur": probe_duration(wav)})
        print(f"  page {i+1:02d}  {rows[-1]['dur']:.2f}s  {text[:26]}")

    with open(os.path.join(proj, "durations.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"配音完成：{len(rows)} 页，总时长 {sum(r['dur'] for r in rows):.1f}s")


if __name__ == "__main__":
    main()
