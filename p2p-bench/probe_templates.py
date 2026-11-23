"""How long a node takes to hand out mining work: py -3.14 probe_templates.py MINING_PORT PAYOUT [SECONDS [TX]]

Asks for a template every 100 ms, as a miner does after each find, and prints every 5 s how many
answers came, how many were busy and the latency (median, p90, max). Run it beside a benchmark to
see the template service under load. TX is the number of transactions to ask for; the default 0
takes none away from the miners.
"""

import http.client
import json
import sys
import time


def main():
    port, payout = int(sys.argv[1]), sys.argv[2]
    seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 60
    body = json.dumps({"payout": payout, "max_transactions": int(sys.argv[4]) if len(sys.argv) > 4 else 0})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    end = time.time() + seconds
    mark = time.time() + 5
    took, busy, failed, with_work = [], 0, 0, 0
    while time.time() < end:
        started = time.perf_counter()
        try:
            connection.request("POST", "/v1/mining/templates", body, {"Content-Type": "application/json"})
            reply = connection.getresponse()
            data = reply.read()
            if reply.status == 200:
                took.append((time.perf_counter() - started) * 1000)
                with_work += len(json.loads(data).get("body_hex", "")) > 2000
            elif reply.status == 503:
                busy += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            connection.close()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        if time.time() >= mark:
            took.sort()
            if took:
                print(f"{time.strftime('%H:%M:%S')} {len(took)} templates ({with_work} with transactions), "
                      f"{busy} busy, {failed} failed, ms median {took[len(took) // 2]:.1f} "
                      f"p90 {took[int(len(took) * 0.9)]:.1f} max {took[-1]:.1f}", flush=True)
            else:
                print(f"{time.strftime('%H:%M:%S')} no template, {busy} busy, {failed} failed", flush=True)
            took, busy, failed, with_work = [], 0, 0, 0
            mark += 5
        time.sleep(max(0.0, 0.1 - (time.perf_counter() - started)))


main()
