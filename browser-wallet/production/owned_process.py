"""Fixture-only subprocess ownership. Never imports a benchmark, Git helper or product module."""
import ctypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

CLEANUP_FAILURE = 249

def _control(stopping):
    # The target's stdin is NUL, not this pipe. Parent death closes this exact owned pipe.
    try:
        os.read(sys.stdin.fileno(), 64)
    finally:
        stopping.set()

def _windows(arguments, stopping):
    import ctypes.wintypes as w
    import msvcrt

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", w.DWORD), ("min_working", ctypes.c_size_t),
                    ("max_working", ctypes.c_size_t), ("active_limit", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("reads", "writes", "others", "read_bytes", "write_bytes", "other_bytes")]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    class Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                    (name, w.DWORD) for name in ("faults", "total", "active", "terminated")]
    class Startup(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR)] + [
                    (name, w.DWORD) for name in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")] + [
                    ("show", w.WORD), ("reserved_bytes", w.WORD), ("reserved_data", ctypes.c_void_p),
                    ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]
    class ProcessInfo(ctypes.Structure):
        _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    def api(name, result, arguments):
        call = getattr(kernel, name); call.restype = result; call.argtypes = arguments; return call
    create_job = api("CreateJobObjectW", w.HANDLE, [ctypes.c_void_p, w.LPCWSTR])
    set_job = api("SetInformationJobObject", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD])
    query_job = api("QueryInformationJobObject", w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p])
    assign = api("AssignProcessToJobObject", w.BOOL, [w.HANDLE, w.HANDLE])
    terminate_job = api("TerminateJobObject", w.BOOL, [w.HANDLE, w.UINT])
    terminate = api("TerminateProcess", w.BOOL, [w.HANDLE, w.UINT])
    close = api("CloseHandle", w.BOOL, [w.HANDLE])
    wait = api("WaitForSingleObject", w.DWORD, [w.HANDLE, w.DWORD])
    exit_code = api("GetExitCodeProcess", w.BOOL, [w.HANDLE, ctypes.POINTER(w.DWORD)])
    resume = api("ResumeThread", w.DWORD, [w.HANDLE])
    inherit = api("SetHandleInformation", w.BOOL, [w.HANDLE, w.DWORD, w.DWORD])
    create = api("CreateProcessW", w.BOOL, [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                 w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(Startup), ctypes.POINTER(ProcessInfo)])
    def checked(value, operation):
        if not value: raise ctypes.WinError(ctypes.get_last_error(), operation)
        return value
    def empty(job):
        end = time.monotonic() + 10
        while True:
            accounting = Accounting()
            checked(query_job(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None), "QueryInformationJobObject")
            if accounting.active == 0: return
            if time.monotonic() >= end: raise RuntimeError("the owned Job did not physically drain")
            time.sleep(0.02)

    job = None; info = ProcessInfo(); assigned = False; nul = None
    try:
        job = checked(create_job(None, None), "CreateJobObjectW") # Private unnamed, non-inheritable.
        limits = ExtendedLimits(); limits.basic.flags = 0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        checked(set_job(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "SetInformationJobObject")
        nul = os.open(os.devnull, os.O_RDONLY); os.set_inheritable(nul, True)
        # The target cannot retain the supervisor's stop/parent-death pipe.
        checked(inherit(msvcrt.get_osfhandle(sys.stdin.fileno()), 1, 0), "stop pipe inheritance")
        for stream in (sys.stdout, sys.stderr): checked(inherit(msvcrt.get_osfhandle(stream.fileno()), 1, 1), "output inheritance")
        startup = Startup(); startup.cb = ctypes.sizeof(startup); startup.flags = 0x101; startup.show = 0
        startup.stdin = msvcrt.get_osfhandle(nul)
        startup.stdout = msvcrt.get_osfhandle(sys.stdout.fileno()); startup.stderr = msvcrt.get_osfhandle(sys.stderr.fileno())
        executable = shutil.which(arguments[0])
        if executable is None: raise FileNotFoundError("the fixture executable was not found")
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline([executable] + arguments[1:]))
        checked(create(executable, command, None, None, True, 0x08000004, None, None,
                       ctypes.byref(startup), ctypes.byref(info)), "CreateProcessW") # HIDDEN + SUSPENDED.
        # No target instruction or target grandchild can run before assignment succeeds.
        checked(assign(job, info.process), "AssignProcessToJobObject"); assigned = True
        if not stopping.is_set():
            if resume(info.thread) == 0xffffffff: raise ctypes.WinError(ctypes.get_last_error(), "ResumeThread")
        while not stopping.wait(0.02):
            status = wait(info.process, 0)
            if status == 0:
                code = w.DWORD(); checked(exit_code(info.process, ctypes.byref(code)), "GetExitCodeProcess")
                return int(code.value)
            if status != 258: raise ctypes.WinError(ctypes.get_last_error(), "WaitForSingleObject")
        return 0
    finally:
        # Every exception after CreateProcess still owns its exact handle, even if assignment failed.
        try:
            if assigned:
                checked(terminate_job(job, 1), "TerminateJobObject"); empty(job)
            elif info.process:
                checked(terminate(info.process, 1), "TerminateProcess suspended fixture")
                if wait(info.process, 10_000) != 0: raise RuntimeError("suspended fixture did not drain")
        finally:
            if info.thread: close(info.thread)
            if info.process: close(info.process)
            if job: close(job)
            if nul is not None: os.close(nul)


