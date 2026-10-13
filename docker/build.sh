#!/usr/bin/env bash
# Build Linux wheels for st3215-bus in Docker, for any supported target, from
# an x86_64 or aarch64 host. No Rust toolchain and no QEMU needed: see --help.
#
# Kept to bash 3.2 features so it also runs on macOS's stock bash.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: docker/build.sh [--targets LIST] [--manylinux X_Y] [--sdist] [--no-test]

Builds st3215-bus wheels in a Docker image (docker/Dockerfile) and writes them
to dist/. Works the same on x86_64 and aarch64 hosts: rustc cross-compiles and
maturin links with zig, so every target is built natively, without QEMU.

The wheels are abi3 (cp38-abi3): one wheel per target covers CPython 3.8 and
newer, so there is no per-Python-version build.

Options:
  --targets LIST    Comma-separated targets (default: x86_64,aarch64), or "all".
                      x86_64        manylinux, glibc   (x86_64-unknown-linux-gnu)
                      aarch64       manylinux, glibc   (aarch64-unknown-linux-gnu)
                      armv7         manylinux, glibc   (armv7-unknown-linux-gnueabihf;
                                                        32-bit hard-float, e.g.
                                                        Raspberry Pi OS 32-bit)
                      x86_64-musl   musllinux_1_2      (x86_64-unknown-linux-musl)
                      aarch64-musl  musllinux_1_2      (aarch64-unknown-linux-musl;
                                                        e.g. Alpine)
  --manylinux X_Y   glibc baseline for the manylinux targets (default: 2_17).
                    The wheel runs on any distro with glibc >= X.Y. Lower means
                    older distros: 2_17 (manylinux2014, the default) covers
                    CentOS 7-era systems and is the lowest Rust supports; 2_28
                    covers Debian 10+, Ubuntu 18.10+ and RHEL 8+. zig supplies the
                    glibc stubs, so any value from 2_17 up works for all three
                    manylinux targets, armv7 included. Ignored by the musl
                    targets, which are always musllinux_1_2 (musl >= 1.2,
                    Alpine 3.13+).
  --sdist           Also build the source distribution.
  --no-test         Skip the import smoke tests.
  -h, --help        Show this help.

After building, every wheel's platform tag is checked against the request (wheel
filename, WHEEL metadata, ELF architecture and glibc symbol versions, and
`auditwheel show` for manylinux); any mismatch fails the run.

Smoke tests install each wheel into fresh CPython 3.8 to 3.13 venvs and
check that it imports, that opening a missing port raises OSError (no serial
hardware is touched), that st3215-export-calibration --help works, and run the
unit tests in tests/ against the installed package (offline; the exporter is
tested against a fake station on 127.0.0.1). Only wheels for the host's architecture can be tested:
glibc wheels in the builder image, musl wheels in python:3.x-alpine containers
(alpine3.22, except 3.8, which has no Alpine 3.22 image: it uses alpine3.20).
Wheels for other architectures are built and tag-checked but not import-tested.

Caches (cargo registry, build dir, zig) live in target/docker/; delete it to
start clean. The container runs as your uid/gid, so nothing it writes is
root-owned.

Examples:
  docker/build.sh                          # x86_64 + aarch64 manylinux_2_17
  docker/build.sh --targets all --sdist    # every target and the sdist
  docker/build.sh --targets armv7 --manylinux 2_28
  docker/build.sh --targets aarch64-musl --no-test
EOF
}

die() { echo "error: $*" >&2; exit 2; }

ALL_TARGETS="x86_64 aarch64 armv7 x86_64-musl aarch64-musl"
IMAGE=${ST3215_BUILDER_IMAGE:-st3215-bus-builder}
ALPINE=${ST3215_SMOKE_ALPINE:-alpine3.22}
# python:3.8 stopped getting new Alpine variants after Alpine 3.20 (3.8 is EOL).
ALPINE_38=${ST3215_SMOKE_ALPINE_38:-alpine3.20}
PYTHONS="3.8 3.9 3.10 3.11 3.12 3.13"
alpine_tag() {
    case $1 in
        3.8) echo "$ALPINE_38" ;;
        *) echo "$ALPINE" ;;
    esac
}

targets_arg=x86_64,aarch64
manylinux=2_17
want_sdist=0
want_test=1
while [ $# -gt 0 ]; do
    case $1 in
        --targets) [ $# -ge 2 ] || die "--targets needs a value"; targets_arg=$2; shift 2 ;;
        --targets=*) targets_arg=${1#*=}; shift ;;
        --manylinux) [ $# -ge 2 ] || die "--manylinux needs a value"; manylinux=$2; shift 2 ;;
        --manylinux=*) manylinux=${1#*=}; shift ;;
        --sdist) want_sdist=1; shift ;;
        --no-test) want_test=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1 (see --help)" ;;
    esac
