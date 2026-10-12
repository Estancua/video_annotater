# -*- coding: utf-8 -*-
"""v2.2 标注回流合并：把便携数据根（如 C:\\Users\\19614\\portable\\load_unload_video）里
标注出的 result/annotations 合并回桌面 E 盘（或反向），逐文件按 mtime 取新：

- 仅一端存在        → 复制到另一端
- 两端内容一致      → 跳过
- 内容不同          → mtime 新者覆盖旧者（被覆盖文件先备份到 dst/_backup_merge/<时间戳>/）
- mtime 相差 < 2s 且内容不同 → 视为冲突：报警跳过，人工处理

用法（conda shanghai_cv 或任意 py3）：
  python merge_annotations.py                          # 默认便携端 → E 盘
  python merge_annotations.py --src ... --dst ...
  python merge_annotations.py --dry-run                # 只看会做什么，不落盘
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_SRC = Path(r"C:\Users\19614\portable\load_unload_video\result\annotations")
DEFAULT_DST = Path(r"E:\load_unload_video\result\annotations")
CONFLICT_TOL = 2.0  # 秒：mtime 差距小于该值且内容不同 → 冲突


def load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def same_content(a: Path, b: Path) -> bool:
    """先比字节，再比解析后的 JSON（格式化差异不算内容差异）。"""
    if a.read_bytes() == b.read_bytes():
        return True
    ja, jb = load_json(a), load_json(b)
    return ja is not None and ja == jb


def collect(root: Path):
    """返回 {相对路径: 绝对路径}（只收 *.json）。"""
    out = {}
    if not root.is_dir():
        return out
    for p in root.rglob("*.json"):
        if "_backup_merge" in p.parts:
            continue
        out[p.relative_to(root).as_posix()] = p
    return out


def main():
    ap = argparse.ArgumentParser(description="合并两套标注目录（mtime 取新 + 冲突报警）")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="便携端 annotations 目录")
    ap.add_argument("--dst", default=str(DEFAULT_DST), help="桌面端 annotations 目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if not src.is_dir():
        sys.exit(f"[错误] 源目录不存在：{src}")
    if not dst.is_dir():
        dst.mkdir(parents=True, exist_ok=True)

    fs, fd = collect(src), collect(dst)
    backup_dir = dst / "_backup_merge" / datetime.now().strftime("%Y%m%d_%H%M%S")
    n_copy = n_skip = n_newer = n_conflict = 0

    def info(msg):
        print(("[dry-run] " if args.dry_run else "") + msg)

    for rel, sp in sorted(fs.items()):
        dp = fd.get(rel)
        if dp is None:  # 仅便携端有 → 新增到桌面
            n_copy += 1
            info(f"新增   {rel}")
            if not args.dry_run:
                dp2 = dst / rel
                dp2.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(sp, dp2)
            continue
        if same_content(sp, dp):
            n_skip += 1
            continue
        ms, md = sp.stat().st_mtime, dp.stat().st_mtime
        if abs(ms - md) < CONFLICT_TOL:  # 难判新旧：报警跳过，人工处理
            n_conflict += 1
            print(f"[冲突] {rel} 两端内容不同且 mtime 相差<{CONFLICT_TOL}s，跳过")
            continue
        if ms > md:  # 便携端新 → 备份桌面旧文件后覆盖
            n_newer += 1
            info(f"取新   {rel}（便携端 {datetime.fromtimestamp(ms):%m-%d %H:%M:%S} > 桌面 {datetime.fromtimestamp(md):%m-%d %H:%M:%S}）")
            if not args.dry_run:
                (backup_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dp, backup_dir / rel)
                shutil.copy2(sp, dp)
        else:  # 桌面端新 → 反向把桌面内容推回便携端
            n_newer += 1
            info(f"取新   {rel}（桌面端 {datetime.fromtimestamp(md):%m-%d %H:%M:%S} > 便携 {datetime.fromtimestamp(ms):%m-%d %H:%M:%S}，回写便携端）")
            if not args.dry_run:
                shutil.copy2(dp, sp)

    # 桌面独有文件：说明是回桌面后新标的，正常保留，仅计数
    dst_only = sorted(set(fd) - set(fs))
    if dst_only:
        print(f"[提示] 桌面独有 {len(dst_only)} 个文件，保留不动")

    print("-" * 60)
    print(f"复制新增 {n_copy}｜内容一致跳过 {n_skip}｜按 mtime 取新 {n_newer}｜"
          f"冲突（mtime 相差<{CONFLICT_TOL}s 且内容不同，需人工） {n_conflict}｜桌面独有 {len(dst_only)}")
    if n_conflict:
        print("[警告] 存在冲突文件，未自动处理，请人工比对后重跑。")
    if not args.dry_run and n_newer:
        print(f"被覆盖的桌面旧文件已备份到：{backup_dir}")


if __name__ == "__main__":
    main()
