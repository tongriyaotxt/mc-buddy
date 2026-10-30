#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launcher/ui/main.py —— 陪玩启动器（Qt 版，类 PCL 的壳）

operator 2026-10-29：
  「UI 用 qt」＋「加 buddy 时 buddy 的界面默认不可见，但是有 debug 可以看」＋「最后做成类似 PCL 的软件」

设计：
  · **业务全在 `launcher/core.py`**，这里只摆控件、调函数（换皮不换芯）；
  · 默认启动 = **屏外留 1px**（`--offscreen`：看不见但渲染照常），"调试可见"按钮起一份看得见的；
  · 五个页签：实例 / 陪玩 / AI / 日志 / 诊断。

跑起来：
    python -X utf8 launcher/ui/main.py
    python -X utf8 launcher/ui/main.py --selftest     # 只建窗口不显示（CI/验收用）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

def _root() -> Path:
    """项目根：源码 = 上两级；**打包 = exe 所在目录**（tools/ config/ companion/ 由
    `core.unpack_assets()` 首次运行时解到那里，`--run-py` 子进程才读得到）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


ROOT = _root()
# ⚠️ 2026-10-30 血证：打包后如果把 exe 旁边的目录**插到 sys.path 最前**，磁盘上的**旧副本**
# 会盖住 exe 里那份新模块 ⇒ `import core` 拿到的是上一版的 core.py ⇒ 它自己的
# "换版本刷新解包"逻辑永远跑不起来（鸡生蛋）。所以打包态一律**追加到最后**：
# bundle 里的模块优先，磁盘副本只用来找 `companion/` `tools/` 这类非模块代码。
_FROZEN = bool(getattr(sys, "frozen", False))
for _p in (str(ROOT), str(ROOT / "launcher")):
    while _p in sys.path:
        sys.path.remove(_p)
    if _FROZEN:
        sys.path.append(_p)
    else:
        sys.path.insert(0, _p)


# ── 打包态唯一的"跑脚本"通道 ────────────────────────────────────────────────
# 为什么需要：打包后 `sys.executable` 是 Buddy.exe 自己，`[sys.executable, "tools/x.py"]`
# 会把 GUI 再开一个（而不是跑工具）。所以统一走 `Buddy.exe --run-py <脚本> [参数…]`，
# 用打包内的解释器执行那个脚本 —— **机器上不需要装 Python**。
def _restore_std() -> None:
    """PyInstaller `--windowed` 会把 sys.stdout/stderr 置 None。

    但被父进程用管道拉起时，OS 句柄还在（父进程要读工具的输出）。这里把它接回来，
    否则子工具一 print 就 AttributeError。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import ctypes
        import msvcrt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for attr, sig in (("stdout", -11), ("stderr", -12)):
            h = k32.GetStdHandle(sig)
            if not h or h == 0xFFFFFFFFFFFFFFFF or h == 0xFFFFFFFF:
                continue
            fd = msvcrt.open_osfhandle(h, 0o0002)          # O_WRONLY
            setattr(sys, attr, os.fdopen(fd, "w", buffering=1,
                                         encoding="utf-8", errors="replace", closefd=False))
    except Exception:
        pass


