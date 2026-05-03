#!/usr/bin/env bash
set -euo pipefail

cargo build --release

mkdir -p dist
if [[ "$OSTYPE" == msys* || "$OSTYPE" == cygwin* || "$OSTYPE" == win32* ]]; then
	cp target/release/extract_grp.exe dist/extract_grp.exe
else
	cp target/release/extract_grp dist/extract_grp
fi