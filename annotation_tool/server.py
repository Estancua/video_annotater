# -*- coding: utf-8 -*-
r"""
标注程序本地服务器 v1.4
- 提供静态页面 (/)
- API: /api/folders /api/videos /api/loud /api/annotation(GET/POST) /api/asr /api/units /api/vocals
- 视频: /media/<文件夹>/<视频名> 自动转码 MOV->MP4(编码探测, h264/hevc 视频流copy+其它重编码, 音频 AAC)
  缓存到 result/annotation_cache, 支持 Range
v1.1: 转码前 ffprobe 探测视频编码，非 h264/hevc（如 *_tracked.mov 的 mpeg4）直接走 libx264 重编码，
      避免产出浏览器无法解码的 MP4
v1.2: 新增 /api/asr 读取 result/asr/<文件夹>/<视频名>.json 的 ASR 命令词识别结果
v1.3: 新增 /api/vocals 提供 BS-RoFormer 人声分离产物 result/vocal_separation/<文件夹>/<视频名>/vocals.wav
      (支持 Range, 供前端人声轨播放; 缺文件 404 由前端回退原音)
v1.4: 新增 /api/units —— 以 result/asr_v2 的命令词为标注单元，命令词把所在响段切开，
      切剩空隙全部成 gap 单元（文本用 asr_v2 的 fragments 预填），响段外的命令词标 source=asr_only；
      /api/annotation POST 增加防误删校验（已有 actions 不得被清空，voice_segments 缺失时沿用旧值）
v2.2: --data-root 可切换数据根（便携机指向 C:\Users\19614\portable\load_unload_video）；
      数据根下无 raw_video 时自动降级音频模式——/api/videos 返回 audio_extract 的 wav
      并带 has_video:false，新增 /api/audio 供浏览器直接播原音轨 wav
v2.5: 修预标注单元互相重叠——build_units 里命令词按起点排序后逐个裁剪（重叠时截断「起点较早」
      的那条到后一条起点，截空则整条丢弃），空隙改为「响段减去全部命令词区间」而非只减归属本段的；
      _merge_close_units 改用组内 running max end 判相邻。重叠不再被 0.5s 合并放大成跨段鬼影单元
启动: conda run --no-capture-output -n shanghai_cv python code/annotation_tool/server.py
便携: python code/annotation_tool/server.py --data-root C:\Users\19614\portable\load_unload_video
浏览器打开 http://localhost:8765
"""
import argparse
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "raw_video"
LOUD_DIR = PROJECT_ROOT / "result" / "loud_segments"
LOUD_V_DIR = PROJECT_ROOT / "result" / "loud_segments_vocals"  # 人声轨响段（v2.1）
ANN_DIR = PROJECT_ROOT / "result" / "annotations"
ASR_DIR = PROJECT_ROOT / "result" / "asr"
ASR_V2_DIR = PROJECT_ROOT / "result" / "asr_v2"
VOCAL_DIR = PROJECT_ROOT / "result" / "vocal_separation"
AUDIO_DIR = PROJECT_ROOT / "result" / "audio_extract"  # 原音轨 wav（v2.2 音频模式播放源）
CACHE_DIR = PROJECT_ROOT / "result" / "annotation_cache"
STATIC_DIR = Path(__file__).resolve().parent / "static"

VIDEO_EXTS = {".mov", ".mp4", ".avi", ".mkv"}
HOST, PORT = "127.0.0.1", 8765

_trans_locks = {}
_trans_locks_guard = threading.Lock()


def _path_lock(key):
    with _trans_locks_guard:
        if key not in _trans_locks:
            _trans_locks[key] = threading.Lock()
        return _trans_locks[key]


def safe_name(s: str) -> str:
    if not s or "/" in s or "\\" in s or ".." in s or s in (".", ".."):
        raise ValueError(f"非法名称: {s!r}")
    return s


def list_folders():
    """有响段 JSON 且原始视频文件夹（v2.2: 或音频文件夹）存在的日期文件夹"""
    if not LOUD_DIR.is_dir():
        return []
    return [p.stem for p in sorted(LOUD_DIR.glob("*.json"))
            if (RAW_DIR / p.stem).is_dir() or (AUDIO_DIR / p.stem).is_dir()]


