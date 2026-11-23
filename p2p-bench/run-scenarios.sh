#!/bin/sh
# Runs scenarios one after another, each into SCENARIO.out: sh run-scenarios.sh main-quic bbr-quic ...
# Set BENCH_DATA2 first when the nodes belong on another disk.
cd "$(dirname "$0")" || exit 1
for scenario in "$@"; do
  echo "== $(date +%H:%M:%S) $scenario"
  py -3.14 -u bench.py "$scenario" > "$scenario.out" 2>&1
  grep -v "pending$" "$scenario.out" | tail -20
  py -3.14 verdict_check.py "$scenario.out"
done
echo "== $(date +%H:%M:%S) done"
