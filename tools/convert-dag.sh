#!/usr/bin/env bash
# convert-dag.sh — convert a Dagor .dag model to .glb using Blender + dag4blend.
#
# Usage:
#   ./convert-dag.sh path/to/model.dag [output.glb]
#
# Requires:
#   - Blender 3.6+ on PATH, or BLENDER env var pointing to the executable.
#   - dag4blend addon. Set DAG4BLEND to the addon folder if not already
#     installed in Blender. Defaults to D:/DagorEngine/prog/tools/dag4blend.
#
# Output defaults to <input>.glb next to the source.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <input.dag> [output.glb]" >&2
  exit 1
fi

INPUT="$1"
OUTPUT="${2:-${INPUT%.*}.glb}"

if [ ! -f "$INPUT" ]; then
  echo "Input not found: $INPUT" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_DAG4BLEND="$SCRIPT_DIR/../lib/dag4blend"
DEFAULT_DUMPGRP="$SCRIPT_DIR/../lib/tools/dagor_cdk/windows-x86_64/dumpGrp-dev.exe"
BLENDER_FROM_ENV=0
DAG4BLEND_FROM_ENV=0
BUNDLED_DAG4BLEND_PRESENT=0
DUMPGRP_FROM_ENV=0
GRP2DAG_FROM_ENV=0

if [ -n "${BLENDER-}" ]; then
  BLENDER_FROM_ENV=1
fi

if [ -n "${DAG4BLEND-}" ]; then
  DAG4BLEND_FROM_ENV=1
fi

if [ -n "${DUMPGRP-}" ]; then
  DUMPGRP_FROM_ENV=1
fi

if [ -n "${GRP2DAG-}" ]; then
  GRP2DAG_FROM_ENV=1
fi

if [ -d "$DEFAULT_DAG4BLEND" ]; then
  BUNDLED_DAG4BLEND_PRESENT=1
fi