def list_videos(folder: str):
    """有原始视频返回视频名；v2.2 无视频时回退 audio_extract 的 wav（音频模式）"""
    d = RAW_DIR / folder
    if d.is_dir():
        vids = sorted(p.name for p in d.iterdir()
                      if p.suffix.lower() in VIDEO_EXTS and p.is_file())
        if vids:
            return vids
    ad = AUDIO_DIR / folder
    if ad.is_dir():
        return sorted(p.name for p in ad.iterdir() if p.suffix.lower() == ".wav" and p.is_file())
    return []


def folder_has_video(folder: str) -> bool:
    """该日期文件夹是否有可播视频源（否则前端进音频模式）"""
    d = RAW_DIR / folder
    return d.is_dir() and any(p.suffix.lower() in VIDEO_EXTS and p.is_file() for p in d.iterdir())


def _load_loud_json(d: Path, folder: str):
    f = d / f"{folder}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _loud_by_stem(data):
    return {Path(item.get("file", "")).stem: item
            for item in (data or {}).get("files") or []}


def has_saved_units(folder: str, stem: str) -> bool:
    """该视频是否已有语音单元标注（用于按视频锁定响段基准，保护存量标注）"""
    p = ANN_DIR / folder / f"{stem}.json"
    if not p.is_file():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("speech_units"))
    except Exception:
        return False


def pick_loud_entry(folder: str, stem: str, v_map: dict, o_map: dict):
    """v2.1 按视频锁定基准：已标语音单元的视频沿用整轨响段，其余优先人声版；缺哪个回退另一个"""
    if has_saved_units(folder, stem):
        first, second = o_map.get(stem), v_map.get(stem)
    else:
        first, second = v_map.get(stem), o_map.get(stem)
    e = first or second
    return e, ("vocals" if e is v_map.get(stem) else "full_mix")


def loud_entry(folder: str, stem: str):
    v_map = _loud_by_stem(_load_loud_json(LOUD_V_DIR, folder))
    o_map = _loud_by_stem(_load_loud_json(LOUD_DIR, folder))
    return pick_loud_entry(folder, stem, v_map, o_map)[0]


def load_loud(folder: str):
    """合并视图：每个文件带 loud_source 标记（vocals/full_mix）"""
    v_map = _loud_by_stem(_load_loud_json(LOUD_V_DIR, folder))
    o_map = _loud_by_stem(_load_loud_json(LOUD_DIR, folder))
    files = []
    for stem in sorted(set(v_map) | set(o_map)):
        e, src = pick_loud_entry(folder, stem, v_map, o_map)
        if e:
            files.append({**e, "loud_source": src})
    return {"folder": folder, "files": files}


def annotation_path(folder: str, video: str) -> Path:
    return ANN_DIR / folder / f"{Path(video).stem}.json"


def load_annotation(folder: str, video: str):
    p = annotation_path(folder, video)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"video": video, "folder": folder, "actions": []}


def load_asr(folder: str, video: str):
    """ASR 命令词识别结果; 无则返回 exists:false"""
    p = ASR_DIR / folder / f"{Path(video).stem}.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("exists", True)
            return data
        except Exception as e:
            return {"exists": False, "error": str(e)}
    return {"exists": False}


def save_annotation(folder: str, video: str, data: dict):
    p = annotation_path(folder, video)
    p.parent.mkdir(parents=True, exist_ok=True)
    data.setdefault("video", video)
    data.setdefault("folder", folder)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def annotation_flags(folder: str, video: str):
    """v2.6 左侧列表灯：一次读 json 给出是否有标注 + 两个人工确认标志（缺字段一律未确认）"""
    res = {"has_ann": False, "confirm_video": False, "confirm_unit": False}
    p = annotation_path(folder, video)
    if not p.is_file():
        return res
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return res
    res["has_ann"] = bool(d.get("actions") or d.get("speech_units"))
    res["confirm_video"] = bool(d.get("confirm_video"))
    res["confirm_unit"] = bool(d.get("confirm_unit"))
    return res


def load_asr_v2(folder: str, video: str):
    """新版 ASR（含逐字时间戳与命令词之外的 fragments）；无则 exists:false"""
    p = ASR_V2_DIR / folder / f"{Path(video).stem}.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data.setdefault("exists", True)
            return data
        except Exception as e:
            return {"exists": False, "error": str(e)}
    return {"exists": False}


