# -*- coding: utf-8 -*-
"""
调用 MSST (Music-Source-Separation-Training) 的 BS-RoFormer 人声分离模型
(viperx 版权重 model_bs_roformer_ep_317_sdr_12.9755, SDR vocals 10.87)，
对 result/audio_extract/<folder> 下的 wav 做人声分离。

v1.3 全量批跑改造:
    - --folder 通配符可匹配多个日期文件夹（旧版只取第一个），缺省 "2025*" 即全部
    - 断点续跑: 输出目录已有 <视频名>/vocals.wav(或 .flac) 的视频自动跳过；
      仅把待处理 wav 硬链接(失败则复制)进 .staging 暂存目录后交给 MSST
    - 每个文件夹跑完自动把峰值<=1.0 输出的 vocals.flac 无损转为 PCM_16 wav 并删除 flac
      (与原 unify_vocals_format.py 行为一致，不再需要单独跑)
    - 单个文件夹失败不影响后续文件夹，末尾汇总，有失败则退出码非 0

在 WSL cfy_env 中运行:
    source /home/cfy/CodeFile/cfy_env/bin/activate
    python separate_vocals.py                       # 全部文件夹
    python separate_vocals.py --folder 2025.7.22*   # 指定(通配符防中文传参乱码)
    python separate_vocals.py --limit 2             # 每文件夹最多处理 2 个(冒烟测试)

输出:
    result/vocal_separation/<folder>/<name>/vocals.wav   人声
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MSST_DIR = PROJECT_ROOT / "code" / "audio_separation" / "Music-Source-Separation-Training"
CKPT = PROJECT_ROOT / "code" / "audio_separation" / "models" / "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
CONFIG = MSST_DIR / "configs" / "viperx" / "model_bs_roformer_ep_317_sdr_12.9755.yaml"
IN_ROOT = PROJECT_ROOT / "result" / "audio_extract"
OUT_ROOT = PROJECT_ROOT / "result" / "vocal_separation"
STAGE_ROOT = OUT_ROOT / ".staging"


def already_done(out_dir: Path, stem: str) -> bool:
    return (out_dir / stem / "vocals.wav").exists() or (out_dir / stem / "vocals.flac").exists()


def convert_flac_dir(out_dir: Path, stems) -> int:
    """flac -> PCM_16 wav（无损），删 flac；返回转换数"""
    n = 0
    for stem in stems:
        flac = out_dir / stem / "vocals.flac"
        wav = out_dir / stem / "vocals.wav"
        if flac.exists() and not wav.exists():
            data, sr = sf.read(str(flac))
            sf.write(str(wav), data, sr, subtype="PCM_16")
            flac.unlink()
            n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description="BS-RoFormer 人声分离（全文件夹批跑）")
    parser.add_argument("--folder", default="2025*",
                        help="audio_extract 下日期文件夹名(支持通配符,可匹配多个;缺省全部)")
    parser.add_argument("--limit", type=int, default=0,
                        help="每个文件夹最多处理的待处理文件数(0=不限,冒烟测试用)")
    args = parser.parse_args()

    if not CKPT.exists():
        print(f"权重文件不存在: {CKPT}", flush=True)
        sys.exit(1)
    dirs = [d for d in sorted(IN_ROOT.glob(args.folder)) if d.is_dir()]
    if not dirs:
        print(f"未匹配到输入目录: {IN_ROOT / args.folder}", flush=True)
        sys.exit(1)

    failures = []
    t_all = time.time()
    for in_dir in dirs:
        wavs = sorted(in_dir.glob("*.wav"))
        out_dir = OUT_ROOT / in_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)
        pending = [w for w in wavs if not already_done(out_dir, w.stem)]
        skipped = len(wavs) - len(pending)
        if args.limit:
            pending = pending[:args.limit]
        print(f"[vsep] ==== {in_dir.name}: total={len(wavs)} skip={skipped} pending={len(pending)}",
              flush=True)
        if not pending:
            continue

        stage = STAGE_ROOT / in_dir.name
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, exist_ok=True)
        for w in pending:  # 硬链接避免复制 16GB 音频；drvfs 不支持时回退复制
            dst = stage / w.name
            try:
                os.link(w, dst)
            except OSError:
                shutil.copy2(w, dst)

        cmd = [
            sys.executable, str(MSST_DIR / "inference.py"),
            "--model_type", "bs_roformer",
            "--config_path", str(CONFIG),
            "--start_check_point", str(CKPT),
            "--input_folder", str(stage),
            "--store_dir", str(out_dir),
            "--device_ids", "0",
            "--pcm_type", "PCM_16",
            "--disable_detailed_pbar",
        ]
        t0 = time.time()
        rc = subprocess.run(cmd).returncode
        n_conv = convert_flac_dir(out_dir, [w.stem for w in pending])
        shutil.rmtree(stage, ignore_errors=True)
        got = sum(1 for w in pending if already_done(out_dir, w.stem))
        print(f"[vsep] FOLDER_DONE {in_dir.name}: processed={got}/{len(pending)} "
              f"flac2wav={n_conv} rc={rc} elapsed={time.time() - t0:.0f}s", flush=True)
        if rc != 0 or got < len(pending):
            failures.append(in_dir.name)

    shutil.rmtree(STAGE_ROOT, ignore_errors=True)
    print(f"[vsep] ALL_DONE folders={len(dirs)} failed={len(failures)} "
          f"total_elapsed={time.time() - t_all:.0f}s"
          + (f" -> {failures}" if failures else ""), flush=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