done

# --manylinux: accept 2_28, 2.28, manylinux_2_28, and 2014 (= 2_17).
manylinux=${manylinux#manylinux_}
manylinux=${manylinux//./_}
[ "$manylinux" = 2014 ] && manylinux=2_17
[[ $manylinux =~ ^2_([0-9]+)$ ]] || die "--manylinux must look like 2_28, got '$manylinux'"
[ "${BASH_REMATCH[1]}" -ge 17 ] || die "--manylinux must be 2_17 or newer (Rust's minimum glibc)"

# --targets: expand "all", validate, drop duplicates, keep order.
[ "$targets_arg" = all ] && targets_arg=${ALL_TARGETS// /,}
targets=""
for t in ${targets_arg//,/ }; do
    case " $ALL_TARGETS " in
        *" $t "*) ;;
        *) die "unknown target '$t' (supported: $ALL_TARGETS, all)" ;;
    esac
    case " $targets " in *" $t "*) ;; *) targets="$targets $t" ;; esac
done
targets=${targets# }
[ -n "$targets" ] || die "no targets given"

rust_triple() {
    case $1 in
        x86_64) echo x86_64-unknown-linux-gnu ;;
        aarch64) echo aarch64-unknown-linux-gnu ;;
        armv7) echo armv7-unknown-linux-gnueabihf ;;
        x86_64-musl) echo x86_64-unknown-linux-musl ;;
        aarch64-musl) echo aarch64-unknown-linux-musl ;;
    esac
}
wheel_arch() {
    case $1 in
        x86_64*) echo x86_64 ;;
        aarch64*) echo aarch64 ;;
        armv7) echo armv7l ;;
    esac
}
compat() {
    case $1 in
        *-musl) echo musllinux_1_2 ;;
        *) echo "manylinux_$manylinux" ;;
    esac
}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$SCRIPT_DIR/.." && pwd)

# The protocol is compiled from the NormaCore submodule; without it nothing builds.
[ -f "$REPO/third_party/norma-core/software/drivers/st3215/src/protocol/mod.rs" ] ||
    die "third_party/norma-core is missing: clone with --recurse-submodules or run \`git submodule update --init\`"
WORK=target/docker                 # relative to the repo, which is /src in the container
start=$SECONDS

host_arch=$(docker info --format '{{.Architecture}}')
case $host_arch in
    amd64) host_arch=x86_64 ;;
    arm64) host_arch=aarch64 ;;
esac

echo "==> Building image $IMAGE (docker host: $host_arch)"
docker build -t "$IMAGE" -f "$SCRIPT_DIR/Dockerfile" "$SCRIPT_DIR"

# Created here, as the invoking user, so Docker does not create them root-owned.
mkdir -p "$REPO/dist" "$REPO/$WORK"

in_builder() {
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -v "$REPO:/src" -w /src \
        -e HOME=/tmp \
        -e CARGO_HOME="/src/$WORK/cargo" \
        -e CARGO_TARGET_DIR="/src/$WORK/build" \
        -e XDG_CACHE_HOME="/src/$WORK/cache" \
        "$@"
}

# Per-target results for the summary (parallel to $targets).
summary=()
failed=0
built_wheels=()   # "target|path" for the smoke tests

