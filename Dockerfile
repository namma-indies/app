# syntax=docker/dockerfile:1

# ---- frontend build stage -------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
ARG VITE_MULTI_ANIMAL_ENABLED=false
RUN VITE_MULTI_ANIMAL_ENABLED=$VITE_MULTI_ANIMAL_ENABLED npm run build
# produces /build/frontend/dist

# ---- backend runtime stage -------------------------------------------------
FROM python:3.12-slim AS runtime

# uv gives us fast, lockfile-pinned installs matching the dev workflow.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install backend dependencies first (better layer caching).
COPY backend/pyproject.toml backend/uv.lock /app/backend/
RUN cd /app/backend && uv sync --frozen --no-install-project

# Now copy the rest of the backend source.
COPY backend/ /app/backend/
RUN cd /app/backend && uv sync --frozen

# Static-serve layout constraint: backend/app/main.py resolves the frontend
# dist dir as Path(__file__).resolve().parents[2] / "frontend" / "dist",
# i.e. relative to /app/backend/app/main.py that is /app/frontend/dist.
# Preserve that relative layout in the image.
COPY --from=frontend /build/frontend/dist /app/frontend/dist

COPY deploy/entrypoint.sh /app/deploy/entrypoint.sh
RUN chmod +x /app/deploy/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
