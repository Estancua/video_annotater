# -*- coding: utf-8 -*-
"""
从人工标注的上升/下降动作段中抽帧，供后续语义标注。

规则：
- 每段采样点按段长在整个段内均匀铺开，槽位数 = min(max_per_seg, 段帧数//min_step + 1)，
  保证相邻帧间隔 >= min_step 帧（默认 30 帧 ≈1s）且长段（20s+）全段覆盖；
- 相邻两张保留帧在 64x64 灰度上的平均绝对差(MAE)低于阈值则视为"太像"，跳过；
- 输出: result/pick/ 平铺帧图（文件名带视频名前缀防重名）+ index.csv 清单。

用法:
  python pick_frames.py [--folder 2025.7.22集装箱作业视频] [--min-step 10]
                         [--mae 1.5] [--max-per-seg 12]
"""
import argparse
import csv
import json
import os
import sys

import cv2
import numpy as np

ROOT = r"e:\load_unload_video"
ADIR = os.path.join(ROOT, "result", "annotations")
CDIR = os.path.join(ROOT, "result", "annotation_cache")
OUT = os.path.join(ROOT, "result", "pick")


def mae64(img, prev):
    """64x64 灰度平均绝对差，衡量两帧像素相似度（越大越不像）"""
    if prev is None:
        return 999.0
    return float(np.mean(np.abs(img.astype(np.int16) - prev.astype(np.int16))))


def downscale_gray(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (64, 64), interpolation=cv2.INTER_AREA)


def pick_segment(cap, fps, s_f, e_f, min_step, mae_th, max_per_seg):
    """在 [s_f, e_f] 帧区间内抽帧，返回 [(frame_idx, frame, mae)]
    采样点按段长均匀铺开（保证长段全段覆盖），间隔自动 >= min_step 帧"""
    span = e_f - s_f
    n_slots = max(2, min(max_per_seg, span // min_step + 1))
    step = max(min_step, span / (n_slots - 1))
    kept = []
    prev_gray = None
    for i in range(n_slots):
        f = s_f + int(round(i * step))
        if f > e_f:
            f = e_f
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, frame = cap.read()
        if not ok:
            break
        gray = downscale_gray(frame)
        d = mae64(gray, prev_gray)
        if not kept or d >= mae_th:
            kept.append((f, frame, d))
            prev_gray = gray
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="2025.7.22集装箱作业视频")
    ap.add_argument("--min-step", type=int, default=30, help="候选帧最小间隔（帧）")
    ap.add_argument("--mae", type=float, default=5.0, help="相邻保留帧灰度MAE阈值，低于则太像")
    ap.add_argument("--max-per-seg", type=int, default=8)
    args = ap.parse_args()

    adir = os.path.join(ADIR, args.folder)
    cdir = os.path.join(CDIR, args.folder)
    if not os.path.isdir(adir):
        sys.exit("无标注目录: " + adir)

    os.makedirs(OUT, exist_ok=True)
    files = sorted(f for f in os.listdir(adir) if f.endswith(".json"))
    rows = []
    total_frames = 0
    for fn in files:
        ann = json.load(open(os.path.join(adir, fn), encoding="utf-8"))
        name = os.path.splitext(fn)[0]
        mp4 = os.path.join(cdir, name + ".mp4")
        if not os.path.exists(mp4):
            print("SKIP(无缓存视频)", name)
            continue
        acts = [a for a in (ann.get("actions") or []) if a.get("type") in ("up", "down")]
        if not acts:
            continue
        cap = cv2.VideoCapture(mp4)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        n_img = 0
        for si, a in enumerate(acts):
            s_f = int(round(a["start_sec"] * fps))
            e_f = int(round(a["end_sec"] * fps))
            picks = pick_segment(cap, fps, s_f, e_f, args.min_step, args.mae, args.max_per_seg)
            for f, frame, d in picks:
                t = f / fps
                fname = "%s_seg%02d_%s_f%06d_t%07.2f.jpg" % (name, si, a["type"], f, t)
                cv2.imwrite(os.path.join(OUT, fname), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 92])
                rows.append([name, si, a["type"], f, round(t, 3),
                             round(d, 2), "pick/" + fname])
                n_img += 1
        cap.release()
        total_frames += n_img
        print("%s: %d 段 -> %d 帧" % (name, len(acts), n_img))

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "index.csv"), "w", newline="", encoding="utf-8-sig") as fo:
        w = csv.writer(fo)
        w.writerow(["video", "seg_idx", "type", "frame_idx", "t_sec", "mae_to_prev", "path"])
        w.writerows(rows)
    print("合计 %d 帧，清单: %s" % (total_frames, os.path.join(OUT, "index.csv")))


if __name__ == "__main__":
    main()
