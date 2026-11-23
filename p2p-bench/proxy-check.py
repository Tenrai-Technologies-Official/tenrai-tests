"""A UDP flow through wan-proxy must survive its target being closed: py -3.14 proxy-check.py [PROXY]

A node that restarts, or one that is dialled before it listens, leaves its port closed for a moment.
The proxy's packet to it then earns an ICMP port unreachable, which Windows reports as an error on the
proxy's next read. The flow has to carry on once the port is open again, in both directions.
"""

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PROXY = Path(sys.argv[1]) if len(sys.argv) > 1 else TOOLS / "wan-proxy" / "target" / "release" / "wan-proxy.exe"
LISTEN, TARGET, CLIENT = 29951, 29911, 29912


def main():
    folder = Path(tempfile.mkdtemp(prefix="wan-proxy-check-"))
    (folder / "wan.txt").write_text("off")
    config = {"sources": {str(CLIENT): "a"}, "switch_file": str(folder / "wan.txt"),
              "proxies": [{"node": "b", "listen": LISTEN, "target": TARGET, "protocol": "udp"}]}
    (folder / "proxy.json").write_text(json.dumps(config))
    proxy = subprocess.Popen([str(PROXY), str(folder / "proxy.json"), "b"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", CLIENT))
        client.settimeout(2)
        for _ in range(3):
            client.sendto(b"too early", ("127.0.0.1", LISTEN))
            time.sleep(0.2)

        target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target.bind(("127.0.0.1", TARGET))
        target.settimeout(0.3)
        client.settimeout(0.3)
        request = reply = None
        for _ in range(10):
            # A sender repeats what got lost, as QUIC does; the first packets may still meet the error.
            client.sendto(b"ping", ("127.0.0.1", LISTEN))
            try:
                request, sender = target.recvfrom(100)
                target.sendto(b"pong", sender)
                reply = client.recvfrom(100)[0]
                break
            except (TimeoutError, ConnectionResetError):
                continue
        print("the target got", request, "and the client got", reply)
        return 0 if reply == b"pong" else 1
    finally:
        proxy.kill()


if __name__ == "__main__":
    sys.exit(main())
