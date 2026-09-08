# On-device model assets

`yolo26n_seg_<size>_fp16.tflite` belongs here, and is **not committed**.

Produce it with:

```sh
python scripts/export_mobile_models.py --formats tflite
```

## Why it is not in git

Same reason the server's weights are not: these are Ultralytics YOLO weights
under **AGPL-3.0**, and this repository is MIT and public. Committing them is a
redistribution act with terms attached, which `backend/app/ml/NOTICE.md` already
flags as unresolved. It is 6.4 MB, so size is not the objection.

The Android release workflow exports it during the build instead, so a store
build has the model without the repository carrying it.

## If it is missing

`DogSegmenter.start()` rejects with a load failure. It does not fall back to
running without segmentation, because a viewfinder that silently stops
outlining animals is indistinguishable from one that sees no animals.

## Do not compress it

`app/build.gradle` sets `noCompress += 'tflite'`. `Segmenter` memory-maps the
model straight out of the APK with `AssetManager.openFd`, and that throws on a
compressed asset with an error that mentions nothing about compression.
