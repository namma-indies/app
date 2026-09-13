# Bundled model notice

`miewid_msv3.onnx` is **MiewID-msv3** (`conservationxlabs/miewid-msv3` on
HuggingFace, revision `4f1d7f2b521149e5fe34bb85f377248ce9971a7d`), exported to
ONNX for CPU inference in `app/embed.py`.

**Upstream declares no licence.** The HuggingFace model card carries no licence
field, so redistribution terms are unknown. For that reason -- and because the
export is 206 MB -- the weights are **not committed**; they are produced locally
by `scripts/export_miewid_onnx.py`. Resolve the licence question with the
authors (Wild Me / Conservation X Labs) before shipping the weights inside any
distributed artifact or public image.

The `app/embed.py` interface is model-agnostic in the same way `detect_reid.py`
is: it needs a fixed-size crop in and a float vector out. Swapping the embedder
means changing the ONNX file, `MODEL_NAME`, and `EMBED_DIM` -- plus a schema
change if the new dimension differs from 2152.

---

`yolo26x.onnx` is **Ultralytics YOLO26x**, distributed under the **AGPL-3.0**
license (https://github.com/ultralytics/ultralytics), used by
`app/detect_reid.py` to locate and score the animal before embedding. Not
committed (223 MB); produced by `scripts/export_yolo26x_onnx.py`.

Implications to resolve before any public/commercial launch (flagged, not
decided): AGPL's network-use terms can extend to the combined service. Options
when that matters — obtain an Ultralytics commercial license, or swap this
weight for a permissively-licensed detector (the `detect_reid.py` interface
needs a box plus a confidence per animal class).

This replaced an earlier, separate YOLOv8n presence gate: it missed roughly
half of harder real-world photos (9/17 vs YOLO26x's 15/17, measured on a
varied set) and scored a clearly visible dog at 0.021 where YOLO26x gives
0.800. `app/detect_reid.py` now does both jobs -- locating and scoring -- with
this one model.
