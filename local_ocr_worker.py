"""Disposable RapidOCR process used to keep model memory out of the bot."""

import json
import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")


def main():
    if len(sys.argv) != 2:
        raise SystemExit(2)
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    with Image.open(sys.argv[1]) as source:
        image=source.convert("RGB")
        engine=RapidOCR()
        result,_=engine(np.asarray(image))
    lines=[str(row[1]).strip() for row in result or [] if len(row)>=2 and str(row[1] or '').strip()]
    sys.stdout.write(json.dumps(lines,ensure_ascii=False))


if __name__ == "__main__":
    main()
