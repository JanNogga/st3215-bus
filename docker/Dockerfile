# Cross-platform wheel builder for st3215-bus.
#
# Builds Linux wheels for every supported target from either an x86_64 or an
# aarch64 host, natively (no QEMU): rustc cross-compiles and maturin links with
# zig, which also provides the glibc/musl stubs for the chosen baseline.
#
# The source is NOT baked in. docker/build.sh mounts the checkout at /src and
# runs maturin there, so one image builds any checkout. Use build.sh rather
# than this file directly.
#
# Every tool is pinned; bump the versions here deliberately.

ARG RUST_VERSION=1.99.0
ARG UV_VERSION=0.12.20

# uv ships as a multi-arch image; copying its binaries avoids arch-specific
# download URLs.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM rust:${RUST_VERSION}-slim-bookworm

# Rust standard libraries for every target build.sh knows about, plus clippy
# (CI runs `cargo clippy --locked --all-targets`) for the pinned toolchain.
# Changes rarely, so it comes first for layer caching. (The host's own target
# is already installed; adding it again is a no-op.)
RUN rustup component add clippy \
    && rustup target add \
        x86_64-unknown-linux-gnu \
        aarch64-unknown-linux-gnu \
        armv7-unknown-linux-gnueabihf \
        x86_64-unknown-linux-musl \
        aarch64-unknown-linux-musl \
    && rm -rf /usr/local/rustup/downloads /usr/local/rustup/tmp

COPY --from=uv /uv /uvx /usr/local/bin/

# Interpreters and tools live under /opt so that the unprivileged user
# build.sh runs as (the invoking user's uid/gid) can use them read-only.
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_NO_CACHE=1

# CPython 3.8-3.13 for the smoke tests (the pinned uv fixes the patch
# versions). 3.13 also runs maturin itself.
RUN uv python install 3.8 3.9 3.10 3.11 3.12 3.13

# maturin with zig for linking, plus auditwheel for checking platform tags.
# zig is linked onto PATH so maturin finds it without relying on `python3 -m
# ziglang`.
# (Declared here, not at the top, so bumping them keeps the layers above cached.)
ARG MATURIN_VERSION=1.15.0
ARG ZIGLANG_VERSION=0.16.0
ARG AUDITWHEEL_VERSION=6.8.2
ENV PATH=/opt/tools/bin:$PATH
RUN uv venv --python 3.13 /opt/tools \
    && uv pip install --python /opt/tools/bin/python \
        "maturin[zig]==${MATURIN_VERSION}" \
        "ziglang==${ZIGLANG_VERSION}" \
        "auditwheel==${AUDITWHEEL_VERSION}" \
    && ln -s "$(/opt/tools/bin/python -c 'import ziglang, os; print(os.path.join(os.path.dirname(ziglang.__file__), "zig"))')" /usr/local/bin/zig \
    && zig version && maturin --version && auditwheel --version

WORKDIR /src
