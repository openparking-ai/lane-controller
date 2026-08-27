# Evaluation data, and why most of it cannot be used

Every accuracy number Open Parking AI publishes comes from
`scripts/eval_plates.py`. This page is the audit of what that harness is allowed
to run on, and why the answer is mostly "nothing public".

## The rule

A dataset is usable only if its licence permits **commercial use**. "Free for
research" is not free for us. And nothing is committed to this repository: eval
data is downloaded locally by a script, or generated.

## What was checked

| Dataset | Licence | Usable | Note |
|---|---|---|---|
| Roboflow / Open Images — License Plates (US/EU) | images CC BY 2.0, annotations CC BY 4.0 | **yes**, with attribution | Only **350 images**, bounding boxes only — no plate text. A real-image sanity check, never a benchmark. |
| CCPD | **MIT** | legally yes | Chinese plates. Tells us nothing about Florida accuracy. |
| UFPR-ALPR | academic research only, non-commercial | **no** | Explicitly excludes commercial use. |
| VeRi-776 | non-commercial, email request | **no** | Also: "due to the privacy issue we will not provide the license plates in the future". |
| Stanford Cars | ImageNet-style, research | **no** | Still available, still not commercially licensed. |

Sources are linked from `scripts/fetch_eval_data.py` and the receipt.

**There is no adequately licensed public set for US plate READING.** That is a
finding, not a failure — and it is why the recogniser is trained on generated
plates instead.

## What we use instead

`lane_controller.vision.plates.generator` produces both the training and the
evaluation sets. The eval set is re-derived from a **seed**, so it is
reproducible without being stored, and there is nothing to accidentally commit.

Two consequences worth stating plainly:

**The training data has no licence question**, because we made it. The weights
that come out are Open Parking AI's own asset, under our own terms, with no
upstream able to withdraw them.

**Synthetic accuracy is not real accuracy.** The generator uses OpenCV's
built-in fonts; real plates use embossing typefaces we neither have nor could
redistribute. That is the largest domain gap, and it is why the harness prints
real-plate accuracy as NOT MEASURABLE rather than quoting the synthetic number
and hoping. The real number comes from the physical bench, from our own
vehicles, kept local, never committed.

## Attribution

If the Roboflow/Open Images set is used, CC BY requires attribution: Open Images
Dataset (Google LLC), curated by Roboflow. `scripts/fetch_eval_data.py` writes
the notice alongside the download.

---

Built by 72 Knots Method by 72Knots.ai
