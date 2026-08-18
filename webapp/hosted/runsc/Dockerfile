# syntax=docker/dockerfile:1
# Minimal, non-root distroless image for the SE Skills post-call sandbox.
#
# The runtime contains only the typed-tool loop, the runtime contract, and the
# entry point. It has no shell, package manager, Git binary, cloud credentials,
# or Anthropic API key. The worker supplies a serialized RuntimeJob and mounts
# read-only inputs and a read-write output directory at runtime.

# Builder stage: install the runtime dependencies into a virtual environment.
FROM python:3.11-slim-bookworm@sha256:bb3a5d38989ec658710f06b08bc23cb78d079eb852405e42b124fdf430281454 AS builder

WORKDIR /app

# Create a virtual environment with a known path. The runtime stage copies it
# verbatim and runs Python with PYTHONPATH pointing at the site-packages.
RUN python3 -m venv /app/venv

COPY webapp/hosted/runsc/requirements.txt /tmp/requirements.txt
RUN /app/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /app/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# Copy only the source files the sandbox needs. The full repo is not included.
COPY webapp/hosted/runtime_contract.py /app/webapp/hosted/runtime_contract.py
COPY webapp/hosted/agent_loop_harness.py /app/webapp/hosted/agent_loop_harness.py
COPY webapp/hosted/runsc/sandbox_entry.py /app/webapp/hosted/runsc/sandbox_entry.py

# Prepare the /runtime directory so the runtime stage can mount
# /runtime/input and /runtime/output over it.
RUN mkdir -p /tmp/runtime && chmod 755 /tmp/runtime

# Runtime stage: distroless Python 3.11 (Debian 12). No shell, apt, or root login.
FROM gcr.io/distroless/python3-debian12@sha256:0f8ca62dea61023c1fe02e445bd154ee02b2d97aec377f965ae3c641ec838e61

# The distroless image runs as nonroot (65532) by default. Explicitly enforce.
USER 65532:65532

ENV PYTHONPATH=/app/venv/lib/python3.11/site-packages:/app
ENV SE_RUNTIME_JOB_PATH=/runtime/job.json
ENV SE_RUNTIME_RESULT_PATH=/runtime/output/result.json

# Copy the installed runtime and source from the builder. Files remain owned by
# root (0) and readable by the nonroot user, which prevents sandbox tampering.
COPY --from=builder /app/venv /app/venv
COPY --from=builder /app/webapp /app/webapp
COPY --from=builder /tmp/runtime /runtime

ENTRYPOINT ["/usr/bin/python3", "/app/webapp/hosted/runsc/sandbox_entry.py"]
