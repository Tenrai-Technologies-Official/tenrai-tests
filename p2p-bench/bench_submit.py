"""Paced transaction submission over keep-alive HTTP, and batched status reads."""

import http.client
import json
import mmap
import queue
import threading
import time

WORKERS_PER_NODE = 6
BUSY_RETRIES = 20
# Sends of one payment over a connection the node had closed meanwhile.
DROPPED_RETRIES = 3
# A payment this far behind its slot is not sent, so a stalled chain cannot hold the run forever.
GIVE_UP_SECONDS = 30


def is_busy(status, body):
    """Only a busy node is asked again; a full mempool is an answer, as a wallet would show it."""
    return status == 503 and b"busy" in body


def retry_after(body):
    """A busy node names how long to wait; clients wait that long and ask again."""
    try:
        return json.loads(body).get("retry_after_millis", 250) / 1000
    except ValueError:
        return 0.25


def load(path):
    with open(path) as file:
        return [tuple(line.split()) for line in file if line.strip()]


def count(path):
    """The payments in a file as `load` reads them, counted without holding them."""
    with open(path, "rb") as file:
        return sum(1 for line in file if line.strip())


class Payments:
    """The first `count` payments of a file as `load` reads them, with only their IDs and places
    in memory: a signed payment is some 9 kB of hex, read from the file when it is sent, so that a
    run of tens of thousands leaves the memory to the nodes."""

    def __init__(self, path, count=None):
        self.file = open(path, "rb")
        self.map = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        self.entries = []
        start, size = 0, len(self.map)
        while start < size and (count is None or len(self.entries) < count):
            end = self.map.find(b"\n", start)
            end = size if end < 0 else end
            line = self.map[start:end].rstrip()
            if line:
                space = line.index(b" ")
                self.entries.append((line[:space].decode(), start + space + 1, start + len(line)))
            start = end + 1

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, index):
        txid, start, end = self.entries[index]
        return txid, self.map[start:end].decode()


def connect(port):
    return http.client.HTTPConnection("127.0.0.1", port, timeout=15)


def post(connection, path, payload):
    connection.request("POST", path, json.dumps(payload), {"Content-Type": "application/json"})
    reply = connection.getresponse()
    return reply.status, reply.read()


