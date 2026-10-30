#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""launcher/win32_desktop.py —— 让陪玩的 MC 窗口"默认看不见，但要能随时调出来看"

operator 2026-10-29：
  「到时候在加 buddy 时 buddy 的界面是默认不可见的，但是有 debug 可以看」

为什么不能简单地 `ShowWindow(SW_HIDE)` 或最小化（两条都实测过）：
  · 最小化 ⇒ 渲染帧停 ⇒ 挖矿进度永远 -0.10（tungsten 的瞄准按帧推进）；
  · 被别的窗口完全盖住 ⇒ 同一次实测里移开窗口后仍冻 ⇒ 不能指望"挡住"。

所以这里给两条路，按可用性排序：

  A. ~~**独立桌面**（首选）~~ ⇒ **2026-10-30 实测被推翻，不要用它跑陪玩**：
     客户端确实"看不见也点不到"，但**渲染主循环会停** —— 准星 `crosshairTarget` 冻住
     （探针实测：Buddy 桌面上的客户端无论怎么瞄准都读到同一个 MISS，而同样的探针在
     可见窗口上能读到 `short_grass@…`）。tungsten 的瞄准/挖掘是按**帧**推进的 ⇒ 那台客户端干不了活。
     仍保留 `spawn_on_desktop()`：它是唯一能让 `lpDesktop` 真正生效的写法（见下），
     但产品默认**不再走它**。

  B. **留 1 像素在屏内**（现在的默认，`--offscreen`）：把窗口挪到屏幕外，但保留 1px 可见。
     这样 DWM 不会判定为"完全遮挡"而节流；用户几乎看不见（屏幕角落的一条线），**渲染照常**。

  ⚠️ 还有一条比"选哪条路"更容易咬人的：`subprocess.STARTUPINFO.lpDesktop` **会被静默忽略**
  （子进程自报 `GetThreadDesktop = Default`）。要让 lpDesktop 真生效必须 `CreateProcessW` 直调，
  见 `spawn_on_desktop()`。

Debug 查看：不做"运行时切换桌面"（SetThreadDesktop 在建窗后不可换）——
而是 `relaunch_visible()`：杀掉 → 用默认桌面重拉。简单、可预期、不会半残。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

DESKTOP_NAME = os.environ.get("BUDDY_DESKTOP", "Buddy")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

DESKTOP_ALL_ACCESS = 0x01FF
DF_ALLOWOTHERACCOUNTHOOK = 0x0001

# ⚠️ 2026-10-29 实测踩到的坑：这些 API 不设 argtypes 时，ctypes 会把 64 位句柄当 32 位 C int
# 传进去 ⇒ `EnumDesktopWindows(480, …)` 拿到一个**被截断的**桌面句柄 ⇒ 永远枚举到 0 个窗口，
# 而 `EnumWindows` 也一样静默返回 0。现象是"窗口明明在，探针说没有"。
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.EnumDesktopWindows.argtypes = [ctypes.c_void_p, WNDENUMPROC, wt.LPARAM]
user32.EnumDesktopWindows.restype = wt.BOOL
user32.OpenDesktopW.argtypes = [ctypes.c_wchar_p, wt.DWORD, wt.BOOL, wt.DWORD]
user32.OpenDesktopW.restype = ctypes.c_void_p
user32.CreateDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]
user32.CreateDesktopW.restype = ctypes.c_void_p
user32.CloseDesktop.argtypes = [ctypes.c_void_p]
user32.CloseDesktop.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int


def ensure_desktop(name: str = DESKTOP_NAME) -> int | None:
    """建/打开一个桌面，返回句柄（失败返回 None）。"""
    try:
        h = user32.OpenDesktopW(name, 0, False, DESKTOP_ALL_ACCESS)
        if h:
            return h
        h = user32.CreateDesktopW(name, None, None, 0,
                                  DESKTOP_ALL_ACCESS, None)
        return h or None
    except Exception:
        return None


