"""Linux lifecycle fixture: exec the actual supervisor with inherited SIGCHLD ignored."""
import os
from pathlib import Path
import signal
import sys

signal.signal(signal.SIGCHLD, signal.SIG_IGN)
helper = str(Path(__file__).resolve().parents[2] / "owned_process.py")
os.execv(sys.executable, [sys.executable, "-I", "-u", helper] + sys.argv[1:])
