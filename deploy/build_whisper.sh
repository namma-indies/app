#!/bin/sh
# Build the whisper-cli the GPU worker transcribes with, and print its pins.
#
#   deploy/build_whisper.sh OUTPUT_DIR [CUDA_ARCH]          # anywhere with docker
#   deploy/build_whisper.sh --here OUTPUT_DIR [CUDA_ARCH]   # inside a pod/container
#                                                           # of the worker's NGC image
#
# The DGX cluster has no Docker (Podman only), so --here is the usual route
# there: run it in a separate test pod from the worker's NGC image, never the
# production pod, writing OUTPUT_DIR onto the model PVC.
#
# Runs the compile inside the same NGC image the worker runs on (PVC_IMAGE in
# render_gpu_worker.py, FROM in backend/gpu_worker/Dockerfile), so the binary
# links against exactly the CUDA runtime and cuBLAS it will find there.
# ggml/whisper are linked statically: the result is one file, pinned and
# snapshotted by hash like the ONNX models, so neither runtime mode needs an
# image rebuild or a venv change to carry it.
#
# CUDA_ARCH is the GPU's compute capability without the dot: 90 for H100/H200
# (the default), 80 for A100. Check with `nvidia-smi --query-gpu=compute_cap
# --format=csv` on the DGX. A MIG slice reports its parent GPU's capability.
#
# Also fetches ggml-large-v3 and the silero VAD model. Copy all three onto the
# model PVC and put the printed paths/hashes into the renderer's config as
# whisper_{bin,model,vad}_{file,sha256}. Needs network access, and docker
# unless --here; the build itself needs no GPU.
set -eu

HERE=0
if [ "${1:-}" = "--here" ]; then HERE=1; shift; fi
OUT=${1:?usage: build_whisper.sh [--here] OUTPUT_DIR [CUDA_ARCH]}
ARCH=${2:-90}
# The revision smoke-tested against ggml-large-v3 for this pipeline
# (whisper-cpp-large-v3-vad-v1). A new revision is a new pipeline version.
WHISPER_REV=231638a0717b7822784297e5980965ffcec05810
IMAGE=nvcr.io/nvidia/pytorch@sha256:d1eac6220dd98ef5870b1a76673cfb6f84451135a6d8a174cb92258a6bf4576d
MODELS=https://huggingface.co/ggerganov/whisper.cpp/resolve/main
VAD=https://huggingface.co/ggml-org/whisper-vad/resolve/main

case "$ARCH" in *[!0-9]*|'') echo "CUDA_ARCH must be digits, e.g. 90" >&2; exit 2;; esac
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

# Where the binary lands: the mounted OUT under docker, OUT itself in place.
if [ "$HERE" = 1 ]; then DEST=$OUT; else DEST=/out; fi
BUILD="
  git clone -q https://github.com/ggml-org/whisper.cpp /tmp/whisper-src
  git -C /tmp/whisper-src checkout -q $WHISPER_REV
  cmake -S /tmp/whisper-src -B /tmp/whisper-build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=$ARCH -DGGML_NATIVE=OFF \
    -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF
  cmake --build /tmp/whisper-build -j \$(nproc) --target whisper-cli
  install -m 0555 /tmp/whisper-build/bin/whisper-cli $DEST/whisper-cli
  # Anything left dynamically linked must come from the runtime image.
  ldd $DEST/whisper-cli
"
if [ "$HERE" = 1 ]; then
  bash -euc "$BUILD"
else
  docker run --rm --platform linux/amd64 -v "$OUT:/out" "$IMAGE" bash -euc "$BUILD"
fi

curl -fsSL -o "$OUT/ggml-large-v3.bin" "$MODELS/ggml-large-v3.bin"
curl -fsSL -o "$OUT/ggml-silero-v6.2.0.bin" "$VAD/ggml-silero-v6.2.0.bin"

if command -v sha256sum >/dev/null; then SHA="sha256sum"; else SHA="shasum -a 256"; fi
cd "$OUT"
echo
echo "whisper.cpp $WHISPER_REV, sm_$ARCH. Pins for render_gpu_worker.py:"
for f in whisper-cli ggml-large-v3.bin ggml-silero-v6.2.0.bin; do
  echo "  $f  $($SHA "$f" | cut -d' ' -f1)"
done
