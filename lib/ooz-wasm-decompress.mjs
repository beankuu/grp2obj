#!/usr/bin/env node

/**
 * ooz-wasm decompression wrapper for Node.js
 * 
 * Usage: node ooz-wasm-decompress.mjs <compressed_file> <expected_size> <output_file>
 * 
 * Requires: npm install ooz-wasm
 */

import { promises as fs } from 'fs';
import { decompress } from 'ooz-wasm';

async function main() {
  const args = process.argv.slice(2);
  if (args.length < 3) {
    console.error('Usage: node ooz-wasm-decompress.mjs <compressed_file> <expected_size> <output_file>');
    process.exit(1);
  }

  const [compressedFile, expectedSizeStr, outputFile] = args;
  const expectedSize = parseInt(expectedSizeStr, 10);

  if (isNaN(expectedSize)) {
    console.error('Invalid expected_size (must be integer)');
    process.exit(1);
  }

  try {
    // Read compressed data
    const compressedData = await fs.readFile(compressedFile);
    const compressedArray = new Uint8Array(compressedData);

    // Decompress
    const decompressed = decompress(compressedArray, expectedSize);

    // Validate size
    if (decompressed.length !== expectedSize) {
      console.error(`Size mismatch: expected=${expectedSize} got=${decompressed.length}`);
      process.exit(1);
    }

    // Write output
    await fs.writeFile(outputFile, Buffer.from(decompressed));
    process.exit(0);
  } catch (error) {
    console.error(`Error: ${error.message}`);
    process.exit(1);
  }
}

main();
