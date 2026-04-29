"""Oodle-family decompressor wrapper using ooz-wasm.

Decompresses via open-source ooz-wasm (requires Node.js runtime).
"""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


class OodleDecompressor:
    """Handle Oodle-family decompression via ooz-wasm Node.js wrapper."""

    def __init__(self, backend_path: Optional[str], verbose: bool) -> None:
        self.verbose = verbose
        self.wrapper_path: Optional[str] = None
        self.backend_name = "unavailable"
        self._load_backend(backend_path)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[Oodle] {msg}")

    def _load_backend(self, backend_path: Optional[str]) -> None:
        """Locate ooz-wasm Node.js wrapper script."""
        search_paths = []
        if backend_path:
            search_paths.append(backend_path)
        search_paths.extend(
            [
                "./lib/ooz-wasm-decompress.mjs",
                "../lib/ooz-wasm-decompress.mjs",
                "lib/ooz-wasm-decompress.mjs",
                os.path.join(os.path.dirname(__file__), "../lib/ooz-wasm-decompress.mjs"),
            ]
        )

        for path in search_paths:
            if os.path.exists(path):
                self.wrapper_path = str(Path(path).resolve())
                self.backend_name = f"ooz-wasm:{self.wrapper_path}"
                self._log(f"Found ooz-wasm wrapper: {self.wrapper_path}")
                return

        self._log("ooz-wasm wrapper not found (ooz-wasm may not be installed)")

    def decompress(self, compressed: bytes, expected_size: int) -> Optional[bytes]:
        """Decompress Oodle-compressed data using ooz-wasm."""
        if not self.wrapper_path:
            self._log("ooz-wasm wrapper not configured")
            return None

        try:
            with tempfile.TemporaryDirectory(prefix="ooz_") as tmp:
                # Write compressed data and size to temporary files
                compressed_path = Path(tmp) / "compressed.bin"
                compressed_path.write_bytes(compressed)

                output_path = Path(tmp) / "decompressed.bin"

                # Call Node.js wrapper
                result = subprocess.run(
                    [
                        "node",
                        self.wrapper_path,
                        str(compressed_path),
                        str(expected_size),
                        str(output_path),
                    ],
                    capture_output=True,
                    check=False,
                )

                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    self._log(f"Decompression failed: {stderr}")
                    return None

                # Read output before leaving the temp dir context.
                if not output_path.exists():
                    self._log(f"Output file not created: {output_path}")
                    return None

                out = output_path.read_bytes()
            if expected_size > 0 and len(out) != expected_size:
                self._log(
                    f"Size mismatch: expected={expected_size} got={len(out)}"
                )
                return None

            self._log(f"Decompressed {len(compressed)} -> {len(out)} via ooz-wasm")
            return out

        except Exception as exc:
            self._log(f"Decompression error: {exc}")
            return None

