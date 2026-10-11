# -*- coding: utf-8 -*-
"""
从 raw_video 下批量剥离音频，输出 44.1kHz / 16bit / 立体声 WAV（便于后续 BS-RoFormer 人声分离）。

v1.1: --folder 改为可选（缺省处理 raw_video 下全部 *集装箱作业视频 文件夹）、
      多扩展名自动匹配（mov/mp4/avi/mkv，忽略大小写）、--workers 并行加速。
v1.2: 无音频流的视频（如 ffmpeg 重导出的 *_tracked.mov）识别为"无音频流跳过"，不再计入失败。
用法:
    python extract_audio.py                 # 处理全部日期文件夹
    python extract_audio.py --folder 2025.7.21集装箱作业视频
"""
import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "raw_video"
OUT_DIR = PROJECT_ROOT / "result" / "audio_extract"

VIDEO_EXTS = {".mov", ".mp4", ".avi", ".mkv"}


def extract_one(video_path: Path, wav_path: Path) -> tuple[str, str]:
    """剥离单个视频音频，返回 ("ok"|"noaudio"|"fail", 文件名)"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",                       # 去掉视频流
        "-acodec", "pcm_s16le",      # 16bit PCM
        "-ar", "44100",              # BS-RoFormer 模型采样率
        "-ac", "2",                  # 立体声
        str(wav_path),
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        return "ok", video_path.name
    wav_path.unlink(missing_ok=True)  # 清理可能残留的损坏文件
    if "does not contain any stream" in (r.stderr or ""):
        return "noaudio", video_path.name
    return "fail", video_path.name


def main():
    parser = argparse.ArgumentParser(description="从视频批量剥离音频")
    parser.add_argument("--folder", default=None,
                        help="raw_video 下的子文件夹名；缺省处理全部 *集装箱作业视频 文件夹")
    parser.add_argument("--workers", type=int, default=6, help="并行 ffmpeg 进程数，默认 6")
    args = parser.parse_args()

    if args.folder:
        folders = [RAW_DIR / args.folder]
        if not folders[0].is_dir():
            print(f"文件夹不存在: {folders[0]}")
            sys.exit(1)
    else:
        folders = sorted(d for d in RAW_DIR.iterdir()
                         if d.is_dir() and d.name.endswith("集装箱作业视频"))

    if not folders:
        print("未找到任何 *集装箱作业视频 文件夹")
        sys.exit(1)

    total_ok = total_skip = total_fail = total_noaudio = 0
    for video_dir in folders:
        out_dir = OUT_DIR / video_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)

        videos = sorted(p for p in video_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
        print(f"[{video_dir.name}] 视频 {len(videos)} 个，输出目录: {out_dir}")

        jobs = []
        for v in videos:
            wav_path = out_dir / (v.stem + ".wav")
            if wav_path.exists() and wav_path.stat().st_size > 0:
                total_skip += 1
                continue
            jobs.append((v, wav_path))

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(extract_one, v, w) for v, w in jobs]
            for fut in as_completed(futures):
                status, name = fut.result()
                if status == "ok":
                    total_ok += 1
                elif status == "noaudio":
                    total_noaudio += 1
                    print(f"  [无音频流, 跳过] {name}")
                else:
                    total_fail += 1
                    print(f"  [FAIL] {name}")
                done += 1
                if done % 20 == 0 or done == len(jobs):
                    print(f"  进度 {done}/{len(jobs)}")

        print(f"[{video_dir.name}] 完成: 新剥离 {len(jobs)}, 跳过已有 {len(videos) - len(jobs)}")

    print(f"\n全部完成: 新剥离 {total_ok}, 跳过已有 {total_skip}, "
          f"无音频流 {total_noaudio}, 失败 {total_fail}")


if __name__ == "__main__":
    main()
