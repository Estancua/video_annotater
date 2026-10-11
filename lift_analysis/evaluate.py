# -*- coding: utf-8 -*-
"""
吊运起降识别 v1.0 评估（独立实验，只读 result/lift_analysis + asr + annotations）
两套“考卷”：
  A. ASR 口令：对每条 up/down(含 small) 口令，取窗口 [end_sec, end_sec+LAG]，
     若存在同向事件与窗口相交 → 命中。过滤退化条目（时长>CMD_MAX_DUR 的脏数据）。
  B. 人工动作标注：对每个 [start,end] 动作段，
     覆盖率 = 同向事件与动作段相交时长 / 动作段时长；
     符号检验 = 段内稳定样本融合速度的均值方向。
反向也查：每个检测事件是否被任一考卷佐证（precision 侧参考）。
输出：result/lift_analysis/evaluation.json + evaluation.md
启动: conda run --no-capture-output -n shanghai_cv python code/lift_analysis/evaluate.py
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LIFT_DIR = PROJECT_ROOT / "result" / "lift_analysis"

LAG_S = 5.0            # 口令→动作最大滞后窗口
CMD_MAX_DUR = 3.0      # 口令条目自身时长超过此值视为脏数据
MIN_COVER = 0.3        # 动作段被同向事件覆盖比例达到此值记命中


def load_json(p: Path):
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def eval_video(ser, cmds, actions):
    events = ser.get("events", [])
    t = ser.get("t", [])
    v = ser.get("v", [])
    stable = ser.get("stable", [])
    up_down = {"up": "up", "up_small": "up", "down": "down", "down_small": "down"}

    # A. ASR 口令召回
    cmd_rows = []
    for c in cmds:
        d = up_down.get(c.get("label"))
        s, e = c.get("start_sec"), c.get("end_sec")
        if d is None or s is None or e is None:
            continue
        if e - s > CMD_MAX_DUR:
            cmd_rows.append({"cmd": c.get("word"), "dir": d, "at": round(s, 1),
                             "hit": None, "note": "dirty_skipped"})
            continue
        w0, w1 = e, e + LAG_S
        hit = next((ev for ev in events if ev["dir"] == d
                    and overlap(ev["start"], ev["end"], w0, w1) > 0), None)
        cmd_rows.append({"cmd": c.get("word"), "dir": d, "at": round(e, 1),
                         "hit": bool(hit),
                         "event": [hit["start"], hit["end"]] if hit else None})

    # B. 人工标注段
    act_rows = []
    for a in actions:
        typ = a.get("type")
        if typ not in ("up", "down"):
            continue
        s, e = a.get("start_sec", 0.0), a.get("end_sec", 0.0)
        if e <= s:
            continue
        cov = sum(overlap(s, e, ev["start"], ev["end"])
                  for ev in events if ev["dir"] == typ) / (e - s)
        # 符号检验：段内稳定样本的均值方向
        vs = [v[i] for i in range(len(t)) if t[i] >= s and t[i] <= e
              and i < len(stable) and stable[i] and i < len(v)]
        mean_v = round(statistics.mean(vs), 1) if vs else None
        sign_ok = (mean_v is not None and
                   (mean_v > 0) == (typ == "up") and abs(mean_v) > 5.0)
        act_rows.append({"type": typ, "start": round(s, 1), "end": round(e, 1),
                         "cover": round(min(cov, 1.0), 2),
                         "hit": cov >= MIN_COVER, "mean_v": mean_v,
                         "sign_ok": sign_ok})

    # 反向：事件被佐证比例
    ev_rows = []
    for ev in events:
        by_cmd = any(up_down.get(c.get("label")) == ev["dir"] and
                     c.get("end_sec", 0) - c.get("start_sec", 0) <= CMD_MAX_DUR and
                     overlap(ev["start"], ev["end"], c["end_sec"],
                             c["end_sec"] + LAG_S) > 0 for c in cmds)
        by_act = any(a.get("type") == ev["dir"] and
                     overlap(ev["start"], ev["end"], a.get("start_sec", 0),
                             a.get("end_sec", 0)) > 0 for a in actions)
        ev_rows.append({"dir": ev["dir"], "start": ev["start"], "end": ev["end"],
                        "supported": by_cmd or by_act or not actions and not cmds})
    return {"events_n": len(events), "cmd": cmd_rows, "act": act_rows,
            "ev": ev_rows}


def summarize(per_video):
    def rate(rows, key):
        valid = [r for r in rows if r.get(key) is not None]
        ok = sum(1 for r in valid if r[key])
        return ok, len(valid)

    all_cmd = [r for p in per_video.values() for r in p["cmd"]]
    all_act = [r for p in per_video.values() for r in p["act"]]
    all_ev = [r for p in per_video.values() for r in p["ev"]]
    cmd_ok, cmd_n = rate(all_cmd, "hit")
    act_ok, act_n = rate(all_act, "hit")
    sign_ok, sign_n = rate(all_act, "sign_ok")
    sup_ok, sup_n = rate(all_ev, "supported")
    return {
        "videos": len(per_video),
        "events_total": sum(p["events_n"] for p in per_video.values()),
        "asr_cmd_recall": [cmd_ok, cmd_n],
        "annot_action_event_cover_hit": [act_ok, act_n],
        "annot_action_sign_ok": [sign_ok, sign_n],
        "event_supported": [sup_ok, sup_n],
    }


def write_md(path, per_video, summ):
    lines = ["# 吊运起降识别 v1.0 评估", "",
             f"- 视频数: {summ['videos']}  事件总数: {summ['events_total']}",
             f"- ASR 口令命中率(召回): {summ['asr_cmd_recall'][0]}/{summ['asr_cmd_recall'][1]}",
             f"- 标注动作段被同向事件覆盖(≥{int(MIN_COVER*100)}%): "
             f"{summ['annot_action_event_cover_hit'][0]}/{summ['annot_action_event_cover_hit'][1]}",
             f"- 标注动作段内速度符号正确: "
             f"{summ['annot_action_sign_ok'][0]}/{summ['annot_action_sign_ok'][1]}",
             f"- 检测事件被口令/标注佐证(参考): "
             f"{summ['event_supported'][0]}/{summ['event_supported'][1]}", ""]
    for stem, p in sorted(per_video.items()):
        lines.append(f"## {stem}")
        if p["cmd"]:
            lines.append("| 口令 | 方向 | 时刻s | 命中 | 事件 |")
            lines.append("|---|---|---|---|---|")
            for r in p["cmd"]:
                h = "-" if r.get("hit") is None else ("Y" if r["hit"] else "N")
                lines.append(f"| {r['cmd']} | {r['dir']} | {r['at']} | {h} "
                             f"| {r.get('event', '')} |")
        if p["act"]:
            lines.append("")
            lines.append("| 标注 | 起s | 止s | 覆盖率 | 命中 | 段内均速 | 符号 |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in p["act"]:
                lines.append(f"| {r['type']} | {r['start']} | {r['end']} "
                             f"| {r['cover']} | {'Y' if r['hit'] else 'N'} "
                             f"| {r['mean_v']} | {'Y' if r['sign_ok'] else 'N'} |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="2025.7.21集装箱作业视频")
    args = ap.parse_args()
    vdir = LIFT_DIR / args.folder
    per_video, missing = {}, []
    for jp in sorted(vdir.glob("*.json")):
        ser = load_json(jp)
        if not ser:
            continue
        cmds = (load_json(PROJECT_ROOT / "result" / "asr" / args.folder
                          / jp.name) or {}).get("commands", [])
        acts = (load_json(PROJECT_ROOT / "result" / "annotations" / args.folder
                          / jp.name) or {}).get("actions", [])
        if not cmds and not acts:
            missing.append(jp.stem)
            continue
        per_video[jp.stem] = eval_video(ser, cmds, acts)
    summ = summarize(per_video)
    out = {"params": {"lag_s": LAG_S, "cmd_max_dur": CMD_MAX_DUR,
                      "min_cover": MIN_COVER},
           "summary": summ, "per_video": per_video,
           "no_ground_truth": missing}
    (LIFT_DIR / "evaluation.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    write_md(LIFT_DIR / "evaluation.md", per_video, summ)
    print(f"[eval] videos={len(per_video)} no_gt={len(missing)}")
    for k, val in summ.items():
        print(f"  {k}: {val}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
