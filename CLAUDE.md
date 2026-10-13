# CLAUDE.md

Orientation for coding agents. User-facing documentation (API, exporter,
platforms, licensing) is in [README.md](README.md); do not duplicate it here.

## What this is

`st3215-bus` is a PyPI package (maturin, PyO3, abi3) of Python bindings for
Feetech ST3215 servo buses. The Rust extension compiles NormaCore's ST3215 wire
protocol unchanged from a pinned git submodule and adds only the batching: one
`Buses` call reads or writes every motor on every bus, buses driven
concurrently on tokio with the GIL released. It also ships
`st3215-export-calibration`, a pure-Python tool that copies saved calibration
ranges out of a running NormaCore station into JSON.

## Layout

- `src/lib.rs`: the extension (`Buses`, `COUNTS_PER_TURN`); pulls in the protocol with `#[path]`.
- `build.rs`: only fails early, with a clear message, if the submodule is missing.
- `python/st3215_bus/`: the package. `__init__.py` re-exports the extension
  `st3215_bus.st3215_bus`; `__init__.pyi` + `py.typed` are the type stubs;
  `export_calibration.py` is the calibration exporter.
- `tests/`: stdlib `unittest` tests of the exporter against a fake station; no hardware.
- `docker/Dockerfile`: pinned builder image (Rust, uv, CPython 3.8-3.13, maturin + zig, auditwheel).
- `docker/build.sh`: builds and checks wheels/sdist into `dist/`; `--help` documents everything.
- `docker/check_wheel.py`: verifies a wheel's platform tags, ELF arch and glibc symbol versions.
- `docker/smoke_test.py`: imports an installed wheel, runs the CLI and the unit tests.
- `.github/workflows/ci.yml`: cargo test + clippy, unit tests (3.8, 3.13), x86_64/aarch64 wheels, sdist, PyPI publish.
- `third_party/norma-core/`: NormaCore submodule. Only
  `software/drivers/st3215/src/protocol/` is compiled (plus its `LICENSE`, which ships).

## Build and test

Clone with `git clone --recurse-submodules`, or run `git submodule update --init`.

```sh
python3 -m unittest discover -s tests       # exporter tests, from the sources; no build needed
docker/build.sh                             # x86_64 + aarch64 wheels, tag checks, smoke tests
docker/build.sh --targets aarch64 --sdist   # one target plus the sdist (checks sdist contents)
docker/build.sh --targets all --sdist --no-test
```

Rust tests (NormaCore's protocol tests) in the builder image, which
`docker/build.sh` builds as `st3215-bus-builder`:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/src" -w /src -e HOME=/tmp \
  -e CARGO_HOME=/src/target/docker/cargo -e CARGO_TARGET_DIR=/src/target/docker/build \
  st3215-bus-builder cargo test --locked
```

Clippy, as in CI, in the same image:

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/src" -w /src -e HOME=/tmp \
  -e CARGO_HOME=/src/target/docker/cargo -e CARGO_TARGET_DIR=/src/target/docker/build \
  st3215-bus-builder cargo clippy --locked --all-targets
```

Stub check, against the latest wheel `docker/build.sh` built for this host's
architecture (`target/docker/wheels/<arch>/`; `dist/` may hold stale ones):

```sh
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/src" -w /tmp -e HOME=/tmp \
  st3215-bus-builder sh -ec '
    uv venv -q --python 3.13 v && uv pip install -q --python v/bin/python mypy
    uv pip install -q --python v/bin/python --no-index \
      --find-links "/src/target/docker/wheels/$(uname -m)" st3215-bus
    echo st3215_bus.st3215_bus > allow.txt
    v/bin/python -m mypy.stubtest --allowlist allow.txt st3215_bus'
```

The allowlist line is needed because the stubs live in `__init__.pyi`, not in
a stub for the extension submodule.

## Rules

- **NormaCore code is compiled, never copied or edited.** Do not modify or copy
  anything from `third_party/`. Bumping the submodule pin is a deliberate change:
  re-run the cargo and unit tests, and update the revision in README.md and
  `TESTED_AGAINST` in `export_calibration.py`.
- **The Python API of `Buses` is stable.** Do not rename or change signatures;
  `__init__.pyi` must match the extension exactly (stubtest above must pass).
- **Python 3.8 floor** (abi3-py38, `requires-python >=3.8`): the package,
  `tests/` and `docker/smoke_test.py` must run on 3.8 (`check_wheel.py` runs
  only in the builder's 3.13).
- **The exporter stays stdlib-only and read-only.** It sends only `setup`, `ping`
  and `read` requests; never add a `write` (ClientRequest field 10) encoder or
  any dependency, and never import the compiled extension from it.
- **Wheels:** manylinux_2_17 by default in `docker/build.sh`; CI builds manylinux2014
  (the same baseline). The sdist must contain the protocol sources and NormaCore's
  `LICENSE` (`Cargo.toml` and `pyproject.toml` `include`; `build.sh --sdist` checks).
- **Release:** version in `Cargo.toml` (pyproject's version is dynamic); a `vX.Y.Z`
  tag runs ci.yml's `publish` job, PyPI trusted publishing via environment `pypi`.
- **Licensing and tone:** credit NormaCore positively. Users must adhere to
  NormaCore's licensing terms for the NormaCore-derived parts; keep its notice shipping.
- **Git:** the owner (Jan) does all commits, pushes, tags and releases. Agents
  must not commit, push, tag or change what is staged.
- No hardware or serial access from tests or tools; Docker containers `--rm` only.