WAVE_BIN_SEC = 0.02  # 人声波形包络粒度（秒/箱）


def _compute_waveform(wav_path: Path):
    """vocals.wav -> 每 20ms RMS 包络（mono）"""
    import numpy as np
    import soundfile as sf
    arr, sr = sf.read(str(wav_path), dtype="float32")
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    duration = len(arr) / float(sr) if sr else 0.0
    hop = max(1, int(round(sr * WAVE_BIN_SEC)))
    n = max(1, int(np.ceil(len(arr) / hop)))
    if n * hop > len(arr):
        arr = np.pad(arr, (0, n * hop - len(arr)))
    rms = np.sqrt(np.mean(arr[:n * hop].reshape(n, hop) ** 2, axis=1))
    return {
        "bin_sec": WAVE_BIN_SEC,
        "duration_sec": round(duration, 3),
        "max": round(float(rms.max()) if rms.size else 0.0, 5),
        "rms": [round(float(x), 5) for x in rms],
    }


def load_waveform(folder: str, video: str):
    """人声波形包络；磁盘缓存 result/annotation_cache/waveforms/<folder>/<stem>.json"""
    stem = Path(video).stem
    wav = VOCAL_DIR / folder / stem / "vocals.wav"
    out = CACHE_DIR / "waveforms" / folder / f"{stem}.json"
    if wav.is_file() and out.is_file() and out.stat().st_mtime >= wav.stat().st_mtime:
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not wav.is_file():
        return {"available": False, "reason": "no_vocals"}
    with _path_lock(str(out)):
        if out.is_file() and out.stat().st_mtime >= wav.stat().st_mtime:
            try:
                return json.loads(out.read_text(encoding="utf-8"))
            except Exception:
                pass
        try:
            payload = _compute_waveform(wav)
        except Exception as e:
            return {"available": False, "reason": repr(e)}
        payload["available"] = True
        payload["file"] = wav.name
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # 缓存写失败不影响返回
        return payload


def _unit_id(kind: str, start: float, end: float, label: str = "") -> str:
    """单元稳定标识：同一起止+标签重建后仍是同一个 id，便于增量保存标注"""
    return f"{kind}:{start:.2f}-{end:.2f}" + (f":{label}" if label else "")


MERGE_GAP_SEC = 0.5


def _merge_close_units(units, gap_sec=MERGE_GAP_SEC):
    """相邻单元间隔 < gap_sec（含无缝衔接）合并为一条；多条并一条时 label 取非空去重拼接"""
    groups = []
    tails = []  # v2.5: 每组内已见到的最大 end_sec，不能用「组内最后一个」判相邻——
                # 嵌套/重叠时最后一条可能比组内其他条短，会把本该同组的单元劈到下一组，产出仍重叠的两条
    for u in sorted(units, key=lambda x: (x["start_sec"], x["end_sec"])):
        if groups and u["start_sec"] - tails[-1] < gap_sec:
            groups[-1].append(u)
            tails[-1] = max(tails[-1], u["end_sec"])
        else:
            groups.append([u])
            tails.append(u["end_sec"])
    merged = []
    for g in groups:
        if len(g) == 1:
            merged.append(g[0])
            continue
        start = min(u["start_sec"] for u in g)
        end = max(u["end_sec"] for u in g)
        kind = "cmd" if any(u["kind"] == "cmd" for u in g) else "gap"
        labels = list(dict.fromkeys(u["label"] for u in g if u.get("label")))
        label = "+".join(labels)
        words = " ".join(u["word"] for u in g if u.get("word"))
        text = " ".join(u["text"] for u in g if u.get("text"))
        sources = {u["source"] for u in g}
        source = "loud" if "loud" in sources else ("asr_only" if "asr_only" in sources else "loud_gap")
        sentence = next((u["sentence"] for u in g if u.get("sentence")), "")
        merged.append({
            "id": _unit_id(kind, start, end, label),
            "kind": kind,
            "label": label,
            "word": words,
            "text": text or words,
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "dur_sec": round(end - start, 3),
            "source": source,
            "sentence": sentence,
            "merged_from": [u["id"] for u in g],
        })
    return merged


