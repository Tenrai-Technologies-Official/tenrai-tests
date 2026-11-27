"""Resolve the node checkout next to the release tools."""

import os
from pathlib import Path


def node_checkout(workspace: Path) -> Path:
    if configured := os.environ.get("TENRAI_REPO"):
        return Path(configured).expanduser()
    for name in ("tenrai", "tenrai-core", "Tenrai Technology"):
        checkout = workspace / name
        if (checkout / "crates" / "traidag" / "Cargo.toml").is_file():
            return checkout
    return workspace / "tenrai"


if __name__ == "__main__":
    print(node_checkout(Path(__file__).resolve().parents[2]))
