# Aura in a container.
#
#   docker build -t aura .
#   docker run -it --rm -v aura-data:/data -v "$PWD":/work \
#       -e AI_BASE_URL=https://api.openai.com/v1 -e AI_MODEL=<model> -e AI_API_KEY \
#       aura
#
#   - uv copied from its official image, pinned to the version the installers
#     use (docs.astral.sh/uv/guides/integration/docker); Python base pinned
#     by digest (refresh: hub.docker.com, tag 3.12-slim-trixie).
#   - Aura's own sandbox (Landlock) works inside: Docker's default seccomp
#     profile allows landlock_* (moby/profiles seccomp/default.json); the
#     host kernel needs 5.13+. No --privileged, no extra capabilities.
#   - no system keychain in a container: pass keys as environment variables
#     (-e AI_API_KEY …); anything Aura saves lives in the /data volume.
#   - a model server on the host is not "localhost" from inside: use
#     http://host.docker.internal:11434/v1 (Linux: add
#     --add-host=host.docker.internal:host-gateway).
#   - runs as a normal user (uid 1000), not root.
#
# Add-ons are the user's choice (none by default): --build-arg EXTRAS=vault,web adds
# the key vault and the browser tool; its Chromium
# is downloaded at build time then (about 150 MB more).

ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

# ------------------------------------------------------------------ build --
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
ARG EXTRAS=
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv venv /opt/aura \
 && uv pip install --python /opt/aura/bin/python ".${EXTRAS:+[${EXTRAS}]}"

# -------------------------------------------------------------------- run --
FROM ${PYTHON_IMAGE}
ARG EXTRAS=
COPY --from=build /opt/aura /opt/aura
ENV PATH=/opt/aura/bin:$PATH \
    AURA_HOME=/data \
    AURA_KEYSTORE=file \
    PYTHONUNBUFFERED=1
# the browser tool only when asked for (its Chromium + system libraries)
RUN case ",${EXTRAS}," in *,web,*) \
        PLAYWRIGHT_BROWSERS_PATH=/opt/playwright /opt/aura/bin/python -m playwright install --with-deps chromium \
        && chmod -R a+rX /opt/playwright ;; esac \
 && useradd --create-home --uid 1000 aura \
 && mkdir -p /data /work && chown aura:aura /data /work
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
USER aura
VOLUME ["/data"]
WORKDIR /work
ENTRYPOINT ["aura"]
CMD ["chat"]
