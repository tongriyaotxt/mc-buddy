#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/start_companion.py —— 从**打包好的 exe** 起指挥层（不需要机器上装 Python）

为什么需要这个文件
------------------
`Buddy.exe` 是 GUI 程序；打包后 `sys.executable` 是 exe 自己，所以"再起一个 python 跑
`-m companion.commander`"这条路在没装 Python 的机器上不存在。
`launcher/ui/main.py` 的 `--run-py <脚本>` 通道就是为这种事准备的：用 exe 内自带的解释器
`runpy` 跑一个磁盘上的脚本。本文件就是那个脚本 —— 把参数原样转给 `companion.commander.main()`。

用法
----
    # 打包态（推荐给用户）
    Buddy.exe --run-py tools\\start_companion.py --port 25351 ^
        --settings "%BUDDY_CLIENT%" ^
        --player 你的ID --seconds 3600 --hz 6 --server 127.0.0.1:25566

    # 源码态等价物
    python -X utf8 -m companion.commander --port 25351 ...

自检（不连身体，只列工具就退出）：
    Buddy.exe --run-py tools\\start_companion.py --catalog
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))          # 打包态安全：追加而不是抢在最前（见 main.py 的注释）


def main() -> int:
    from companion.commander import main as commander_main
    # commander.main() 自己读 sys.argv —— 这里的 argv 已经是 `[start_companion.py, --port, …]`，
    # 它只用 argparse 看选项，所以脚本名不会干扰。
    return int(commander_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