def startupinfo(desktop: str | None = None) -> subprocess.STARTUPINFO:
    """构造 STARTUPINFO：桌面名 + 不抢前台。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4                      # SW_SHOWNOACTIVATE
    if desktop:
        ensure_desktop(desktop)
        # ⚠️ 必须是 "桌面名" 或 "窗口站\\桌面名"；写错会静默落到默认桌面
        si.lpDesktop = desktop
    return si


def move_offscreen(hwnd: int, keep_px: int = 1) -> bool:
    """兜底方案：把窗口挪到屏幕外，只留 keep_px 在屏内（避免 DWM 节流）。"""
    try:
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        x = screen_w - keep_px
        y = screen_h - keep_px
        SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
        return bool(user32.SetWindowPos(hwnd, 0, x, y, w, h,
                                        SWP_NOACTIVATE | SWP_SHOWWINDOW))
    except Exception:
        return False


def _titles_on(desktop_handle: int | None = None) -> list[str]:
    """枚举某个桌面（None = 当前桌面）上所有有标题的窗口标题。"""
    found: list[str] = []

    def _cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append(buf.value)
        return True

    proc = WNDENUMPROC(_cb)
    if desktop_handle:
        user32.EnumDesktopWindows(ctypes.c_void_p(int(desktop_handle)), proc, 0)
    else:
        user32.EnumWindows(proc, 0)
    return found


def find_windows(title_part: str = "Minecraft") -> list[int]:
    """按标题找窗口句柄（EnumWindows + GetWindowText）。

    ⚠️ `EnumWindows` **只枚举调用线程所在桌面**的窗口 —— 客户端在独立桌面时，
    这里永远是 0。要查独立桌面用下面的 `find_windows_on_desktop()`。
    """
    found: list[int] = []

    def _cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part.lower() in buf.value.lower():
                found.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def find_windows_on_desktop(title_part: str = "Minecraft",
                            desktop: str = DESKTOP_NAME) -> list[int]:
    """枚举**指定桌面**上的窗口（W6-3 的实证：客户端真的在 Buddy 桌面上）。

    为什么必须这么查：窗口在别的桌面时 `EnumWindows` 看不见（它只看当前线程的桌面），
    于是"看不见窗口"和"客户端根本没起来"在旧探针里长得一模一样。
    """
    found: list[int] = []
    h = ensure_desktop(desktop)
    if not h:
        return found

    def _cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title_part.lower() in buf.value.lower():
                found.append(int(hwnd))
        return True

    try:
        user32.EnumDesktopWindows(ctypes.c_void_p(int(h)), WNDENUMPROC(_cb), 0)
    except Exception:
        pass
    try:
        user32.CloseDesktop(ctypes.c_void_p(int(h)))
    except Exception:
        pass
    return found


def probe_rendering() -> dict:
    """可选的健康探针：窗口存在 + 是否在活动桌面上。给闸/面板用。"""
    hs = find_windows("Minecraft")
    active = user32.OpenInputDesktop(0, False, DESKTOP_ALL_ACCESS)
    return {"windows": len(hs), "hwnd": hs[:3], "input_desktop": bool(active)}


def window_rect(hwnd: int) -> dict:
    """窗口矩形 + 屏幕尺寸（判"是不是在屏外"用）。"""
    try:
        r = wt.RECT()
        if not user32.GetWindowRect(wt.HWND(int(hwnd)), ctypes.byref(r)):
            return {}
        return {"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom,
                "screen_w": user32.GetSystemMetrics(0),
                "screen_h": user32.GetSystemMetrics(1)}
    except Exception:
        return {}


def is_offscreen(hwnd: int, keep_slack: int = 16) -> bool:
    """窗口是不是"基本在屏幕外"（只要留了一点点在屏内，DWM 就不会判它完全遮挡）。"""
    r = window_rect(hwnd)
    if not r:
        return False
    sw, sh = r["screen_w"], r["screen_h"]
    off_x = r["left"] >= sw - keep_slack or r["right"] <= keep_slack
    off_y = r["top"] >= sh - keep_slack or r["bottom"] <= keep_slack
    return bool(off_x or off_y)


def find_window_for_pid(pid: int, title_part: str = "") -> int:
    """找某个进程（及其子进程）的顶层窗口句柄。"""
    k32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    found: list[int] = []

    def _cb(hwnd, _):
        d = wt.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(d))
        if int(d.value) == int(pid) and user32.GetWindowTextLengthW(hwnd) > 0:
            if title_part:
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title_part.lower() not in buf.value.lower():
                    return True
            found.append(int(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else 0


def wait_and_move_offscreen(pid: int, *, timeout: float = 180.0,
                           keep_px: int = 1) -> dict:
    """等窗口出现，然后把它挪到屏幕外（只留 keep_px 像素在屏内）。

    为什么这是**首选**而不是"独立桌面"（2026-10-29 实测）：
    独立桌面上的客户端 **渲染主循环会停** —— 准星 `crosshairTarget` 不再更新
    （探针实测：可见窗口上准星会跟着瞄准变，Buddy 桌面上永远冻在同一个方块），
    而 tungsten 的挖掘/瞄准是按**帧**推进的 ⇒ 那个客户端干不了活。
    屏内留 1px 则不同：窗口仍在活动桌面上，DWM 不判它"完全遮挡"，渲染照常。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        hwnd = find_window_for_pid(pid)
        if hwnd and move_offscreen(hwnd, keep_px=keep_px):
            return {"ok": True, "hwnd": hwnd, "waited_s": round(time.time() - t0, 1),
                    "keep_px": keep_px}
        time.sleep(2.0)
    return {"ok": False, "hwnd": find_window_for_pid(pid),
            "waited_s": round(time.time() - t0, 1)}


