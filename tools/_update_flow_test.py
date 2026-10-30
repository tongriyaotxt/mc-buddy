#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_update_flow_test.py —— jar 发布/更新/回滚的闭环回归（W2-3）

用真的发布脚本 + 真的 `install_mod`/`check_update`/`install_update`/`rollback_jar`，
在一个**临时游戏目录**里跑一遍（不碰真的 .minecraft）：

  ① 发布 v1（假 jar A）      ⇒ latest.json 成型 + sha/size 校验通过
  ② 检查更新（还没装）        ⇒ update=True
  ③ 更新到最新               ⇒ 两个 mods 目录都有；再检查 ⇒ "已是最新"（按 sha 判，不只看名字/大小）
  ④ 发布 v2（假 jar B，**同名不同内容**）⇒ 再检查必须说"有更新"
       （这条专治老毛病：同名同大小被判成已是最新 → 用户永远收不到修复）
  ⑤ 回滚到 previous(v1)      ⇒ 装回去的是 v1 的 sha
  ⑥ 再点「回滚陪玩 jar」(.bak) ⇒ 也能退（双保险路径）

用法：
    python -X utf8 tools/_update_flow_test.py             # 0=PASS
    python -X utf8 tools/_update_flow_test.py --json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "launcher") not in sys.path:
    sys.path.insert(0, str(ROOT / "launcher"))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import core as L                                        # noqa: E402
import publish_jar as P                                 # noqa: E402

VER = "1.21.11-Fabric 0.19.3"


def make_jar(p: Path, payload: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"PK\x03\x04" + payload)
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    probs: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="updflow_"))
    gd = tmp / "mc"
    rel = tmp / "releases"
    (gd / "versions" / VER).mkdir(parents=True, exist_ok=True)
    try:
        # ① 发布 v1
        j1 = make_jar(tmp / "src" / "unionclef-1.21.11-0.95.63-buddy9.jar", b"v1-payload")
        r1 = P.publish(j1, rel, version="buddy9", notes="第一版")
        if not r1.get("ok"):
            probs.append(f"发布 v1 失败：{r1}")
        v1 = P.verify(rel / "latest.json")
        if not v1["ok"]:
            probs.append(f"v1 校验失败：{v1['problems']}")
        sha1 = P.sha256_of(rel / j1.name)

        # ② 未装时检查
        c0 = L.check_update(gd, VER, rel_dir=rel)
        if not c0.get("update"):
            probs.append(f"没装却说没更新：{c0}")

        # ③ 更新到最新 v1
        u1 = L.install_update(gd, VER, rel_dir=rel)
        if not u1.get("ok"):
            probs.append(f"更新 v1 失败：{u1}")
        c1 = L.check_update(gd, VER, rel_dir=rel)
        if c1.get("update"):
            probs.append(f"装了 v1 还说有更新：{c1}")
        placed = [Path(x) for x in (u1.get("placed") or [])]
        if len(placed) != 2:
            probs.append(f"应放两个 mods 目录，实际 {len(placed)}")
        for p in placed:
            if not p.exists() or P.sha256_of(p) != sha1:
                probs.append(f"装上去的内容不是 v1：{p}")

        # ④ 发 v2，但**同名同大小**（最阴的一种"新版"）
        j2 = make_jar(tmp / "src2" / j1.name, b"v2-payload")   # 长度与 v1-payload 相同
        if j1.stat().st_size != j2.stat().st_size:
            probs.append("测试构造失误：两份 jar 大小应相同（这条才是要防的）")
        r2 = P.publish(j2, rel, version="buddy10", notes="同名修复")
        if not r2.get("ok"):
            probs.append(f"发布 v2 失败：{r2}")
        c2 = L.check_update(gd, VER, rel_dir=rel)
        if not c2.get("update"):
            probs.append("同名同大小但内容不同 ⇒ 必须报有更新（sha 判据没生效）")

        # ⑤ 回滚到 previous（v1）
        back = L.install_update(gd, VER, rel_dir=rel, to_version="buddy9")
        if not back.get("ok"):
            probs.append(f"回滚到 v1 失败：{back}")
        else:
            for p in [Path(x) for x in (back.get("placed") or [])]:
                if P.sha256_of(p) != sha1:
                    probs.append(f"回滚后内容不是 v1：{p}")

        # ⑥ .bak 双保险：装一个**换了文件名**的新版（install_mod 才会给旧 jar 留 .bak），
        #    然后点「回滚陪玩 jar」必须退回上一版
        j3 = make_jar(tmp / "src3" / "unionclef-1.21.11-0.95.63-buddy11.jar", b"v3-payload")
        r3 = P.publish(j3, rel, version="buddy11", notes="换名新版")
        if not r3.get("ok"):
            probs.append(f"发布 v3 失败：{r3}")
        u3 = L.install_update(gd, VER, rel_dir=rel)
        if not u3.get("ok"):
            probs.append(f"更新 v3 失败：{u3}")
        if not (u3.get("backed_up") or []):
            probs.append("换名更新没留 .bak（回滚就无从下手）")
        rb = L.rollback_jar(gd, VER)
        if not rb.get("ok"):
            probs.append(f".bak 回滚失败：{rb}")
        else:
            names = [Path(x).name for x in rb.get("restored") or []]
            if any("buddy11" in n for n in names):
                probs.append(f".bak 回滚回错版本：{names}")
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    rep = {"ok": not probs, "problems": probs,
           "tmp": str(tmp) if a.keep else "（已清理）"}
    if a.json:
        print(json.dumps(rep, ensure_ascii=False))
    else:
        for p in probs:
            print(f"  [BAD] {p}")
        print(f"[update-flow] {'PASS' if rep['ok'] else 'FAIL'}"
              "（发布/检查/更新/sha 判据/回滚 五项）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
