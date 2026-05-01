#!/usr/bin/env node

/*
 * ooz-wasm decompression wrapper for Node.js
 *
 * Usage: node ooz-wasm-decompress.mjs <compressed_file> <expected_size> <output_file>
 */

import fs from "node:fs";
import { decompress } from "ooz-wasm";

function fail(msg) {
  console.error(msg);
  process.exit(1);
}

const [, , compressedPath, expectedSizeArg, outputPath] = process.argv;
if (!compressedPath || !expectedSizeArg || !outputPath) {
  fail("Usage: node ooz-wasm-decompress.mjs <compressed_file> <expected_size> <output_file>");
}

const expectedSize = Number.parseInt(expectedSizeArg, 10);
if (!Number.isFinite(expectedSize) || expectedSize <= 0) {
  fail(`Invalid expected size: ${expectedSizeArg}`);
}

let compressed;
try {
  compressed = fs.readFileSync(compressedPath);
} catch (err) {
  fail(`Failed to read compressed input: ${err}`);
}

let out;
try {
  out = decompress(compressed, expectedSize);
} catch (err) {
  fail(`ooz-wasm error: ${err}`);
}

if (!out || out.length !== expectedSize) {
  fail(`Size mismatch: expected=${expectedSize} got=${out ? out.length : 0}`);
}

try {
  fs.writeFileSync(outputPath, out);
} catch (err) {
  fail(`Failed to write output: ${err}`);
}
