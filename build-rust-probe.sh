#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="$SCRIPT_DIR/src/rust"

if [ ! -f "$RUST_DIR/Cargo.toml" ]; then
  echo "Error: src/rust/Cargo.toml not found."
  exit 1
fi

cd "$RUST_DIR"
cargo build --release

echo "Built: $RUST_DIR/target/release/grp_probe"
