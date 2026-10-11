# -*- coding: utf-8 -*-
"""
基于人声分离轨（vocals.wav）检测响段，替代整轨（含机械噪声）版本。

与 detect_loud_segments.py 的区别只在数据源与输出目录：
- 输入: result/vocal_separation/<日期文件夹>/<视频名>/vocals.wav
- 输出: result/loud_segments_vocals/<日期文件夹>.json
- JSON 内的 file 字段写 "<视频名>.wav"（与整轨版一致），
  保证 annotation_tool/server.py 按 stem 匹配的逻辑不用改。

复用 detect_loud_segments.detect_one（自适应阈值 = 该文件 mean_volume + margin）。

用法:
    python detect_loud_segments_vocals.py --folder 2025.7.21集装箱作业视频
    python detect_loud_segments_vocals.py                      # 全部日期文件夹
    python detect_loud_segments_vocals.py --adaptive-margin 4  # 调阈值裕量
"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from detect_loud_segments import detect_one

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VOCAL_DIR = PROJECT_ROOT / "result" / "vocal_separation"
OUT_DIR = PROJECT_ROOT / "result" / "loud_segments_vocals"


def main():
    parser = argparse.ArgumentParser(description="基于 vocals.wav 检测响段")
    parser.add_argument("--folder", default=None,
                        help="vocal_separation 下的子文件夹名；缺省处理全部")
    parser.add_argument("--threshold-db", type=float, default=-40.0,
                        help="固定响度阈值 dB，默认 -40（仅在未启用自适应时生效）")
    parser.add_argument("--adaptive-margin", type=float, default=6.0,
                        help="自适应阈值裕量 dB：每个文件阈值=自身平均响度+该值；默认 6.0")
    parser.add_argument("--min-silence", type=float, default=0.5,
                        help="最短静音时长(秒)，默认 0.5")
    parser.add_argument("--min-loud", type=float, default=0.3,
                        help="最短响段时长(秒)，默认 0.3")
    parser.add_argument("--workers", type=int, default=4, help="并行进程数，默认 4")
    parser.add_argument("--no-adaptive", action="store_true",
                        help="禁用自适应阈值，改用 --threshold-db 固定值")
    args = parser.parse_args()

    if args.folder:
        folders = [VOCAL_DIR / args.folder]
        if not folders[0].is_dir():
            print(f"文件夹不存在: {folders[0]}")
            sys.exit(1)
    else:
        folders = sorted(d for d in VOCAL_DIR.iterdir() if d.is_dir())

    if not folders:
        print("vocal_separation 下没有文件夹")
        sys.exit(1)

    adaptive = None if args.no_adaptive else args.adaptive_margin
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for folder_dir in folders:
        video_dirs = sorted(d for d in folder_dir.iterdir()
                            if d.is_dir() and (d / "vocals.wav").is_file())
        if not video_dirs:
            print(f"[{folder_dir.name}] 无 vocals.wav，跳过")
            continue
        print(f"[{folder_dir.name}] {len(video_dirs)} 个 vocals.wav, "
              f"adaptive_margin={adaptive}, threshold={args.threshold_db}dB")

        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(detect_one, d / "vocals.wav", args.threshold_db,
                              args.min_silence, args.min_loud, adaptive): d
                    for d in video_dirs}
            done = 0
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                    # vocals.wav 的 stem 固定，改回视频名，与整轨版 JSON 对齐
                    r["file"] = futs[fut].name + ".wav"
                    results.append(r)
                except Exception as e:
                    print(f"  [FAIL] {futs[fut].name}: {e}")
                done += 1
                if done % 20 == 0 or done == len(video_dirs):
                    print(f"  进度 {done}/{len(video_dirs)}")

        results.sort(key=lambda r: r["file"])
        total_dur = sum(r["duration_sec"] for r in results)
        total_loud = sum(r["duration_sec"] * r["loud_ratio"] for r in results)
        out = {
            "folder": folder_dir.name,
            "source": "vocals",
            "threshold_db": None if adaptive is not None else args.threshold_db,
            "adaptive_margin_db": adaptive,
            "min_silence_sec": args.min_silence,
            "min_loud_sec": args.min_loud,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {
                "files": len(results),
                "total_duration_sec": round(total_dur, 1),
                "total_loud_sec": round(total_loud, 1),
            },
            "files": results,
        }
        out_path = OUT_DIR / f"{folder_dir.name}.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  -> {out_path} ({len(results)} files)")


if __name__ == "__main__":
    main()
