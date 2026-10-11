# -*- coding: utf-8 -*-
"""
生成 result/lift_analysis/index.html 审阅页（只读 lift_analysis 产物，纯本地静态页）
每视频一节：事件表 + 曲线图。顶部放评估摘要（若 evaluation.json 存在）。
启动: conda run --no-capture-output -n shanghai_cv python code/lift_analysis/make_index.py
"""
import html
import json
import sys
from pathlib import Path

LIFT_DIR = Path(__file__).resolve().parents[2] / "result" / "lift_analysis"


def load(p):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def esc(x):
    return html.escape(str(x))


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "2025.7.21集装箱作业视频"
    vdir = LIFT_DIR / folder
    ev_all = load(LIFT_DIR / "evaluation.json")
    per_video_ev = (ev_all or {}).get("per_video", {})

    parts = ["<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>",
             "<title>吊运起降识别 v1.0 审阅</title><style>",
             "body{font-family:'Microsoft YaHei',sans-serif;margin:24px;background:#fafafa}",
             "h1{font-size:22px} h2{font-size:16px;margin-top:36px;border-bottom:2px solid #ddd;padding-bottom:4px}",
             "table{border-collapse:collapse;font-size:13px;margin:8px 0}",
             "th,td{border:1px solid #ccc;padding:3px 10px;text-align:center}",
             "th{background:#eee}", ".up{color:#1a7f1a;font-weight:bold}",
             ".down{color:#c0392b;font-weight:bold}",
             "img{max-width:100%;border:1px solid #ddd;background:#fff}",
             ".meta{color:#666;font-size:13px}",
             ".summary{background:#fff;border:1px solid #ddd;padding:12px 16px;border-radius:6px}",
             "</style></head><body>",
             "<h1>吊运起降识别 v1.0 —— 传统 CV 可行性验证</h1>",
             "<div class='meta'>信号：全局运动 v_cam（跟随工况） + 残差光流 v_obj（注视工况），"
             "稳定窗口门控后取幅值大者；阈值 ±25 px/s、≥0.6s 成事件。<br>",
             "约定：<span class='up'>升=正</span> / <span class='down'>降=负</span>；"
             "图中淡色带=人工标注动作段，^v s 标记=ASR 口令。</div>"]

    if ev_all:
        s = ev_all["summary"]
        r = lambda x: f"{x[0]}/{x[1]}"
        parts.append(
            "<div class='summary'><b>自动评估摘要</b><br>"
            f"视频数 {s['videos']}，事件总数 {s['events_total']}；"
            f"ASR 口令命中 {r(s['asr_cmd_recall'])}；"
            f"标注段覆盖命中 {r(s['annot_action_event_cover_hit'])}；"
            f"标注段符号正确 {r(s['annot_action_sign_ok'])}；"
            f"事件被佐证 {r(s['event_supported'])}</div>")

    n = 0
    for jp in sorted(vdir.glob("*.json")):
        if jp.name == "evaluation.json":
            continue
        ser = load(jp)
        if not ser:
            continue
        n += 1
        stem = jp.stem
        evs = ser.get("events", [])
        stable_frac = (sum(ser.get("stable", [])) / max(1, len(ser.get("stable", []))))
        parts.append(f"<h2>{n}. {esc(stem)}</h2>")
        parts.append(
            f"<div class='meta'>时长 {ser.get('duration_sec')}s · "
            f"事件 {sum(1 for e in evs if e['dir']=='up')}升 / "
            f"{sum(1 for e in evs if e['dir']=='down')}降 · "
            f"稳定样本占比 {stable_frac:.0%}</div>")
        if evs:
            parts.append("<table><tr><th>#</th><th>方向</th><th>起(s)</th><th>止(s)</th>"
                         "<th>时长(s)</th><th>峰值px/s</th><th>均值px/s</th><th>稳定占比</th><th>佐证</th></tr>")
            pev = per_video_ev.get(stem, {}).get("ev", [])
            for i, e in enumerate(evs):
                sup = pev[i]["supported"] if i < len(pev) else "-"
                parts.append(
                    f"<tr><td>{i+1}</td><td class='{e['dir']}'>{'升' if e['dir']=='up' else '降'}</td>"
                    f"<td>{e['start']}</td><td>{e['end']}</td>"
                    f"<td>{e['end']-e['start']:.1f}</td><td>{e['peak_px_s']}</td>"
                    f"<td>{e['mean_px_s']}</td><td>{e['stable_frac']:.0%}</td>"
                    f"<td>{'Y' if sup else 'N'}</td></tr>")
            parts.append("</table>")
        png = LIFT_DIR / "plots" / f"{stem}.png"
        if png.is_file():
            parts.append(f"<img src='plots/{esc(png.name)}' loading='lazy'>")
        parts.append("<hr>")

    parts.append(f"<div class='meta'>共 {n} 个视频 · 生成自 code/lift_analysis/make_index.py</div>")
    parts.append("</body></html>")
    out = LIFT_DIR / "index.html"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"[index] {out} videos={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
