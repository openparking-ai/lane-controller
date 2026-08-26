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

const EMAIL = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;

/** Addresses that are fine to write down. */
const ALLOWED_EMAIL = [
  /@users\.noreply\.github\.com$/i,
  /^noreply@github\.com$/i,
  /^noreply@anthropic\.com$/i,
  /@example\.(com|org|net)$/i,
  /^[^@]+@example$/i,
];

/** Things that are real, and are named so the scan cannot miss them. */
const FORBIDDEN = [
  { pattern: /gulec@me\.com/i, why: "a maintainer's personal address" },
  { pattern: /gokhan@72knots\.ai/i, why: "a maintainer's work address" },
];

const SKIP = /^(LICENSE|package-lock\.json|\.github\/scripts\/check-no-real-data\.js)$/;

function trackedFiles() {
  return execFileSync('git', ['ls-files'], { encoding: 'utf8' })
    .split('\n')
    .filter(Boolean)
    .filter((f) => !SKIP.test(f));
}

function scanText(file, text) {
  const problems = [];
  for (const { pattern, why } of FORBIDDEN) {
    if (pattern.test(text)) problems.push({ file, value: pattern.source, why });
  }
  for (const match of text.match(EMAIL) ?? []) {
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

function selfTest() {
  const probe = '_no_real_data_control.md';
  try {
    writeFileSync(probe, 'contact someone.real@a-real-company.example-not\n');
    const caught = scanText(probe, readFileSync(probe, 'utf8'));
    if (caught.length === 0) {
      console.error('SELF-TEST FAILED: a planted address was not caught');
      return false;
    }
    const clean = scanText(probe, 'write to nobody@example.com, which is invented\n');
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
  console.error('\nFixtures, tests and docs use invented values. See docs/DATA_RETENTION.md.\n');
  process.exit(1);
}
console.log(`${trackedFiles().length} tracked file(s) scanned; no real personal data.`);