for t in $targets; do
    expected="$(compat "$t")_$(wheel_arch "$t")"
    stage="$WORK/wheels/$t"
    echo
    echo "==> $t: $(rust_triple "$t"), $expected"
    rm -rf "${REPO:?}/$stage"
    if ! in_builder "$IMAGE" maturin build --release --locked \
            --target "$(rust_triple "$t")" --zig --compatibility "$(compat "$t")" \
            --out "/src/$stage"; then
        echo "!! $t: build failed" >&2
        summary+=("$t|-|BUILD FAILED")
        failed=1
        continue
    fi
    wheel=""
    for w in "$REPO/$stage"/*.whl; do
        [ -e "$w" ] || continue
        [ -z "$wheel" ] || { echo "!! $t: more than one wheel in $stage" >&2; wheel=""; break; }
        wheel=$w
    done
    if [ -z "$wheel" ]; then
        summary+=("$t|-|NO SINGLE WHEEL")
        failed=1
        continue
    fi
    name=$(basename "$wheel")
    echo "--> checking $name"
    if in_builder "$IMAGE" python -I /src/docker/check_wheel.py "/src/$stage/$name" "$expected"; then
        cp "$wheel" "$REPO/dist/"
        summary+=("$t|$name|tag OK")
        built_wheels+=("$t|$name")
    else
        echo "!! $t: $name does not match $expected; not copied to dist/" >&2
        summary+=("$t|$name|TAG MISMATCH")
        failed=1
    fi
done

if [ "$want_sdist" = 1 ]; then
    echo
    echo "==> sdist"
    rm -rf "${REPO:?}/$WORK/sdist"
    if in_builder "$IMAGE" maturin sdist --out "/src/$WORK/sdist"; then
        for s in "$REPO/$WORK/sdist"/*.tar.gz; do
            name=$(basename "$s")
            # The sdist is defined by Cargo.toml's `include`: docker/ must stay
            # out, and what the build needs (the protocol, NormaCore's license,
            # the Python package) must be in.
            # shellcheck disable=SC2016 # $1 expands in the container's sh, not here
            if in_builder "$IMAGE" sh -c '
                    list=$(tar -tzf "$1" | sed "s|^[^/]*/||" | sort)
                    echo "$list"
                    ok=0
                    if echo "$list" | grep -q "^docker/"; then
                        echo "!! sdist contains docker/" >&2; ok=1
                    fi
                    for f in third_party/norma-core/software/drivers/st3215/src/protocol/mod.rs \
                             third_party/norma-core/LICENSE \
                             python/st3215_bus/__init__.py \
                             python/st3215_bus/export_calibration.py; do
                        echo "$list" | grep -qxF "$f" || { echo "!! sdist is missing $f" >&2; ok=1; }
                    done
                    exit $ok' sh "/src/$WORK/sdist/$name" 2>&1 | sed 's/^/    /'; then
                cp "$s" "$REPO/dist/"
                summary+=("sdist|$name|contents OK")
            else
                summary+=("sdist|$name|BAD CONTENTS")
                failed=1
            fi
        done
    else
        summary+=("sdist|-|BUILD FAILED")
        failed=1
    fi
fi

tests=()
if [ "$want_test" = 1 ] && [ ${#built_wheels[@]} -gt 0 ]; then
    for entry in "${built_wheels[@]}"; do
        t=${entry%%|*}
        name=${entry#*|}
        echo
        if [ "$(wheel_arch "$t")" != "$host_arch" ]; then
            echo "==> smoke test $t: skipped, $(wheel_arch "$t") wheel is not import-tested on this $host_arch host"
            tests+=("$t|not import-tested on this $host_arch host")
            continue
        fi
        case $t in
            *-musl)
                echo "==> smoke test $t in python:<v>-alpine ($ALPINE; 3.8: $ALPINE_38)"
                result=""
                for v in $PYTHONS; do
                    # shellcheck disable=SC2016 # $1/$2 expand in the container's sh, not here
                    if docker run --rm --network none \
                            --user "$(id -u):$(id -g)" \
                            -e HOME=/tmp -e PIP_NO_CACHE_DIR=1 -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
                            -v "$REPO/$WORK/wheels/$t:/wheels:ro" \
                            -v "$SCRIPT_DIR/smoke_test.py:/smoke_test.py:ro" \
                            -v "$REPO/tests:/tests:ro" \
                            -e PYTHONDONTWRITEBYTECODE=1 \
                            -w /tmp "python:$v-$(alpine_tag "$v")" sh -ec \
                            'python -m venv /tmp/venv
                             /tmp/venv/bin/pip install -q --no-index "/wheels/$1"
                             /tmp/venv/bin/python -I /smoke_test.py /tests' sh "$name"; then
                        result="$result $v:ok"
                    else
                        result="$result $v:FAIL"
                        failed=1
                    fi
                done
                ;;
            *)
                echo "==> smoke test $t in the builder image"
                result=""
                for v in $PYTHONS; do
                    # shellcheck disable=SC2016 # $1/$2 expand in the container's sh, not here
                    if in_builder --network none -w /tmp -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" sh -ec \
                            'uv venv -q --python "$1" /tmp/venv
                             uv pip install -q --python /tmp/venv/bin/python --no-index --offline "/src/$2"
                             /tmp/venv/bin/python -I /src/docker/smoke_test.py /src/tests' \
                            sh "$v" "$WORK/wheels/$t/$name"; then
                        result="$result $v:ok"
                    else
                        result="$result $v:FAIL"
                        failed=1
                    fi
                done
                ;;
        esac
        tests+=("$t|${result# }")
    done
fi

echo
echo "==> Summary ($((SECONDS - start)) s)"
for s in "${summary[@]}"; do
    IFS='|' read -r t name status <<<"$s"
    printf '  %-13s %-26s %s\n' "$t" "$status" "$name"
done
if [ "$want_test" = 1 ]; then
    echo "  smoke tests:"
    for s in ${tests[@]+"${tests[@]}"}; do
        printf '    %-13s %s\n' "${s%%|*}" "${s#*|}"
    done
else
    echo "  smoke tests: skipped (--no-test)"
fi
echo "  output: $REPO/dist/"
if [ "$failed" != 0 ]; then
    echo "FAILED (see above)" >&2
    exit 1
fi
