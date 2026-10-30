#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_model_fetch_test.py —— 首启"取模型"的离线回归（W2-2）

真实模型 239MB，不能每次回归都下。所以这里起一个**本地 HTTP 仓库**，用同名小文件
（model.int8.onnx 造 4KB）把真链路跑一遍：

  ① 全新下载        ⇒ 两个文件落到位，`model_status()` 认出来（等于"能加载"的前提）
  ② 断点续传        ⇒ 留半个 `.part`，续完内容必须与源完全一致（不是"看起来像"）
  ③ sha256 正确     ⇒ 通过
  ④ sha256 错误     ⇒ **必须失败**，而且坏文件**改名成 .onnx.bad**，不能留在原地冒充已装好
     （这条是 2026-10-29 实测的坑：只报错不清理 ⇒ 第二次调用直接"已装好"）

用法：
    python -X utf8 tools/_model_fetch_test.py            # 0=PASS
    python -X utf8 tools/_model_fetch_test.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "launcher") not in sys.path:
    sys.path.insert(0, str(ROOT / "launcher"))

import core as L                                        # noqa: E402

SRC = {"model.int8.onnx": os.urandom(4096), "tokens.txt": "一 二 三\n四 五\n".encode("utf-8")}


class Repo(BaseHTTPRequestHandler):
    """最小可用的"仓库"：支持 Range（测续传）、支持 404。"""

    def do_GET(self) -> None:                            # noqa: N802
        name = self.path.rsplit("/", 1)[-1]
        body = SRC.get(name)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range") or ""
        start = 0
        if rng.startswith("bytes="):
            try:
                start = int(rng.split("=")[1].split("-")[0])
            except Exception:
                start = 0
        part = body[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(part)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.end_headers()
        self.wfile.write(part)

    def log_message(self, *a) -> None:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    probs: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="model_fetch_"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Repo)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    repo = f"http://127.0.0.1:{port}/some/repo/resolve/main"
    sha = hashlib.sha256(SRC["model.int8.onnx"]).hexdigest()
    try:
        # ① 全新下载
        d1 = tmp / "dl"
        r1 = L.ensure_model(d1, repo=repo, on_line=lambda s: None)
        if not r1.get("ok"):
            probs.append(f"下载失败：{r1}")
        for fn, want in SRC.items():
            p = Path(r1.get("dir") or d1) / fn
            if not p.exists():
                probs.append(f"{fn} 没落地")
            elif p.read_bytes() != want:
                probs.append(f"{fn} 内容不对")
        if not L.model_status(d1).get("ok"):
            probs.append("下完了 model_status 仍不认识（形状不对）")

        # ② 断点续传：留半个 part，续完必须与源一致
        d2 = tmp / "resume"
        target = d2 / "some" / "repo"
        target.mkdir(parents=True, exist_ok=True)
        half = SRC["model.int8.onnx"][:2048]
        (target / "model.int8.onnx.part").write_bytes(half)
        r2 = L.ensure_model(d2, repo=repo, on_line=lambda s: None)
        p = Path(r2.get("dir") or d2) / "model.int8.onnx"
        if not p.exists() or p.read_bytes() != SRC["model.int8.onnx"]:
            probs.append(f"续传结果不对（{p.stat().st_size if p.exists() else '不存在'} bytes）")

        # ③ sha 正确
        d3 = tmp / "sha_ok"
        r3 = L.ensure_model(d3, repo=repo, sha256=sha, on_line=lambda s: None)
        if not r3.get("ok"):
            probs.append(f"正确 sha 却失败：{r3}")

        # ④ sha 错误：必须失败 + 坏文件改名
        d4 = tmp / "sha_bad"
        r4 = L.ensure_model(d4, repo=repo, sha256="de" * 32, on_line=lambda s: None)
        if r4.get("ok"):
            probs.append("错误 sha 竟然通过了")
        if "sha256" not in str(r4.get("why") or ""):
            probs.append(f"失败理由没说清是 sha：{r4.get('why')}")
        d4dir = Path(r4.get("dir") or d4)
        if (d4dir / "model.int8.onnx").exists():
            probs.append("坏文件还留在原地（下一轮会被当已装好）")
        if not (d4dir / "model.int8.onnx.bad").exists():
            probs.append("坏文件没改名成 .bad（丢了证据）")
    finally:
        srv.shutdown()
        srv.server_close()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    rep = {"ok": not probs, "problems": probs, "port": port,
           "tmp": str(tmp) if a.keep else "（已清理）"}
    if a.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        for p in probs:
            print(f"  [BAD] {p}")
        print(f"[model-fetch] {'PASS' if rep['ok'] else 'FAIL'}"
              f"（下载/续传/sha 正例/sha 反例 四项）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
