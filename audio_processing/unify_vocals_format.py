# -*- coding: utf-8 -*-
"""
将 MSST 推理输出中因峰值<=1.0 被自动存成 flac 的 vocals 统一无损转为 wav 并删除 flac，
使结果集格式统一（flac->wav 无损，内容完全一致）。
在 WSL cfy_env 或装有 soundfile 的环境中运行:
    python unify_vocals_format.py
"""
import glob
import os

import soundfile as sf

PATTERNS = [
    "/mnt/e/load_unload_video/result/vocal_separation/*/2025*/vocals.flac",  # WSL
    r"E:\load_unload_video\result\vocal_separation\*\2025*\vocals.flac",     # Windows
]

files = []
for p in PATTERNS:
    files.extend(glob.glob(p))

if not files:
    print("没有需要转换的 vocals.flac")
for f in sorted(set(files)):
    data, sr = sf.read(f)
    sf.write(f[:-5] + ".wav", data, sr, subtype="PCM_16")
    os.remove(f)
    print("converted:", f)
