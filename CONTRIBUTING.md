# Contributing to Open Parking AI

Open Parking AI does not accept outside contributions. Pull requests, issues and comments are limited to the maintainers.

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

Built by 72 Knots Method by 72Knots.ai
