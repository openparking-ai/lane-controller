#!/usr/bin/env python3
"""The control for the phone prompt and the validation claimed when the phone is entered.

Runs tests/test_reader_validation.py once intact, where it must pass, and once
per break, where it must fail. A prompt that has never been seen to show the
undiscounted amount first, or to log a number, is not known not to.

  shown_before_the_claim           the fee as priced goes up, and the discount corrects it after.
  fee_worked_out_by_the_lane       the discounted fee is added up here, not read.
  skip_still_claims                Skip asks the platform anyway.
  failure_logged_with_its_message  a failed claim is logged with what it carried.
  another_fees_answer_shown        an answer made on another fee is put up.
  no_prompt                        the reader never asks for a phone.
  receipt_only_wording             the words offer a receipt and not the validation.
  number_in_the_url                the phone travels in the URL.
"""

from __future__ import annotations

import sys

from _control import intact, judge, run

SUITE = ["-q", "tests/test_reader_validation.py"]
BREAKAGES = [
    ("shown_before_the_claim", "the fee as priced goes up, and the discount corrects it after"),
    ("fee_worked_out_by_the_lane", "the discounted fee is added up here, not read"),
    ("skip_still_claims", "Skip asks the platform anyway"),
    ("failure_logged_with_its_message", "a failed claim is logged with what it carried"),
    ("another_fees_answer_shown", "an answer made on another fee is put up"),
    ("no_prompt", "the reader never asks for a phone"),
    ("receipt_only_wording", "the words offer a receipt and not the validation"),
    ("number_in_the_url", "the phone travels in the URL"),
]


failures = 0

print("== control A: the validation-prompt suite must PASS intact ==")
collected, _ = intact(SUITE, {"BREAK_VALIDATION_PROMPT": ""})
if collected < 0:
    failures += 1

print("\n== control B: each breakage must make it FAIL ==")
for mode, description in BREAKAGES:
    if not judge(mode, description, collected, run(SUITE, {"BREAK_VALIDATION_PROMPT": mode})):
        failures += 1

if failures:
    print(
        f"\n{failures} control(s) failed. Do not trust the validation-prompt tests.",
        file=sys.stderr,
    )
    sys.exit(1)
print("\nall controls OK — the validation-prompt suite fails when the prompt is broken.")
