# -*- coding: utf-8 -*-
"""
吊运起降识别 v1.0（独立实验，不接入标注工具）
思路：第一人称贴身鱼眼视频，先按相机稳定性门控，再在稳定窗口内用双信号估计集箱垂直运动：
  信号A cam  : 全局运动补偿得到的相机自运动（人眼跟随箱子时，相机仰拍速率 ≈ 箱子上升速率）
  信号B obj  : 补偿后残差光流中离群簇的垂直速度（相机较稳、箱子在画面内移动时有效）
  融合 v     : 两信号取幅值大者（两个机制分别在"跟随"与"注视不动"两种工况下生效）
事件判定：v 平滑后过阈值（升为正、降为负）持续 >= min_dur 记一个事件。
输出：result/lift_analysis/<视频名>.json（时间序列+事件） 与 plots/<视频名>.png（曲线图）
启动: conda run --no-capture-output -n shanghai_cv python code/lift_analysis/lift_detect.py --folder 2025.7.21集装箱作业视频
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "raw_video"
OUT_DIR = PROJECT_ROOT / "result" / "lift_analysis"
PLOT_DIR = OUT_DIR / "plots"
VIDEO_EXTS = {".mov", ".mp4", ".avi", ".mkv"}

# ---------------- 参数（v1 起步值，跑完看图再调） ----------------
PROC_W = 640          # 处理分辨率宽
PROC_H = 360
TARGET_FPS = 10.0     # 分析帧率
MAX_CORNERS = 400
QUALITY = 0.01
MIN_DIST = 12
RANSAC_TH = 2.5       # 全局运动重投影阈值 px
OUTLIER_PX = 2.0      # 残差离群阈值 px
MIN_INLIER = 0.35     # 内点率低于此视为全局运动估计失败（画面剧烈模糊/纹理缺失）
MIN_TRACKS = 60       # 有效跟踪点太少则样本无效
SMOOTH_S = 1.0        # 平滑窗（秒）
THR_PX_S = 12.0       # 事件阈值 px/s（v1.1 调参：实测真实动作均速多在 12~25 区间）
MIN_DUR_S = 0.8       # 事件最短时长（v1.1: 0.6→0.8 抑制抖动碎段）
MERGE_GAP_S = 0.4     # 同向事件合并间隙
MIN_OUTLIER_CLUSTER = 30   # v_obj 离群簇最少点数（防散点噪声）
CLUSTER_AGREE = 0.75       # 离群簇方向一致率下限（鱼眼旋转散差会双向对半）
STABLE_CAM_PX_S = 80.0     # 平滑相机自运动超过此值视为剧烈移动，窗口不稳定
MIN_EVENT_STABLE = 0.5     # 事件内稳定样本占比下限
MIN_MEAN_RATIO = 0.5       # 事件段内均速需达阈值的比例（滤掉勉强越线的抖动）
CONF_MIN_OBJ_PX_S = 3.0    # 两信号"符号冲突"判定的 v_obj 幅值下限（低于视为噪声不算冲突）
CONFLICT_MODE = "keep"     # 冲突处理：keep=维持幅值优先 / zero=冲突样本置零 / obj=冲突时信v_obj


def analyze_video(path: Path, target_fps=TARGET_FPS):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None, "无法打开视频"
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    stride = max(1, int(round(fps / target_fps)))
    dt = stride / fps

    t_arr, v_cam_arr, v_obj_arr, qual_arr = [], [], [], []
    prev = None
    idx = -1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        idx += 1
        if idx % stride:
            continue
        g = cv2.cvtColor(
            cv2.resize(frame, (PROC_W, PROC_H), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2GRAY,
        )
        if prev is None:
            prev = g
            t = idx / fps
            t_arr.append(t)
            v_cam_arr.append(0.0)
            v_obj_arr.append(0.0)
            qual_arr.append(0.0)
            continue

        p0 = cv2.goodFeaturesToTrack(prev, maxCorners=MAX_CORNERS, qualityLevel=QUALITY,
                                     minDistance=MIN_DIST, blockSize=7)
        v_cam = 0.0
        v_obj = 0.0
        qual = 0.0
        if p0 is not None and len(p0) >= MIN_TRACKS:
            p1, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, p0, None,
                                                 winSize=(21, 21), maxLevel=3)
            good = st.ravel() == 1
            a, b = p0[good].reshape(-1, 2), p1[good].reshape(-1, 2)
            if len(a) >= MIN_TRACKS:
                M, inl = cv2.estimateAffinePartial2D(
                    a, b, method=cv2.RANSAC, ransacReprojThreshold=RANSAC_TH,
                    confidence=0.995)
                if M is not None:
                    inl = inl.ravel().astype(bool)
                    inlier_frac = inl.mean()
                    # 信号A：静态场景整体平移的垂直分量（画面下移=相机上仰=箱子上升）
                    v_cam = -M[1, 2] / dt
                    # 信号B：残差离群簇垂直中位数
                    pred = a @ M[:2, :2].T + M[:2, 2]
                    ry = b[:, 1] - pred[:, 1]
                    out = (~inl) & (np.abs(ry) > OUTLIER_PX)
                    if out.sum() >= MIN_OUTLIER_CLUSTER and inlier_frac >= MIN_INLIER:
                        med = float(np.median(ry[out]))
                        agree = float(np.mean(np.sign(ry[out]) == np.sign(med)))
                        if agree >= CLUSTER_AGREE:
                            v_obj = -med / dt
                    qual = inlier_frac
        t = idx / fps
        t_arr.append(t)
        v_cam_arr.append(round(float(v_cam), 2))
        v_obj_arr.append(round(float(v_obj), 2))
        qual_arr.append(round(float(qual), 3))
        prev = g

    cap.release()
    dur = n_frames / fps if fps else t_arr[-1] if t_arr else 0.0
    return {
        "fps_src": round(fps, 3), "target_fps": target_fps, "dt": round(dt, 4),
        "duration_sec": round(dur, 2),
        "t": [round(x, 2) for x in t_arr],
        "v_cam": v_cam_arr, "v_obj": v_obj_arr, "inlier_frac": qual_arr,
    }, None


def smooth(arr, k):
    a = np.asarray(arr, dtype=float)
    n = len(a)
    if k <= 1 or n == 0:
        return a.copy()
    pad = k // 2
    ap = np.pad(a, (pad, pad), mode="edge")
    out = np.convolve(ap, np.ones(k) / k, mode="valid")
    if len(out) > n:  # k 为偶数时 valid 会多出一位
        out = out[:n]
    elif len(out) < n:
        out = np.pad(out, (0, n - len(out)), mode="edge")
    return out


def fuse_and_events(ser):
    """融合 v = 两信号取幅值大者；平滑；阈值切事件"""
    t = np.asarray(ser["t"])
    v_cam = np.asarray(ser["v_cam"])
    v_obj = np.asarray(ser["v_obj"])
    q = np.asarray(ser["inlier_frac"])
    k = max(3, int(SMOOTH_S * ser["target_fps"]))
    # 稳定性窗口：平滑后的相机自运动过大（行走/剧烈转动）→ 不采信
    shake = np.abs(v_cam)
    stable = (q >= MIN_INLIER) & (smooth(shake, k) < STABLE_CAM_PX_S)
    # 幅值优先：稳定样本内两信号取幅值大者
    pick = np.where(np.abs(v_cam) >= np.abs(v_obj), v_cam, v_obj)
    # 符号冲突：v_cam 幅值占优但 v_obj 非噪声且方向相反
    # （常见于跟随工况——仿射拟合到背景或箱体之一，两信号必有一路倒签）
    conflict = (stable & (v_cam * v_obj < 0) & (np.abs(v_cam) >= np.abs(v_obj))
                & (np.abs(v_obj) >= CONF_MIN_OBJ_PX_S))
    if CONFLICT_MODE == "zero":
        pick = np.where(conflict, 0.0, pick)
    elif CONFLICT_MODE == "obj":
        pick = np.where(conflict, v_obj, pick)
    pick = np.where(stable, pick, 0.0)
    v_s = smooth(pick, k)

    events = []
    if len(t) >= 3:
        state = np.zeros(len(t), dtype=int)
        state[v_s > THR_PX_S] = 1
        state[v_s < -THR_PX_S] = -1
        dt = float(np.median(np.diff(t))) if len(t) > 1 else 0.1
        i = 0
        n = len(t)
        while i < n:
            d = state[i]
            if d == 0:
                i += 1
                continue
            j = i
            while j + 1 < n and state[j + 1] == d:
                j += 1
            s_t, e_t = t[i], t[j]
            # 与下一段同向且间隙小则合并
            while True:
                k2 = j + 1
                while k2 < n and state[k2] == 0:
                    k2 += 1
                if k2 < n and state[k2] == d and (t[k2] - e_t) <= MERGE_GAP_S:
                    j = k2
                    while j + 1 < n and state[j + 1] == d:
                        j += 1
                    e_t = t[j]
                else:
                    break
            if e_t - s_t >= MIN_DUR_S:
                seg = slice(i, j + 1)
                peak = float(np.max(np.abs(v_s[seg])))
                mean_v = float(np.mean(v_s[seg]))
                frac_stable = float(np.mean(stable[seg]))
                if frac_stable >= MIN_EVENT_STABLE and \
                        abs(mean_v) >= MIN_MEAN_RATIO * THR_PX_S:
                    events.append({
                        "start": round(float(s_t), 2), "end": round(float(e_t), 2),
                        "dir": "up" if d > 0 else "down",
                        "peak_px_s": round(peak, 1), "mean_px_s": round(mean_v, 1),
                        "stable_frac": round(frac_stable, 2),
                    })
            i = j + 1
    ser["v"] = [round(float(x), 2) for x in v_s]
    ser["stable"] = [bool(x) for x in stable]
    ser["events"] = events
    return ser


def load_asr_commands(folder, stem):
    p = PROJECT_ROOT / "result" / "asr" / folder / f"{stem}.json"
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return d.get("commands", [])


def load_actions(folder, stem):
    p = PROJECT_ROOT / "result" / "annotations" / folder / f"{stem}.json"
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return d.get("actions", [])


def plot_video(ser, folder, stem, out_png):
    t = np.asarray(ser["t"])
    fig, axes = plt.subplots(2, 1, figsize=(16, 7), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1]})
    ax = axes[0]
    ax.plot(t, ser["v"], lw=1.6, color="#1f77b4", label="v fused (px/s)")
    ax.plot(t, ser["v_cam"], lw=0.7, color="#999999", alpha=0.7, label="v_cam")
    ax.plot(t, ser["v_obj"], lw=0.7, color="#cccccc", alpha=0.9, label="v_obj")
    ax.axhline(THR_PX_S, color="#2ca02c", ls="--", lw=0.6)
    ax.axhline(-THR_PX_S, color="#d62728", ls="--", lw=0.6)
    ax.axhline(0, color="k", lw=0.4)
    for e in ser["events"]:
        c = "#2ca02c" if e["dir"] == "up" else "#d62728"
        ax.axvspan(e["start"], e["end"], color=c, alpha=0.25)
    cmds = load_asr_commands(folder, stem)
    ytop = max(60.0, float(np.nanmax(np.abs(ser["v"]))) * 1.05) if len(t) else 60.0
    for c in cmds:
        m = {"up": ("^", "#2ca02c", "full", 9), "up_small": ("^", "#2ca02c", "none", 8),
             "down": ("v", "#d62728", "full", 9),
             "down_small": ("v", "#d62728", "none", 8),
             "stop": ("s", "#7f7f7f", "full", 7)}.get(c.get("label"))
        if not m:
            continue
        x = c.get("end_sec", c.get("start_sec"))
        ax.plot([x], [ytop * 0.92], marker=m[0], color=m[1], markersize=m[3],
                fillstyle=m[2], clip_on=False)
    for a in load_actions(folder, stem):
        c = "#2ca02c" if a.get("type") == "up" else "#d62728"
        ax.axvspan(a.get("start_sec", 0), a.get("end_sec", 0), color=c,
                   alpha=0.10, lw=0)
    ax.set_ylim(-ytop, ytop)
    ax.legend(loc="upper right", fontsize=8)
    ax.set_ylabel("垂直速度 px/s（上正下负）")
    ax.set_title(f"{stem}  绿=上升事件 红=下降事件  ▲▼■=ASR口令  淡色带=人工动作标注")

    ax2 = axes[1]
    ax2.fill_between(t, 0, 1, where=ser["stable"], color="#2ca02c", alpha=0.35,
                     step="mid")
    ax2.plot(t, ser["inlier_frac"], lw=0.8, color="#333")
    ax2.axhline(MIN_INLIER, color="r", ls=":", lw=0.7)
    ax2.set_ylim(-0.05, 1.15)
    ax2.set_ylabel("稳定窗口")
    ax2.set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(out_png, dpi=90)
    plt.close(fig)


def main():
    global THR_PX_S, MIN_DUR_S, MIN_MEAN_RATIO, CONFLICT_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="2025.7.21集装箱作业视频")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-plot", action="store_true")
    ap.add_argument("--thr", type=float, default=THR_PX_S, help="事件阈值 px/s")
    ap.add_argument("--min-dur", type=float, default=MIN_DUR_S, help="事件最短时长 s")
    ap.add_argument("--mean-ratio", type=float, default=MIN_MEAN_RATIO,
                    help="事件均速/阈值下限")
    ap.add_argument("--conflict", choices=["keep", "zero", "obj"],
                    default=CONFLICT_MODE,
                    help="v_cam/v_obj 符号冲突处理：keep=幅值优先 / zero=不判 / obj=信v_obj")
    ap.add_argument("--reuse", action="store_true",
                    help="不重解码视频，用已存 JSON 里的原始信号重算事件与图")
    args = ap.parse_args()
    THR_PX_S = args.thr
    MIN_DUR_S = args.min_dur
    MIN_MEAN_RATIO = args.mean_ratio
    CONFLICT_MODE = args.conflict

    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / args.folder
    out.mkdir(parents=True, exist_ok=True)

    if args.reuse:
        jss = sorted(p for p in out.glob("*.json") if p.name != "evaluation.json")
        print(f"[lift] reuse folder={args.folder} jsons={len(jss)} thr={THR_PX_S} "
              f"conflict={CONFLICT_MODE}")
        for i, jp in enumerate(jss, 1):
            meta = json.loads(jp.read_text(encoding="utf-8"))
            ser = {k: meta[k] for k in
                   ("fps_src", "target_fps", "dt", "duration_sec", "t",
                    "v_cam", "v_obj", "inlier_frac")}
            ser = fuse_and_events(ser)
            meta.update({k: ser[k] for k in ("v", "stable", "events")})
            jp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            if not args.no_plot:
                plot_video(ser, args.folder, jp.stem, PLOT_DIR / f"{jp.stem}.png")
            nu = sum(1 for e in ser["events"] if e["dir"] == "up")
            print(f"  [{i}/{len(jss)}] {jp.stem} events up={nu} down={len(ser['events'])-nu}")
        print("[lift] done")
        return

    vdir = RAW_DIR / args.folder
    vids = sorted(p for p in vdir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if args.limit:
        vids = vids[: args.limit]
    print(f"[lift] folder={args.folder} videos={len(vids)} -> {OUT_DIR}")
    for i, vp in enumerate(vids, 1):
        t0 = time.time()
        ser, err = analyze_video(vp)
        if err:
            print(f"  [{i}/{len(vids)}] {vp.name} SKIP: {err}")
            continue
        ser = fuse_and_events(ser)
        meta = {"folder": args.folder, "video": vp.name, **ser}
        (out / f"{vp.stem}.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if not args.no_plot:
            plot_video(ser, args.folder, vp.stem, PLOT_DIR / f"{vp.stem}.png")
        nu = sum(1 for e in ser["events"] if e["dir"] == "up")
        nd = len(ser["events"]) - nu
        print(f"  [{i}/{len(vids)}] {vp.name} dur={ser['duration_sec']}s "
              f"events up={nu} down={nd} ({time.time()-t0:.1f}s)")
    print("[lift] done")


if __name__ == "__main__":
    sys.exit(main())
