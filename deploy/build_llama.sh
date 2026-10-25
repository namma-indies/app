#!/bin/sh
# Build the llama-server the GPU worker suggests details with, fetch its model,
# and print the pins for render_gpu_worker.py (llama_{bin,model,mmproj}_*).
#
#   deploy/build_llama.sh OUTPUT_DIR [CUDA_ARCH]          # anywhere with docker
#   deploy/build_llama.sh --here OUTPUT_DIR [CUDA_ARCH]   # inside a pod/container
#                                                         # of the worker's NGC image
#
# The DGX cluster has no Docker, so --here is the usual route there: start a
# separate test pod from the worker's pinned NGC image (never the production
# pod), run this inside it, and write OUTPUT_DIR onto the model PVC. Either way
# the binary links against the CUDA runtime of the image the worker runs on.
#
# CUDA_ARCH is the compute capability without the dot (default 90 for
# H100/H200); read it in the pod with
#   nvidia-smi --query-gpu=compute_cap --format=csv
#
# Model: Qwen3.8-27B, Unsloth Dynamic 3.0 UD-Q4_K_XL (~17.6 GB) and its F16
# vision projector (~0.9 GB). Apache-2.0. About 21 GB of the slice while the
# worker runs, beside whisper and the ONNX models.
set -eu

HERE=0
if [ "${1:-}" = "--here" ]; then HERE=1; shift; fi
OUT=${1:?usage: build_llama.sh [--here] OUTPUT_DIR [CUDA_ARCH]}
ARCH=${2:-90}
# The revision tested with Qwen3.8-27B + mmproj through the worker's own
# request path (gpu_worker/llm.py). A new revision is a new deployment pin.
LLAMA_REV=3f03b17724ad8b69040825f45c43c59b092a0244
IMAGE=nvcr.io/nvidia/pytorch@sha256:d1eac6220dd98ef5870b1a76673cfb6f84451135a6d8a174cb92258a6bf4576d
MODELS=https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main

case "$ARCH" in *[!0-9]*|'') echo "CUDA_ARCH must be digits, e.g. 90" >&2; exit 2;; esac
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)

# Where the binary lands: the mounted OUT under docker, OUT itself in place.
if [ "$HERE" = 1 ]; then DEST=$OUT; else DEST=/out; fi
BUILD="
  git clone -q https://github.com/ggml-org/llama.cpp /tmp/llama-src
  git -C /tmp/llama-src checkout -q $LLAMA_REV
  cmake -S /tmp/llama-src -B /tmp/llama-build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=$ARCH -DGGML_NATIVE=OFF -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
  cmake --build /tmp/llama-build -j \$(nproc) --target llama-server
  install -m 0555 /tmp/llama-build/bin/llama-server $DEST/llama-server
  # Anything left dynamically linked must come from the runtime image.
  ldd $DEST/llama-server
"
if [ "$HERE" = 1 ]; then
  bash -euc "$BUILD"
else
  docker run --rm --platform linux/amd64 -v "$OUT:/out" "$IMAGE" bash -euc "$BUILD"
fi

curl -fsSL -o "$OUT/Qwen3.8-27B-UD-Q4_K_XL.gguf" "$MODELS/Qwen3.8-27B-UD-Q4_K_XL.gguf"
curl -fsSL -o "$OUT/mmproj-F16.gguf" "$MODELS/mmproj-F16.gguf"

if command -v sha256sum >/dev/null; then SHA="sha256sum"; else SHA="shasum -a 256"; fi
cd "$OUT"
echo
echo "llama.cpp $LLAMA_REV, sm_$ARCH. Pins for render_gpu_worker.py:"
for f in llama-server Qwen3.8-27B-UD-Q4_K_XL.gguf mmproj-F16.gguf; do
  echo "  $f  $($SHA "$f" | cut -d' ' -f1)"
done