class FinalWatch(threading.Thread):
    """Reads the status of accepted payments that later ones spend from, a batch a second, and
    reports each once it is final or failed."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.port = port
        self.watched = queue.Queue()
        self.settled = queue.Queue()
        self.stopped = threading.Event()

    def run(self):
        pending = set()
        while not self.stopped.is_set():
            while not self.watched.empty():
                pending.add(self.watched.get())
            if pending:
                try:
                    current = statuses(self.port, list(pending))
                except Exception:
                    current = {}
                for txid, status in current.items():
                    if is_terminal(status) or lifecycle(status) in ("failed", "dropped"):
                        pending.discard(txid)
                        self.settled.put((txid, lifecycle(status) == "finalized"))
            self.stopped.wait(1)


SENT_AWAY = b"send the transaction to another node"


def submit(ports, transactions, rate, label, log=print, down=frozenset(), parent_step=0, status_port=None,
           slower=None):
    """Sends transaction k at start + k / rate to the next node in turn that is not down; returns
    one record each. With `slower` = (seconds, rate), the rest goes at that rate from then on.
    With `parent_step`, transaction k spends the change of transaction k - parent_step and leaves
    only once that one is final, as a wallet that spends finalized coins only; one whose parent
    was refused or did not become final in time is not sent."""
    names = sorted(ports)
    queues = {name: queue.Queue() for name in names}
    records = [None] * len(transactions)
    watch = FinalWatch(status_port) if parent_step else None
    if watch:
        watch.start()
    turn = [0]
    held = {}
    finished = {}
    accepted_parents = queue.Queue()

    def put(index, due, tried=(), first_sent=None):
        """Queues the payment for the next node in turn that is up and did not send it away yet."""
        up = [name for name in names if name not in down] or names
        left = [name for name in up if name not in tried] or up
        queues[left[turn[0] % len(left)]].put((index, due, tried, first_sent))
        turn[0] += 1

    def withhold(index, due, error):
        now = time.time()
        records[index] = {"txid": transactions[index][0], "node": None, "due": due, "sent": now,
                          "done": now, "status": -2, "error": error, "busy": 0}

    def settle_parents(now):
        """Releases the payments whose parent became final; gives up on those that waited too long."""
        while not accepted_parents.empty():
            watch.watched.put(accepted_parents.get())
        while not watch.settled.empty():
            parent, final = watch.settled.get()
            child = held.pop(parent, None)
            if child is None:
                finished[parent] = final
            elif final:
                put(*child)
            else:
                withhold(*child, "not sent: its parent failed")
        for parent, (index, due) in list(held.items()):
            record = records[index - parent_step]
            if record is not None and record["status"] != 200:
                del held[parent]
                withhold(index, due, "not sent: its parent was refused")
            elif now - due > GIVE_UP_SECONDS:
                del held[parent]
                withhold(index, due, "not sent: its parent was not final in time")

    def worker(name):
        connection = connect(ports[name])
        while True:
            item = queues[name].get()
            if item is None:
                return
            index, due, tried, first_sent = item
            txid, wire = transactions[index]
            sent = first_sent or time.time()
            if sent - due > GIVE_UP_SECONDS:
                records[index] = {"txid": txid, "node": name, "due": due, "sent": sent, "done": sent,
                                  "status": -1, "error": "not sent: too far behind", "busy": 0}
                continue
            busy = dropped = 0
            while True:
                try:
                    status, body = post(connection, "/v1/transactions", {"api_version": 1, "wire_hex": wire})
                    error = None if status == 200 else body[:300].decode(errors="replace")
                except Exception as failure:
                    connection.close()
                    connection = connect(ports[name])
                    status, body, error = 0, b"", repr(failure)
                    # A kept-alive connection the node closed while it idled: sent again on the new one, as a
                    # wallet does; the node takes a payment it holds already as known.
                    if isinstance(failure, ConnectionError) and dropped < DROPPED_RETRIES:
                        dropped += 1
                        continue
                if not is_busy(status, body) or busy == BUSY_RETRIES:
                    break
                busy += 1
                time.sleep(retry_after(body))
            # A node that is catching up says to send the payment to another, and a wallet does.
            if SENT_AWAY in body and len(tried) + 1 < len(names):
                put(index, due, (*tried, name), sent)
                continue
            records[index] = {"txid": txid, "node": name, "due": due, "sent": sent, "done": time.time(),
                              "status": status, "error": error, "busy": busy, "sent_away": len(tried)}
            if parent_step and status == 200 and index + parent_step < len(transactions):
                accepted_parents.put(txid)

    threads = [threading.Thread(target=worker, args=(name,), daemon=True)
               for name in names for _ in range(WORKERS_PER_NODE)]
    for thread in threads:
        thread.start()
    start = time.time() + 0.5
    next_report = start + 10
    next_settle = start
    switch = round(slower[0] * rate) if slower else len(transactions)
    for index in range(len(transactions)):
        if index < switch:
            due = start + index / rate
        else:
            due = start + slower[0] + (index - switch) / slower[1]
        wait = due - time.time()
        if wait > 0.002:
            time.sleep(wait)
        if watch and time.time() >= next_settle:
            settle_parents(time.time())
            next_settle = time.time() + 0.1
        parent = transactions[index - parent_step][0] if parent_step and index >= parent_step else None
        if parent is None:
            put(index, due)
        elif parent not in finished:
            held[parent] = (index, due)
        elif finished.pop(parent):
            put(index, due)
        else:
            withhold(index, due, "not sent: its parent failed")
        if time.time() >= next_report:
            done = [record for record in records[:index] if record]
            behind = sum(queue_.qsize() for queue_ in queues.values())
            refused = sum(1 for record in done if record["status"] not in (200, -2))
            withheld = sum(1 for record in done if record["status"] == -2)
            log(f"{label}: {index} queued, {len(done)} answered, {refused} refused, {withheld} withheld, "
                f"{len(held)} wait for their parent, {behind} waiting")
            next_report += 10
    while watch and held:
        settle_parents(time.time())
        time.sleep(0.1)
    if watch:
        watch.stopped.set()
    for name in names:
        for _ in range(WORKERS_PER_NODE):
            queues[name].put(None)
    for thread in threads:
        thread.join()
    # A payment a node sent away at the very end may still wait for another node; it was never offered.
    unsent = sum(1 for record in records if record is None)
    if unsent:
        log(f"{label}: {unsent} were still waiting for another node when the run ended")
    return [record for record in records if record is not None]


def lifecycle(status):
    return str(status.get("lifecycle", "")).lower()


def is_terminal(status):
    """As wallets read it: finalized, or failed with the cut that settled it. A node that merely lost
    track of a payment says dropped, and the payment may still finalize through another node."""
    return lifecycle(status) == "finalized" or (
        lifecycle(status) == "failed" and status.get("finalized_cut") is not None)


def statuses(port, txids):
    connection = connect(port)
    found = {}
    for first in range(0, len(txids), 1024):
        chunk = txids[first:first + 1024]
        for _ in range(BUSY_RETRIES):
            status, body = post(connection, "/v1/transactions/status", {"api_version": 1, "transaction_ids": chunk})
            if not is_busy(status, body):
                break
            time.sleep(retry_after(body))
        if status != 200:
            raise RuntimeError(f"status read failed: {status} {body[:200]!r}")
        reply = json.loads(body)
        for entry in reply["statuses"]:
            found[entry["transaction_id"]] = entry
    connection.close()
    return found


def refusal_of(body):
    try:
        told = json.loads(body)
    except ValueError:
        return "unreadable"
    return told.get("reason") or told.get("message") or "unnamed"


def resend_open(port, transactions, pending, log, budget=240):
    """What a wallet does with a payment the nodes forgot: the same signed bytes again until it is
    final. In the order they were signed, so that a parent goes before what spends from it.
    Returns what each round answered, what ended final and what did not."""
    order = {transactions.entries[index][0]: index for index in range(len(transactions))}
    waiting = sorted(pending, key=order.get)
    result = {"sent_again": len(waiting), "rounds": [], "refused_as": {}, "final": {}, "still_open": []}
    deadline = time.time() + budget
    while waiting and time.time() < deadline:
        # A connection left idle through the wait below would be closed by now, at the cost of the first send.
        connection = connect(port)
        answers = {}
        for txid in waiting:
            wire = transactions[order[txid]][1]
            try:
                status, body = post(connection, "/v1/transactions", {"api_version": 1, "wire_hex": wire})
            except OSError:
                connection = connect(port)
                status, body = 0, b"{}"
            answers[status] = answers.get(status, 0) + 1
            if status not in (0, 200):
                told = refusal_of(body)
                result["refused_as"][told] = result["refused_as"].get(told, 0) + 1
        result["rounds"].append(answers)
        log(f"sent {len(waiting)} open payments again, answers {answers}")
        settled, waiting = wait_final(port, waiting, 30, "sent again", log)
        result["final"].update(settled)
    result["still_open"] = waiting
    if waiting:
        result["still_open_as"] = statuses(port, waiting[:16])
    log(f"open payments: {result['sent_again']} sent again, {len(result['final'])} final, "
        f"{len(waiting)} still open; refused as {result['refused_as']}; "
        f"{json.dumps(result.get('still_open_as', {}))[:600]}")
    return result


def wait_final(port, txids, budget, label, log=print, terminal=is_terminal):
    """Polls until every transaction is finalized or failed; returns the last statuses."""
    pending = list(txids)
    final = {}
    deadline = time.time() + budget
    while pending and time.time() < deadline:
        time.sleep(2)
        try:
            current = statuses(port, pending)
        except OSError as error:
            # The memory guard may have stopped the nodes; what the load window showed is kept.
            log(f"{label}: the status node stopped answering ({error}), {len(pending)} left pending")
            break
        for txid, status in current.items():
            if terminal(status):
                final[txid] = status
        pending = [txid for txid in pending if txid not in final]
        log(f"{label}: {len(final)} final, {len(pending)} pending")
    failed = [txid for txid, status in final.items() if lifecycle(status) == "failed"]
    if failed:
        log(f"{label}: {len(failed)} failed, first {final[failed[0]]}")
    return final, pending
