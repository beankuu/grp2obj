"""Oodle LZ decompressor wrapper."""

import ctypes
import os
from typing import List, Optional


class OodleDecompressor:
    """Handle Oodle LZ decompression via oo2core DLL."""

    def __init__(self, dll_path: Optional[str], verbose: bool) -> None:
        self.verbose = verbose
        self.dll = None
        self.decompress_func = None
        self._load_dll(dll_path)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[Oodle] {msg}")

    def _load_dll(self, dll_path: Optional[str]) -> None:
        search_paths: List[str] = []
        if dll_path:
            search_paths.append(dll_path)
        search_paths.extend(
            [
                "./lib/oo2core_9_win64.dll",
                "../lib/oo2core_9_win64.dll",
                "oo2core_9_win64.dll",
                os.path.join(os.path.dirname(__file__), "../lib/oo2core_9_win64.dll"),
            ]
        )

        for path in search_paths:
            if not os.path.exists(path):
                continue
            try:
                self.dll = ctypes.CDLL(path)
                self.decompress_func = self.dll.OodleLZ_Decompress
                self.decompress_func.restype = ctypes.c_int64
                self.decompress_func.argtypes = [
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_int32,
                    ctypes.c_int32,
                    ctypes.c_int32,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_char_p,
                    ctypes.c_int64,
                    ctypes.c_int32,
                ]
                self._log(f"Loaded: {path}")
                return
            except Exception as exc:
                self._log(f"Failed to load {path}: {exc}")

        self._log("oo2core DLL not found; Oodle path unavailable")

    def decompress(self, compressed: bytes, expected_size: int) -> Optional[bytes]:
        if not self.decompress_func or expected_size <= 0:
            return None

        try:
            out_buf = ctypes.create_string_buffer(expected_size)
            comp_buf = ctypes.c_char_p(compressed)
            result = self.decompress_func(
                comp_buf,
                ctypes.c_int64(len(compressed)),
                out_buf,
                ctypes.c_int64(expected_size),
                ctypes.c_int(0),
                ctypes.c_int(0),
                ctypes.c_int(0),
                None,
                ctypes.c_int64(0),
                None,
                None,
                None,
                ctypes.c_int64(0),
                ctypes.c_int(3),
            )
            if result > 0:
                out = bytes(out_buf.raw[: int(result)])
                self._log(f"Decompressed {len(compressed)} -> {result} (requested {expected_size})")
                return out
            self._log(f"Decompress failed: expected={expected_size} got={result}")
            return None
        except Exception as exc:
            self._log(f"Decompression error: {exc}")
            return None
