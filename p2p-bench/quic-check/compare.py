"""Cubic against BBR on one QUIC stream through wan-proxy: py -3.14 compare.py RTT_MS LOSS SECONDS"""

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE.parent / "run-check"
CHECK = HERE / "target" / "release" / "quic-check.exe"
PROXY = HERE.parent / "wan-proxy" / "target" / "release" / "wan-proxy.exe"


def main():
    rtt, loss, seconds = float(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
    WORK.mkdir(exist_ok=True)
    config = {"rtt_ms": {"a-b": rtt}, "jitter_ms": 0, "loss": {"a-b": loss}, "spikes": {},
              "sources": {"39103": "a"},
              "proxies": [{"node": "b", "listen": 39101, "target": 39102, "protocol": "udp"}]}
    (WORK / "quic.json").write_text(json.dumps(config))
    server = subprocess.Popen([CHECK, "server", "39102"])
    proxy = subprocess.Popen([PROXY, WORK / "quic.json", "b"], stdout=subprocess.DEVNULL)
    time.sleep(1)
    try:
        for control in ["cubic", "bbr"]:
            print(f"== {control}, RTT {rtt:.0f} ms, loss {loss * 100:.2f} %", flush=True)
            subprocess.run([CHECK, "client", "127.0.0.1:39101", "39103", seconds, control])
    finally:
        proxy.kill()
        server.kill()


main()