to_shell_path() {
  case "$1" in
    [A-Za-z]:\\*)
      if command -v wslpath >/dev/null 2>&1; then
        wslpath -a "$1"
      elif command -v cygpath >/dev/null 2>&1; then
        cygpath -u "$1"
      else
        local drive rest
        drive=$(printf '%s' "$1" | cut -c1 | tr '[:upper:]' '[:lower:]')
        rest=${1#?:\\}
        rest=${rest//\\//}
        printf '/%s/%s\n' "$drive" "$rest"
      fi
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

to_windows_path() {
  case "$1" in
    [A-Za-z]:\\*)
      printf '%s\n' "$1"
      ;;
    /*)
      if command -v wslpath >/dev/null 2>&1; then
        wslpath -aw "$1"
      elif command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
      else
        local drive rest
        case "$1" in
          /mnt/[A-Za-z]/*)
            drive=$(printf '%s' "$1" | cut -c6 | tr '[:lower:]' '[:upper:]')
            rest=${1#/mnt/[A-Za-z]/}
            ;;
          /[A-Za-z]/*)
            drive=$(printf '%s' "$1" | cut -c2 | tr '[:lower:]' '[:upper:]')
            rest=${1#/[A-Za-z]/}
            ;;
          *)
            printf '%s\n' "$1"
            return 0
            ;;
        esac
        rest=${rest//\//\\}
        printf '%s:\\%s\n' "$drive" "$rest"
      fi
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

# Set defaults first. Explicit environment variables win over config.json.
BLENDER="${BLENDER:-blender}"
if [ -d "$DEFAULT_DAG4BLEND" ]; then
  DAG4BLEND="${DAG4BLEND:-$DEFAULT_DAG4BLEND}"
else
  DAG4BLEND="${DAG4BLEND:-D:/DagorEngine/prog/tools/dag4blend}"
fi
if [ -f "$DEFAULT_DUMPGRP" ]; then
  DUMPGRP="${DUMPGRP:-$DEFAULT_DUMPGRP}"
else
  DUMPGRP="${DUMPGRP:-}"
fi
GRP2DAG="${GRP2DAG:-}"

# Try to read blender and dag4blend paths from config.json only when env vars are unset.
if [ -f "$SCRIPT_DIR/../config.json" ]; then
  CONFIG_BLENDER=$(sed -n 's/.*"blender"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/../config.json" 2>/dev/null || true)
  if [ "$BLENDER_FROM_ENV" -eq 0 ] && [ -n "${CONFIG_BLENDER:-}" ]; then
    BLENDER="$CONFIG_BLENDER"
  fi
  CONFIG_DAG4BLEND=$(sed -n 's/.*"dag4blend"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/../config.json" 2>/dev/null || true)
  if [ "$DAG4BLEND_FROM_ENV" -eq 0 ] && [ "$BUNDLED_DAG4BLEND_PRESENT" -eq 0 ] && [ -n "${CONFIG_DAG4BLEND:-}" ]; then
    DAG4BLEND="$CONFIG_DAG4BLEND"
  fi
  CONFIG_DUMPGRP=$(sed -n 's/.*"dumpGrp"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/../config.json" 2>/dev/null || true)
  if [ "$DUMPGRP_FROM_ENV" -eq 0 ] && [ -n "${CONFIG_DUMPGRP:-}" ]; then
    DUMPGRP="$CONFIG_DUMPGRP"
  fi
  CONFIG_GRP2DAG=$(sed -n 's/.*"grp2obj"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/../config.json" 2>/dev/null || true)
  if [ "$GRP2DAG_FROM_ENV" -eq 0 ] && [ -n "${CONFIG_GRP2DAG:-}" ]; then
    GRP2DAG="$CONFIG_GRP2DAG"
  fi
fi

# Resolve to absolute paths so Blender (which may chdir) finds them.
abspath() {
  case "$1" in
    /*|?:*) echo "$1" ;;
    *) echo "$PWD/$1" ;;
  esac
}

normalize_path_for_abs() {
  case "$1" in
    [A-Za-z]:\\*) printf '%s\n' "$1" ;;
    *) printf '%s\n' "${1//\\//}" ;;
  esac
}

INPUT_ABS="$(abspath "$INPUT")"
OUTPUT_ABS="$(abspath "$OUTPUT")"
BLENDER_PATH="$(normalize_path_for_abs "$BLENDER")"
DAG4BLEND_PATH="$(normalize_path_for_abs "$DAG4BLEND")"
BLENDER_ABS="$(abspath "$BLENDER_PATH")"
DAG4BLEND_ABS="$(abspath "$DAG4BLEND_PATH")"
BLENDER_CMD="$(to_shell_path "$BLENDER_ABS")"
SCRIPT_PY="$(to_windows_path "$SCRIPT_DIR/convert-dag.py")"
INPUT_WIN="$(to_windows_path "$INPUT_ABS")"
OUTPUT_WIN="$(to_windows_path "$OUTPUT_ABS")"
DAG4BLEND_WIN="$(to_windows_path "$DAG4BLEND_ABS")"

input_ext="${INPUT##*.}"
input_ext="$(printf '%s' "$input_ext" | tr '[:upper:]' '[:lower:]')"

if [ "$input_ext" = "grp" ]; then
  if [ -z "$DUMPGRP" ]; then
    echo "[convert-dag] .grp input detected: $INPUT" >&2
    echo "[convert-dag] missing dumpGrp extractor. Set DUMPGRP or add \"dumpGrp\" to config.json." >&2
    echo "[convert-dag] This wrapper can unpack .grp first, but it still needs a built dumpGrp executable." >&2
    exit 1
  fi

  DUMPGRP_PATH="$(normalize_path_for_abs "$DUMPGRP")"
  DUMPGRP_ABS="$(abspath "$DUMPGRP_PATH")"
  DUMPGRP_CMD="$(to_shell_path "$DUMPGRP_ABS")"
  EXTRACT_DIR="${OUTPUT_ABS%.*}__grp_extract"
  EXTRACT_DIR_WIN="$(to_windows_path "$EXTRACT_DIR")"

  mkdir -p "$EXTRACT_DIR"

  echo "[convert-dag] grp tool   = $DUMPGRP"
  echo "[convert-dag] extracting = $EXTRACT_DIR"

  "$DUMPGRP_CMD" "$INPUT_WIN" "-exp:$EXTRACT_DIR_WIN"

  EXTRACTED_DAG="$(find "$EXTRACT_DIR" -type f -name '*.dag' | head -n 1)"
  if [ -z "$EXTRACTED_DAG" ]; then
    if [ -n "$GRP2DAG" ]; then
      GRP2DAG_PATH="$(normalize_path_for_abs "$GRP2DAG")"
      GRP2DAG_ABS="$(abspath "$GRP2DAG_PATH")"
      OBJ_OUT_DIR="${EXTRACT_DIR}__obj"
      OBJ_OUT_DIR_WIN="$(to_windows_path "$OBJ_OUT_DIR")"
      mkdir -p "$OBJ_OUT_DIR"

      echo "[convert-dag] grp2obj    = $GRP2DAG"
      echo "[convert-dag] converting = $EXTRACT_DIR -> $OBJ_OUT_DIR"

      # If GRP2DAG is a .bat file, use canonical Python entrypoint from same directory
      if [[ "$GRP2DAG" == *.bat ]]; then
        GRP2DAG_DIR="$(dirname "$GRP2DAG_ABS")"
        GRP2CONVERTER_PY="$GRP2DAG_DIR/main.py"
        GRP2CONVERTER_CMD="$(to_shell_path "$GRP2CONVERTER_PY")"
        
        # Use Python directly from WSL/bash
        python3 "$GRP2CONVERTER_CMD" "$(to_shell_path "$EXTRACT_DIR")" "$(to_shell_path "$OBJ_OUT_DIR")"
      else
        GRP2DAG_CMD="$(to_shell_path "$GRP2DAG_ABS")"
        "$GRP2DAG_CMD" "$EXTRACT_DIR_WIN" "$OBJ_OUT_DIR_WIN"
      fi
      
      EXTRACTED_DAG="$(find "$OBJ_OUT_DIR" -type f -name '*.obj' | head -n 1)"
    fi

    if [ -z "$EXTRACTED_DAG" ]; then
      echo "[convert-dag] dumpGrp extraction completed but did not produce any mesh files (.dag or .obj)." >&2
      echo "[convert-dag] Provide GRP2OBJ (env) or \"grp2obj\" in config.json to convert extracted resources." >&2
      echo "[convert-dag] Expected converter CLI: <grp2obj.bat> <extract_dir> <output_dir>" >&2
      echo "[convert-dag] extracted contents left at: $EXTRACT_DIR" >&2
      exit 1
    fi
  fi

  INPUT_ABS="$EXTRACTED_DAG"
  INPUT_WIN="$(to_windows_path "$INPUT_ABS")"
  echo "[convert-dag] extracted dag = $INPUT_ABS"
fi

echo "[convert-dag] blender   = $BLENDER"
echo "[convert-dag] command   = $BLENDER_CMD"
echo "[convert-dag] dag4blend = $DAG4BLEND"
echo "[convert-dag] input     = $INPUT_ABS"
echo "[convert-dag] output    = $OUTPUT_ABS"

"$BLENDER_CMD" --background --factory-startup \
  --python "$SCRIPT_PY" -- \
  --input "$INPUT_WIN" --output "$OUTPUT_WIN" --addon "$DAG4BLEND_WIN"
