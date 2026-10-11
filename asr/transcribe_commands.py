#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcribe_commands.py — FunASR 全文转写 + 装卸命令词抽取

运行环境: WSL cfy_env (GPU)
用法示例:
  wsl.exe bash -lc "source ~/CodeFile/cfy_env/bin/activate && python /mnt/e/load_unload_video/code/asr/transcribe_commands.py --folder-pattern '2025.7.21*'"

说明:
  - 中文文件夹名不经 wsl.exe 传参（会乱码），用 --folder-pattern 通配符在 WSL 内部匹配
  - 输入: result/audio_extract/<日期文件夹>/*.wav
  - 输出: result/asr/<日期文件夹>/<wav名>.json（全文转写 + 命令词事件，逐字时间戳定位命令词起止）
  - 断点续跑: 已存在的输出 JSON 自动跳过（--overwrite 强制重跑）
"""
import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

PROJECT = Path("/mnt/e/load_unload_video")
AUDIO_DIR = PROJECT / "result" / "audio_extract"
OUT_DIR = PROJECT / "result" / "asr"

# 命令词表（最长优先匹配，避免"上升一点"被"上升"截胡）
COMMANDS = [
    ("上升一点", "up_small"),
    ("下降一点", "down_small"),
    ("上升", "up"),
    ("下降", "down"),
    ("停", "stop"),
]
HOTWORD = "上升一点 20 下降一点 20 上升 20 下降 20 停 20"
PUNCT_RE = re.compile(r"[，。、,.!?？；;：:“”\"'‘’《》<>\[\]()（）~～…—\s]")


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / float(w.getframerate())


def build_char_ts(sent):
    """句子 text 与逐字 timestamp 对齐（剔除标点），返回 (无标点文本, 每字时间戳列表)"""
    text = sent.get("text", "")
    ts = sent.get("timestamp") or []
    chars, char_ts = [], []
    ti = 0
    for ch in text:
        if PUNCT_RE.match(ch):
            continue
        chars.append(ch)
        if ti < len(ts):
            char_ts.append(ts[ti])
            ti += 1
        else:
            char_ts.append(None)
    return "".join(chars), char_ts


def extract_commands(sent_text, char_ts, sent_start_ms, sent_end_ms):
    """在无标点文本上做最长优先匹配，返回命令词事件列表（起止取逐字时间戳，缺失时回退整句边界）"""
    events = []
    i, n = 0, len(sent_text)
    while i < n:
        hit = None
        for word, label in COMMANDS:
            if sent_text.startswith(word, i):
                hit = (word, label)
                break
        if hit is None:
            i += 1
            continue
        word, label = hit
        # 过滤状态播报: 命令词后紧跟"结束"是完成汇报而非指令，如"下降结束""上升结束"
        if label != "stop" and sent_text.startswith("结束", i + len(word)):
            i += len(word)
            continue
        j = i + len(word) - 1
        s_ms = char_ts[i][0] if i < len(char_ts) and char_ts[i] else sent_start_ms
        e_ms = char_ts[j][1] if j < len(char_ts) and char_ts[j] else sent_end_ms
        if s_ms is None:
            s_ms = sent_start_ms
        if e_ms is None:
            e_ms = sent_end_ms
        events.append({
            "label": label,
            "word": word,
            "start_sec": round((s_ms or 0) / 1000.0, 3),
            "end_sec": round((e_ms or 0) / 1000.0, 3),
        })
        i = j + 1
    return events


def transcribe_file(model, wav: Path, out_json: Path):
    duration = wav_duration(wav)
    res = model.generate(input=str(wav), batch_size_s=300, hotword=HOTWORD,
                         sentence_timestamp=True)
    item = res[0] if res else {}
    sentences = item.get("sentence_info") or []
    transcript, commands = [], []
    if sentences:
        for sent in sentences:
            raw, char_ts = build_char_ts(sent)
            if not raw:
                continue
            s_ms, e_ms = sent.get("start", 0), sent.get("end", 0)
            transcript.append({
                "text": sent.get("text", raw),
                "start_sec": round((s_ms or 0) / 1000.0, 3),
                "end_sec": round((e_ms or 0) / 1000.0, 3),
            })
            for ev in extract_commands(raw, char_ts, s_ms, e_ms):
                ev["sentence"] = sent.get("text", raw)
                commands.append(ev)
    else:
        # 兜底: 无 sentence_info 时整段作为一句
        raw, char_ts = build_char_ts({"text": item.get("text", ""),
                                      "timestamp": item.get("timestamp") or []})
        if raw:
            transcript.append({"text": item.get("text", raw), "start_sec": 0.0,
                               "end_sec": round(duration, 3)})
            commands.extend(extract_commands(raw, char_ts, 0, round(duration * 1000)))
    payload = {
        "folder": wav.parent.name,
        "file": wav.name,
        "duration_sec": round(duration, 3),
        "engine": "funasr-paraformer-zh+fsmn-vad+ct-punc",
        "hotword": HOTWORD,
        "transcript": transcript,
        "commands": commands,
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return transcript, commands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder-pattern", required=True,
                    help="audio_extract 下文件夹通配符，如 '2025.7.21*'")
    ap.add_argument("--limit", type=int, default=0,
                    help="每个文件夹最多处理多少个文件（0=全部），用于试跑")
    ap.add_argument("--overwrite", action="store_true", help="重跑已存在的输出")
    args = ap.parse_args()

    folders = sorted([p for p in AUDIO_DIR.glob(args.folder_pattern) if p.is_dir()])
    if not folders:
        print(f"[error] 未匹配到文件夹: {args.folder_pattern}")
        sys.exit(1)

    import torch
    from funasr import AutoModel
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"[init] device={device}, 加载模型中（首次会下载约 1GB）…", flush=True)
    model = AutoModel(
        model="paraformer-zh",
        vad_model="fsmn-vad",
        vad_kwargs={"max_single_segment_time": 30000},
        punc_model="ct-punc",
        device=device,
    )
    print("[init] 模型就绪", flush=True)

    t0 = time.time()
    total_done = total_skip = total_fail = total_cmd = 0
    counts = {}
    for folder in folders:
        wavs = sorted(folder.glob("*.wav"))
        if args.limit:
            wavs = wavs[: args.limit]
        out_folder = OUT_DIR / folder.name
        print(f"[folder] {folder.name}: {len(wavs)} 个 wav", flush=True)
        for wav in wavs:
            out_json = out_folder / (wav.stem + ".json")
            if out_json.is_file() and not args.overwrite:
                total_skip += 1
                continue
            try:
                _, cmds = transcribe_file(model, wav, out_json)
            except Exception as exc:
                print(f"  [fail] {wav.name}: {exc}", flush=True)
                out_json.unlink(missing_ok=True)
                total_fail += 1
                continue
            total_done += 1
            total_cmd += len(cmds)
            for c in cmds:
                counts[c["word"]] = counts.get(c["word"], 0) + 1
            preview = " ".join(f"{c['word']}@{c['start_sec']:.1f}" for c in cmds[:8])
            print(f"  [ok] {wav.name}: 命令 {len(cmds)} 个 {preview}", flush=True)
    print(f"[done] 处理 {total_done}, 跳过 {total_skip}, 失败 {total_fail}, "
          f"命令词总数 {total_cmd} {counts}, 耗时 {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
