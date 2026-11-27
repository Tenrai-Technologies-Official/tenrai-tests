#!/bin/bash
# The mainnet release set, from the genesis inputs to frozen and checked binaries and packages; README.md describes it.
#   bash build-release.sh --treasury trai1... --fingerprint TRAI-XXXX-XXXX --bonds A,B,C --private-from-genesis yes|no
#                         [--reuse-current-treasury] [--out DIR] [--rehearse] [--from PHASE] [--until PHASE]
#                         [--holder NAME] [--jobs N] [--fail-fast] [--resume]
HERE="$(cd "$(dirname "$0")" && pwd)"
TESTS="$(cd "$HERE/.." && pwd)"
GITHUB="$(cd "$TESTS/.." && pwd)"
node_checkout=${TENRAI_REPO:-}
[ -n "$node_checkout" ] || node_checkout=$(py -3.14 -E -s -B "$HERE/node_checkout.py") || exit 2
REPO=$(cygpath -au "$node_checkout") || exit 2
FILES=$(cygpath -au "${TENRAI_MAINNET_FILES:-$GITHUB/tenrai-mainnet-files}") || exit 2
TENRAI_REPO=$(cygpath -w "$REPO") || exit 2
TENRAI_MAINNET_FILES=$(cygpath -w "$FILES") || exit 2
export TENRAI_REPO TENRAI_MAINNET_FILES
Q="$TESTS/qualification"
PHASES="genesis binaries wasm web desktop checks records"
unset RUST_LOG

treasury='' fingerprint='' bonds='' private='' reuse_current='' out='' rehearse=0 from=genesis until='' holder='' jobs=1
fail_fast=0 resume=0
while [ $# -gt 0 ]; do
  case "$1" in --treasury|--fingerprint|--bonds|--private-from-genesis|--out|--from|--until|--holder|--jobs)
    [ $# -ge 2 ] || { echo "missing value for $1" >&2; exit 2; } ;;
  esac
  case "$1" in
    --treasury) treasury=$2; shift 2 ;;
    --fingerprint) fingerprint=$2; shift 2 ;;
    --bonds) bonds=$2; shift 2 ;;
    --private-from-genesis) private=$2; shift 2 ;;
    --reuse-current-treasury) reuse_current=yes; shift ;;
    --out) out=$2; shift 2 ;;
    --rehearse) rehearse=1; shift ;;
    --from) from=$2; shift 2 ;;
    --until) until=$2; shift 2 ;;
    --holder) holder=$2; shift 2 ;;
    --jobs) jobs=$2; shift 2 ;;
    --fail-fast) fail_fast=1; shift ;;
    --resume) resume=1; shift ;;
    *) echo "unknown argument $1"; exit 2 ;;
  esac
done
case " $PHASES " in *" $from "*) ;; *) echo "--from names no phase of: $PHASES"; exit 2 ;; esac
case " $PHASES " in *" ${until:-records} "*) ;; *) echo "--until names no phase of: $PHASES"; exit 2 ;; esac
[ $resume -eq 0 ] || [ "$from" = checks ] || { echo "--resume goes on with the checks phase: --from checks --resume"; exit 2; }
OUT="${out:-${TENRAI_RELEASE_OUT:-$GITHUB/tenrai-release-artifacts/rc-$(date +%Y%m%d)}}"
OUT="$(cygpath -au "$OUT")" || exit 2
if [ $rehearse -eq 1 ]; then
  [ "$from" = genesis ] || { echo "a rehearsal makes the genesis only; it takes no --from"; exit 2; }
  OUT="$OUT-rehearsal-$(date +%H%M%S)"
fi
L="$OUT/logs"

win() { cygpath -w "$1"; }
say() { echo "$(date +%H:%M:%S) $*" | tee -a "$OUT/release.log"; }
# A stopped checks phase goes on with the checks that passed kept.
goes_on() { [ "$PHASE" != checks ] || printf ' --resume'; }

# Refuse repository destinations before creating a raw log, table or receipt.
py -3.14 -E -s -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from release_evidence import artifact_root; artifact_root(sys.argv[2])' \
  "$(win "$HERE")" "$(win "$OUT")" || exit 2

# A release names the commit it comes from, so it is built from pushed commits only. After its genesis phase the
# release itself may have changed the files it records its identity in.
if [ $rehearse -eq 0 ]; then
  committed=()
  for repo in "$REPO" "$TESTS" "$GITHUB"/tenrai-{pool,reference-pool,explorer,api,webwallet,reference-webwallet,reference-explorer,sdk,paper-wallet,browser-miner,wallet-daemon-client,docs}; do
    committed+=(--repo "$(win "$repo")")
  done
  if [ "$from" != genesis ]; then
    committed+=(--allow-change "$(win "$REPO")=config/mainnet.traidag.json"
      --allow-change "$(win "$REPO")=config/mainnet.genesis-id"
      --allow-change "$(win "$GITHUB/tenrai-api")=README.md"
      --allow-change "$(win "$GITHUB/tenrai-docs")=src/content/docs/en/understand/reference/parameters.md")
  fi
  py -3.14 -E -s -B "$HERE/release_commits.py" "${committed[@]}" || exit 2
fi

# A new folder starts a release with all four inputs; a folder of a stopped release goes on with the inputs it started
# with, and any input given again must be the same.
INPUTS="$OUT/inputs.txt"
if [ -f "$INPUTS" ]; then
  seen=','
  while IFS='=' read -r key value; do
    case "$key" in treasury|fingerprint|bonds|private|reuse_current) ;; *) echo "unknown recorded input key" >&2; exit 2 ;; esac
    case "$seen" in *",$key,"*) echo "duplicate recorded input key" >&2; exit 2 ;; esac
    seen="$seen$key,"
    given=${!key}
    [ -z "$given" ] || [ "$given" = "$value" ] || { echo "--$key $given differs from the input this release started with"; exit 2; }
    printf -v "$key" '%s' "$value"
  done < "$INPUTS"
