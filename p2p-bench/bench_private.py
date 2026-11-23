"""Private statements at their limit during a run: pre-proven ENTER, TRANSFER and EXIT frames, paced, and
which cut ran each."""

import http.client
import json
import threading
import time

from bench_submit import BUSY_RETRIES, is_busy, retry_after


ROUTES = {"enter": "/v1/private/enters", "transfer": "/v1/private/transfers", "exit": "/v1/private/exits"}


def post(port, route, frame):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        body = json.dumps({"bytes": frame})
        connection.request("POST", route, body, {"Content-Type": "application/json"})
        reply = connection.getresponse()
        return reply.status, reply.read()
    finally:
        connection.close()


def statement(port, digest):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", f"/v1/private/statements/{digest}")
        reply = connection.getresponse()
        body = reply.read()
        return json.loads(body) if reply.status == 200 else None
    finally:
        connection.close()


def spread(values):
    if not values:
        return None
    values = sorted(values)
    return {"p50": values[len(values) // 2], "p90": values[len(values) * 9 // 10], "max": values[-1]}


class PrivateLoad(threading.Thread):
    """Sends one frame every 1/`rate` seconds to the validators in turn and watches one validator
    for the cut that runs each accepted statement."""

    def __init__(self, frames, ports, rate, watch_port, log, down=frozenset(), after=0):
        super().__init__(daemon=True)
        self.frames = frames
        self.ports = ports
        self.down = down
        self.rate = rate
        self.after = after
        self.watch_port = watch_port
        self.log = log
        self.records = []
        self.stopped = threading.Event()
        self.watcher = threading.Thread(target=self.watch, daemon=True)

    def run(self):
        self.watcher.start()
        started = time.time() + self.after
        for index, path in enumerate(self.frames):
            delay = started + index / self.rate - time.time()
            if delay > 0 and self.stopped.wait(delay):
                return
            if self.stopped.is_set():
                return
            frame = path.read_text()
            class_name = path.stem.split("-", 1)[1]
            # A validator that is down is skipped, as a wallet would try another node.
            up = [port for name, port in self.ports.items() if name not in self.down] or list(self.ports.values())
            record = {"sent": time.time(), "port": up[index % len(up)], "ran": None,
                      "height": None, "class": class_name}
            try:
                status, body = post(record["port"], ROUTES[class_name], frame)
                # A busy node is asked again after the wait it names, as a wallet and the payments do.
                busy = 0
                while is_busy(status, body) and busy < BUSY_RETRIES:
                    busy += 1
                    time.sleep(retry_after(body))
                    status, body = post(record["port"], ROUTES[class_name], frame)
                record["status"] = status
                record["busy"] = busy
                if status == 200:
                    record["digest"] = json.loads(body)["id"]
                else:
                    record["error"] = body[:200].decode(errors="replace")
                    self.log(f"private statement {index} refused: {status} {record['error']}")
            except OSError as error:
                record["status"] = -1
                record["error"] = str(error)
                self.log(f"private statement {index} failed: {error}")
            record["answered"] = time.time()
            self.records.append(record)

    def watch(self):
        while not self.stopped.is_set():
            for record in list(self.records):
                if record.get("digest") and record["ran"] is None:
                    try:
                        found = statement(self.watch_port, record["digest"])
                    except OSError:
                        continue
                    if found and found.get("height") is not None:
                        record["ran"] = time.time()
                        record["height"] = found["height"]
            self.stopped.wait(0.5)

    def finish(self):
        """All were sent or the run is over; a last look gives what is still on its way time to land."""
        self.join(timeout=max(0.0, self.after + len(self.frames) / self.rate + 30))
        deadline = time.time() + 30
        while time.time() < deadline and any(r.get("digest") and r["ran"] is None for r in self.records):
            time.sleep(1)
        self.stopped.set()
        self.watcher.join(timeout=5)

    def summary(self, validators):
        accepted = [record for record in self.records if record.get("digest")]
        executed = {}
        for name, port in validators.items():
            heights = {}
            for record in accepted:
                try:
                    found = statement(port, record["digest"])
                except OSError:
                    found = None
                if found and found.get("height") is not None:
                    heights[record["digest"]] = found["height"]
            executed[name] = heights
        first = next(iter(executed.values()), {})
        agree = all(heights == first for heights in executed.values())
        per_cut = {}
        for height in first.values():
            per_cut[height] = per_cut.get(height, 0) + 1
        ran = {digest for digest in first}
        by_class = {}
        for record in accepted:
            counts = by_class.setdefault(record["class"], {"accepted": 0, "executed": 0})
            counts["accepted"] += 1
            counts["executed"] += record["digest"] in ran
        return {
            "sent": len(self.records),
            "accepted": len(accepted),
            "by_class": by_class,
            "executed_digests": sorted(ran),
            "refused": [record.get("error") for record in self.records if not record.get("digest")][:5],
            "executed": {name: len(heights) for name, heights in executed.items()},
            "all_validators_agree": agree,
            "most_per_cut": max(per_cut.values(), default=0),
            "cuts_with_statements": len(per_cut),
            "admission_seconds": spread([record["answered"] - record["sent"] for record in accepted]),
            "seconds_to_a_final_cut": spread([record["ran"] - record["sent"] for record in accepted if record["ran"]]),
            "ran_at": [record["ran"] for record in accepted if record["ran"]],
        }
