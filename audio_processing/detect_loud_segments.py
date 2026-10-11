# -*- coding: utf-8 -*-
"""
检测音频中响度高于阈值的片段，记录每段的开始/结束时间。

两种阈值模式:
1. 固定阈值: --threshold-db（默认 -40dB），低于阈值为静音，响段=补集。
2. 自适应阈值: --adaptive-margin M，每个文件的阈值 = 该文件平均响度(mean_volume) + M dB，
   标记"相对更响"的段落。适合背景噪声持续很响的录音（如集装箱作业现场机械噪声）。

响段 = 全长减去静音段的补集，再过滤短于 min_loud_sec 的响段。
min_silence 默认 0.5s、min_loud 默认 0.3s，参数偏宽松，尽量多保留候选段。

输出: result/loud_segments/<日期文件夹>.json（每个文件夹一个 JSON，内含全部音频的响段）
用法:
    python detect_loud_segments.py                          # 全部，固定阈值 -40dB
    python detect_loud_segments.py --adaptive-margin 3      # 全部，自适应阈值（均值+3dB）
    python detect_loud_segments.py --folder 2025.7.21集装箱作业视频
"""
import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDIO_DIR = PROJECT_ROOT / "result" / "audio_extract"
OUT_DIR = PROJECT_ROOT / "result" / "loud_segments"

SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


def fmt_ts(sec: float) -> str:
    """秒 -> HH:MM:SS.mmm"""
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def probe_duration(wav_path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(wav_path)]
    r = subprocess.run(cmd, check=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return float(r.stdout.strip())


def probe_mean_volume(wav_path: Path) -> float:
    """整轨平均响度(dB)，用于自适应阈值基准"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav_path),
           "-af", "volumedetect", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", r.stderr)
    return float(m.group(1)) if m else -70.0


def detect_one(wav_path: Path, threshold_db: float, min_silence: float, min_loud: float,
               adaptive_margin: float | None = None) -> dict:
    duration = probe_duration(wav_path)
    if adaptive_margin is not None:
        threshold_db = round(probe_mean_volume(wav_path) + adaptive_margin, 1)

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(wav_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    silences = []
    cur = None
    for line in r.stderr.splitlines():
        m = SILENCE_START_RE.search(line)
        if m:
            cur = max(0.0, float(m.group(1)))
            continue
        m = SILENCE_END_RE.search(line)
        if m and cur is not None:
            silences.append((cur, min(duration, float(m.group(1)))))
            cur = None
    if cur is not None:  # 结尾静音延伸到文件末尾
        silences.append((cur, duration))

    loud = []
    pos = 0.0
    for s, e in silences:
        if s - pos >= min_loud:
            loud.append((pos, s))
        pos = max(pos, e)
    if duration - pos >= min_loud:
        loud.append((pos, duration))

    loud_dur = sum(e - s for s, e in loud)
    return {
        "file": wav_path.name,
        "duration_sec": round(duration, 3),
        "threshold_db": threshold_db,
        "loud_ratio": round(loud_dur / duration, 3) if duration > 0 else 0.0,
        "segments": [
            {
                "start_sec": round(s, 3),
                "end_sec": round(e, 3),
                "start": fmt_ts(s),
                "end": fmt_ts(e),
            }
            for s, e in loud
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="检测音频响段（人声候选段）")
    parser.add_argument("--folder", default=None,
                        help="audio_extract 下的子文件夹名；缺省处理全部")
    parser.add_argument("--threshold-db", type=float, default=-40.0,
                        help="固定响度阈值 dB，默认 -40（仅在未启用自适应时生效）")
    parser.add_argument("--adaptive-margin", type=float, default=None,
                        help="自适应阈值裕量 dB：每个文件阈值=自身平均响度+该值；如 3。提供则覆盖 --threshold-db")
    parser.add_argument("--min-silence", type=float, default=0.5,
                        help="最短静音时长(秒)，默认 0.5")
    parser.add_argument("--min-loud", type=float, default=0.3,
                        help="最短响段时长(秒)，默认 0.3")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数，默认 4")
    args = parser.parse_args()

    if args.folder:
        folders = [AUDIO_DIR / args.folder]
        if not folders[0].is_dir():
            print(f"文件夹不存在: {folders[0]}")
            sys.exit(1)
    else:
        folders = sorted(d for d in AUDIO_DIR.iterdir() if d.is_dir())

    if not folders:
        print("audio_extract 下没有文件夹")
        sys.exit(1)

    for audio_dir in folders:
        wavs = sorted(audio_dir.glob("*.wav"))
        if not wavs:
            print(f"[{audio_dir.name}] 无 wav，跳过")
            continue
        print(f"[{audio_dir.name}] {len(wavs)} 个 wav, threshold={args.threshold_db}dB")

        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(detect_one, w, args.threshold_db, args.min_silence, args.min_loud,
                              args.adaptive_margin): w
                    for w in wavs}
            done = 0
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    print(f"  [FAIL] {futs[fut].name}: {e}")
                done += 1
                if done % 50 == 0 or done == len(wavs):
                    print(f"  进度 {done}/{len(wavs)}")

        results.sort(key=lambda r: r["file"])
        total_dur = sum(r["duration_sec"] for r in results)
        total_loud = sum(r["duration_sec"] * r["loud_ratio"] for r in results)
        out = {
            "folder": audio_dir.name,
            "threshold_db": None if args.adaptive_margin is not None else args.threshold_db,
            "adaptive_margin_db": args.adaptive_margin,
            "min_silence_sec": args.min_silence,
            "min_loud_sec": args.min_loud,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "files": len(results),
                "total_duration_sec": round(total_dur, 1),
                "total_loud_sec": round(total_loud, 1),
                "overall_loud_ratio": round(total_loud / total_dur, 3) if total_dur else 0.0,
            },
            "files": results,
        }
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"{audio_dir.name}.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{audio_dir.name}] 输出 {out_path.name}, overall_loud_ratio={out['summary']['overall_loud_ratio']}")


if __name__ == "__main__":
    main()