def build_units(loud_segs, asr):
    """以 ASR 命令词为标注单元；命令词把所在响段切开，切剩的空隙全部成为待确认单元"""
    cmds = sorted(asr.get("commands") or [], key=lambda c: c.get("start_sec", 0))
    frags = asr.get("fragments") or []
    sents = asr.get("transcript") or []

    def host_seg(c):
        mid = (c.get("start_sec", 0) + c.get("end_sec", 0)) / 2.0
        for seg in loud_segs:
            if seg["start_sec"] - 0.05 <= mid <= seg["end_sec"] + 0.05:
                return seg
        return None

    def sentence_of(start, end):
        mid = (start + end) / 2.0
        for s in sents:
            if s.get("start_sec", 0) <= mid <= s.get("end_sec", 1e18):
                return s.get("text", "")
        return ""

    def frag_text(a, b):
        parts = []
        for fr in frags:
            fs, fe = fr.get("start_sec"), fr.get("end_sec")
            if fs is None or fe is None:
                continue
            if fs < b and fe > a:
                t = (fr.get("text") or "").strip()
                if t:
                    parts.append(t)
        return " ".join(parts)

    # v2.5: 命令词先按起点排序再逐个裁剪重叠——重叠时把「起点较早」那条的终点截到本条起点，
    #       截到没长度就整条丢弃（被完全包住的短词因此能完整保留）。ASR 的超长命令词命中会
    #       盖住相邻的正常词，这就是 cmd/cmd 重叠、并被 0.5s 合并进一步放大的源头。
    clipped = 0
    spans = []
    for c in sorted(cmds, key=lambda x: (round(x.get("start_sec") or 0, 3),
                                         -(round(x.get("end_sec") or 0, 3)))):
        s = round(float(c.get("start_sec") or 0), 3)
        e = round(float(c.get("end_sec") or 0), 3)
        if e <= s:
            continue
        while spans and s < spans[-1][1]:
            spans[-1][1] = s
            clipped += 1
            if spans[-1][1] - spans[-1][0] < 0.05:
                spans.pop()
            else:
                break
        spans.append([s, e, c])

    units = []
    for s, e, c in spans:
        in_seg = host_seg(c) is not None
        units.append({
            "id": _unit_id("cmd", s, e, c.get("label", "")),
            "kind": "cmd",
            "label": c.get("label", ""),
            "word": c.get("word", ""),
            "text": c.get("word", ""),
            "start_sec": s,
            "end_sec": e,
            "dur_sec": round(e - s, 3),
            "source": "loud" if in_seg else "asr_only",
            "sentence": c.get("sentence") or sentence_of(s, e),
        })

    # v2.5: 空隙改为「响段减去全部命令词区间」，而不是只减去按中点归属到本段的命令词。
    #       跨段命中、或归属被别的段抢走的命令词，原先会在本段留下与别处 cmd 重叠的 gap。
    cmd_iv = sorted((u["start_sec"], u["end_sec"]) for u in units)

    def add_gap(a, b):
        if b - a < 0.05:
            return
        units.append({
            "id": _unit_id("gap", a, b),
            "kind": "gap",
            "label": "",
            "word": "",
            "text": frag_text(a, b),
            "start_sec": round(a, 3),
            "end_sec": round(b, 3),
            "dur_sec": round(b - a, 3),
            "source": "loud_gap",
            "sentence": sentence_of(a, b),
        })

    for seg in loud_segs:
        cursor = seg["start_sec"]
        for cs, ce in cmd_iv:
            if ce <= cursor:
                continue
            if cs >= seg["end_sec"]:
                break
            add_gap(cursor, min(cs, seg["end_sec"]))
            cursor = ce
            if cursor >= seg["end_sec"]:
                break
        add_gap(cursor, seg["end_sec"])

    units.sort(key=lambda u: (u["start_sec"], u["end_sec"]))
    raw_count = len(units)
    units = _merge_close_units(units)
    stats = {
        "cmds": len(cmds),
        "cmd_units": sum(1 for u in units if u["kind"] == "cmd"),
        "gap_units": sum(1 for u in units if u["kind"] == "gap"),
        "asr_only": sum(1 for u in units if u["source"] == "asr_only"),
        "loud_segs": len(loud_segs),
        "merged": raw_count - len(units),
        "cmd_clipped": clipped,  # v2.5: 本次被裁剪/丢弃的重叠命令词条数
    }
    return units, stats


