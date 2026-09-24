import os
import subprocess
import sys
import threading
import time


class MemoryLimitExceeded(Exception):
    pass


def process_memory(pid):
    """Return (current_rss, peak_rss) in bytes where the platform exposes it."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:
                return None
            raise ctypes.WinError(error)
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                error = ctypes.get_last_error()
                if error in (6, 87):
                    return None
                raise ctypes.WinError(error)
            return counters.WorkingSetSize, counters.PeakWorkingSetSize
        finally:
            kernel.CloseHandle(handle)

    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status", encoding="ascii") as handle:
                values = {
                    parts[0]: int(parts[1]) * 1024
                    for line in handle
                    if (parts := line.split()) and parts[0] in ("VmRSS:", "VmHWM:")
                }
            return (values["VmRSS:"], values.get("VmHWM:", values["VmRSS:"]))
        except (FileNotFoundError, ProcessLookupError):
            return None

    if sys.platform == "darwin":
        result = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(pid)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        value = result.stdout.strip()
        if not value:
            return None
        rss = int(value.splitlines()[-1].strip()) * 1024
        return rss, rss

    return None


def start_self_watchdog(limit_bytes, parent_pid=None, poll_seconds=0.1):
    stopped = threading.Event()

    def watch():
        while not stopped.wait(poll_seconds):
            try:
                usage = process_memory(os.getpid())
                if usage and max(usage) > limit_bytes:
                    os._exit(86)
                if parent_pid is not None:
                    parent_missing = process_memory(parent_pid) is None
                    if sys.platform.startswith("linux"):
                        parent_missing = parent_missing or os.getppid() != parent_pid
                    if parent_missing:
                        os._exit(87)
            except Exception:
                os._exit(88)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return stopped


def monitor_process(process, limit_bytes, poll_seconds=0.1):
    peak = 0
    try:
        while process.poll() is None:
            usage = process_memory(process.pid)
            if usage:
                peak = max(peak, *usage)
                if peak > limit_bytes:
                    raise MemoryLimitExceeded(
                        f"子进程 PID={process.pid} 内存峰值 {peak} 超过限制 {limit_bytes}"
                    )
            time.sleep(poll_seconds)
        code = process.wait()
        if code == 86:
            raise MemoryLimitExceeded(
                f"子进程 PID={process.pid} 内存超过限制 {limit_bytes}"
            )
        return code, peak
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
