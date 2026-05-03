"""Oodle-family decompressor wrapper.

Uses caller-supplied native Oodle DLL when configured (best compatibility),
then falls back to the publishable ``ooz-wasm`` Node wrapper.
"""

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


class OodleDecompressor:
    """Handle Oodle-family decompression via native DLL or ooz-wasm."""

    def __init__(self, backend_path: Optional[str], verbose: bool) -> None:
        self.verbose = verbose
        self.wrapper_path: Optional[str] = None
        self.dll_path: Optional[str] = None
        self._dll: Optional[ctypes.CDLL] = None
        self._native_decompress = None
        self.backend_name = "unavailable"
        self._load_backend(backend_path)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[Oodle] {msg}")

    def _load_backend(self, backend_path: Optional[str]) -> None:
        """Locate configured native DLL and/or ooz-wasm wrapper."""
        dll_search_paths = []
        wrapper_search_paths = []

        env_dll = os.environ.get("OODLE_DLL")
        if env_dll:
            dll_search_paths.append(env_dll)

        if backend_path:
            lower_backend = backend_path.lower()
            if lower_backend.endswith(".dll"):
                dll_search_paths.append(backend_path)
            else:
                wrapper_search_paths.append(backend_path)

        if getattr(sys, "frozen", False):
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                wrapper_search_paths.append(
                    os.path.join(meipass, "ooz-wasm-decompress.mjs")
                )

        wrapper_search_paths.extend(
            [
                "./lib/ooz-wasm-decompress.mjs",
                "../lib/ooz-wasm-decompress.mjs",
                "lib/ooz-wasm-decompress.mjs",
                os.path.join(os.path.dirname(__file__), "../lib/ooz-wasm-decompress.mjs"),
            ]
        )

        for path in dll_search_paths:
            if os.path.exists(path):
                self.dll_path = str(Path(path).resolve())
                try:
                    dll = ctypes.CDLL(self.dll_path)
                    func = dll.OodleLZ_Decompress
                    func.restype = ctypes.c_longlong
                    func.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_longlong,
                        ctypes.c_void_p,
                        ctypes.c_longlong,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_void_p,
                        ctypes.c_longlong,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_longlong,
                        ctypes.c_int,
                    ]
                    self._dll = dll
                    self._native_decompress = func
                    self.backend_name = f"oo2core:{self.dll_path}"
                    self._log(f"Found native Oodle DLL: {self.dll_path}")
                    break
                except Exception as exc:
                    self._log(f"Failed to load native Oodle DLL {path}: {exc}")

        for path in wrapper_search_paths:
            if os.path.exists(path):
                self.wrapper_path = str(Path(path).resolve())
                if self.backend_name == "unavailable":
                    self.backend_name = f"ooz-wasm:{self.wrapper_path}"
                self._log(f"Found ooz-wasm wrapper: {self.wrapper_path}")
                break

        if not self.dll_path and not self.wrapper_path:
            self._log(
                "No Oodle backend found (set OODLE_DLL, configure ooz-wasm, "
                "or set config.json oodle path)"
            )

    def _decompress_native(
        self, compressed: bytes, expected_size: int
    ) -> Optional[bytes]:
        if not self._native_decompress:
            return None
        try:
            comp_buf = ctypes.create_string_buffer(compressed)
            out_buf = ctypes.create_string_buffer(expected_size)
            result = self._native_decompress(
                comp_buf,
                len(compressed),
                out_buf,
                expected_size,
                0,     # OodleLZ_FuzzSafe_No
                0,     # OodleLZ_CheckCRC_No
                0,     # OodleLZ_Verbosity_None
                None,
                0,
                None,
                None,
                None,
                0,
                0,     # OodleLZ_Decode_ThreadPhase_All in oo2core_9
            )
            if result != expected_size:
                self._log(
                    f"Native Oodle failed: expected={expected_size} got={result}"
                )
                return None
            out = bytes(out_buf.raw[:expected_size])
            self._log(
                f"Decompressed {len(compressed)} -> {len(out)} via native Oodle"
            )
            return out
        except Exception as exc:
            self._log(f"Native Oodle error: {exc}")
            return None

    def _decompress_wasm(
        self, compressed: bytes, expected_size: int
    ) -> Optional[bytes]:
        if not self.wrapper_path:
            return None

        try:
            with tempfile.TemporaryDirectory(prefix="ooz_") as tmp:
                compressed_path = Path(tmp) / "compressed.bin"
                compressed_path.write_bytes(compressed)

                output_path = Path(tmp) / "decompressed.bin"

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
            self._log(f"ooz-wasm decompression error: {exc}")
            return None

    def decompress(self, compressed: bytes, expected_size: int) -> Optional[bytes]:
        """Decompress Oodle-compressed data."""
        if expected_size <= 0:
            return None

        native = self._decompress_native(compressed, expected_size)
        if native is not None:
            return native

        wasm = self._decompress_wasm(compressed, expected_size)
        if wasm is not None:
            return wasm

        self._log("All Oodle decompression backends failed")
        return None

