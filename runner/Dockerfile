# wf-runner as a Docker image — run an exported workflow bundle with Docker.
#
# Published (public) as ghcr.io/icicle-ai/workflow-runner — users just pull it:
#   docker pull ghcr.io/icicle-ai/workflow-runner:v0.1.2
#
# Rebuilding (maintainers only; --platform because Apptainer's .deb is amd64):
#   docker build --platform linux/amd64 -t ghcr.io/icicle-ai/workflow-runner:<tag> runner/
#
# Run (mount host paths at their OWN paths, so the bundle's absolute paths and
# the run report mean the same thing inside and out):
#   docker run --rm --privileged \
#     -v "$PWD":"$PWD" -w "$PWD" -v /data:/data \
#     ghcr.io/icicle-ai/workflow-runner:v0.1.2 bundle.json --data-root /data --workdir ./wf-local --images-dir /data/images
#
# Steps still run as Apptainer .sif images — that is the format every step
# image is published in — so Apptainer is installed here and started once per
# step INSIDE this container. That nesting is why --privileged is required.
# Where Docker isn't available or privileged containers aren't allowed, use
# wf_runner.def (Apptainer) or run `python3 -m wf_runner` on the host instead;
# all three execute the same code.
#
# After pushing a new tag, update the default in backend/engine/deployment.py
# (or set WF_RUNNER_DOCKER_IMAGE) so the Deploy dialog points users at it.

FROM python:3.12-slim-bookworm

# Apptainer ships as a .deb on its GitHub releases (amd64 only); apt resolves
# its runtime dependencies (squashfuse, fuse2fs, uidmap, ...).
ARG APPTAINER_VERSION=1.3.6
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL -o /tmp/apptainer.deb \
      "https://github.com/apptainer/apptainer/releases/download/v${APPTAINER_VERSION}/apptainer_${APPTAINER_VERSION}_amd64.deb" \
 && apt-get install -y --no-install-recommends /tmp/apptainer.deb \
 && rm -f /tmp/apptainer.deb \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

COPY wf_runner /app/wf_runner

RUN python3 -c "import wf_runner; print('runner OK', wf_runner.__version__)" \
 && apptainer --version

LABEL org.opencontainers.image.title="wf-runner" \
      org.opencontainers.image.description="Runs a Workflow Orchestrator bundle on a self-hosted node; each step runs as its own Apptainer image." \
      org.opencontainers.image.version="0.1.2"

ENTRYPOINT ["python3", "-m", "wf_runner"]
