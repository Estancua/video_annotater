# -*- coding: utf-8 -*-
"""
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
启动: conda run --no-capture-output -n shanghai_cv python code/annotation_tool/server.py
浏览器打开 http://localhost:8765
"""
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
ANN_DIR = PROJECT_ROOT / "result" / "annotations"
ASR_DIR = PROJECT_ROOT / "result" / "asr"
ASR_V2_DIR = PROJECT_ROOT / "result" / "asr_v2"
VOCAL_DIR = PROJECT_ROOT / "result" / "vocal_separation"
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
    """有响段 JSON 且原始视频文件夹存在的日期文件夹"""
    if not LOUD_DIR.is_dir():
        return []
    return [p.stem for p in sorted(LOUD_DIR.glob("*.json")) if (RAW_DIR / p.stem).is_dir()]


def list_videos(folder: str):
    d = RAW_DIR / folder
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir()
                  if p.suffix.lower() in VIDEO_EXTS and p.is_file())


def load_loud(folder: str):
    f = LOUD_DIR / f"{folder}.json"
    if not f.is_file():
        return {"files": []}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return {"files": [], "error": str(e)}


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


def _unit_id(kind: str, start: float, end: float, label: str = "") -> str:
    """单元稳定标识：同一起止+标签重建后仍是同一个 id，便于增量保存标注"""
    return f"{kind}:{start:.2f}-{end:.2f}" + (f":{label}" if label else "")


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

    assigned, orphans = {}, []
    for c in cmds:
        seg = host_seg(c)
        if seg is None:
            orphans.append(c)
        else:
            assigned.setdefault(id(seg), []).append(c)

    units = []
    for c in cmds:
        s, e = c.get("start_sec", 0), c.get("end_sec", 0)
        in_seg = host_seg(c) is not None
        units.append({
            "id": _unit_id("cmd", s, e, c.get("label", "")),
            "kind": "cmd",
            "label": c.get("label", ""),
            "word": c.get("word", ""),
            "text": c.get("word", ""),
            "start_sec": round(s, 3),
            "end_sec": round(e, 3),
            "dur_sec": round(e - s, 3),
            "source": "loud" if in_seg else "asr_only",
            "sentence": c.get("sentence") or sentence_of(s, e),
        })

    for seg in loud_segs:
        inner = sorted(assigned.get(id(seg), []), key=lambda c: c.get("start_sec", 0))
        heads = [seg["start_sec"]] + [c.get("end_sec", 0) for c in inner]
        tails = [c.get("start_sec", 0) for c in inner] + [seg["end_sec"]]
        for a, b in zip(heads, tails):
            if b - a < 0.05:
                continue
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

    units.sort(key=lambda u: (u["start_sec"], u["end_sec"]))
    stats = {
        "cmds": len(cmds),
        "cmd_units": sum(1 for u in units if u["kind"] == "cmd"),
        "gap_units": sum(1 for u in units if u["kind"] == "gap"),
        "asr_only": sum(1 for u in units if u["source"] == "asr_only"),
        "loud_segs": len(loud_segs),
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
                return self.send_json({"videos": list_videos(safe_name(q.get("folder", [""])[0]))})
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
                segs = []
                for item in load_loud(folder).get("files") or []:
                    if Path(item.get("file", "")).stem == stem:
                        segs = item.get("segments") or []
                        break
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
            save_annotation(folder, video, data)
            return self.send_json({"ok": True, "path": str(annotation_path(folder, video))})
        except (ValueError, KeyError) as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": repr(e)}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True


def main():
    server = Server((HOST, PORT), Handler)
    print(f"标注服务器已启动: http://localhost:{PORT}")
    print(f"  原始视频: {RAW_DIR}")
    print(f"  响段目录: {LOUD_DIR}")
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
