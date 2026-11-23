"""One immutable genesis history per Chain ID, rooted in committed public release metadata.

Only public Git objects and the bounded identity event ledger are read. No key, wallet,
network, signing operation or product process is accessed by this module.
"""
import hashlib
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import subprocess

FORMAT = "tenrai-identity-history-v1"
SEAL_FORMAT = "tenrai-identity-history-seal-v1"
LEDGER = "genesis/identity-history.json"
PUBLISHED = "genesis/identity.json"
MAX_BYTES = 1 << 20
MAX_EVENTS = 4096
MAX_HISTORY_COMMITS = 4096
MAX_HISTORY_PARENT_EDGES = 4096
_VERIFIED_HISTORY = OrderedDict()
MAX_HISTORY_CACHE = 8
HEX96 = re.compile(r"[0-9a-f]{96}")
HEX64 = re.compile(r"[0-9a-f]{64}")
GIT_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class IdentityHistoryError(ValueError):
    pass


def public_files():
    return Path(os.environ.get("TENRAI_MAINNET_FILES", Path(__file__).resolve().parents[2] / "tenrai-mainnet-files"))


def require(condition, message):
    if not condition:
        raise IdentityHistoryError(message)


def _id(value):
    require(isinstance(value, str) and HEX96.fullmatch(value) is not None
            and value != "0" * 96, "identity history needs a canonical nonzero Hash384")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "identity history contains a duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    require(len(data) <= MAX_BYTES, "identity history exceeds its byte limit")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise IdentityHistoryError("identity history is not canonical JSON") from error


def _read(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), "public identity history must be a regular file")
    with path.open("rb") as source:
        data = source.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, "public identity history exceeds its byte limit")
    return data


def parse(data):
    """Returns immutable event tuples and the final active (CID,GID), or None after retirement."""
    document = _json(data)
    require(isinstance(document, dict) and set(document) == {"format", "events"}
            and document["format"] == FORMAT, "identity history format is invalid")
    events = document["events"]
    require(isinstance(events, list) and 0 < len(events) <= MAX_EVENTS,
            "identity history event count is outside its fixed limits")
    seen, retired, canonical, active = set(), set(), [], None
    for event in events:
        require(isinstance(event, dict), "identity history event is not an object")
        kind = event.get("type")
        if kind == "retire":
            require(set(event) == {"type", "chain_id"}, "retirement has unknown or missing fields")
            chain = _id(event["chain_id"])
            require(chain not in retired, "Chain ID was already retired")
            retired.add(chain)
            seen.add(chain)
            if active is not None and active[0] == chain:
                active = None
            canonical.append((kind, chain, None))
        elif kind == "activate":
            require(set(event) == {"type", "chain_id", "genesis_id"}, "activation has unknown or missing fields")
            chain, genesis = _id(event["chain_id"]), _id(event["genesis_id"])
            require(chain not in seen, "used or retired Chain ID cannot activate again")
            require(active is None, "retire the previous active Chain ID before activating another")
            seen.add(chain)
            active = (chain, genesis)
            canonical.append((kind, chain, genesis))
        else:
            raise IdentityHistoryError("identity history event type is unsupported")
    return tuple(canonical), active