def probe_vcodec(src: Path) -> str:
    """探测首个视频流编码名，失败返回空串"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return (r.stdout or "").strip().splitlines()[0].strip() if r.stdout.strip() else ""
    except Exception:
        return ""


def transcode_to_mp4(src: Path, dst: Path):
    """视频 -> MP4 (h264/hevc 视频流 copy + AAC 音频); 其它编码(如 mpeg4)或 copy 失败则回退 libx264 重编码"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.mp4")
    base = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(src)]
    tail = ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp)]
    cmds = []
    if probe_vcodec(src) in ("h264", "hevc"):
        cmds.append(base + ["-c:v", "copy"] + tail)
    cmds.append(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"] + tail)
    last_err = ""
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                tmp.replace(dst)
                return
            last_err = r.stderr[-500:]
        except FileNotFoundError:
            raise RuntimeError("未找到 ffmpeg, 请确认当前环境已安装 ffmpeg")
        finally:
            if tmp.exists() and not dst.exists():
                tmp.unlink(missing_ok=True)
    raise RuntimeError(f"转码失败: {last_err}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[http]", fmt % args)

    # ---------- 响应工具 ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file_bytes(self, path: Path, ctype: str):
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_media_range(self, path: Path, ctype="video/mp4"):
        """支持 HTTP Range, 浏览器拖动进度条必需"""
        size = path.stat().st_size
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "") or "")
        if m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            code, extra = 206, {"Content-Range": f"bytes {start}-{end}/{size}"}
        else:
            start, end, code, extra = 0, size - 1, 200, {}
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 18, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        try:
            if path in ("/", "/index.html"):
                return self.send_file_bytes(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            if path == "/static/app.js":
                return self.send_file_bytes(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            if path == "/static/style.css":
                return self.send_file_bytes(STATIC_DIR / "style.css", "text/css; charset=utf-8")
            if path == "/api/folders":
                return self.send_json({"folders": list_folders()})
            if path == "/api/videos":
                folder = safe_name(q.get("folder", [""])[0])
                vids = list_videos(folder)
                return self.send_json({"videos": vids, "has_video": folder_has_video(folder),
                                       "ann_status": {v: annotation_flags(folder, v) for v in vids}})
            if path == "/api/loud":
                return self.send_json(load_loud(safe_name(q.get("folder", [""])[0])))
            if path == "/api/annotation":
                folder = safe_name(q.get("folder", [""])[0])
                video = safe_name(q.get("video", [""])[0])
                return self.send_json(load_annotation(folder, video))
            if path == "/api/asr":
                folder = safe_name(q.get("folder", [""])[0])
                video = safe_name(q.get("video", [""])[0])
                return self.send_json(load_asr(folder, video))
            if path == "/api/units":
                folder = safe_name(q.get("folder", [""])[0])
                video = safe_name(q.get("video", [""])[0])
                stem = Path(video).stem
                segs = (loud_entry(folder, stem) or {}).get("segments") or []
                asr = load_asr_v2(folder, video)
                legacy = False
                if not asr.get("exists"):
                    asr = load_asr(folder, video)  # 回退旧版：有命令词但无 fragments 预填
                    legacy = bool(asr.get("exists"))
                units, stats = build_units(segs, asr)
                return self.send_json({"exists": bool(asr.get("exists")), "legacy": legacy,
                                       "duration_sec": asr.get("duration_sec"),
                                       "units": units, "stats": stats})
            if path == "/api/vocals":
                folder = safe_name(q.get("folder", [""])[0])
                video = safe_name(q.get("video", [""])[0])
                p = VOCAL_DIR / folder / Path(video).stem / "vocals.wav"
                if not p.is_file():
                    return self.send_error(404, "无人声分离文件")
                return self.send_media_range(p, "audio/wav")
            if path == "/api/waveform":
                folder = safe_name(q.get("folder", [""])[0])
                video = safe_name(q.get("video", [""])[0])
                return self.send_json(load_waveform(folder, video))
            if path == "/api/audio":  # v2.2 音频模式：直接播 audio_extract 的原音轨 wav
                folder = safe_name(q.get("folder", [""])[0])
                video = safe_name(q.get("video", [""])[0])
                p = AUDIO_DIR / folder / (Path(video).stem + ".wav")
                if not p.is_file():
                    return self.send_error(404, "无音频文件")
                return self.send_media_range(p, "audio/wav")
            if path.startswith("/media/"):
                parts = [safe_name(unquote(x)) for x in path[len("/media/"):].split("/") if x]
                if len(parts) != 2:
                    return self.send_error(400)
                folder, name = parts
                src = RAW_DIR / folder / name
                if not src.is_file():
                    return self.send_error(404, "视频不存在")
                dst = CACHE_DIR / folder / f"{Path(name).stem}.mp4"
                if not dst.is_file():
                    with _path_lock(str(dst)):
                        if not dst.is_file():
                            transcode_to_mp4(src, dst)
                return self.send_media_range(dst)
            self.send_error(404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": repr(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/annotation":
            return self.send_error(404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            folder = safe_name(data["folder"])
            video = safe_name(data["video"])
            old = load_annotation(folder, video)
            if (old.get("actions") or []) and not data.get("actions"):
                return self.send_json({"error": "拒绝保存：提交内容丢失了已有的 %d 条 actions"
                                                % len(old["actions"])}, 409)
            if "voice_segments" not in data and "voice_segments" in old:
                data["voice_segments"] = old["voice_segments"]  # 旧字段不清空
            # v2.6 人工确认标志：save_annotation 是整文件覆盖，本次未回传必须沿用旧值
            for k in ("confirm_video", "confirm_unit"):
                if k not in data and k in old:
                    data[k] = old[k]
            save_annotation(folder, video, data)
            return self.send_json({"ok": True, "path": str(annotation_path(folder, video))})
        except (ValueError, KeyError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": repr(e)}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True


def apply_data_root(root: str):
    """v2.2: --data-root 切换数据根（便携机指 C:\\Users\\19614\\portable\\load_unload_video）"""
    global PROJECT_ROOT, RAW_DIR, LOUD_DIR, LOUD_V_DIR, ANN_DIR, ASR_DIR, ASR_V2_DIR, \
           VOCAL_DIR, AUDIO_DIR, CACHE_DIR
    PROJECT_ROOT = Path(root).resolve()
    RAW_DIR = PROJECT_ROOT / "raw_video"
    LOUD_DIR = PROJECT_ROOT / "result" / "loud_segments"
    LOUD_V_DIR = PROJECT_ROOT / "result" / "loud_segments_vocals"
    ANN_DIR = PROJECT_ROOT / "result" / "annotations"
    ASR_DIR = PROJECT_ROOT / "result" / "asr"
    ASR_V2_DIR = PROJECT_ROOT / "result" / "asr_v2"
    VOCAL_DIR = PROJECT_ROOT / "result" / "vocal_separation"
    AUDIO_DIR = PROJECT_ROOT / "result" / "audio_extract"
    CACHE_DIR = PROJECT_ROOT / "result" / "annotation_cache"


def main():
    ap = argparse.ArgumentParser(description="标注服务器（v2.2 支持 --data-root 切换数据根）")
    ap.add_argument("--data-root", default=None,
                    help="数据根目录（含 raw_video/ 与 result/）；缺省取代码位置推导的仓库根")
    ap.add_argument("--port", type=int, default=PORT, help=f"监听端口，默认 {PORT}")
    args = ap.parse_args()
    if args.data_root:
        apply_data_root(args.data_root)
    port = args.port
    server = Server((HOST, port), Handler)
    print(f"标注服务器已启动: http://localhost:{port}")
    print(f"  数据根  : {PROJECT_ROOT}" + ("（--data-root）" if args.data_root else ""))
    print(f"  原始视频: {RAW_DIR}" + ("" if RAW_DIR.is_dir() else "  【缺失→音频模式】"))
    print(f"  响段目录: {LOUD_DIR}")
    print(f"  人声响段: {LOUD_V_DIR}（新视频优先，已标视频按视频锁定沿用整轨版）")
    print(f"  标注输出: {ANN_DIR}")
    print(f"  新版ASR : {ASR_V2_DIR}")
    print(f"  转码缓存: {CACHE_DIR}")
    print(f"  人声分离: {VOCAL_DIR}")
    print("Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
