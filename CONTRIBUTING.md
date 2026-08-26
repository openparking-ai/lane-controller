# Contributing to Open Parking AI

Contributions are welcome. This page is short on purpose; everything on it is
enforced mechanically, so there is nothing to remember.

## Before your first pull request: sign the CLA

Read [CLA.md](CLA.md), then open a pull request that adds one entry to
`cla/signatures.json` and changes nothing else:

```json
{ "github": "your-github-login", "name": "Your Full Legal Name", "date": "YYYY-MM-DD" }
```

That pull request is your signature. Once it is merged, your later pull requests
pass the CLA check automatically.

The CLA grants 72 Knots the right to relicense contributions. Section 3 of
[CLA.md](CLA.md) explains why in plain terms. If you are not comfortable with
that clause, please do not contribute — it is not negotiable, and it is better
to know before you spend time on a change.

## How a change gets in

1. Open an issue first for anything larger than a fix. Agreeing on the approach
   is cheaper than reviewing the wrong one.
2. Branch from `main`. Nobody pushes to `main` directly; the branch protection
   refuses it.
3. Open a pull request. Three checks must be green before it can merge: `lint`,
   `test` and `cla`.
4. A maintainer reviews and merges. Opening the pull request is not merging it.

## What gets rejected on sight

**Anything that handles a raw card number.** Payments are processor-tokenized,
end to end. If a primary account number can reach a variable in this codebase,
the design is wrong, not the code.

**A tenant-owned table without row-level security.** Every table holding tenant
data follows the template in the platform repository's `docs/RLS_TEMPLATE.md` —
tenant column, `ENABLE` **and** `FORCE ROW LEVEL SECURITY`, and a policy with
both `USING` and `WITH CHECK`. Application-level scoping does not substitute for
it; the point is that two independent controls have to fail before data crosses
a tenant boundary.

**A test that has never been seen to fail.** If you add a control, show it
failing when the thing it protects is removed. `npm run rls-fail-control` in the
platform repository is the worked example.

**A silent guess in the lane.** When vehicle identification is not confident
enough, the lane takes its declared fallback path. It does not pick the most
likely answer and open the gate.

## Style

Match the code already there — its naming, its comment density, its idioms. A
change that reads like the file it lands in is easier to review than a better
one that does not.

Comments should say why, not what.

---

Built by 72 Knots. Method by [72Knots.ai](https://72knots.ai)