else
  [ -e "$OUT" ] && { echo "$OUT exists and holds no release; a new release goes into a new folder"; exit 2; }
  [ "$from" = genesis ] || { echo "a release starts with its genesis phase"; exit 2; }
  for value in "$treasury" "$fingerprint" "$bonds" "$private"; do
    [ -n "$value" ] || { echo "the genesis needs --treasury, --fingerprint, --bonds and --private-from-genesis"; exit 2; }
  done
  mkdir -p "$L" "$OUT/genesis" "$OUT/checks"
  reuse_current=${reuse_current:-no}
  printf 'treasury=%s\nfingerprint=%s\nbonds=%s\nprivate=%s\nreuse_current=%s\n' "$treasury" "$fingerprint" "$bonds" "$private" "$reuse_current" > "$INPUTS"
fi
reuse_current=${reuse_current:-no}
case "$private" in yes|no) ;; *) echo "invalid recorded private activation choice"; exit 2 ;; esac
case "$reuse_current" in yes|no) ;; *) echo "invalid recorded treasury reuse choice"; exit 2 ;; esac
case "$jobs" in ''|*[!0-9]*|0) echo "--jobs must be a positive integer"; exit 2 ;; esac
[ "$jobs" -le 64 ] || { echo "--jobs must not exceed 64"; exit 2; }
RUNTIME="$HERE/release_runtime.py"
RESUME="$HERE/release_resume.py"
mkdir -p "$OUT/scratch" || exit 1
# A resumed checks phase goes on in the attempt whose frozen test binary its kept checks ran, so that the checks still
# to run use that binary too; its logs go into a folder of their own.
RESUMED=''
if [ $resume -eq 1 ]; then
  RESUMED=$(py -3.14 -E -s -B "$RESUME" resumable --work "$(win "$OUT")" --phase checks) || exit 2
fi
if [ -n "$RESUMED" ]; then
  SCRATCH="$OUT/scratch/$RESUMED"
  L=$(mktemp -d "$OUT/logs/$RESUMED-resume-XXXXXXXX") || exit 1
else
  SCRATCH=$(mktemp -d "$OUT/scratch/attempt-XXXXXXXX") || exit 1
  L="$OUT/logs/$(basename "$SCRATCH")"
  mkdir -p "$L" || exit 1
fi
mkdir -p "$SCRATCH/tmp" "$SCRATCH/targets" "$SCRATCH/test-cwd" || exit 1
export TMP="$(win "$SCRATCH/tmp")" TEMP="$(win "$SCRATCH/tmp")"
export CARGO_BUILD_JOBS="$jobs" TENRAI_TEST_STORE="$(win "$SCRATCH/frozen-tests")" TENRAI_TEST_CWD="$(win "$SCRATCH/test-cwd")"
CORE_TARGET="$SCRATCH/targets/tests"
PROD_TARGET="$SCRATCH/targets/production"
export CARGO_TARGET_DIR="$(win "$CORE_TARGET")"
STAGE="$SCRATCH/stage"
PREPARED="$(win "$SCRATCH/desktop-prepared")"
DESKTOP_SOURCE="$SCRATCH/desktop-source"
HOLDER=()
[ -z "$holder" ] || HOLDER=(--holder "$holder")
HOLDER_PS=()
[ -z "$holder" ] || HOLDER_PS=(-Holder "$holder")
owned_archive() { py -3.14 -E -s -B "$RUNTIME" archive --work "$(win "$OUT")" --path "$(win "$1")"; }

GENESIS_ARGS=(--treasury "$treasury" --fingerprint "$fingerprint" --bonds "$bonds" --private-from-genesis "$private")
[ "$reuse_current" = no ] || GENESIS_ARGS+=(--reuse-current-treasury)

