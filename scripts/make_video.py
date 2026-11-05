#!/usr/bin/env python3
"""一条命令跑完整链路：prepare → tts → render → assemble。"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, project, extra=None):
    cmd = [sys.executable, os.path.join(HERE, script), "--project", project] + (extra or [])
    print("→ " + os.path.basename(script))
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="含 pages.json 的项目目录")
    ap.add_argument("--out", default=None)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--rate", type=int, default=None)
    ap.add_argument("--skip-tts", action="store_true")
    ap.add_argument("--audio", default=None, help="完整口播稿的一条音频（默认用法，自动切页）")
    ap.add_argument("--audio-dir", default=None, help="逐页音频目录（可选，数量须等于页数）")
    args = ap.parse_args()
    proj = os.path.abspath(args.project)
    if not os.path.exists(os.path.join(proj, "pages.json")):
        raise SystemExit(f"缺 pages.json：{proj}")

    run("prepare.py", proj)
    if args.audio_dir or args.audio:
        extra = ["--audio-dir", args.audio_dir] if args.audio_dir else ["--audio", args.audio]
        run("audio_in.py", proj, extra)
    elif not args.skip_tts:
        extra = []
        if args.voice:
            extra += ["--voice", args.voice]
        if args.rate:
            extra += ["--rate", str(args.rate)]
        run("tts.py", proj, extra)
    run("render.py", proj)
    run("assemble.py", proj, ["--out", args.out] if args.out else [])


if __name__ == "__main__":
    main()
