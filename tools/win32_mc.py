#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/win32_mc.py —— 给"真客户端"用的最小 Win32 注入垫片

为什么需要它（2026-10-26）：
  AltoClef 的 MCP 只有**移动/挖掘/放置/菜单**这些杠杆，缺三件陪玩必需的事：
  `递给玩家东西` / `手势（点头摇头跳）` / `复活按钮`。给 mod 加工具要能构建 jar，
  而本机 `maven.fabricmc.net` 超时（fabric-loom 拉不下来）⇒ 先用**等价的外部注入**顶上：
    · 聚焦窗口（SetForegroundWindow）——Minecraft 失焦会释放鼠标 capture，一切点击/按键失效
    · 按键（SendInput 扫键码）—— Q 丢弃 / 空格 跳
    · 鼠标相对位移 —— MC 用相对位移转视角 ⇒ 点头 / 摇头
    · 绝对点击 —— 死亡界面的"重生"按钮

两个客户端窗口标题一样（`Minecraft* 1.21.11 - 多人游戏`），所以**按窗口尺寸区分**：
人类实例 1600x900，陪玩实例 1280x720（见 tools/_mc121_launch.py 的 --width/--height）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

u32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _send(inp: INPUT) -> None:
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def move_rel(dx: int, dy: int) -> None:
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, None)
    _send(i)


def key(vk: int, hold_ms: int = 70) -> None:
    d = INPUT(type=INPUT_KEYBOARD)
    d.ki = KEYBDINPUT(vk, 0, 0, 0, None)
    _send(d)
    time.sleep(hold_ms / 1000.0)
    u = INPUT(type=INPUT_KEYBOARD)
    u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None)
    _send(u)


VK = {"space": 0x20, "q": 0x51, "e": 0x45, "esc": 0x1B, "enter": 0x0D,
      "shift": 0x10, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
      "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39}


def key_name(name: str, hold_ms: int = 70) -> bool:
    vk = VK.get(str(name).lower())
    if not vk:
        return False
    key(vk, hold_ms)
    return True


def list_windows() -> list[dict]:
    out: list[dict] = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(h, _l):
        if not u32.IsWindowVisible(h):
            return True
        n = u32.GetWindowTextLengthW(h)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, buf, n + 1)
        title = buf.value
        if "Minecraft" not in title:
            return True
        r = wt.RECT()
        u32.GetClientRect(h, ctypes.byref(r))
        out.append({"hwnd": h, "title": title, "w": r.right - r.left, "h": r.bottom - r.top})
        return True

    u32.EnumWindows(CB(cb), 0)
    return out


def find_by_size(w: int, h: int, tol: int = 40) -> int | None:
    for win in list_windows():
        if abs(win["w"] - w) <= tol and abs(win["h"] - h) <= tol:
            return win["hwnd"]
    return None


def focus(hwnd: int) -> bool:
    """把窗口弄到前台。Windows 默认禁止"背景进程抢前台"，所以先敲一下 ALT 解锁，
    再 SetForegroundWindow；仍失败就用 SwitchToThisWindow 兜底。"""
    try:
        u32.ShowWindow(hwnd, 9)          # SW_RESTORE
        key(0x12, 30)                    # ALT 点一下（允许前台切换的经典技巧）
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        if u32.GetForegroundWindow() != hwnd:
            u32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.2)
        ok = u32.GetForegroundWindow() == hwnd
        # 失焦的 MC 会释放鼠标 capture：点一下客户区中心让它重新抓
        if ok:
            r = wt.RECT()
            u32.GetClientRect(hwnd, ctypes.byref(r))
            move_rel(int((r.right - r.left) / 2), int((r.bottom - r.top) / 2))
        return ok
    except Exception:
        return False


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def key_bg(hwnd: int, vk: int, hold_ms: int = 60) -> bool:
    """**后台**发一次按键：不激活窗口、不挪鼠标、不抢焦点。

    为什么必须有它（operator 2026-10-27：「不要让我的鼠标焦点一直在 mc 上，这样我干不了别的」）：
    以前关 GUI 走的是 `focus(hwnd)` + 全局 SendInput —— 每关一次界面就把玩家的焦点抢走一次，
    人就没法干别的了。GLFW 的按键是走窗口消息的，所以直接 PostMessage 到那个窗口就行，
    窗口在后台（甚至被别的窗口盖住）也能收到，**前台窗口完全不动**。
    """
    try:
        u32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0x00000001)
        time.sleep(max(0, hold_ms) / 1000.0)
        u32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001)
        return True
    except Exception:
        return False


def key_bg_name(hwnd: int, name: str, hold_ms: int = 60) -> bool:
    vk = VK.get(str(name).lower())
    return key_bg(hwnd, vk, hold_ms) if vk else False


def no_activate(hwnd: int) -> bool:
    """把窗口显示出来但**不激活**（SW_SHOWNOACTIVATE + SWP_NOACTIVATE）。"""
    try:
        u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        return True
    except Exception:
        return False


def foreground_hwnd() -> int:
    return int(u32.GetForegroundWindow() or 0)


def is_foreground(hwnd: int) -> bool:
    return foreground_hwnd() == int(hwnd)


def click_client_center(hwnd: int) -> bool:
    """在窗口客户区中心点一下（死亡界面的"重生"按钮就在那儿）。

    ⚠️ 这一条**必须抢焦点**（真点击要落在激活窗口上），只在"重生"这种一次性场景用，
    日常操作一律走 `key_bg`。
    """
    if not focus(hwnd):
        return False
    r = wt.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    # 相对位移把光标推到中心，再点左键（MC 吃相对位移）
    move_rel(int(w / 2), int(h / 2))
    time.sleep(0.15)
    d = INPUT(type=INPUT_MOUSE)
    d.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, None)
    _send(d)
    time.sleep(0.08)
    u = INPUT(type=INPUT_MOUSE)
    u.mi = MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, None)
    _send(u)
    return True


if __name__ == "__main__":
    for win in list_windows():
        print(win)