def _event_digest(events):
    # A public history has the same authority after an LF/CRLF checkout or JSON reindent.
    # Only fixed ASCII field names/IDs occur here, so this encoding is platform-independent.
    records = [{"type": kind, "chain_id": chain,
                **({"genesis_id": genesis} if kind == "activate" else {})}
               for kind, chain, genesis in events]
    canonical = json.dumps({"format": FORMAT, "events": records}, sort_keys=True,
                           ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _git(root, arguments):
    command = ["git", "--no-replace-objects", "--no-optional-locks", "-c", "safe.directory=" + str(root),
               "-c", "core.fsmonitor=false", "-c", "protocol.allow=never",
               "-c", "protocol.file.allow=never", "-C", str(root), *arguments]
    # -C alone does not override GIT_DIR/WORK_TREE/COMMON_DIR or replacement-object refs.
    # Every Git read must name the exact public repository and immutable committed object.
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0", GIT_GRAFT_FILE=os.devnull,
                       GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15, check=False, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IdentityHistoryError("committed public identity baseline is unavailable") from error
    require(result.returncode == 0, "committed public identity baseline could not be read")
    return result.stdout


def _blob(root, commit, name):
    # Resolve one immutable tree entry first; reject symlinks and oversized blobs before reading.
    entry = _git(root, ["ls-tree", "-z", commit, "--", name])
    if not entry:
        return None
    require(entry.endswith(b"\0") and entry.count(b"\0") == 1,
            "public identity baseline tree is ambiguous")
    meta, filename = entry[:-1].split(b"\t", 1)
    fields = meta.split()
    require(len(fields) == 3 and fields[0] in (b"100644", b"100755")
            and fields[1] == b"blob" and filename == name.encode("ascii"),
            "public identity baseline is not a regular Git blob")
    object_id = fields[2].decode("ascii")
    require(GIT_OID.fullmatch(object_id) is not None, "public identity baseline object is malformed")
    size = _git(root, ["cat-file", "-s", object_id]).decode("ascii").strip()
    require(size.isdecimal() and int(size) <= MAX_BYTES, "committed identity metadata exceeds its byte limit")
    data = _git(root, ["cat-file", "blob", object_id])
    require(len(data) == int(size), "committed identity baseline length changed")
    return data


def _published(data):
    document = _json(data)
    require(isinstance(document, dict), "committed public identity is malformed")
    return _id(document.get("chain_id")), _id(document.get("genesis_id"))


def _commit_parents(root, commit):
    # Read actual object headers: path-filtered revision walks may rewrite parent links,
    # and shallow metadata must not turn a missing ancestor into an initial ledger.
    size = _git(root, ["cat-file", "-s", commit]).decode("ascii").strip()
    require(size.isdecimal() and int(size) <= MAX_BYTES, "public identity commit exceeds its byte limit")
    raw = _git(root, ["cat-file", "commit", commit])
    require(len(raw) == int(size), "public identity commit length changed")
    headers = raw.split(b"\n\n", 1)[0].splitlines()
    parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
    require(all(GIT_OID.fullmatch(parent) is not None for parent in parents),
            "public identity commit has a malformed parent")
    return parents


def _validate_committed_history(root, commit):
    key = (str(root), commit)
    if key in _VERIFIED_HISTORY:
        _VERIFIED_HISTORY.move_to_end(key)
        return
    raw = _git(root, ["rev-list", "--full-history", "--topo-order", "--reverse", "--parents",
                      "--max-count=" + str(MAX_HISTORY_COMMITS + 1), commit, "--", LEDGER])
    changed = [line.split()[0].decode("ascii") for line in raw.splitlines()]
    require(len(changed) <= MAX_HISTORY_COMMITS
            and all(GIT_OID.fullmatch(value) is not None for value in changed),
            "public identity history exceeds its commit limit")
    edges = 0
    for version in changed:
        current = _blob(root, version, LEDGER)
        require(current is not None, "a committed identity event ledger was deleted")
        current_events, _ = parse(current)
        parents = _commit_parents(root, version)
        require(parents, "identity ledger has no committed predecessor; implicit bootstrap is refused")
        edges += len(parents)
        require(edges <= MAX_HISTORY_PARENT_EDGES, "public identity history exceeds its ancestry limit")
        for parent in parents:
            previous = _blob(root, parent, LEDGER)
            if previous is None:
                public = _blob(root, parent, PUBLISHED)
                require(public is not None, "identity ledger ancestry has no published prototype")
                prototype = _published(public)
                require(current_events[0] == ("retire", prototype[0], None),
                        "initial committed ledger did not retire its published prototype CID")
            else:
                previous_events, _ = parse(previous)
                require(len(current_events) >= len(previous_events)
                        and current_events[:len(previous_events)] == previous_events,
                        "committed identity history rewrote or truncated a parent event prefix")
            # Process one parent blob at a time; do not retain every historical event vector.
    _VERIFIED_HISTORY[key] = True
    while len(_VERIFIED_HISTORY) > MAX_HISTORY_CACHE:
        _VERIFIED_HISTORY.popitem(last=False)


def baseline(files):
    """Pin HEAD once; every subsequent baseline read names that immutable commit object."""
    root = Path(files).resolve()
    top = Path(_git(root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()).resolve()
    require(top == root, "public identity metadata must be rooted in its own Git repository")
    commit = _git(root, ["rev-parse", "--verify", "HEAD"]).decode("ascii").strip()
    require(GIT_OID.fullmatch(commit) is not None, "public identity baseline commit is malformed")
    require(_git(root, ["rev-parse", "--is-shallow-repository"]).strip() == b"false",
            "public identity history requires complete Git ancestry (fetch-depth: 0)")
    _validate_committed_history(root, commit)
    published = _blob(root, commit, PUBLISHED)
    require(published is not None, "no committed public identity exists; implicit bootstrap is refused")
    pair = _published(published)
    ledger = _blob(root, commit, LEDGER)
    if ledger is None:
        return commit, None, pair, "legacy-public-identity", hashlib.sha256(published).hexdigest()
    events, active = parse(ledger)
    require(active == pair, "committed ledger and published identity disagree")
    return commit, events, pair, "ledger", hashlib.sha256(ledger).hexdigest()


def authorize(files, chain_id, genesis_id):
    """Require the requested active pair and an append-only extension of committed history."""
    pair = (_id(chain_id), _id(genesis_id))
    root = Path(files).resolve()
    require((root / LEDGER).resolve().is_relative_to(root), "public identity history escapes its Git repository")
    raw = _read(root / LEDGER)
    events, active = parse(raw)
    commit, previous, published, kind, baseline_hash = baseline(root)
    if previous is None:
        require(events[0] == ("retire", published[0], None),
                "first identity ledger must retire the committed prototype Chain ID")
    else:
        require(len(events) >= len(previous) and events[:len(previous)] == previous,
                "identity history truncated or rewrote the committed event prefix")
    require(active == pair, "requested identity is retired or differs from the active public pair")
    # A Git commit or ledger change during checking cannot produce a valid reusable seal.
    require(_git(root, ["rev-parse", "--verify", "HEAD"]).decode("ascii").strip() == commit
            and _read(root / LEDGER) == raw, "public identity baseline changed during authorization")
    return {"format": SEAL_FORMAT, "sha256": _event_digest(events),
            "baseline_commit": commit, "baseline_sha256": baseline_hash, "baseline_kind": kind}


def verify(files, chain_id, genesis_id, seal):
    """Recheck current history even for a previously successful native/evidence receipt."""
    require(isinstance(seal, dict) and set(seal) == {
        "format", "sha256", "baseline_commit", "baseline_sha256", "baseline_kind"},
        "identity derivation has no complete history seal")
    require(seal["format"] == SEAL_FORMAT and isinstance(seal["sha256"], str)
            and HEX64.fullmatch(seal["sha256"]) is not None
            and isinstance(seal["baseline_sha256"], str) and HEX64.fullmatch(seal["baseline_sha256"]) is not None
            and isinstance(seal["baseline_commit"], str) and GIT_OID.fullmatch(seal["baseline_commit"]) is not None
            and seal["baseline_kind"] in ("ledger", "legacy-public-identity"),
            "identity history seal is malformed")
    current = authorize(files, chain_id, genesis_id)
    require(current["sha256"] == seal["sha256"], "identity history changed since public identity derivation")
    return current
