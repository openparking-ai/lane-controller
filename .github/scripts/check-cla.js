#!/usr/bin/env node
/**
 * CLA gate.
 *
 * Deliberately has no dependencies, no bot, no hosted third party and no token:
 * the list of signatories is a file in this repository, and merging a signature
 * is an act only a maintainer can perform. That keeps the record auditable in
 * git history and keeps control of it here.
 *
 * The workflow checks out the BASE commit before running this, so a pull
 * request cannot edit its own gate.
 *
 * Usage: check-cla.js <author-login> <changed-files-list>
 */
import { readFileSync } from 'node:fs';

const [author, changedFilesPath] = process.argv.slice(2);

if (!author) {
  console.error('usage: check-cla.js <author-login> <changed-files-list>');
  process.exit(2);
}

const SIGNATURES_PATH = 'cla/signatures.json';

// Bots that open pull requests as part of repository upkeep. A bot cannot hold
// copyright, so there is nothing for it to assign.
const EXEMPT_BOTS = new Set(['dependabot[bot]', 'github-actions[bot]', 'renovate[bot]']);

const changedFiles = changedFilesPath
  ? readFileSync(changedFilesPath, 'utf8')
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean)
  : [];

function fail(message) {
  console.error(`\nCLA CHECK FAILED\n\n${message}\n`);
  process.exit(1);
}

function pass(message) {
  console.log(`\nCLA CHECK PASSED — ${message}\n`);
  process.exit(0);
}

if (EXEMPT_BOTS.has(author)) {
  pass(`${author} is an exempt automation account`);
}

// A pull request that touches nothing but the signature file IS the signature.
// It has to be allowed through, or signing would require having already signed.
if (changedFiles.length > 0 && changedFiles.every((f) => f === SIGNATURES_PATH)) {
  pass(`this pull request only modifies ${SIGNATURES_PATH} — it is a signature`);
}

let signatures;
try {
  signatures = JSON.parse(readFileSync(SIGNATURES_PATH, 'utf8'));
} catch (err) {
  fail(`could not read ${SIGNATURES_PATH}: ${err.message}`);
}

const signatories = Array.isArray(signatures.signatories) ? signatures.signatories : [];
const signed = signatories.some(
  (s) => typeof s.github === 'string' && s.github.toLowerCase() === author.toLowerCase(),
);

if (!signed) {
  fail(
    `@${author} has not signed the Contributor Licence Agreement.\n\n` +
      `To sign:\n` +
      `  1. Read CLA.md.\n` +
      `  2. Open a separate pull request that adds this entry to ${SIGNATURES_PATH},\n` +
      `     and changes nothing else:\n\n` +
      `       { "github": "${author}", "name": "<your full legal name>", "date": "<YYYY-MM-DD>" }\n\n` +
      `  3. Once that pull request is merged, re-run this check.\n\n` +
      `Opening that pull request is your agreement to the CLA as written.`,
  );
}

pass(`@${author} has signed the CLA`);