# Each invocation gets a fresh resource admission and an immutable source boundary.
gate() {
  local commit=$1 rooms=() room
  shift
  for room in "$@"; do rooms+=(--room "$room"); done
  py -3.14 -E -s -B "$Q/envcheck.py" --commit-gb "$commit" "${HOLDER[@]}" "${rooms[@]}"
}
boundary_begin() {
  local name=$1 kind=build args=(--repo "$(win "$TESTS")") repo
  case "$name" in
    web-frontends) kind=setup ;;
    genesis-install) kind=mutation; args+=(--repo "$(win "$REPO")"
      --allow-change "$(win "$REPO")=config/mainnet.traidag.json"
      --allow-change "$(win "$REPO")=config/mainnet.genesis-id") ;;
    records-identity) kind=mutation; args+=(--repo "$(win "$REPO")" --repo "$(win "$GITHUB/tenrai-api")" --repo "$(win "$GITHUB/tenrai-docs")"
      --allow-change "$(win "$GITHUB/tenrai-api")=README.md"
      --allow-change "$(win "$GITHUB/tenrai-docs")=src/content/docs/en/understand/reference/parameters.md") ;;
    desktop-source) kind=setup; args+=(--repo "$(win "$GITHUB/tenrai-webwallet")") ;;
    desktop-dependencies|desktop-test-build|desktop-release-build|check-desktop-e2e)
      args+=(--repo "$(win "$GITHUB/tenrai-webwallet")" --repo "$(win "$DESKTOP_SOURCE")") ;;
    binaries-tenrai-reference-pool) args+=(--repo "$(win "$REPO")" --repo "$(win "$GITHUB/tenrai-reference-pool")") ;;
    binaries-tenrai-pool) args+=(--repo "$(win "$REPO")" --repo "$(win "$GITHUB/tenrai-pool")") ;;
    binaries-tenrai-explorer) args+=(--repo "$(win "$REPO")" --repo "$(win "$GITHUB/tenrai-explorer")") ;;
    binaries-tenrai-api) args+=(--repo "$(win "$REPO")" --repo "$(win "$GITHUB/tenrai-api")") ;;
    binaries-copies) args+=(--repo "$(win "$REPO")" --repo "$(win "$GITHUB/tenrai-api")" --repo "$(win "$GITHUB/tenrai-pool")"
      --repo "$(win "$GITHUB/tenrai-reference-pool")") ;;
    # The paper wallet's build embeds the release's WASM, its fonts and its logo in these four files.
    web-paper-wallet) kind=mutation; args+=(--repo "$(win "$GITHUB/tenrai-paper-wallet")"
      --allow-change "$(win "$GITHUB/tenrai-paper-wallet")=js/tenrai-wasm.js"
      --allow-change "$(win "$GITHUB/tenrai-paper-wallet")=js/tenrai-wasm-bytes.js"
      --allow-change "$(win "$GITHUB/tenrai-paper-wallet")=css/fonts.css"
      --allow-change "$(win "$GITHUB/tenrai-paper-wallet")=js/logo.js") ;;
    web-artifacts)
      for repo in tenrai-sdk tenrai-webwallet tenrai-reference-webwallet tenrai-reference-explorer tenrai-paper-wallet tenrai-explorer tenrai-browser-miner; do args+=(--repo "$(win "$GITHUB/$repo")"); done ;;
    check-live-*|check-browser-rounds)
      args+=(--repo "$(win "$REPO")")
      for repo in tenrai-sdk tenrai-webwallet tenrai-reference-webwallet tenrai-reference-explorer tenrai-explorer tenrai-api tenrai-pool tenrai-reference-pool; do args+=(--repo "$(win "$GITHUB/$repo")"); done ;;
    *) args+=(--repo "$(win "$REPO")") ;;
  esac
  case "$name" in genesis-tools|genesis-manifest|genesis-check|genesis-identity|desktop-prepare|wasm-packages) kind=setup ;; esac
  py -3.14 -E -s -B "$RUNTIME" begin --work "$(win "$OUT")" --name "$name" --kind "$kind" \
    --envcheck "$(win "$Q/envcheck.py")" --commit-gb 8 --room "$(win "$OUT")=12" "${HOLDER[@]}" "${args[@]}"
}
boundary_end() {
  local name=$1 ticket=$2 code=$3 args=() exe
  case "$name" in
    genesis-tools) [ "$code" -ne 0 ] || args+=(--cargo-log "$(win "$L/$name.log")" --cargo-artifact "$(win "$PROD_TARGET/release/tenraid.exe")" --cargo-artifact "$(win "$PROD_TARGET/release/tenrai-wallet.exe")") ;;
    binaries-workspace)
      if [ "$code" -eq 0 ]; then
        args+=(--cargo-log "$(win "$L/$name.log")")
        for exe in tenraid tenrai-release tenrai-wallet tenrai-miner tenrai-stratum-bridge; do args+=(--cargo-artifact "$(win "$STAGE/$exe.exe")"); done
      fi ;;
    binaries-tenrai-*) [ "$code" -ne 0 ] || args+=(--cargo-log "$(win "$L/$name.log")" --cargo-artifact "$(win "$STAGE/${name#binaries-}.exe")") ;;
    checks-node-build)
      exe=$(grep -a "Executable unittests src" "$L/$name.log" | tail -n 1 | sed 's/.*(\(.*\))/\1/' | tr '\\' '/')
      case "$exe" in [A-Za-z]:/*|/*) ;; *) exe="$REPO/$exe" ;; esac
      [ "$code" -ne 0 ] || args+=(--cargo-artifact "$(win "$exe")") ;;
    desktop-test-build) [ "$code" -ne 0 ] || args+=(--cargo-artifact "$(win "$SCRATCH/desktop-test.exe")") ;;
    desktop-release-build) [ "$code" -ne 0 ] || args+=(--cargo-artifact "$(win "$SCRATCH/desktop-release.exe")") ;;
    web-paper-wasm) [ "$code" -ne 0 ] || args+=(--artifact-tree "$(win "$SCRATCH/wasm-no-modules")") ;;
    web-artifacts) [ "$code" -ne 0 ] || args+=(--artifact-tree "$(win "$OUT/web")" --artifact-tree "$(win "$OUT/sdk")") ;;
    check-*) args+=(--log "$(win "$L/$name.log")") ;;
  esac
  py -3.14 -E -s -B "$RUNTIME" end --work "$(win "$OUT")" --ticket "$ticket" --exit-code "$code" "${args[@]}"
}
step() {
  local name=$1 code admitted ticket
  shift
  case "$name" in records-release|records-sources|records-files)
    "$@" > "$L/$name.log" 2>&1; code=$? ;;
  *)
    ticket=$(boundary_begin "$name" 2>"$L/$name.preflight.log") || {
      cat "$L/$name.preflight.log" > "$L/$name.log"; say "$name: NOT TESTED"; return 1; }
    "$@" > "$L/$name.log" 2>&1; code=$?
    boundary_end "$name" "$ticket" "$code" > "$L/$name.source.log" 2>&1; admitted=$?
    [ "$code" -ne 0 ] || code=$admitted ;;
  esac
  say "$name: exit $code"
  [ "$code" -eq 0 ] || { grep -avE "^\s*$" "$L/$name.log" | tail -n 12 | cut -c1-200; return 1; }
}
check() {
  local name=$1 code admitted ticket
  shift
  # A resumed phase holds only the rows whose results still belong to this build (release_resume.py keep).
  if [ $resume -eq 1 ] && awk -F'\t' -v phase="$PHASE" -v name="$name" '$1 == phase && $2 == name && $3 == 0' \
      "$OUT/checks.tsv" | grep -q .; then
    say "check $name: kept, its result still belongs to this build"
    return 0
  fi
  if ticket=$(boundary_begin "check-$name" 2>"$L/check-$name.preflight.log"); then
    "$@" > "$L/check-$name.log" 2>&1; code=$?
    boundary_end "check-$name" "$ticket" "$code" > "$L/check-$name.source.log" 2>&1; admitted=$?
    [ "$code" -ne 0 ] || code=$admitted
  else cat "$L/check-$name.preflight.log" > "$L/check-$name.log"; code=3
  fi
  printf '%s\t%s\t%s\t%s\n' "$PHASE" "$name" "$code" "$(win "$L/check-$name.log")" >> "$OUT/checks.tsv"
  say "check $name: exit $code"
  [ "$code" -eq 0 ] || grep -aE "FAIL|panicked|failed|error|refused" "$L/check-$name.log" | head -n 8 | cut -c1-200
  if [ "$code" -ne 0 ] && [ "$fail_fast" -eq 1 ]; then
    say "release stopped at check $name of phase $PHASE (--fail-fast); after the fix it goes on with --from $PHASE$(goes_on)"
    exit 1
  fi
  return 0
}
no_warnings() {
  py -3.14 -E -s -B -c '
import json,sys
warnings=[]
for line in open(sys.argv[1],encoding="utf-8",errors="replace"):
    if line.startswith(("warning:", "warning[")): warnings.append(line)
    try: value=json.loads(line)
    except ValueError: continue
    if value.get("reason")=="compiler-message" and value["message"].get("level")=="warning":
        warnings.append(value["message"].get("rendered",value["message"]["message"]))
if warnings: print("".join(warnings[:10])); print(str(len(warnings))+" compiler warnings")
raise SystemExit(bool(warnings))
' "$1"
}
freeze() {
  py -3.14 -E -s -B -c "
import sys
sys.path.insert(0, sys.argv[3])
from pathlib import Path
import preflight
sources = [Path(path) for path in sys.argv[4:]] or sorted(Path(sys.argv[1]).glob('*.exe'))
manifest = preflight.freeze({path.name: path for path in sources}, Path(sys.argv[2]))
for name, digest in manifest['files'].items():
    print(digest[:16], name)
" "$(win "$1")" "$(win "$2")" "$(win "$TESTS/p2p-bench")" "${@:3}"
}
copytree() {
  py -3.14 -E -s -B -c "
import shutil, sys
shutil.copytree(sys.argv[1], sys.argv[2], ignore=shutil.ignore_patterns(*sys.argv[3:]))
" "$(win "$1")" "$(win "$2")" "${@:3}"
}

# The genesis: made from the mainnet files, checked, its identities computed by the node of this tree, then installed
# into the node's config/ and the mainnet files. A rehearsal stops before the install.
production_bin() {
  local package=$1 binary=$2
  gate 8 "$(win "$OUT")=12" || return 1
  (cd "$REPO" && CARGO_TARGET_DIR="$(win "$PROD_TARGET")" timeout 3600 \
    cargo build --release --locked -p "$package" --bin "$binary" -j "$jobs" --message-format=json)
}
genesis_tools() {
  production_bin tenraid tenraid || return 1
  production_bin tenrai-wallet-cli tenrai-wallet
}
phase_genesis() {
  py -3.14 -E -s -B "$HERE/release_genesis.py" inputs "${GENESIS_ARGS[@]}" | tee -a "$OUT/release.log"
  [ "${PIPESTATUS[0]}" -eq 0 ] || return 1
  gate 6 "$(win "$OUT")=12" || return 1
  step genesis-tools genesis_tools || return 1
  step genesis-manifest py -3.14 -E -s -B "$FILES/genesis/make-mainnet-manifest.py" "$(win "$OUT/genesis/mainnet.traidag.json")" \
    --bonds "$bonds" --treasury "$treasury" || return 1
  step genesis-check py -3.14 -E -s -B "$FILES/genesis/check-mainnet-manifest.py" "$(win "$OUT/genesis/mainnet.traidag.json")" || return 1
  cat "$L/genesis-check.log" | tee -a "$OUT/release.log"
  step genesis-identity py -3.14 -E -s -B "$HERE/release_genesis.py" identity --work "$(win "$OUT")" \
    --tenraid "$(win "$PROD_TARGET/release/tenraid.exe")" --wallet "$(win "$PROD_TARGET/release/tenrai-wallet.exe")" \
    "${GENESIS_ARGS[@]}" || return 1
  cat "$L/genesis-identity.log" | tee -a "$OUT/release.log"
  if [ $rehearse -eq 1 ]; then
    say "rehearsal over: nothing written outside $OUT"
    exit 0
  fi
  step genesis-install py -3.14 -E -s -B "$HERE/release_genesis.py" install --work "$(win "$OUT")" || return 1
  cat "$L/genesis-install.log" | tee -a "$OUT/release.log"
}

# Every binary from this one source state, frozen read-only into bin/ with its SHA-256.
copies() {
  local crate asset pool source differ=0
  for crate in auth client-api metrics platform primitives wire; do
    diff -rq --exclude=target "$REPO/crates/$crate" "$GITHUB/tenrai-api/crates/$crate" || differ=1
  done
  for asset in protocol/test-vectors/tsp2-key-derivation.json; do
    cmp "$REPO/$asset" "$GITHUB/tenrai-api/$asset" || differ=1
  done
  # Both pools hash with their own copy of the TenQ engine; a mainnet share must be the node's value.
  for pool in tenrai-pool tenrai-reference-pool; do
    for source in tenq.rs tenq_templates.rs; do
      diff -q --strip-trailing-cr "$REPO/crates/tenq/src/$source" "$GITHUB/$pool/src/pow/$source" || differ=1
    done
    diff -q --strip-trailing-cr "$REPO/crates/tenq/vectors/tenq_vectors.json" \
      "$GITHUB/$pool/tests/vectors/tenq_vectors.json" || differ=1
  done
  return $differ
}
workspace() {
  local pair package binary
  mkdir -p "$STAGE" || return 1
  for pair in tenraid:tenraid tenrai-release:tenrai-release tenrai-wallet-cli:tenrai-wallet tenrai-miner:tenrai-miner tenrai-stratum-bridge:tenrai-stratum-bridge; do
    package=${pair%%:*}; binary=${pair#*:}
    production_bin "$package" "$binary" || return 1
    cp "$PROD_TARGET/release/$binary.exe" "$STAGE/$binary.exe" || return 1
    cp "$PROD_TARGET/release/$binary.d" "$STAGE/$binary.d" || return 1
  done
  no_warnings "$L/binaries-workspace.log"
}
other() {
  local name=$1 dir=$2 exe=$3
  shift 3
  (cd "$dir" && env "$@" CARGO_TARGET_DIR="$(win "$PROD_TARGET")" timeout 3600 cargo build --release --locked --bin "$(basename "$exe" .exe)" -j "$jobs" --message-format=json) && no_warnings "$L/binaries-$name.log" || return 1
  cp "$PROD_TARGET/release/$(basename "$exe")" "$STAGE/$name.exe" || return 1
  cp "$PROD_TARGET/release/$(basename "$exe" .exe).d" "$STAGE/$name.d" || return 1
}
phase_binaries() {
  gate 6 "$(win "$OUT")=12" || return 1
  step binaries-copies copies || return 1
  step binaries-workspace workspace || return 1
  step binaries-built-in py -3.14 -E -s -B "$HERE/release_genesis.py" built-in --work "$(win "$OUT")" \
    --tenraid "$(win "$PROD_TARGET/release/tenraid.exe")" "${HOLDER[@]}" || return 1
  mkdir -p "$STAGE" || return 1
  step binaries-tenrai-reference-pool other tenrai-reference-pool "$GITHUB/tenrai-reference-pool" \
    "$GITHUB/tenrai-reference-pool/target/release/tenrai-pool.exe" || return 1
  step binaries-tenrai-pool other tenrai-pool "$GITHUB/tenrai-pool" "$GITHUB/tenrai-pool/target/release/tenrai-pool.exe" || return 1
  step binaries-tenrai-explorer other tenrai-explorer "$GITHUB/tenrai-explorer" \
    "$SCRATCH/targets/tenrai-explorer/release/tenrai-explorer.exe" || return 1
  step binaries-tenrai-api other tenrai-api "$GITHUB/tenrai-api" "$GITHUB/tenrai-api/target/release/tenrai-api.exe" || return 1
  local prover="$REPO/crates/tsp2/prover"
  if [[ ! -f "$prover/Cargo.toml" ]]; then
    prover="$REPO/crates/tsp2-prover"
  fi
  step binaries-tenrai-tsp2-prove other tenrai-tsp2-prove "$prover" \
    "$prover/target/release/tenrai-tsp2-prove.exe" || return 1
  owned_archive "$OUT/bin" || return 1
  step binaries-freeze freeze "$STAGE" "$OUT/bin" || return 1
  cat "$L/binaries-freeze.log" | tee -a "$OUT/release.log"
}

# The browser packages from the same tree, each with its source inventory, vendored where they are used.
phase_wasm() {
  gate 8 "$(win "$OUT")=12" || return 1
  step wasm-packages powershell -NoProfile -ExecutionPolicy Bypass -File "$(win "$HERE/rebuild_wasm.ps1")" \
    -Out "$(win "$OUT/wasm")" -Work "$(win "$OUT")" -Jobs "$jobs" -Scratch "$(win "$SCRATCH/wasm")" "${HOLDER_PS[@]}" || return 1
  grep -aE "revision|built|verified|done" "$L/wasm-packages.log" | cut -c1-200 | tee -a "$OUT/release.log"
}

# The web apps take the packages, build and test; the paper wallet embeds a no-modules wallet WASM. Like
# scripts/package-wasm.ps1 its build names the dependency sources and the toolchain by neutral roots, so no path of the
# building machine ships in it; the paper wallet refuses a WASM that still names one.
paper_wasm() {
  local cargo_home sysroot pair source destination form flags=() seen=" "
  cargo_home=$(win "${CARGO_HOME:-$HOME/.cargo}")
  sysroot=$(cd "$REPO" && rustc --print sysroot | tr -d '\r') || return 1
  for pair in "$cargo_home=/cargo" "$sysroot=/rust-sysroot" "$sysroot\\lib\\rustlib\\src\\rust=/rust"; do
    source=${pair%=*}
    destination=${pair##*=}
    for form in "$source" "${source//\\//}"; do
      case "$seen" in *" $form "*) continue ;; esac
      seen="$seen$form "
      flags+=("--remap-path-prefix=$form=$destination")
    done
  done
  (cd "$REPO" && CARGO_ENCODED_RUSTFLAGS="$(IFS=$'\x1f'; echo "${flags[*]}")" \
    CARGO_TARGET_DIR="$(win "$SCRATCH/targets/paper-wasm")" timeout 3600 wasm-pack build crates/wasm --target no-modules \
    --release --out-dir "$(win "$SCRATCH/wasm-no-modules")" --out-name tenrai_wasm)
}
paper_wallet() { (cd "$GITHUB/tenrai-paper-wallet" && node scripts/build.mjs "$(win "$SCRATCH/wasm-no-modules")" && npm test); }
# A web app goes out as its public sources, never as its working folder: ignored local files such as a build cache or
# a helper script stay behind.
public_tree() {
  local repo=$1 destination=$2 copy
  shift 2
  copy="$SCRATCH/public-$(basename "$repo")"
  py -3.14 -E -s -B "$RUNTIME" copy --work "$(win "$OUT")" --repo "$(win "$repo")" --destination "$(win "$copy")" &&
    copytree "$copy" "$destination" "$@"
}
web_artifacts() {
  owned_archive "$OUT/web" && owned_archive "$OUT/sdk" || return 1
  mkdir -p "$OUT/web" "$OUT/sdk" || return 1
  public_tree "$GITHUB/tenrai-webwallet" "$OUT/web/webwallet" node_modules tests scripts desktop '.git*' '*.log' || return 1
  for app in tenrai-reference-webwallet tenrai-reference-explorer; do
    mkdir -p "$OUT/web/$app" && cp -r "$GITHUB/$app/dist" "$GITHUB/$app/server" "$GITHUB/$app/package.json" "$OUT/web/$app/" \
      || return 1
  done
  public_tree "$GITHUB/tenrai-paper-wallet" "$OUT/web/paper-wallet" node_modules tests scripts '.git*' '*.log' 'package*.json' \
    || return 1
  mkdir -p "$OUT/web/explorer" && cp -r "$GITHUB/tenrai-explorer/index.html" "$GITHUB/tenrai-explorer/assets" "$OUT/web/explorer/" \
    || return 1
  public_tree "$GITHUB/tenrai-browser-miner" "$OUT/web/browser-miner" node_modules tests scripts '.git*' '*.log' || return 1
  (cd "$GITHUB/tenrai-sdk" && npm pack --pack-destination "$(win "$OUT/sdk")")
}
phase_web() {
  gate 6 "$(win "$OUT")=12" || return 1
  step web-frontends bash "$HERE/vendor_frontends.sh" "$L/web" --work "$(win "$OUT")" --jobs "$jobs" --bin "$OUT/bin" \
    --scratch "$(win "$SCRATCH/frontends")" "${HOLDER[@]}" || return 1
  step web-paper-wasm paper_wasm || return 1
  step web-paper-wallet paper_wallet || return 1
  step web-artifacts web_artifacts || return 1
}

# The desktop app: built once on a test chain and driven through its end-to-end test, then built with its default
# settings for the release and frozen.
desktop_prepare() {
  (cd "$TESTS/browser-wallet" && node e2e-desktop.mjs prepare --repo "$(win "$REPO")" \
    --wasm "$(win "$GITHUB/tenrai-webwallet/vendor/tenrai-wasm")" --out "$PREPARED")
}
desktop_source() { py -3.14 -E -s -B "$RUNTIME" copy --work "$(win "$OUT")" --repo "$(win "$GITHUB/tenrai-webwallet")" --destination "$(win "$DESKTOP_SOURCE")"; }
desktop_dependencies() { (cd "$DESKTOP_SOURCE/desktop" && npm ci --no-audit --no-fund); }
desktop_build() {
  local mode=$1
  shift
  (cd "$DESKTOP_SOURCE/desktop" && CARGO_TARGET_DIR="$(win "$DESKTOP_SOURCE/desktop/target")" timeout 3600 node build.mjs --no-linux "$@" \
    --prover "$(win "$OUT/bin/tenrai-tsp2-prove.exe")") || return 1
  cp "$DESKTOP_SOURCE/desktop/target/release/tenrai-wallet-desktop.exe" "$SCRATCH/desktop-$mode.exe" || return 1
  if [ -f "$DESKTOP_SOURCE/desktop/target/release/tenrai-wallet-desktop.d" ]; then
    cp "$DESKTOP_SOURCE/desktop/target/release/tenrai-wallet-desktop.d" "$SCRATCH/desktop-$mode.d" || return 1
  fi
}
desktop_run() {
  (cd "$TESTS/browser-wallet" && timeout 1800 node e2e-desktop.mjs run --tenraid "$(win "$OUT/bin/tenraid.exe")" \
    --app "$(win "$SCRATCH/desktop-test.exe")" --prepared "$PREPARED" \
    --data "$(win "$SCRATCH/desktop-e2e")" --shots "$(win "$OUT/checks/desktop-shots")" --out "$(win "$OUT/checks/desktop.json")")
}
phase_desktop() {
  gate 8 "$(win "$OUT")=12" || return 1
  step desktop-prepare desktop_prepare || return 1
  step desktop-source desktop_source || return 1
  step desktop-dependencies desktop_dependencies || return 1
  step desktop-test-build desktop_build test --config "$PREPARED\\config.json" --test-genesis "$PREPARED\\genesis.json" || return 1
  check desktop-e2e desktop_run
  step desktop-release-build desktop_build release || return 1
  local version
  version=$(py -3.14 -E -s -B -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$(win "$GITHUB/tenrai-webwallet/package.json")")
  owned_archive "$OUT/desktop" || return 1
  step desktop-freeze freeze "$STAGE" "$OUT/desktop" \
    "$(win "$DESKTOP_SOURCE/desktop/dist/tenrai-wallet-desktop-$version-windows-x64.exe")" || return 1
  cat "$L/desktop-freeze.log" | tee -a "$OUT/release.log"
}

# The checks on the new genesis: the node suite and the acceptance tests on a frozen test binary, the other crates,
# clippy, the live integration with both pools and the browser rounds on the frozen release binaries.
node_tests_build() {
  (cd "$REPO" && CARGO_TARGET_DIR="$(win "$CORE_TARGET")" timeout 3600 \
    cargo test --release -p tenrai-node --lib --no-run --locked -j "$jobs")
}
# A resumed attempt froze its test binary before; its build must then have given exactly the frozen bytes.
node_freeze() {
  if [ -n "$RESUMED" ]; then
    py -3.14 -E -s -B "$RESUME" frozen --store "$TENRAI_TEST_STORE" --name "$1" --binary "$2"
    return
  fi
  (cd "$Q" && py -3.14 -E -s -B testrun.py freeze "$1" "$2")
}
testrun() {
  local name=$1 filter=$2
  shift 2
  (cd "$Q" && py -3.14 -E -s -B testrun.py run "$name" "$filter" --out "$(win "$OUT/checks/filters")" --envcheck "$(win "$Q/envcheck.py")" "${HOLDER[@]}" "$@")
}
# The mainnet profile of TenQ against its vectors and its light against its full path, on 256 MiB caches and the
# 2 GiB dataset; ignored in an ordinary test run for their size.
tenq_production() {
  local log="$SCRATCH/tenq-production.log" code
  (cd "$REPO" && CARGO_TARGET_DIR="$(win "$CORE_TARGET")" timeout 3600 cargo test --release -p tenrai-tenq --lib --locked \
    -j "$jobs" -- --ignored --exact --test-threads=1 vectors::the_production_profile_matches_the_vector_file \
    differential::the_two_paths_agree_on_the_production_profile) > "$log" 2>&1
  code=$?
  cat "$log"
  # A filter that names no test passes with none run; both must have run.
  [ "$code" -eq 0 ] && grep -q "test result: ok. 2 passed" "$log"
}
crate_tests() {
  (cd "$REPO" && CARGO_TARGET_DIR="$(win "$CORE_TARGET")" timeout 3600 \
    cargo test --release --workspace --exclude tenrai-node --locked -j "$jobs")
}
clippy_all() { (cd "$REPO" && CARGO_TARGET_DIR="$(win "$CORE_TARGET")" timeout 3600 cargo clippy --release --workspace --all-targets --locked -j "$jobs" -- -D warnings); }
live() {
  (cd "$TESTS/browser-wallet" && timeout 2700 node e2e-live.mjs --bin "$(win "$OUT/bin")" \
    --bridge "$(win "$OUT/bin/tenrai-stratum-bridge.exe")" --pool "$(win "$OUT/bin/$1.exe")" \
    --explorer "$(win "$OUT/bin/tenrai-explorer.exe")" --explorer-root "$(win "$GITHUB/tenrai-explorer")" \
    --repo "$(win "$REPO")" --data "$(win "$SCRATCH/live-$1")" --out "$(win "$OUT/checks/live-$1.json")")
}
private_cross_pool_upgrade() (
  unset TENRAI_PRIVATE_FROM_GENESIS
  testrun "$@"
)
# A test in phases: each phase is a test run of its own within the hour, until the test writes down that it is done.
# The first phase that fails ends it; the stores of a test that passed go, those of one that failed stay.
phases() {
  local binary=$1 room=$2 own=$3 most=$4 test=$5 part=1 code began dir="$SCRATCH/phases"
  shift 5
  while [ "$part" -le "$most" ]; do
    began=$(date +%s)
    testrun "$binary" "$test" --expect "$test" --env "TENRAI_TEST_PHASE_DIR=$(win "$dir")" \
      --env TENRAI_TEST_PORTS=38800-38999 --timeout 3600 --room "$room" "$@"
    code=$?
    echo "phase $part of $own: exit $code after $(( $(date +%s) - began )) s"
    [ "$code" -eq 0 ] || return "$code"
    if grep -q '"stage": "done"' "$dir/$own/phase.json" 2>/dev/null; then
      rm -rf "${dir:?}/$own"
      return 0
    fi
    part=$((part + 1))
  done
  echo "$own: no end after $most phases"
  return 1
}
# The full source of the long offline recovery goes on from the phases it shares with the pruned one.
offline_from_full() {
  grep -q '"stage": "back"' "$SCRATCH/phases/private-long-offline/phase.json" 2>/dev/null ||
    { echo "not run: the phases shared with the pruned source did not finish"; return 3; }
  phases "$1" "$2" private-long-offline-full "${@:3}" && rm -rf "$SCRATCH/phases/private-long-offline"
}
phase_checks() {
  export TENRAI_TSP2_PROVER="$(win "$OUT/bin/tenrai-tsp2-prove.exe")"
  gate 8 "$(win "$OUT")=12" || return 1
  check mainnet-identities py -3.14 -E -s -B "$HERE/mainnet_identities.py" --sealed \
    --wallet "$(win "$OUT/bin/tenrai-wallet.exe")" --tenraid "$(win "$OUT/bin/tenraid.exe")"
  step checks-node-build node_tests_build || return 1
  local exe name="release-$(basename "$SCRATCH")" room="$(win "$OUT")=12"
  exe=$(grep -a "Executable unittests src" "$L/checks-node-build.log" | tail -n 1 | sed 's/.*(\(.*\))/\1/' | tr '\\' '/')
  case "$exe" in [A-Za-z]:/*|/*) ;; *) exe="$REPO/$exe" ;; esac
  step checks-node-freeze node_freeze "$name" "$(win "$exe")" || return 1
  # With private transfers from genesis, the private tests among these take the path of the mainnet.
  local genesis_env=()
  [ "$private" = yes ] && genesis_env=(--env TENRAI_PRIVATE_FROM_GENESIS=1)
  # The checks that ran least come first, so that a failure among them stops the release early; the phased
  # recoveries and package starts, over four hours together, come last of the acceptance checks.
  local acceptance=p2p::validator_overlay::decentralized_finality_tests
  check decentralized-finality testrun "$name" decentralized_finality_tests --min-tests 7 \
    --expect p2p::validator_overlay::decentralized_finality_tests::three_validators_with_the_mainnet_split_wait_only_for_the_anchor \
    --shards 4 --weight conflicting_spends::wan=30 --weight relaunch_tests=20 \
    --weight four_independent_validators_hold=12 "${genesis_env[@]}" --timeout 3600 --room "$room" -- \
    --skip started_from_a_package_carries_the_quorum --skip full_nodes_alone_carry_the_network \
    --skip three_full_nodes_alone_stop --skip private_long_offline::an_existing
  check finality-simulation testrun "$name" finality::simulation_tests --timeout 3600 --room "$room"
  check many-stakers testrun "$name" many_stakers_tests --timeout 3600 --room "$room"
  check private-cross-pool private_cross_pool_upgrade "$name" chain::private_tests::validators::cross_pool:: --min-tests 2 \
    --expect chain::private_tests::validators::cross_pool::a_finalized_enter_evicts_normal_payments_and_bonds_and_releases_their_leases \
    --expect chain::private_tests::validators::cross_pool::executed_normal_work_filters_and_evicts_a_conflicting_enter_on_every_node \
    --timeout 3600 --room "$room"
  if [ "$private" = yes ]; then
    # The two long offline recoveries run below, in phases of their own.
    check private-from-genesis testrun "$name" private --env TENRAI_PRIVATE_FROM_GENESIS=1 --min-tests 40 \
      --expect chain::private_tests::validators::cross_pool::a_finalized_enter_evicts_normal_payments_and_bonds_and_releases_their_leases \
      --expect chain::private_tests::validators::cross_pool::executed_normal_work_filters_and_evicts_a_conflicting_enter_on_every_node \
      --timeout 3600 --room "$room" -- --skip private_transfers_on_disk_measured --skip private_load \
      --skip private_long_offline
  fi
  check full-nodes-alone testrun "$name" "$acceptance::full_nodes_alone::full_nodes_alone_carry_the_network" \
    --expect "$acceptance::full_nodes_alone::full_nodes_alone_carry_the_network_and_a_validator_comes_back_after_they_let_go_of_its_height" \
    "${genesis_env[@]}" --timeout 3600 --room "$room"
  check three-full-nodes-alone testrun "$name" "$acceptance::full_nodes_alone::three_full_nodes_alone_stop" \
    --expect "$acceptance::full_nodes_alone::three_full_nodes_alone_stop_for_a_restart_and_go_on" \
    "${genesis_env[@]}" --timeout 3600 --room "$room"
  check node-suite testrun "$name" "" --no-ignored --threads "$jobs" --timeout 3600 --room "$room"
  # Run recovery and snapshot checks in phases to stay within the per-test timeout.
  local offline="$acceptance::full_nodes_alone::private_long_offline::an_existing_full_node_recovers_private_money_from"
  check private-long-offline-pruned phases "$name" "$room" private-long-offline-pruned 8 \
    "${offline}_pruned_after_all_providers_forget_its_history" "${genesis_env[@]}"
  check private-long-offline-full offline_from_full "$name" "$room" 3 \
    "${offline}_full_after_all_providers_forget_its_history" "${genesis_env[@]}"
  check package-join phases "$name" "$room" package-join 3 \
    "$acceptance::package_join::a_validator_started_from_a_package_carries_the_quorum" "${genesis_env[@]}"
  check package-join-pruned phases "$name" "$room" package-join-pruned 3 \
    "$acceptance::package_join::a_validator_on_pruned_storage_started_from_a_package_carries_the_quorum" "${genesis_env[@]}"
  gate 8 "$(win "$OUT")=12" || return 1
  check tenq-production tenq_production
  check crate-tests crate_tests
  check clippy clippy_all
  check live-tenrai-reference-pool live tenrai-reference-pool
  check live-tenrai-pool live tenrai-pool
  check browser-rounds powershell -NoProfile -ExecutionPolicy Bypass -File "$(win "$HERE/browser_rounds.ps1")" \
    -Bin "$(win "$OUT/bin")" -OutDir "$(win "$OUT/checks/browser")" -Work "$(win "$OUT")" -Jobs "$jobs" -Scratch "$(win "$SCRATCH/browser")" "${HOLDER_PS[@]}"
}

# The records: the documents that state the mainnet identity, the release's config and guides, its source state and
# RELEASE.json.
release_files() {
  owned_archive "$OUT/config" && owned_archive "$OUT/docs" || return 1
  mkdir -p "$OUT/config" "$OUT/docs/run" "$OUT/docs/qualification" || return 1
  cp -f "$REPO/config/mainnet.chain-id" "$REPO/config/mainnet.genesis-id" "$REPO/config/mainnet.traidag.json" \
    "$REPO/config/node.example.json" "$OUT/config/" || return 1
  cp -f "$GITHUB/tenrai-pool/pool.example.json" "$OUT/config/pool.example.json" || return 1
  cp -f "$GITHUB/tenrai-reference-pool/pool.example.json" "$OUT/config/reference-pool.example.json" || return 1
  cp -r "$GITHUB/tenrai-docs/src/content/docs/en/run/." "$OUT/docs/run/" || return 1
  cp -f "$REPO/docs/security/committee-and-stake.md" "$OUT/docs/qualification/"
}
sources() {
  local args=(--repo "$(win "$REPO")" --repo "$(win "$TESTS")") repo
  for repo in tenrai-pool tenrai-reference-pool tenrai-explorer tenrai-api tenrai-webwallet tenrai-reference-webwallet tenrai-reference-explorer tenrai-sdk tenrai-paper-wallet tenrai-browser-miner tenrai-wallet-daemon-client tenrai-docs; do args+=(--repo "$(win "$GITHUB/$repo")"); done
  py -3.14 -E -s -B "$RUNTIME" inventory --work "$(win "$OUT")" "${args[@]}"
}
phase_records() {
  step records-identity py -3.14 -E -s -B "$HERE/records.py" identity --work "$(win "$OUT")" --write || return 1
  cat "$L/records-identity.log" | tee -a "$OUT/release.log"
  step records-files release_files || return 1
  step records-sources sources || return 1
  step records-release py -3.14 -E -s -B "$HERE/records.py" release --work "$(win "$OUT")" --out "$(win "$OUT")" || return 1
  cat "$L/records-release.log" | tee -a "$OUT/release.log"
}

# Retire earlier success before any resumed phase can replace artifacts or fail.
# The metadata-only action preserves prior reports outside every source repository.
py -3.14 -E -s -B "$HERE/records.py" begin --work "$(win "$OUT")" --phase "$from" || exit 1
say "release into $OUT from phase $from"
touch "$OUT/checks.tsv"
started=0
for PHASE in $PHASES; do
  [ "$PHASE" = "$from" ] && started=1
  [ $started -eq 1 ] || continue
  say "== $PHASE"
  if [ $resume -eq 1 ] && [ "$PHASE" = "$from" ]; then
    # A resumed phase keeps each check whose result still belongs to this build; every other row goes, its check runs.
    py -3.14 -E -s -B "$RESUME" keep --work "$(win "$OUT")" --phase "$PHASE" --attempt "${RESUMED:-none}" \
      --report "$(win "$L/kept-checks.json")" | tee -a "$OUT/release.log"
    [ "${PIPESTATUS[0]}" -eq 0 ] || { say "the kept checks could not be confirmed; the phase goes on with --from $PHASE"; exit 1; }
  else
    # A phase run again replaces the checks it recorded before.
    awk -F'\t' -v phase="$PHASE" '$1 != phase' "$OUT/checks.tsv" > "$OUT/checks.tsv.new" && mv -f "$OUT/checks.tsv.new" "$OUT/checks.tsv"
  fi
  "phase_$PHASE" || { say "release stopped in phase $PHASE; after the fix it goes on with --from $PHASE$(goes_on)"; exit 1; }
  if [ "$PHASE" = "$until" ] && [ "$PHASE" != records ]; then
    say "stopped after phase $PHASE as --until asked; the release record stays incomplete"
    exit 0
  fi
done
# A resume at records still needs every required check/log and the exact fresh built-in receipt.
py -3.14 -E -s -B "$HERE/records.py" verify --work "$(win "$OUT")" --out "$(win "$OUT")" || {
  say "release evidence is incomplete, failed or no longer matches its artifacts"; exit 1;
}
say "release record verified for its mandatory checks and built-in identity"
py -3.14 -E -s -B "$HERE/release_signing.py" sign --release "$(win "$OUT")" --tool "$(win "$OUT/bin/tenrai-release.exe")" \
  | tee -a "$OUT/release.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || { say "release signing failed; the record stays verified, the folder unsigned"; exit 1; }
say "release signed in $OUT/signing; publication remains an owner decision"
