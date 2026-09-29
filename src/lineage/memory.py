"""Peak resident-memory monitoring for chunked reads.

RSS is sampled after every processed chunk. The peak value is visible on the
dashboard and in ``resource-usage.json``; it is intentionally kept out of the
deterministic report so reruns stay byte-identical except timestamps.
"""

from __future__ import annotations

import os
import sys


def resident_bytes() -> int:
    """Current resident-set size in bytes, best effort across platforms."""
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        pass

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize)
        return 0

    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # Linux reports kilobytes, macOS reports bytes.
        return peak * 1024 if sys.platform.startswith("linux") else peak
    except Exception:
        return 0


class ResidentMonitor:
    def __init__(self) -> None:
        self.peak = resident_bytes()
        self.samples = 1 if self.peak else 0

    def sample(self) -> int:
        current = resident_bytes()
        if current:
            self.samples += 1
            if current > self.peak:
                self.peak = current
        return current

    def as_dict(self) -> dict:
        return {
            "resident_peak_bytes": int(self.peak),
            "resident_peak_mib": round(self.peak / (1024 * 1024), 3),
            "samples": int(self.samples),
        }
