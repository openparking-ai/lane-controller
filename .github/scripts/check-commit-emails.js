#!/usr/bin/env node
/**
 * No real email address in git metadata. Ever.
 *
 * "Nothing but code and how-to" includes commit metadata, which is easy to
 * forget precisely because it never appears in a file. This repository's
 * history was rewritten once already to remove a personal address that got in
 * that way; this guard exists because the thing that failed the first time was
 * somebody remembering.
 *
 * Usage:
 *   check-commit-emails.js <range>       check a commit range
 *   check-commit-emails.js --all         check every ref
 *   check-commit-emails.js --self-test   prove the check can actually fail
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

/**
 * Addresses allowed to appear.
 *
 * Deliberately an allow-list of shapes, not a deny-list of known-bad addresses.
 * A deny-list only ever catches the leak you have already had.
 */
const ALLOWED = [
  /^\d+\+[A-Za-z0-9-]+@users\.noreply\.github\.com$/, // GitHub per-account masked address
  /^[A-Za-z0-9-]+@users\.noreply\.github\.com$/, // older GitHub masked form
  /^noreply@github\.com$/, // GitHub's own web-UI commits and merges
  /^noreply@anthropic\.com$/, // Co-Authored-By on assisted commits
];

const isAllowed = (email) => ALLOWED.some((re) => re.test(email));
const git = (args, cwd) => execFileSync('git', args, { cwd, encoding: 'utf8' });

const UNIT = '\x1f';
const RECORD = '\x1e';

function inspect(range, cwd) {
  const args = ['log', `--format=%H${UNIT}%ae${UNIT}%ce${UNIT}%s`];
  args.push(range || '--all');
  const out = git(args, cwd).trim();
  if (!out) return [];

  const problems = [];
  for (const line of out.split('\n')) {
    const [sha, authorEmail, committerEmail, subject] = line.split(UNIT);
    for (const [role, email] of [
      ['author', authorEmail],
      ['committer', committerEmail],
    ]) {
      if (!isAllowed(email)) problems.push({ sha, role, email, subject });
    }
  }
  return problems;
}

/** Co-authored-by trailers carry addresses too, and those get pasted in by hand. */
function inspectTrailers(range, cwd) {
  const args = ['log', `--format=%H${UNIT}%B${RECORD}`];
  args.push(range || '--all');
  const problems = [];
  for (const chunk of git(args, cwd).split(RECORD)) {
    const [sha, body] = chunk.split(UNIT);
    if (!sha || !body) continue;
    for (const match of body.matchAll(/^\s*co-authored-by:.*<([^>]+)>/gim)) {
      if (!isAllowed(match[1])) {
        problems.push({ sha: sha.trim(), role: 'co-author trailer', email: match[1], subject: '' });
      }
    }
  }
  return problems;
}

function report(problems) {
  if (problems.length === 0) return true;
  console.error('\nUNMASKED EMAIL ADDRESS IN GIT METADATA\n');
  for (const p of problems) {
    console.error(`  ${p.sha.slice(0, 10)}  ${p.role.padEnd(18)} ${p.email}`);
    if (p.subject) console.error(`              ${p.subject}`);
  }
  console.error(
    '\nSet your commit identity to the masked address before committing:\n' +
      '\n  git config user.email "<id>+<login>@users.noreply.github.com"\n' +
      '\nand turn on Settings -> Emails -> "Keep my email address private" on\n' +
      "GitHub, or its web UI stamps your real address onto every squash merge.\n",
  );
  return false;
}

// ---------------------------------------------------------------------------
// The control. A guard nobody has watched fail is not known to work.
// ---------------------------------------------------------------------------
function selfTest() {
  const dir = mkdtempSync(path.join(tmpdir(), 'email-guard-'));
  try {
    git(['init', '-q', '-b', 'main'], dir);
    git(['config', 'user.name', 'Control'], dir);

    git(['config', 'user.email', '1234+control@users.noreply.github.com'], dir);
    git(['commit', '-q', '--allow-empty', '-m', 'masked'], dir);
    if (inspect(null, dir).length !== 0) {
      console.error('SELF-TEST FAILED: a properly masked commit was rejected');
      return false;
    }

    git(['config', 'user.email', 'someone@example.com'], dir);
    git(['commit', '-q', '--allow-empty', '-m', 'planted real address'], dir);
    if (inspect(null, dir).length === 0) {
      console.error('SELF-TEST FAILED: a commit with a real address was NOT caught');
      return false;
    }

    git(['config', 'user.email', '1234+control@users.noreply.github.com'], dir);
    git(
      ['commit', '-q', '--allow-empty', '-m', 'x\n\nCo-authored-by: A <real.person@example.org>'],
      dir,
    );
    if (inspectTrailers(null, dir).length === 0) {
      console.error('SELF-TEST FAILED: a real address in a co-author trailer was NOT caught');
      return false;
    }

    console.log('self-test OK — masked passes; a real address in author metadata fails;');
    console.log('               a real address in a co-author trailer fails.');
    return true;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// ---------------------------------------------------------------------------

const arg = process.argv[2];
if (arg === '--self-test') process.exit(selfTest() ? 0 : 1);

const range = arg && arg !== '--all' ? arg : null;
const cwd = process.cwd();
const problems = [...inspect(range, cwd), ...inspectTrailers(range, cwd)];
if (!report(problems)) process.exit(1);
console.log(`commit metadata clean${range ? ` for ${range}` : ' across all refs'}.`);
