#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/publish_jar.py —— jar 发布流水线（W2-3）

问题：`check_update` / `rollback_jar` 只在**本地比较文件名和大小**，
所以"发一个新版"这件事至今是手工复制 jar，客户端也说不清"我是哪个版本"。

这个脚本把发布变成一条命令，产出**发布目录 + latest.json**：

    _releases/
        latest.json                     # 版本/大小/sha256/时间/文件名/备注
        unionclef-…-buddy10.jar         # 二进制（可放同一目录或指向绝对路径）
        history/unionclef-…-buddy9.jar  # 老版本留档（回滚用）

latest.json 的形状（客户端读它）：

    {"schema": 1, "version": "buddy10", "file": "unionclef-…-buddy10.jar",
     "size": 6480997, "sha256": "…", "published_at": "2026-10-29 21:00:00",
     "notes": "…", "previous": "buddy9"}

用法：
    python -X utf8 tools/publish_jar.py                      # 发默认 jar（buddy9）到 _releases/
    python -X utf8 tools/publish_jar.py --jar <新jar> --version buddy10 --notes "修 xxx"
    python -X utf8 tools/publish_jar.py --show               # 只打印当前 latest.json
    python -X utf8 tools/publish_jar.py --verify _releases/latest.json   # 校验 sha/size

设计取舍（和"更新"有关的坑）：
  · **不覆盖同名文件**：新版本必须换名；同名同大小会被 check_update 判成"已是最新"，
    实际内容可能已经变了（我们踩过：改了 jar 再复制过去，客户端说"已是最新"）；
  · 留 `previous` + history/ ⇒ 回滚不需要人记"上一版叫什么"；
  · sha256 一定算，`size` 保留（老客户端只用 size 也能工作）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JAR = (ROOT / "_refs" / "unionclef-0.95.63" / "versions" / "1.21.11" / "build"
               / "libs" / "unionclef-1.21.11-0.95.63-buddy9.jar")
DEFAULT_DIR = ROOT / "_releases"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_latest(rel_dir: Path) -> dict:
    p = Path(rel_dir) / "latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def publish(jar: Path, rel_dir: Path = DEFAULT_DIR, *, version: str = "",
            notes: str = "", keep_history: bool = True) -> dict:
    jar = Path(jar)
    if not jar.exists():
        return {"ok": False, "why": f"jar 不存在：{jar}"}
    rel_dir.mkdir(parents=True, exist_ok=True)
    hist = rel_dir / "history"
    if keep_history:
        hist.mkdir(parents=True, exist_ok=True)
    old = read_latest(rel_dir)
    version = (version or jar.stem.split("-")[-1] or time.strftime("%Y%m%d")).strip()
    idx_p = rel_dir / "versions.json"
    try:
        idx = json.loads(idx_p.read_text(encoding="utf-8"))
    except Exception:
        idx = {}
    # ⚠️ 老版本进 history：**必须在覆盖之前**。
    #    2026-10-29 实测踩过：新版和老版**同名**时，先 copy 新文件再归档 ⇒ history 里
    #    存的是新内容，"回滚到 v1"装回去的还是 v2（回滚形同虚设）。
    old_hist_file = ""
    if old.get("file"):
        oldp = rel_dir / str(old["file"])
        if oldp.exists():
            if keep_history:
                hist.mkdir(parents=True, exist_ok=True)
                dst = hist / oldp.name
                if dst.exists() and sha256_of(dst) != sha256_of(oldp):
                    dst = hist / f"{old.get('version') or 'old'}__{oldp.name}"
                shutil.copy2(oldp, dst)
                old_hist_file = str(Path("history") / dst.name)
            # 把这一版在 versions.json 里的指向改到 history（真实内容在那里）
            ov = old.get("version") or ""
            if ov:
                entry = idx.get(ov) or {}
                entry.setdefault("file", oldp.name)
                entry["file"] = old_hist_file or oldp.name
                entry.setdefault("size", oldp.stat().st_size)
                idx[ov] = entry
    dst = rel_dir / jar.name
    if dst.exists() and dst.stat().st_size == jar.stat().st_size \
            and sha256_of(dst) == sha256_of(jar):
        pass                                     # 同一份，不用再拷
    else:
        shutil.copy2(jar, dst)
    meta = {"schema": 1, "version": version, "file": jar.name,
            "size": dst.stat().st_size, "sha256": sha256_of(dst),
            "published_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "notes": notes or "",
            "previous": old.get("version") or "",
            "previous_file": old_hist_file or (old.get("file") or "")}
    (rel_dir / "latest.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    # 回滚表：version → file（历史版本指向 history/ 里的那一份）
    idx[version] = {"file": jar.name, "size": meta["size"], "sha256": meta["sha256"],
                    "at": meta["published_at"]}
    idx_p.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, **meta, "dir": str(rel_dir), "history": str(hist)}


def verify(meta_path: Path) -> dict:
    """校验 latest.json 指向的文件：在、大小对、sha256 对。"""
    meta_path = Path(meta_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    f = meta_path.parent / str(meta.get("file") or "")
    probs = []
    if not f.exists():
        probs.append(f"文件不存在：{f}")
    else:
        if int(meta.get("size") or -1) != f.stat().st_size:
            probs.append(f"大小不符：{meta.get('size')} vs {f.stat().st_size}")
        if str(meta.get("sha256") or "") != sha256_of(f):
            probs.append("sha256 不符")
    return {"ok": not probs, "problems": probs, "file": str(f), "version": meta.get("version")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", default=str(DEFAULT_JAR))
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--version", default="")
    ap.add_argument("--notes", default="")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--verify", default="")
    a = ap.parse_args()

    if a.show:
        m = read_latest(Path(a.dir))
        print(json.dumps(m or {"ok": False, "why": "还没有发布过"}, ensure_ascii=False, indent=2))
        return 0 if m else 1
    if a.verify:
        r = verify(Path(a.verify))
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1

    r = publish(Path(a.jar), Path(a.dir), version=a.version, notes=a.notes,
                keep_history=not a.no_history)
    if a.jar and r.get("ok"):
        v = verify(Path(a.dir) / "latest.json")
        r["verify"] = v
        if not v["ok"]:
            r["ok"] = False
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
