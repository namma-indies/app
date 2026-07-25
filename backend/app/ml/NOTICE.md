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