def relaunch_visible(argv: list[str]) -> dict:
    """Debug：把陪玩客户端在**默认桌面**上重拉（换桌面只能重启进程）。"""
    try:
        p = subprocess.Popen(argv, startupinfo=startupinfo(None))
        return {"ok": True, "pid": p.pid, "desktop": "default"}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"[:160]}


# ══════════════════════════════════════════════════════════════════════════════
#  2026-10-29 血证：`subprocess.STARTUPINFO.lpDesktop` **不生效**
#  ---------------------------------------------------------------
#  实测（子进程自报 `GetThreadDesktop` 名字）：父进程把 lpDesktop 设成 "Buddy"，
#  子进程起来仍在 `Default` 桌面 —— 不报错、不失败，静默落回默认桌面。
#  于是"陪玩界面默认不可见"这条产品承诺其实是假的：窗口一直开在用户桌面上。
#  结论：**必须自己调 CreateProcessW**，把 STARTUPINFOW.lpDesktop 交给系统。
# ══════════════════════════════════════════════════════════════════════════════
class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def spawn_on_desktop(cmd_line: str, *, cwd: str, desktop: str = DESKTOP_NAME,
                     log_path: str = "", show_window: int = 4) -> dict:
    """在指定 Windows 桌面上起一个进程（stdout/stderr 写进 log_path）。

    用 CreateProcessW 而不是 subprocess：只有它能真正把 `lpDesktop` 交给系统
    （见上面那段血证）。`show_window=4` = SW_SHOWNOACTIVATE（显示但不抢前台）。
    """
    k32 = ctypes.windll.kernel32
    k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                   wt.BOOL, wt.DWORD, ctypes.c_void_p, wt.LPCWSTR,
                                   ctypes.POINTER(STARTUPINFOW),
                                   ctypes.POINTER(PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = wt.BOOL
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
    k32.CreateFileW.restype = wt.HANDLE

    if desktop:
        ensure_desktop(desktop)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = 0x00000001                       # STARTF_USESHOWWINDOW
    si.wShowWindow = show_window
    si.lpDesktop = desktop or None
    # 桌面名可以是 "名字"，但系统更认 "窗口站\\桌面"；两种都试过，前者可用。
    si.lpDesktop = ctypes.cast(ctypes.c_wchar_p(desktop or ""), wt.LPWSTR)

    hlog = None
    GENERIC_WRITE, CREATE_ALWAYS = 0x40000000, 2
    FILE_ATTRIBUTE_NORMAL = 0x80
    if log_path:
        hlog = k32.CreateFileW(log_path, GENERIC_WRITE, 0x00000003, None,
                               CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
        if hlog and hlog != -1:
            si.dwFlags |= 0x00000100              # STARTF_USESTDHANDLES
            si.hStdOutput = hlog
            si.hStdError = hlog
            si.hStdInput = None
        else:
            hlog = None

    pi = PROCESS_INFORMATION()
    buf = ctypes.create_unicode_buffer(cmd_line)
    CREATE_NEW_PROCESS_GROUP, CREATE_UNICODE_ENVIRONMENT = 0x00000200, 0x00000400
    ok = k32.CreateProcessW(None, buf, None, None, bool(hlog),
                            CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT,
                            None, cwd or None, ctypes.byref(si), ctypes.byref(pi))
    err = ctypes.get_last_error() if not ok else 0
    pid = int(pi.dwProcessId) if ok else 0
    for h in (pi.hProcess, pi.hThread, hlog):
        try:
            if h:
                k32.CloseHandle(h)
        except Exception:
            pass
    return {"ok": bool(ok), "pid": pid, "desktop": desktop, "err": err}


if __name__ == "__main__":
    print("desktop handle:", bool(ensure_desktop()))
    print("probe:", probe_rendering())
