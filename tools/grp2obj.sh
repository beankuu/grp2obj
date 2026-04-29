#!/bin/bash
# GRP Converter - Bash wrapper for main.py
# Usage: grp2obj.sh <extracted_dir> [output_dir] [--verbose]

if [ $# -lt 1 ]; then
    echo "Usage: $0 <extracted_dir> [output_dir] [--verbose]"
    echo ""
    echo "Example:"
    echo "  $0 ./model_extracted"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTED_DIR="$1"
OUTPUT_DIR="${2:-$EXTRACTED_DIR/output}"
VERBOSE="$3"

# Find Python (handle both WSL and native Windows bash)
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
elif command -v py &> /dev/null; then
    PYTHON_CMD="py"
else
    echo "Error: Python not found. Please install Python 3.x and ensure it's in PATH."
    exit 1
fi

# Run converter
"$PYTHON_CMD" "$SCRIPT_DIR/main.py" "$EXTRACTED_DIR" "$OUTPUT_DIR" $VERBOSE

exit $?
