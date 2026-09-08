#!/usr/bin/env node
/**
 * No real personal data in the repository.
 *
 * The running system stores real vehicle identity — that is the product. What
 * must never appear in the REPOSITORY is real data: fixtures, tests, docs and
 * examples all use invented values.
 *
 * This is the repo-side half of that rule. The metadata half is
 * check-commit-emails.js.
 *
 * Usage:
 *   check-no-real-data.js               scan every tracked file
 *   check-no-real-data.js --self-test   prove the scan can fail
 */
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync, rmSync } from 'node:fs';
import { createHash } from 'node:crypto';

const EMAIL = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;

/** Addresses that are fine to write down. */
const ALLOWED_EMAIL = [
  /@users\.noreply\.github\.com$/i,
  /^noreply@github\.com$/i,
  /^noreply@anthropic\.com$/i,
  /@example\.(com|org|net)$/i,
  /^[^@]+@example$/i,
];

/**
 * Things that are real, held as sha256 of the lowercased address so that this
 * repository never spells them out. A public repo listing the addresses it is
 * trying to protect publishes the very thing it guards.
 *
 * This is belt and braces over EMAIL below, which already refuses any address
 * that is not obviously invented; these two are named so a rewording of that
 * rule can never quietly stop catching them. The report prints the reason and
 * the digest, never the address.
 */
const FORBIDDEN_DIGESTS = new Map([
  ['c054bf79b58544b0f21de0646d699d9301b0010db6701bec312bec723c0fb9eb', "a maintainer's personal address"],
  ['130b66cf7ee597b1d2fd992086dae292cebfd34d0882a89d17e0aa2a0073e021', "a maintainer's work address"],
]);

const digestOf = (value) =>
  createHash('sha256').update(value.trim().toLowerCase()).digest('hex');

//: The scanner NO LONGER EXEMPTS ITSELF. That exemption is how two real
//: addresses sat unread in this file while every run reported the repository
//: clean. What stays listed here is not ours to edit.
const SKIP = /^(LICENSE|package-lock\.json)$/;

function trackedFiles() {
  return execFileSync('git', ['ls-files'], { encoding: 'utf8' })
    .split('\n')
    .filter(Boolean)
    .filter((f) => !SKIP.test(f));
}

function scanText(file, text) {
  const problems = [];
  for (const match of text.match(EMAIL) ?? []) {
    const why = FORBIDDEN_DIGESTS.get(digestOf(match));
    if (why) {
      problems.push({ file, value: `sha256:${digestOf(match).slice(0, 12)}`, why });
      continue;
    }
    if (!ALLOWED_EMAIL.some((re) => re.test(match))) {
      problems.push({ file, value: match, why: 'an email address that is not obviously invented' });
    }
  }
  return problems;
}

function scanRepo() {
  const problems = [];
  for (const file of trackedFiles()) {
    let text;
    try {
      text = readFileSync(file, 'utf8');
    } catch {
      continue; // binary or unreadable
    }
    problems.push(...scanText(file, text));
  }
  return problems;
}

//: The two probe addresses are ASSEMBLED AT RUNTIME, never written out.
//: This file is now inside the scanned set, so an address-shaped literal here
//: would make the scanner refuse its own source on every run. Splitting them on
//: the `@` keeps a search of this file clean while the self-test still plants a
//: genuinely real-looking address.
const REAL_LOOKING = ['someone.real', 'a-real-company.example-not'].join('@');
const INVENTED = ['nobody', 'example.com'].join('@');

function selfTest() {
  const probe = '_no_real_data_control.md';
  try {
    writeFileSync(probe, `contact ${REAL_LOOKING}\n`);
    const caught = scanText(probe, readFileSync(probe, 'utf8'));
    if (caught.length === 0) {
      console.error('SELF-TEST FAILED: a planted address was not caught');
      return false;
    }
    const clean = scanText(probe, `write to ${INVENTED}, which is invented\n`);
    if (clean.length !== 0) {
      console.error('SELF-TEST FAILED: an example.com address was wrongly rejected');
      return false;
    }
    console.log('self-test OK — a real-looking address fails; an example.com one passes.');
    return true;
  } finally {
    rmSync(probe, { force: true });
  }
}

if (process.argv[2] === '--self-test') process.exit(selfTest() ? 0 : 1);

const problems = scanRepo();
if (problems.length > 0) {
  console.error('\nREAL DATA IN THE REPOSITORY\n');
  for (const p of problems) console.error(`  ${p.file}: ${p.value}  (${p.why})`);
  console.error('\nFixtures, tests and docs use invented values.\n');
  process.exit(1);
}
console.log(`${trackedFiles().length} tracked file(s) scanned; no real personal data.`);