def _dispatch_run_py() -> int | None:
    """`--run-py <脚本> [参数…]`：在打包内的解释器里跑那个脚本，返回它的退出码。"""
    if "--run-py" not in sys.argv:
        return None
    i = sys.argv.index("--run-py")
    rest = sys.argv[i + 1:]
    if not rest:
        print("--run-py 需要一个脚本路径", file=sys.stderr)
        return 2
    _restore_std()
    script = rest[0]
    # 相对路径先按 CWD 找，找不到再按 exe 旁边找（双击启动时 CWD 不是 exe 目录）
    if not Path(script).exists():
        cand = ROOT / script
        if cand.exists():
            script = str(cand)
    # 打包态首启：工具脚本在 exe 内，必须先解到 exe 旁边才读得到
    # （`main()` 里的那次解包发生在 --run-py 之后，所以这里要自己来一遍，幂等）
    try:
        import core as _L
        _L.unpack_assets()
    except Exception:
        pass
    sys.argv = [script] + rest[1:]
    import runpy
    try:
        runpy.run_path(script, run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    except Exception:
        import traceback
        traceback.print_exc()
        return 1


_RUNPY_RC = _dispatch_run_py()
if _RUNPY_RC is not None:
    raise SystemExit(_RUNPY_RC)

from PySide6 import QtCore, QtGui, QtWidgets          # noqa: E402

import core as L                                     # noqa: E402


class Main(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Buddy 陪玩启动器")
        self.resize(1080, 680)
        self.game_dirs: list[dict] = []
        self.cur_game = ""
        self.cur_version = ""
        self._build()
        self.reload_dirs()
        self._maybe_first_run()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh_live)
        self.timer.start(2000)

    # ── 布局 ────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # 左：游戏目录 + 版本
        left = QtWidgets.QVBoxLayout()
        left.addWidget(QtWidgets.QLabel("游戏目录（自动发现）"))
        self.dir_list = QtWidgets.QListWidget()
        self.dir_list.currentRowChanged.connect(self.on_dir_changed)
        left.addWidget(self.dir_list, 2)
        left.addWidget(QtWidgets.QLabel("实例 / 版本"))
        self.ver_list = QtWidgets.QListWidget()
        self.ver_list.currentRowChanged.connect(self.on_ver_changed)
        left.addWidget(self.ver_list, 3)
        btn = QtWidgets.QPushButton("重新扫描")
        btn.clicked.connect(self.reload_dirs)
        left.addWidget(btn)
        root.addLayout(left, 1)

        # 右：页签
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 3)
        self.tabs.addTab(self._tab_instance(), "实例")
        self.tabs.addTab(self._tab_buddy(), "陪玩")
        self.tabs.addTab(self._tab_ai(), "AI")
        self.tabs.addTab(self._tab_voice(), "语音")
        self.tabs.addTab(self._tab_logs(), "日志")
        self.tabs.addTab(self._tab_diag(), "诊断")
        self.tabs.addTab(self._tab_about(), "关于")

        self.status = self.statusBar()
        self.status.showMessage("就绪")

    def _tab_instance(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.inst_table = QtWidgets.QTableWidget(0, 4)
        self.inst_table.setHorizontalHeaderLabels(["版本", "mods 数", "陪玩 jar", "Fabric"])
        self.inst_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.inst_table)
        row = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("安装 / 更新陪玩 jar")
        b1.clicked.connect(self.install_mod)
        b2 = QtWidgets.QPushButton("打开该版本目录")
        b2.clicked.connect(self.open_dir)
        b3 = QtWidgets.QPushButton("检查更新")
        b3.clicked.connect(self.check_update)
        b4 = QtWidgets.QPushButton("更新到最新")
        b4.clicked.connect(self.do_update)
        b5 = QtWidgets.QPushButton("回滚到上一版")
        b5.clicked.connect(self.do_rollback_version)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addWidget(b3)
        row.addWidget(b4)
        row.addWidget(b5)
        row.addStretch(1)
        v.addLayout(row)
        self.upd_label = QtWidgets.QLabel("版本：—")
        v.addWidget(self.upd_label)
        return w

    def _tab_buddy(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QFormLayout(w)
        self.name_edit = QtWidgets.QLineEdit("Buddy")
        self.port_edit = QtWidgets.QLineEdit("25351")
        self.server_edit = QtWidgets.QLineEdit("127.0.0.1:25566")
        self.hidden_box = QtWidgets.QCheckBox("默认不可见（挪到屏外，渲染照常）")
        self.hidden_box.setChecked(True)
        v.addRow("用户名", self.name_edit)
        v.addRow("MCP 端口", self.port_edit)
        v.addRow("服务器", self.server_edit)
        v.addRow("", self.hidden_box)
        row = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("启动陪玩（按上面的设置）")
        b1.clicked.connect(lambda: self.launch_buddy(visible=False))
        b2 = QtWidgets.QPushButton("调试：可见启动")
        b2.clicked.connect(lambda: self.launch_buddy(visible=True))
        b3 = QtWidgets.QPushButton("停止")
        b3.clicked.connect(self.stop_buddy)
        b4 = QtWidgets.QPushButton("一键准备（装好能用）")
        b4.clicked.connect(self.first_run)
        for b in (b1, b2, b3, b4):
            row.addWidget(b)
        holder = QtWidgets.QWidget()
        holder.setLayout(row)
        v.addRow(holder)
        self.buddy_log = QtWidgets.QPlainTextEdit()
        self.buddy_log.setReadOnly(True)
        v.addRow(self.buddy_log)
        return w

    def _tab_voice(self) -> QtWidgets.QWidget:
        """语音页（2026-10-29 定稿：SenseVoice 识别 + SAPI 常驻 TTS）。"""
        w = QtWidgets.QWidget()
        v = QtWidgets.QFormLayout(w)
        cfg = self._voice_cfg()
        self.v_stt = QtWidgets.QComboBox()
        self.v_stt.addItems(["auto", "sherpa", "sapi"])
        self.v_stt.setCurrentText(str(cfg.get("stt") or "auto"))
        self.v_model = QtWidgets.QLineEdit(str(cfg.get("sherpa_model_dir") or ""))
        self.v_device = QtWidgets.QComboBox()
        self.v_device.setEditable(True)
        self.v_device.addItem("")
        for name in self._input_devices():
            self.v_device.addItem(name)
        self.v_device.setCurrentText(str(cfg.get("input_device") or ""))
        self.v_rate = QtWidgets.QSpinBox()
        self.v_rate.setRange(-10, 10)
        self.v_rate.setValue(int(cfg.get("tts_rate") or 5))
        self.v_sil = QtWidgets.QSpinBox()
        self.v_sil.setRange(2, 12)
        self.v_sil.setValue(int(cfg.get("silence_chunks") or 6))
        raw_model = QtWidgets.QLineEdit(str(cfg.get("model_url") or cfg.get("model_repo") or ""))
        raw_model.setPlaceholderText("下载源：仓库前缀（…/resolve/main）或压缩包直链；留空=给手动指南")
        self.v_dest = QtWidgets.QLineEdit(str(cfg.get("model_dest") or r"D:\models"))
        v.addRow("识别引擎", self.v_stt)
        v.addRow("模型目录", self.v_model)
        v.addRow("麦克风", self.v_device)
        v.addRow("播报语速", self.v_rate)
        v.addRow("断句静音块(×0.1s)", self.v_sil)
        v.addRow("下载源", raw_model)
        v.addRow("下载到", self.v_dest)
        row = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("保存")
        b1.clicked.connect(lambda: self.save_voice(raw_model.text().strip()))
        b2 = QtWidgets.QPushButton("列出设备/自检")
        b2.clicked.connect(self.voice_selftest)
        b3 = QtWidgets.QPushButton("取模型")
        b3.clicked.connect(lambda: self.fetch_model(raw_model.text().strip()))
        for b in (b1, b2, b3):
            row.addWidget(b)
        holder = QtWidgets.QWidget()
        holder.setLayout(row)
        v.addRow(holder)
        # 进度条（W2-2）：下载是"每秒一行"的回调，别让界面看着像卡死
        self.v_prog = QtWidgets.QProgressBar()
        self.v_prog.setRange(0, 100)
        self.v_prog.setValue(0)
        self.v_prog.setFormat("%p%  %v")
        self.v_status = QtWidgets.QLabel("模型状态：—")
        v.addRow(self.v_prog)
        v.addRow(self.v_status)
        self.voice_out = QtWidgets.QPlainTextEdit()
        self.voice_out.setReadOnly(True)
        v.addRow(self.voice_out)
        return w

    def _voice_cfg(self) -> dict:
        try:
            import companion.voice_engine as VE
            return VE.load_cfg()
        except Exception:
            return {}

    def _input_devices(self) -> list[str]:
        try:
            import sounddevice as sd
            return [f"{i}: {d['name']}" for i, d in enumerate(sd.query_devices())
                    if d["max_input_channels"] > 0]
        except Exception:
            return []

    def save_voice(self, url: str = "") -> None:
        p = ROOT / "config" / "voice.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            d = {}
        d["stt"] = self.v_stt.currentText()
        d["sherpa_model_dir"] = self.v_model.text().strip()
        d["input_device"] = self.v_device.currentText().strip()
        d["tts_rate"] = int(self.v_rate.value())
        d["silence_chunks"] = int(self.v_sil.value())
        if url:
            # 前缀（…/resolve/main）当 repo 存，其余当压缩包直链存 —— 两种来源别混
            if url.rstrip("/").endswith("resolve/main") or "/resolve/" in url:
                d["model_repo"], d["model_url"] = url.strip(), ""
            else:
                d["model_url"], d["model_repo"] = url.strip(), ""
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        self.voice_out.appendPlainText(f"已保存 {p}")

    def voice_selftest(self) -> None:
        devs = self._input_devices()
        self.voice_out.appendPlainText(f"输入设备 {len(devs)} 个：\n  " + "\n  ".join(devs[:10]))
        try:
            import sounddevice as sd
            out = sd.query_devices(sd.default.device[1])["name"]
            slow = any(k in out for k in ("ToDesk", "主声音", "Virtual"))
            self.voice_out.appendPlainText(
                f"默认输出：{out}" + ("  ⚠️ 虚拟声卡，播报会明显变慢，建议换真扬声器" if slow else ""))
        except Exception:
            pass
        ms = L.model_status(self.v_model.text().strip() or r"D:\models")
        self.voice_out.appendPlainText(f"模型：{ms}")
        self.v_status.setText(f"模型状态：{'就绪 ' + str(ms.get('dir')) if ms.get('ok') else '缺（' + str(ms.get('why'))[:40] + '）'}")

    def fetch_model(self, src: str) -> None:
        """取模型（W2-2）：**后台线程**跑，进度条跟着走；失败要说人话。"""
        dest = Path(self.v_dest.text().strip() or r"D:\models")
        try:
            cfg = self._voice_cfg()
        except Exception:
            cfg = {}
        src = src or str(cfg.get("model_url") or cfg.get("model_repo") or "")
        repo = src if "/resolve/" in src else str(cfg.get("model_repo") or "")
        url = "" if "/resolve/" in src else src
        if not (repo or url):
            self.voice_out.appendPlainText("没有下载源：请填「下载源」，或按指南手动放模型。")
            self.voice_out.appendPlainText(
                "python -X utf8 tools/get_voice_model.py --guide")
            return
        self.v_prog.setValue(0)
        self.v_status.setText("模型状态：下载中…")
        self.voice_out.appendPlainText(f"开始取模型：dest={dest} "
                                       f"{'repo=' + repo if repo else 'url=' + url}")

        def on_line(s: str) -> None:
            self.voice_out.appendPlainText("  " + s)
            m = s.rsplit("%", 1)[0].rsplit(" ", 1)[-1]
            try:
                self.v_prog.setValue(int(float(m)))
            except Exception:
                pass
            QtWidgets.QApplication.processEvents()

        def work() -> None:
            r = L.ensure_model(dest, url=url, repo=repo, on_line=on_line,
                               sha256=str(cfg.get("model_sha256") or ""))
            if r.get("ok"):
                self.v_prog.setValue(100)
                self.v_status.setText(f"模型状态：就绪 {r.get('dir')}")
                self.v_model.setText(str(r.get("dir") or ""))
                self.save_voice("")
                self.voice_out.appendPlainText(f"取模型完成：{r.get('dir')}")
                self.voice_selftest()
            else:
                self.v_status.setText("模型状态：失败（看下面理由）")
                self.voice_out.appendPlainText(
                    f"取模型失败：{r.get('why') or r.get('tail') or r}")

        threading.Thread(target=work, daemon=True, name="fetch-model").start()

    def _tab_ai(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QFormLayout(w)
        self.ai_path = QtWidgets.QLineEdit(str(ROOT / "config" / "ai.json"))
        self.ai_path.setReadOnly(True)
        self.ai_model = QtWidgets.QLineEdit("")
        self.ai_model.setReadOnly(True)
        self.ai_key = QtWidgets.QLineEdit("")
        self.ai_key.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ai_key.setPlaceholderText("粘贴你自己的 DeepSeek key（只存本地，不进日志/诊断包）")
        v.addRow("配置", self.ai_path)
        v.addRow("模型", self.ai_model)
        v.addRow("密钥", self.ai_key)
        save_key = QtWidgets.QPushButton("保存 key（自带 key，我们不代付也不中转）")
        save_key.clicked.connect(self.save_key)
        v.addRow(save_key)
        note = QtWidgets.QLabel("提示：密钥只在本地 config/ai.json；每分钟最多 10 次模型调用，"
                                "没密钥时走确定性执行器（会说做不到，不假装）。")
        note.setWordWrap(True)
        v.addRow(note)
        self.ai_usage = QtWidgets.QLabel("今日用量：—")
        v.addRow(self.ai_usage)
        self.reload_ai()
        return w

    def _maybe_first_run(self) -> None:
        """首次运行须知：边界（公服别用）+ 模型从哪来。只弹一次。"""
        flag = ROOT / "config" / "first_run.json"
        if flag.exists() or "--selftest" in sys.argv:
            return
        try:
            QtWidgets.QMessageBox.information(
                self, "首次使用须知",
                "1) 只支持：单机 / 自己开的服 / 朋友的服。\n"
                "   公服多数把自动操作当宏/脚本，会封号 —— 这条不商量。\n\n"
                "2) 陪玩窗口默认不可见（跑在独立桌面，不抢你焦点）；\n"
                "   想看它在干嘛，用「陪玩 → 调试：可见启动」。\n\n"
                "3) 语音识别要先放模型：「语音」页 → 取模型（或按指南手动放）。\n"
                "   不放也能用，只是走 Windows 自带识别、慢一点。\n\n"
                "4) 每天默认 40 万 token（约 100~150 次对话），用完会降级并告诉你。")
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _tab_logs(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.plan_label = QtWidgets.QLabel("打算：—")
        v.addWidget(self.plan_label)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        v.addWidget(self.log_view)
        return w

    def _tab_diag(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("导出诊断包（zip，可直接发给维护者）")
        b.clicked.connect(self.diagnose)
        b2 = QtWidgets.QPushButton("回滚陪玩 jar")
        b2.clicked.connect(self.rollback)
        b3 = QtWidgets.QPushButton("开通第 3 个陪玩实例")
        b3.clicked.connect(self.provision3)
        for x in (b, b2, b3):
            row.addWidget(x)
        v.addLayout(row)
        # 支持闭环（opt-in）：默认关；开了才会上传诊断包（不含密钥）
        sup = L.support_cfg()
        self.sup_box = QtWidgets.QCheckBox("崩溃后自动上报（opt-in，诊断包不含密钥）")
        self.sup_box.setChecked(bool(sup.get("enabled")))
        self.sup_ep = QtWidgets.QLineEdit(str(sup.get("endpoint") or ""))
        self.sup_ep.setPlaceholderText("上报地址 https://…（留空=只存本地）")
        save_sup = QtWidgets.QPushButton("保存上报设置")
        save_sup.clicked.connect(self.save_support)
        test_up = QtWidgets.QPushButton("测试上报（打包→POST）")
        test_up.clicked.connect(self.test_upload)
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(self.sup_box)
        srow.addWidget(self.sup_ep, 2)
        srow.addWidget(save_sup)
        srow.addWidget(test_up)
        v.addLayout(srow)
        # ⚠️ 勾上不是"立即上传"：先给一张"会发送什么"的清单，用户点确认后才生效（W4-1 ③）
        self.sup_box.stateChanged.connect(self._on_sup_toggle)
        self.sup_last = QtWidgets.QLabel("上报状态：—")
        v.addWidget(self.sup_last)
        self.diag_out = QtWidgets.QPlainTextEdit()
        self.diag_out.setReadOnly(True)
        v.addWidget(self.diag_out)
        return w

    def save_support(self) -> None:
        d = L.save_support(enabled=bool(self.sup_box.isChecked()),
                           endpoint=self.sup_ep.text().strip())
        self.diag_out.appendPlainText(f"上报设置：{d}")
        self.sup_last.setText(f"上报状态：{'已开' if d.get('enabled') else '关'} "
                              f"endpoint={d.get('endpoint') or '（空=只存本地）'}")

    # ── 上报：清单 → 保存 → 测试（W4-1）──────────────────────────────────────
    SUPPORT_FILES = {
        "error.txt": "崩在哪：异常类型 + 调用栈（截断 8KB）",
        "trace_tail.jsonl": "出事前最后 200 条决策/事件（谁让它做的什么）",
        "env.json": "Python 版本 / 进程号 / 内存占用 / 工作目录",
        "last_plan.json": "当时的打算（任务树），便于复现",
        "gate.json": "最近一次自检结论（21 项闸）",
    }

    def _missing_msg(self) -> str:
        return ("会发送：\n" + "\n".join(f"  · {k} —— {v}" for k, v in self.SUPPORT_FILES.items())
                + "\n\n不会发送：\n  · 任何 API key / token（config/ai.json 里的 key 不打包）\n"
                "  · 聊天记录全文、账号密码、屏幕截图\n"
                "  · 服务器地址里的密码段（只留 host:port）\n\n"
                "发送到哪：你自己填的那个地址。留空 = 只存在本机 runs/crash/。")

    def _on_sup_toggle(self) -> None:
        """勾上的瞬间不直接生效：先给清单，用户点「我明白」才写进配置。"""
        if not self.sup_box.isChecked():
            self.save_support()
            return
        if "--selftest" in sys.argv:
            return
        r = QtWidgets.QMessageBox.question(
            self, "开启上报前请确认", self._missing_msg() + "\n\n现在开启？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        if r != QtWidgets.QMessageBox.StandardButton.Yes:
            self.sup_box.blockSignals(True)
            self.sup_box.setChecked(False)
            self.sup_box.blockSignals(False)
            self.diag_out.appendPlainText("上报：用户取消（保持关闭）")
            return
        self.save_support()

    def test_upload(self) -> None:
        """真打一个包再真 POST 一次：成功/失败都写在面板上（不靠猜）。"""
        try:
            b = L.bundle(ROOT / "runs" / "support-test", game_dir=self.cur_game or None)
            if not b.get("ok"):
                self.diag_out.appendPlainText(f"打包失败：{b}")
                self.sup_last.setText("上报状态：打包失败")
                return
            up = L.upload_diag(b["zip"])
            self.diag_out.appendPlainText(f"测试上报：{up}\n  包：{b['zip']}")
            self.sup_last.setText(
                ("上报状态：成功 " + str(up.get("status"))) if up.get("ok")
                else ("上报状态：失败 —— " + str(up.get("why") or up.get("status"))))
        except Exception as e:
            self.diag_out.appendPlainText(f"测试上报异常：{type(e).__name__}: {e}")
            self.sup_last.setText("上报状态：异常")

    # ── 关于/许可（W5-2）：一眼看清"哪些是我的、哪些是别人的、哪些没定"──────
    def _about_text(self) -> str:
        """**从文件里读**，不在这里再抄一份（抄一份就会过期）。"""
        head = ("Buddy 陪玩 —— 一台能陪人玩 Minecraft 的 AI 陪玩\n"
                "本仓库：Apache-2.0（全文见 LICENSE，署名见 NOTICE）\n"
                "第三方与模型清单：THIRD-PARTY.md\n"
                "分发合规三条：docs/COMPLIANCE.md\n"
                "待定案（需人拍板）：docs/LICENSE-QUESTIONS.md\n"
                "源码获取方式：SOURCE-OFFER.txt（随安装包分发）\n"
                + "─" * 74 + "\n")
        bits = [head]
        tp = ROOT / "THIRD-PARTY.md"
        if tp.exists():
            bits.append("【THIRD-PARTY.md 摘要】\n"
                        + "\n".join(tp.read_text(encoding="utf-8", errors="replace")
                                    .splitlines()[:28]) + "\n")
        lq = ROOT / "docs" / "LICENSE-QUESTIONS.md"
        if lq.exists():
            sel = [ln for ln in lq.read_text(encoding="utf-8", errors="replace").splitlines()
                   if ln.startswith("## ") or ln.startswith("### 事实")]
            bits.append("【待定案】\n" + "\n".join(sel[:12]) + "\n")
        return "\n".join(bits)

    def _tab_about(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        lbl = QtWidgets.QLabel(
            "许可与第三方：本程序 Apache-2.0；随包分发的 mod jar 有自己的许可（见下）。"
            "模型**不进安装包**，首次使用由「语音 → 取模型」单独下载。"
            "我们不分发 Minecraft / Paper 本体。")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        self.about_view = QtWidgets.QPlainTextEdit()
        self.about_view.setReadOnly(True)
        self.about_view.setPlainText(self._about_text())
        v.addWidget(self.about_view)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("重新读取许可文件")
        b.clicked.connect(lambda: self.about_view.setPlainText(self._about_text()))
        b2 = QtWidgets.QPushButton("打开 LICENSE-QUESTIONS")
        b2.clicked.connect(lambda: self._open_doc(ROOT / "docs" / "LICENSE-QUESTIONS.md"))
        b3 = QtWidgets.QPushButton("打开 SOURCE-OFFER.txt")
        b3.clicked.connect(lambda: self._open_doc(ROOT / "SOURCE-OFFER.txt"))
        for x in (b, b2, b3):
            row.addWidget(x)
        row.addStretch(1)
        v.addLayout(row)
        return w

    def _open_doc(self, p: Path) -> None:
        try:
            if p.exists():
                os.startfile(str(p))                 # noqa: S606（Windows 打开文档）
            else:
                self.status.showMessage(f"文件不在：{p}")
        except Exception as e:
            self.status.showMessage(f"打不开：{type(e).__name__}: {e}")

    # ── 数据 ────────────────────────────────────────────────────────────────
    def reload_dirs(self) -> None:
        self.game_dirs = L.find_game_dirs()
        self.dir_list.clear()
        for d in self.game_dirs:
            self.dir_list.addItem(f"[{d['kind']}] {d['dir']}")
        if self.game_dirs:
            self.dir_list.setCurrentRow(0)

    def on_dir_changed(self, row: int) -> None:
        if row < 0 or row >= len(self.game_dirs):
            return
        self.cur_game = self.game_dirs[row]["dir"]
        self.ver_list.clear()
        for inst in L.list_instances(Path(self.cur_game)):
            self.ver_list.addItem(inst["version"])
        self.fill_inst_table()
        if self.ver_list.count():
            self.ver_list.setCurrentRow(0)

    def on_ver_changed(self, row: int) -> None:
        it = self.ver_list.item(row)
        self.cur_version = it.text() if it else ""

    def fill_inst_table(self) -> None:
        if not self.cur_game:
            return
        insts = L.list_instances(Path(self.cur_game))
        self.inst_table.setRowCount(len(insts))
        for i, it in enumerate(insts):
            for j, val in enumerate((it["version"], it["mods"], it["buddy_jar"] or "—",
                                     "✓" if it["has_fabric"] else "✗")):
                self.inst_table.setItem(i, j, QtWidgets.QTableWidgetItem(str(val)))

    def save_key(self) -> None:
        """BYO key：用户自带，只写本地 config/ai.json；日志/诊断包不收 key。"""
        k = self.ai_key.text().strip()
        if not k:
            self.status.showMessage("key 是空的")
            return
        try:
            from companion.agent import save_config
            save_config(api_key=k)
            self.reload_ai()
            self.ai_key.clear()
            self.status.showMessage("已保存 key（只写 config/ai.json）")
        except Exception as e:
            self.status.showMessage(f"保存失败：{type(e).__name__}: {e}")

    def reload_ai(self) -> None:
        p = ROOT / "config" / "ai.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            self.ai_model.setText(str(d.get("model") or ""))
            k = str(d.get("api_key") or "")
            self.ai_key.setText(("sk-…" + k[-4:]) if k else "未配置")
        except Exception:
            self.ai_model.setText("未配置")
            self.ai_key.setText("未配置")

    # ── 动作 ────────────────────────────────────────────────────────────────
    def install_mod(self) -> None:
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        r = L.install_mod(Path(self.cur_game), self.cur_version)
        self.status.showMessage(f"安装：{'成功' if r.get('ok') else r.get('why')}"
                                f"（放 {len(r.get('placed') or [])} 处）")
        s = L.ensure_settings(Path(self.cur_game), mcp_port=int(self.port_edit.text() or 25351))
        self.status.showMessage(f"安装完成；设置 {s.get('path')}")
        self.fill_inst_table()

    def check_update(self) -> None:
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        r = L.check_update(Path(self.cur_game), self.cur_version)
        if r.get("update"):
            msg = (f"有更新：{r.get('installed') or '（未装）'} → {r.get('new')}"
                   f"（v{r.get('version') or '?'}；{r.get('why')}）")
            self.status.showMessage(msg)
            self.buddy_log.appendPlainText(msg)
            if r.get("notes"):
                self.buddy_log.appendPlainText(f"  说明：{r['notes']}")
        else:
            self.status.showMessage(f"已是最新（v{r.get('version') or '?'}）")
        self.upd_label.setText(
            (f"版本：v{r.get('version') or '?'}  {r.get('installed') or '未装'}"
             if not r.get("update") else
             f"版本：本地 {r.get('installed') or '未装'} → 可更新 v{r.get('version') or '?'}"))

    def do_update(self) -> None:
        """更新到发布目录的 latest（带备份；失败要说人话）。"""
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        r = L.install_update(Path(self.cur_game), self.cur_version)
        self.buddy_log.appendPlainText(f"更新：{json.dumps(r, ensure_ascii=False)[:300]}")
        self.status.showMessage(f"更新{'成功' if r.get('ok') else '失败：' + str(r.get('why'))}"
                                f"（放 {len(r.get('placed') or [])} 处，备份 "
                                f"{len(r.get('backed_up') or [])} 个）")
        self.fill_inst_table()
        self.check_update()

    def do_rollback_version(self) -> None:
        """回到 latest.json 里记的 previous 版本（不知道就叫人先看 versions.json）。"""
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        meta = L.release_meta()
        prev = str(meta.get("previous") or "")
        if not prev:
            self.status.showMessage("发布目录里没记 previous（用「回滚陪玩 jar」按 .bak 退）")
            return
        r = L.install_update(Path(self.cur_game), self.cur_version, to_version=prev)
        self.buddy_log.appendPlainText(f"回滚到 v{prev}：{json.dumps(r, ensure_ascii=False)[:300]}")
        self.status.showMessage(f"回滚{'成功 v' + prev if r.get('ok') else '失败：' + str(r.get('why'))}")
        self.fill_inst_table()
        self.check_update()

    def open_dir(self) -> None:
        if not (self.cur_game and self.cur_version):
            return
        p = Path(self.cur_game) / "versions" / self.cur_version
        subprocess.Popen(["explorer", str(p)])

    def launch_buddy(self, *, visible: bool) -> None:
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        hidden = (not visible) and self.hidden_box.isChecked()
        cmd = L.launch_cmd(Path(self.cur_game), self.cur_version,
                           self.name_edit.text() or "Buddy",
                           connect=self.server_edit.text().strip(),
                           mcp_port=int(self.port_edit.text() or 25351),
                           hidden=hidden)
        p = subprocess.Popen(cmd, cwd=str(ROOT))
        self.buddy_log.appendPlainText(("启动(隐藏)" if hidden else "启动(可见)")
                                       + f" pid={p.pid}\n  " + " ".join(cmd[-5:]))

    def first_run(self) -> None:
        """一键准备：装 jar → 写设置 → 看模型 → 探活 → 生成启动命令，逐行给结论。"""
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        port = int(self.port_edit.text() or 25351)
        host = self.server_edit.text().strip() or "127.0.0.1:25566"
        srv = int(host.split(":")[-1]) if ":" in host else 25566
        r = L.first_run_steps(Path(self.cur_game), self.cur_version, mcp_port=port,
                              server_port=srv,
                              model_dest=self.v_model.text().strip() or r"D:\models")
        lines = [("✅ " if s["ok"] else "❌ ") + s["step"] + "  " + s["detail"]
                 for s in r["steps"]]
        self.buddy_log.appendPlainText("\n".join(lines) + f"\n→ {r['next']}")

    def stop_buddy(self) -> None:
        """停掉这个陪玩客户端（复用 buddy_body 的 find_pid/kill，不自己发明）。"""
        name = self.name_edit.text() or "Buddy"
        try:
            r = subprocess.run(L.py_prefix() +
                               [str(ROOT / "tools" / "buddy_body.py"), "kill", "--name", name],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
            self.buddy_log.appendPlainText((r.stdout or r.stderr or "").strip()[:200])
        except Exception as e:
            self.buddy_log.appendPlainText(f"停止失败：{type(e).__name__}: {e}")

    def diagnose(self) -> None:
        out = ROOT / "runs" / "diag" / time.strftime("%Y%m%d-%H%M%S")
        r = L.bundle(out, game_dir=Path(self.cur_game) if self.cur_game else None)
        self.diag_out.appendPlainText(
            f"✅ {r.get('zip')}（{r.get('size_mb')} MB）\n{r.get('note','')}")

    def rollback(self) -> None:
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        r = L.rollback_jar(Path(self.cur_game), self.cur_version)
        self.diag_out.appendPlainText(f"回滚：{r}\n")
        self.fill_inst_table()

    def provision3(self) -> None:
        """一键开通第 3 个实例（独立端口/设置，mods 用目录联接，不复制 jar）。"""
        if not (self.cur_game and self.cur_version):
            self.status.showMessage("先选目录和版本")
            return
        real_mods = Path(self.cur_game) / "versions" / self.cur_version / "mods"
        r = L.provision_instance(Path(r"%BUDDY_CLIENT%"), 3, real_mods=real_mods,
                                 mcp_port=int(self.port_edit.text() or 25351) + 2,
                                 gateway_port=25345)
        self.diag_out.appendPlainText(f"开通实例：{r}\n")

    # ── 实时刷新 ────────────────────────────────────────────────────────────
    def refresh_live(self) -> None:
        # AI 今日用量（指挥层每 30 秒写 runs/state/ai.json）
        try:
            a = json.loads((ROOT / "runs" / "state" / "ai.json").read_text(encoding="utf-8"))
            used, bud = int(a.get("tokens_used") or 0), int(a.get("budget") or 0)
            pct = f"（{used*100//bud}%）" if bud else ""
            self.ai_usage.setText(f"今日用量：{used}/{bud or '不限'} tokens {pct}"
                                  + ("  ⚠️ 已用完，降级中" if a.get("exhausted") else ""))
        except Exception:
            self.ai_usage.setText("今日用量：—（指挥层还没起）")
        p = ROOT / "runs" / "state" / "last_plan.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            tree = " → ".join(str(x)[:30] for x in (d.get("tree") or [])[:2])
            self.plan_label.setText(f"打算：{d.get('item') or d.get('asked')} {tree}")
        except Exception:
            self.plan_label.setText("打算：—")
        g = ROOT / "runs" / "gate" / "last.json"
        try:
            gj = json.loads(g.read_text(encoding="utf-8"))
            self.status.showMessage(f"闸：{gj.get('verdict')} "
                                    f"（失败 {gj.get('fails')} / {gj.get('sec')}s）")
        except Exception:
            pass


def main() -> int:
    _restore_std()                    # --windowed 下 sys.stdout 是 None（selftest/日志要能打出来）
    made = L.unpack_assets()          # 打包态首启：把 tools/ config/ companion/ 解到 exe 旁边
    if made:
        print(f"unpacked {len(made)} files next to exe", flush=True)
    app = QtWidgets.QApplication(sys.argv)
    if "--selftest" in sys.argv:
        w = Main()
        print("Qt UI selftest OK:", w.windowTitle(), "| 目录", w.dir_list.count(),
              "| 版本", w.ver_list.count())
        return 0
    w = Main()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
