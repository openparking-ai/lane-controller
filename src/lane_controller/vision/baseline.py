"""The shipped baseline: RapidOCR's packaged PP-OCRv3.

Present for two reasons. It is the number our own recogniser has to beat, and
it is the only third-party option whose WEIGHTS carry a stated permissive
licence -- Apache-2.0, shipped inside the package, rather than the silence
every upstream project offers on the subject (see the V-C1 table).

General-purpose OCR on plates is not expected to be good. The V-C3 probe read
3 of 4 clean synthetic plates. That is the bar, and it is a low one.
"""

from __future__ import annotations

import numpy as np


class RapidOcrBaseline:
    name = "rapidocr-ppocrv3"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()

    def read(self, image: np.ndarray) -> tuple[str, float]:
        result, _ = self._ocr(image)
        if not result:
            return "", 0.0
        # Longest line wins: plate furniture (state name, slogan) is also text,
        # and is usually shorter than the registration.
        best = max(result, key=lambda r: len(str(r[1]).strip()))
        return str(best[1]).strip(), float(best[2])