def _posix(arguments, stopping):
    # Linux adopts/reaps grandchildren rather than leaving their zombies to an unrelated init.
    linux = sys.platform.startswith("linux")
    if not linux:
        raise RuntimeError("owned fixture trees currently require Windows Jobs or Linux subreaper/pidfd support")
    if linux:
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("owned Linux fixture cleanup requires pidfd signalling")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0: raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    if not all(hasattr(os, name) for name in ("posix_spawnp", "waitid", "WNOWAIT")):
        raise RuntimeError("owned POSIX fixture processes require posix_spawn and waitid WNOWAIT")
    # An inherited SIG_IGN auto-reaps children and would invalidate the WNOWAIT PID anchor.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    if stopping.is_set(): return 0
    # Atomic child process-group creation. Keep its PID unreaped until every group signal was sent,
    # so a fast direct-child exit cannot redirect a later group signal to a reused foreign PID.
    pid = os.posix_spawnp(arguments[0], arguments, os.environ.copy(), setpgroup=0,
        file_actions=[(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0)])
    result = 0
    try:
        while not stopping.wait(0.02):
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if status is not None:
                result = status.si_status if status.si_code == os.CLD_EXITED else 128 + status.si_status
                break
    finally:
        for action, delay in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 0)):
            try: os.killpg(pid, action)
            except ProcessLookupError: pass
            if delay: time.sleep(delay)
        # No later signal uses this numeric group ID. Linux also owns adopted children that
        # Chrome legitimately moved to another group: read ONLY this supervisor's kernel child
        # list, bind each still-unreaped child to a pidfd, then signal that handle. A child cannot
        # be PID-reused before this sole reaper collects it. Never enumerate machine processes.
        end = time.monotonic() + 10
        while True:
            if linux:
                with open("/proc/self/task/{}/children".format(os.getpid()), "rt", encoding="ascii") as children:
                    raw = children.read((1 << 20) + 1)
                if len(raw) > (1 << 20): raise RuntimeError("the owned kernel child list exceeded its bound")
                tokens = raw.split()
                if any(not token.isascii() or not token.isdigit() or int(token) <= 0 for token in tokens):
                    raise RuntimeError("the owned kernel child list was malformed")
                owned = [int(value) for value in tokens]
                for child in owned:
                    descriptor = os.pidfd_open(child, 0)
                    try:
                        try: signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                        except ProcessLookupError: pass
                    finally: os.close(descriptor)
            reaped = False; waiting = False
            while True:
                try: child, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError: child = -1
                if child == 0: waiting = True
                if child <= 0: break
                reaped = True
            # Linux subreaper adoption makes every surviving descendant a child (possibly via
            # an alive intermediate child). No numeric group identity is consulted after reap.
            if not waiting: break
            if time.monotonic() >= end: raise RuntimeError("the owned POSIX group did not physically drain")
            if not reaped: time.sleep(0.02)
    return result


def main():
    if len(sys.argv) < 2: raise ValueError("fixture executable is required")
    stopping = threading.Event()
    # A real host abort is EOF, not a time-based lease or a process-name sweep.
    threading.Thread(target=_control, args=(stopping,), daemon=True).start()
    if os.name == "nt": return _windows(sys.argv[1:], stopping)
    # Install every controlled termination handler before the first target spawn.
    for action in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(action, lambda *_: stopping.set())
    return _posix(sys.argv[1:], stopping)

if __name__ == "__main__":
    try: status = main()
    except BaseException as error:
        # Never echo command arguments, prover input, captured CLI output or wallet secrets.
        sys.stderr.write("owned fixture process failed: " + type(error).__name__ + ": " + str(error)[:512] + "\n")
        status = CLEANUP_FAILURE
    sys.exit(status)
