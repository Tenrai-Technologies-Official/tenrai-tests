"""Prints the stack of every thread of a running process: its name, and per frame the module and
offset, with the exported name where the module has one. Each thread is suspended only while its
context is read.

    py -3.14 -I stacks.py PID [max frames]
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
dbg = ctypes.WinDLL(os.environ.get("TENRAI_DBGHELP", "dbghelp.dll"), use_last_error=True,
                   winmode=0x00000800)  # LOAD_LIBRARY_SEARCH_SYSTEM32

PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x400, 0x10
THREAD_GET_CONTEXT, THREAD_SUSPEND_RESUME, THREAD_QUERY_LIMITED = 0x8, 0x2, 0x800
TH32CS_SNAPTHREAD = 0x4
CONTEXT_FULL = 0x10000B
MACHINE_AMD64 = 0x8664


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", ctypes.c_long),
                ("tpDeltaPri", ctypes.c_long), ("dwFlags", wt.DWORD)]


class ADDRESS64(ctypes.Structure):
    _fields_ = [("Offset", ctypes.c_uint64), ("Segment", wt.WORD), ("Mode", wt.DWORD)]


class STACKFRAME64(ctypes.Structure):
    _fields_ = [("AddrPC", ADDRESS64), ("AddrReturn", ADDRESS64), ("AddrFrame", ADDRESS64),
                ("AddrStack", ADDRESS64), ("AddrBStore", ADDRESS64),
                ("FuncTableEntry", ctypes.c_void_p), ("Params", ctypes.c_uint64 * 4),
                ("Far", wt.BOOL), ("Virtual", wt.BOOL), ("Reserved", ctypes.c_uint64 * 3),
                ("KdHelp", ctypes.c_ubyte * 512)]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p), ("SizeOfImage", wt.DWORD),
                ("EntryPoint", ctypes.c_void_p)]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenThread.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
dbg.SymFunctionTableAccess64.restype = ctypes.c_void_p
dbg.SymGetModuleBase64.restype = ctypes.c_uint64
dbg.SymGetModuleBase64.argtypes = [wt.HANDLE, ctypes.c_uint64]
dbg.StackWalk64.argtypes = [wt.DWORD, wt.HANDLE, wt.HANDLE, ctypes.POINTER(STACKFRAME64),
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_void_p]
dbg.SymFromAddr.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64),
                            ctypes.c_void_p]


def modules(process):
    handles = (ctypes.c_void_p * 1024)()
    needed = wt.DWORD()
    psapi.EnumProcessModulesEx(process, handles, ctypes.sizeof(handles), ctypes.byref(needed), 3)
    found = []
    for handle in handles[: needed.value // ctypes.sizeof(ctypes.c_void_p)]:
        name = ctypes.create_unicode_buffer(520)
        psapi.GetModuleFileNameExW(process, ctypes.c_void_p(handle), name, 520)
        info = MODULEINFO()
        psapi.GetModuleInformation(process, ctypes.c_void_p(handle), ctypes.byref(info),
                                   ctypes.sizeof(info))
        found.append((info.lpBaseOfDll or 0, info.SizeOfImage, os.path.basename(name.value)))
    return found


def describe(process, mods, address):
    module = next(((base, name) for base, size, name in mods if base <= address < base + size),
                  None)
    buffer = (ctypes.c_ubyte * (88 + 1024))()
    ctypes.c_uint32.from_buffer(buffer, 0).value = 88
    ctypes.c_uint32.from_buffer(buffer, 80).value = 1000
    displacement = ctypes.c_uint64()
    if dbg.SymFromAddr(process, address, ctypes.byref(displacement), ctypes.addressof(buffer)):
        length = ctypes.c_uint32.from_buffer(buffer, 76).value
        symbol = bytes(buffer[84:84 + length]).decode(errors="replace")
        if module and not module[1].lower().endswith(".exe"):
            return f"{module[1]}!{symbol}+0x{displacement.value:x}"
    if module:
        return f"{module[1]}+0x{address - module[0]:x}"
    return f"0x{address:x}"


def thread_name(handle):
    text = ctypes.c_wchar_p()
    try:
        if k32.GetThreadDescription(handle, ctypes.byref(text)) >= 0 and text.value:
            name = text.value
            k32.LocalFree(text)
            return name
    except AttributeError:
        pass
    return ""


def main():
    pid = int(sys.argv[1])
    depth = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    process = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not process:
        sys.exit(f"cannot open {pid}: {ctypes.get_last_error()}")
    dbg.SymSetOptions(0x2 | 0x4 | 0x200 | 0x80000)
    if not dbg.SymInitialize(process, None, True):
        sys.exit(f"SymInitialize failed: {ctypes.get_last_error()}")
    mods = modules(process)
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    entry = THREADENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    threads = []
    more = k32.Thread32First(snapshot, ctypes.byref(entry))
    while more:
        if entry.th32OwnerProcessID == pid:
            threads.append(entry.th32ThreadID)
        more = k32.Thread32Next(snapshot, ctypes.byref(entry))
    k32.CloseHandle(snapshot)
    table = ctypes.cast(dbg.SymFunctionTableAccess64, ctypes.c_void_p)
    base = ctypes.cast(dbg.SymGetModuleBase64, ctypes.c_void_p)
    for tid in threads:
        thread = k32.OpenThread(THREAD_GET_CONTEXT | THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED,
                                False, tid)
        if not thread:
            print(f"thread {tid}: cannot open")
            continue
        name = thread_name(thread)
        raw = (ctypes.c_ubyte * (1232 + 16))()
        aligned = (ctypes.addressof(raw) + 15) & ~15
        context = (ctypes.c_ubyte * 1232).from_address(aligned)
        ctypes.c_uint32.from_address(aligned + 0x30).value = CONTEXT_FULL
        frames = []
        k32.SuspendThread(thread)
        try:
            if k32.GetThreadContext(thread, context):
                frame = STACKFRAME64()
                frame.AddrPC.Offset = ctypes.c_uint64.from_address(aligned + 0xF8).value
                frame.AddrStack.Offset = ctypes.c_uint64.from_address(aligned + 0x98).value
                frame.AddrFrame.Offset = ctypes.c_uint64.from_address(aligned + 0xA0).value
                for address in (frame.AddrPC, frame.AddrStack, frame.AddrFrame):
                    address.Mode = 3
                while len(frames) < depth and dbg.StackWalk64(
                        MACHINE_AMD64, process, thread, ctypes.byref(frame), aligned, None,
                        table, base, None):
                    if not frame.AddrPC.Offset:
                        break
                    frames.append(frame.AddrPC.Offset)
        finally:
            k32.ResumeThread(thread)
            k32.CloseHandle(thread)
        print(f"== thread {tid} {name!r}")
        for address in frames:
            print("   ", describe(process, mods, address))
    dbg.SymCleanup(process)


if __name__ == "__main__":
    main()
