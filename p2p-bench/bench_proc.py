"""CPU time, memory and disk bytes of processes and the whole machine, read through the Win32 API,
CPU time per thread name of the nodes, plus priority and CPU placement for the processes of a run."""

import ctypes
import ctypes.wintypes as wt
import threading
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ACCESS = 0x1000 | 0x0010


class FILETIME(ctypes.Structure):
    _fields_ = [("low", wt.DWORD), ("high", wt.DWORD)]

    def seconds(self):
        return ((self.high << 32) | self.low) / 1e7


class MEMORY(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("faults", wt.DWORD), ("peak", ctypes.c_size_t), ("working", ctypes.c_size_t),
                ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t)]


class IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]


kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
kernel32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(MEMORY), wt.DWORD]
kernel32.GetProcessIoCounters.argtypes = [wt.HANDLE, ctypes.POINTER(IO)]
kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(FILETIME)] * 3


TH32CS_SNAPTHREAD = 0x4
THREAD_QUERY_LIMITED_INFORMATION = 0x0800


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("size", wt.DWORD), ("usage", wt.DWORD), ("thread", wt.DWORD), ("owner", wt.DWORD),
                ("base_priority", wt.LONG), ("delta_priority", wt.LONG), ("flags", wt.DWORD)]


kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.Thread32First.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.argtypes = [wt.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.OpenThread.restype = wt.HANDLE
kernel32.OpenThread.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetThreadTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
kernel32.GetThreadDescription.restype = ctypes.c_long
kernel32.GetThreadDescription.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


def thread_times(pids):
    """{pid: {thread id: (name, CPU seconds)}} for every thread of these processes."""
    found = {pid: {} for pid in pids}
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    entry = THREADENTRY32()
    entry.size = ctypes.sizeof(entry)
    more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
    while more:
        if entry.owner in found:
            handle = kernel32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, entry.thread)
            if handle:
                times = [FILETIME() for _ in range(4)]
                if kernel32.GetThreadTimes(handle, *(ctypes.byref(time) for time in times)):
                    text = ctypes.c_void_p()
                    name = ""
                    if kernel32.GetThreadDescription(handle, ctypes.byref(text)) >= 0 and text.value:
                        name = ctypes.wstring_at(text.value)
                        kernel32.LocalFree(text)
                    found[entry.owner][entry.thread] = (name or "unnamed", times[2].seconds() + times[3].seconds())
                kernel32.CloseHandle(handle)
        more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    kernel32.CloseHandle(snapshot)
    return found


def system_times():
    idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
    kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    return idle.seconds(), kernel.seconds() + user.seconds()


class Process:
    def __init__(self, name, pid):
        self.name = name
        self.pid = pid
        self.handle = kernel32.OpenProcess(ACCESS, False, pid)

    def read(self):
        created, ended, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(ended),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        memory = MEMORY()
        memory.cb = ctypes.sizeof(memory)
        kernel32.K32GetProcessMemoryInfo(self.handle, ctypes.byref(memory), memory.cb)
        io = IO()
        kernel32.GetProcessIoCounters(self.handle, ctypes.byref(io))
        return kernel.seconds() + user.seconds(), memory.working, io.write_bytes, io.read_bytes, io.writes, io.reads


class Sampler:
    """One row per second: machine CPU and, per process, CPU share of one core, RSS and disk bytes;
    for the processes named in `by_thread` also the cores of each thread name and of its busiest thread."""

    def __init__(self, processes, by_thread=()):
        self.processes = [Process(name, pid) for name, pid in processes]
        self.by_thread = {pid: name for name, pid in processes if name in by_thread}
        self.rows = []
        self.running = False

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join()

    def threads(self, before, span):
        now = thread_times(self.by_thread)
        use = {}
        for pid, threads in now.items():
            names = use.setdefault(self.by_thread[pid], {})
            for tid, (name, spent) in threads.items():
                cores = (spent - before.get(pid, {}).get(tid, (name, spent))[1]) / span
                total, busiest = names.get(name, (0.0, 0.0))
                names[name] = (total + cores, max(busiest, cores))
        return now, {process: {name: {"cores": total, "busiest": busiest} for name, (total, busiest) in names.items()}
                     for process, names in use.items()}

    def loop(self):
        last = {process.name: process.read() for process in self.processes}
        last_threads = thread_times(self.by_thread) if self.by_thread else {}
        last_system = system_times()
        last_at = time.monotonic()
        while self.running:
            time.sleep(1.0)
            now = time.monotonic()
            span = now - last_at
            idle, total = system_times()
            busy = 1.0 - (idle - last_system[0]) / max(total - last_system[1], 1e-9)
            row = {"t": time.time(), "machine_cpu": busy, "processes": {}}
            for process in self.processes:
                value = process.read()
                before = last.get(process.name)
                if value and before:
                    row["processes"][process.name] = {
                        "cpu": (value[0] - before[0]) / span,
                        "rss": value[1],
                        "write_bytes": value[2] - before[2],
                        "read_bytes": value[3] - before[3],
                        "writes": value[4] - before[4],
                        "reads": value[5] - before[5],
                    }
                last[process.name] = value
            if self.by_thread:
                last_threads, row["threads"] = self.threads(last_threads, span)
            last_system, last_at = (idle, total), now
            self.rows.append(row)


ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
SET_INFORMATION = 0x0200
kernel32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]


def raise_priority(pid):
    """The proxies stand in for the network; when they wait for CPU, every packet comes late."""
    process = kernel32.OpenProcess(SET_INFORMATION, False, pid)
    if not kernel32.SetPriorityClass(process, ABOVE_NORMAL_PRIORITY_CLASS):
        raise OSError(ctypes.get_last_error(), "SetPriorityClass")


kernel32.SetProcessAffinityMask.argtypes = [wt.HANDLE, ctypes.c_size_t]


def pin(pid, mask):
    """Keeps a process on the logical CPUs of `mask`."""
    process = kernel32.OpenProcess(SET_INFORMATION, False, pid)
    if not kernel32.SetProcessAffinityMask(process, mask):
        raise OSError(ctypes.get_last_error(), "SetProcessAffinityMask")


class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [("ControlFlags", wt.DWORD), ("CpuRate", wt.DWORD)]


CPU_RATE_CONTROL = 15
CPU_RATE_ENABLE = 0x1
CPU_RATE_HARD_CAP = 0x4
kernel32.CreateJobObjectW.restype = wt.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
kernel32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
kernel32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]


class CpuCap:
    """Holds one process to a share of the whole machine's CPU, as a starved or overloaded host
    would, until `lift`; the process runs on unchanged otherwise."""

    def __init__(self, pid):
        self.job = kernel32.CreateJobObjectW(None, None)
        if not self.job:
            raise OSError(ctypes.get_last_error(), "CreateJobObject")
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process or not kernel32.AssignProcessToJobObject(self.job, process):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
        kernel32.CloseHandle(process)

    def _set(self, flags, rate):
        info = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(flags, rate)
        if not kernel32.SetInformationJobObject(self.job, CPU_RATE_CONTROL, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")

    def limit(self, share):
        """`share` of all logical CPUs together, 0.01 for one percent."""
        self._set(CPU_RATE_ENABLE | CPU_RATE_HARD_CAP, max(1, min(10_000, round(share * 10_000))))

    def lift(self):
        self._set(0, 0)
