#!/bin/bash
# Build/test each frontend under its own source and resource boundary.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
GITHUB="$(cd "$HERE/../.." && pwd)"
L=${1:?usage: vendor_frontends.sh LOG_DIR --bin RELEASE_BIN [--work OUT] [--holder NAME] [--jobs N] [--scratch PATH]}
shift
WORK="$L"
HOLDER=""
JOBS=1
SCRATCH=""
BIN=""
while (($#)); do
  case "$1" in
    --work|--holder|--jobs|--scratch|--bin)
      (($# >= 2)) || { echo "missing option value" >&2; exit 2; }
      case "$1" in
        --work) WORK=$2 ;;
        --holder) HOLDER=$2 ;;
        --jobs) JOBS=$2 ;;
        --scratch) SCRATCH=$2 ;;
        --bin) BIN=$2 ;;
      esac
      shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ "$JOBS" =~ ^([1-9]|[1-5][0-9]|6[0-4])$ ]] || { echo "jobs must be 1..64" >&2; exit 2; }
for exe in tenraid tenrai-wallet tenrai-miner; do
  [[ -n "$BIN" && -f "$BIN/$exe.exe" ]] || { echo "--bin must name the release bin folder with $exe.exe" >&2; exit 2; }
done
# Reject repository-local output and keep every attempt's scratch/logs separate.
WORK=$(py -3.14 -E -s -B -c 'import sys; sys.path.insert(0,sys.argv[1]); from release_evidence import artifact_root; print(artifact_root(sys.argv[2]))' "$HERE" "$WORK") || exit 2
SCRATCH=${SCRATCH:-"$WORK/scratch"}
ATTEMPT=$(py -3.14 -E -s -B -c 'from pathlib import Path; import sys,uuid; w=Path(sys.argv[1]).resolve(); p=Path(sys.argv[2]).resolve(); l=Path(sys.argv[3]).resolve(); assert p.is_relative_to(w) and p!=w; assert l.is_relative_to(w); a=p/("front-"+uuid.uuid4().hex); a.mkdir(parents=True,exist_ok=False); print(a)' "$WORK" "$SCRATCH" "$L") || exit 2
mkdir -p "$ATTEMPT/temp" || exit 2
export TEMP="$ATTEMPT/temp" TMP="$ATTEMPT/temp" CARGO_BUILD_JOBS="$JOBS"
failed=0
step() {
  local name=$1 kind=$2
  shift 2
  local -a begin=("$HERE/release_runtime.py" begin --work "$WORK" --name "front-$name" --kind "$kind" --envcheck "$HERE/../qualification/envcheck.py" --commit-gb 8 --room "$SCRATCH=8")
  local -a artifacts=()
  [[ -z "$HOLDER" ]] || begin+=(--holder "$HOLDER")
  while (($#)) && [[ "$1" != -- ]]; do
    (($# >= 2)) || { echo "missing step option value" >&2; return 2; }
    case "$1" in
      --repo|--allow-change) begin+=("$1" "$2") ;;
      --artifact-tree) artifacts+=("$1" "$2") ;;
      *) echo "invalid step option $1" >&2; return 2 ;;
    esac
    shift 2
  done
  [[ ${1:-} == -- ]] || return 2
  shift
  local ticket log code boundary
  log="$ATTEMPT/$name.log"
  if ! ticket=$(py -3.14 -E -s -B "${begin[@]}"); then
    echo "FAIL $name preflight/source boundary" >&2
    failed=$((failed + 1))
    return 1
  fi
  ticket=$(printf '%s\n' "$ticket" | tail -n 1)
  "$@" > "$log" 2>&1
  code=$?
  py -3.14 -E -s -B "$HERE/release_runtime.py" end --work "$WORK" --ticket "$ticket" --exit-code "$code" --artifact "$log" "${artifacts[@]}"
  boundary=$?
  if ((code != 0 || boundary != 0)); then
    echo "FAIL $name native=$code boundary=$boundary log=$log" >&2
    tail -n 12 "$log" >&2
    failed=$((failed + 1))
    return 1
  fi
  echo "PASS $name log=$log"
}
SDK="$GITHUB/tenrai-sdk"
REFWALLET="$GITHUB/tenrai-reference-webwallet"
REFEXPLORER="$GITHUB/tenrai-reference-explorer"
WALLET="$GITHUB/tenrai-webwallet"
MINER="$GITHUB/tenrai-browser-miner"
cd "$SDK" || exit 2
step sdk-build build --repo "$SDK" --artifact-tree "$SDK/dist" -- npm run build || exit "$failed"
step sdk-types test --repo "$SDK" -- npx tsc --noEmit -p .
# The certified SDK and wallet tests replay proofs that a native run of these very binaries captured.
CAPTURE=$(cygpath -w "$ATTEMPT/native-capture")
NATIVE="$CAPTURE\\synthetic-native-public-vectors.json"
node_checkout=${TENRAI_REPO:-}
[ -n "$node_checkout" ] || node_checkout=$(py -3.14 -E -s -B "$HERE/node_checkout.py") || exit 2
CORE=$(cygpath -w "$node_checkout") || exit 2
PYTHON=$(py -3.14 -E -s -B -c "import sys; print(sys.executable)")
step sdk-capture test --repo "$SDK" -- node "$GITHUB/tenrai-tests/browser-wallet/e2e-production.mjs" --mode native --capture \
  --data "$CAPTURE" --repo "$CORE" --sdk "$(cygpath -w "$SDK")" \
  --tenraid "$(cygpath -w "$BIN/tenraid.exe")" --cli "$(cygpath -w "$BIN/tenrai-wallet.exe")" \
  --miner "$(cygpath -w "$BIN/tenrai-miner.exe")" --python "$PYTHON"
step sdk-test test --repo "$SDK" -- env TENRAI_NATIVE_CAPTURE="$NATIVE" npm test
cd "$GITHUB" || exit 2
step vendor-wallet mutation --repo "$SDK" --repo "$REFWALLET" --allow-change "$REFWALLET=vendor/tenrai-sdk" --allow-change "$REFWALLET=node_modules/@tenrai/sdk" -- py -3.14 -E -s -B "$HERE/vendor_sdk.py" tenrai-reference-webwallet || exit "$failed"
step vendor-explorer mutation --repo "$SDK" --repo "$REFEXPLORER" --allow-change "$REFEXPLORER=vendor/tenrai-sdk" --allow-change "$REFEXPLORER=node_modules/@tenrai/sdk" -- py -3.14 -E -s -B "$HERE/vendor_sdk.py" tenrai-reference-explorer || exit "$failed"
cd "$REFWALLET" || exit 2
step refwallet-check test --repo "$REFWALLET" -- npm run check
# TENRAI_REPO also checks the wallet's pinned chain ids against the core genesis files.
step refwallet-test test --repo "$REFWALLET" -- env TENRAI_NATIVE_CAPTURE="$NATIVE" TENRAI_REPO="$CORE" npm test
step refwallet-build build --repo "$REFWALLET" --artifact-tree "$REFWALLET/dist" -- npm run build
cd "$REFEXPLORER" || exit 2
step refexplorer-check test --repo "$REFEXPLORER" -- npm run check
step refexplorer-test test --repo "$REFEXPLORER" -- npm test
step refexplorer-build build --repo "$REFEXPLORER" --artifact-tree "$REFEXPLORER/dist" -- npm run build
cd "$WALLET" || exit 2
step wallet-pin mutation --repo "$WALLET" --allow-change "$WALLET=js/locale-pins.js" --allow-change "$WALLET=js/prover-pins.js" --allow-change "$WALLET=index.html" -- node scripts/pin-assets.mjs || exit "$failed"
step wallet-check test --repo "$WALLET" -- npm run check
step wallet-test test --repo "$WALLET" -- env TENRAI_NATIVE_CAPTURE="$NATIVE" npm test
step wallet-verify test --repo "$WALLET" -- npm run verify:wasm
cd "$MINER" || exit 2
step miner-test test --repo "$MINER" -- npm test
echo "front ends: $failed failed"
exit "$failed"
