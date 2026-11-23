"""Stands in for the native prover: keeps a copy of each prover input it is given, then lets the real
prover answer. Run through tee-prover.cmd as TENRAI_TSP2_PROVER, with TEE_PROVER_REAL naming the real
prover and TEE_PROVER_OUT the folder for the copies (NNN-FORMAT.bin). The inputs hold the secrets of
test accounts only; keep them out of any repository."""
import json
import os
import subprocess
import sys
from pathlib import Path

data = sys.stdin.buffer.read()
out = Path(os.environ["TEE_PROVER_OUT"])
out.mkdir(parents=True, exist_ok=True)
kind = json.loads(data).get("format", "unknown").replace("tenrai-tsp2-", "").replace("-input-1", "")
(out / f"{len(list(out.glob('*.bin'))):03}-{kind}.bin").write_bytes(data)
answer = subprocess.run([os.environ["TEE_PROVER_REAL"]], input=data, capture_output=True)
sys.stdout.buffer.write(answer.stdout)
sys.stderr.buffer.write(answer.stderr)
sys.exit(answer.returncode)
