# Build only from an exported, reviewed commit (no local/private files):
# git archive <full-commit> | docker build --platform linux/amd64 \
#   --build-arg SOURCE_REVISION=<full-commit> -f backend/gpu_worker/Dockerfile -
# Publish through your approved registry workflow; deploy its resulting digest,
# never the build tag. No credentials, models or live source mounts in the image.
# This is the validated NGC 25.04 CUDA-library base, NOT a newly validated image.
FROM nvcr.io/nvidia/pytorch@sha256:d1eac6220dd98ef5870b1a76673cfb6f84451135a6d8a174cb92258a6bf4576d
ARG SOURCE_REVISION
RUN python3 -c 'import re,sys; assert sys.version_info[:2] == (3,12); assert re.fullmatch("[0-9a-f]{40}", sys.argv[1])' "$SOURCE_REVISION"
LABEL org.opencontainers.image.revision=$SOURCE_REVISION

# Do not inherit NGC's Python packages (including torch or CPU ORT).
RUN python3 -m venv /opt/worker-venv
ENV PATH="/opt/worker-venv/bin:$PATH" \
    PYTHONPATH=/opt/worker \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    TMPDIR=/scratch \
    HOME=/scratch \
    MEDIA_GPU_PROGRESS_FILE=/scratch/progress
COPY backend/gpu_worker/requirements.txt /opt/worker/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/worker/requirements.txt \
    && python -m pip check \
    && python -c 'import importlib.util; assert importlib.util.find_spec("torch") is None; import onnxruntime as ort; assert "CUDAExecutionProvider" in ort.get_available_providers()' \
    && python -m pip freeze > /opt/worker/installed.txt
# Direct versions are pinned; installed.txt records the resolved transitive set.
# This is not a hash-locked dependency build. Archive the built image by digest.
WORKDIR /opt/worker
COPY backend/gpu_worker/__init__.py backend/gpu_worker/__main__.py backend/gpu_worker/client.py backend/gpu_worker/clip.py backend/gpu_worker/inference.py backend/gpu_worker/supervise.py backend/gpu_worker/multi.py ./gpu_worker/
COPY backend/app/__init__.py backend/app/analyse.py backend/app/detect.py backend/app/detect_reid.py backend/app/embed.py backend/app/photos.py backend/app/video.py backend/app/config.py backend/app/tracking.py ./app/
USER 10001:10001
ENTRYPOINT ["/opt/worker-venv/bin/python", "-m", "gpu_worker.supervise"]
