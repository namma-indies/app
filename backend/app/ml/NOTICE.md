# Bundled model notice

`yolov8n.onnx` is **Ultralytics YOLOv8n**, distributed under the **AGPL-3.0**
license (https://github.com/ultralytics/ultralytics).

The rest of this repository is MIT-licensed. This model is an **optional,
self-contained component** used only for the server-side dog-presence gate on
photo capture (`app/detect.py`). It is loaded at runtime via ONNX Runtime and
is not linked into any client-shipped code.

Implications to resolve before any public/commercial launch (flagged, not
decided): AGPL's network-use terms can extend to the combined service. Options
when that matters — obtain an Ultralytics commercial license, or swap this
weight for a permissively-licensed detector/classifier (the `detect.py`
interface is model-agnostic: it only needs a max 'dog'-class confidence).

---

`miewid_msv3.onnx` is **MiewID-msv3** (`conservationxlabs/miewid-msv3` on
HuggingFace, revision `4f1d7f2b521149e5fe34bb85f377248ce9971a7d`), exported to
ONNX for CPU inference in `app/embed.py`.

**Upstream declares no licence.** The HuggingFace model card carries no licence
field, so redistribution terms are unknown. For that reason -- and because the
export is 206 MB -- the weights are **not committed**; they are produced locally
by `scripts/export_miewid_onnx.py`. Resolve the licence question with the
authors (Wild Me / Conservation X Labs) before shipping the weights inside any
distributed artifact or public image.

The `app/embed.py` interface is model-agnostic in the same way `detect.py` is:
it needs a fixed-size crop in and a float vector out. Swapping the embedder
means changing the ONNX file, `MODEL_NAME`, and `EMBED_DIM` -- plus a schema
change if the new dimension differs from 2152.

---

`yolo26x.onnx` is **Ultralytics YOLO26x**, same **AGPL-3.0** terms as
`yolov8n.onnx` above, used by `app/detect_reid.py` to locate the animal before
embedding. Not committed (223 MB); produced by `scripts/export_yolo26x_onnx.py`.

Why a second, much larger detector: the yolov8n presence gate is tuned for
speed on deliberate close-up captures and misses roughly half of harder
real-world photos (9/17 vs 14/17 measured on a varied set). A missed box means
no embedding and a sighting that can never be matched, with nothing surfacing
as an error. The gate keeps yolov8n; only the background embedding path pays
the extra ~300 ms.
