"""Check that a built wheel is what build.sh asked for.

Usage (inside the builder image): python check_wheel.py WHEEL EXPECTED_PLATFORM_TAG
e.g. check_wheel.py dist/st3215_bus-...whl manylinux_2_28_x86_64

Checks, failing (exit 1) on any mismatch:
  * filename tags: cp38-abi3, the expected platform tag present, every
    platform tag for the expected architecture;
  * the WHEEL file's Tag lines agree with the filename;
  * every extension module is an ELF for the expected architecture (and
    hard-float for armv7), and for manylinux needs no glibc symbol newer than
    the baseline, for musllinux no glibc at all and no library but musl's libc;
  * manylinux only: `auditwheel show` finds no external libraries and a policy
    no newer than the requested baseline. auditwheel cannot judge musllinux
    wheels on this glibc-based image (it cannot locate the target's musl
    libc), so for those the ELF checks above stand in for it.
"""

import io
import json
import os
import re
import subprocess
import sys
import zipfile

from elftools.elf.elffile import ELFFile
from elftools.elf.gnuversions import GNUVerNeedSection

# wheel platform arch -> (ELF e_machine, ELF class)
ELF_ARCH = {
    "x86_64": ("EM_X86_64", 64),
    "aarch64": ("EM_AARCH64", 64),
    "armv7l": ("EM_ARM", 32),
}
EF_ARM_ABI_FLOAT_HARD = 0x400
GLIBC_SYSTEM_LIBS = {
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1",
    "libgcc_s.so.1", "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1",
    "ld-linux-armhf.so.3",
}
MUSL_SYSTEM_LIBS = {"libc.so"}


def version_tuple(text):
    return tuple(int(x) for x in re.split(r"[._]", text))


def main(path, expected):
    errors = []
    name = os.path.basename(path)
    print(f"  wheel:      {name}")

    exp = re.fullmatch(r"(manylinux|musllinux)_(\d+_\d+)_(x86_64|aarch64|armv7l)", expected)
    if not exp:
        sys.exit(f"  MISMATCH: cannot parse expected platform tag {expected!r}")
    family, baseline, arch = exp.group(1), version_tuple(exp.group(2)), exp.group(3)

    m = re.fullmatch(r"st3215_bus-[^-]+-([^-]+)-([^-]+)-([^-]+)\.whl", name)
    if not m:
        sys.exit("  MISMATCH: not an st3215_bus wheel filename")
    py, abi, plats = m.group(1), m.group(2), m.group(3).split(".")
    if (py, abi) != ("cp38", "abi3"):
        errors.append(f"python/abi tag is {py}-{abi}, expected cp38-abi3")
    if expected not in plats:
        errors.append(f"platform tags {plats} do not include {expected}")
    for p in plats:
        if not p.endswith("_" + arch):
            errors.append(f"platform tag {p} is not for {arch}")

    with zipfile.ZipFile(path) as zf:
        wheel_files = [n for n in zf.namelist() if n.endswith(".dist-info/WHEEL")]
        if len(wheel_files) != 1:
            errors.append(f"expected one .dist-info/WHEEL, found {wheel_files}")
        else:
            meta = zf.read(wheel_files[0]).decode()
            tags = {line.split(":", 1)[1].strip() for line in meta.splitlines()
                    if line.startswith("Tag:")}
            want = {f"{py}-{abi}-{p}" for p in plats}
            if tags != want:
                errors.append(f"WHEEL Tag lines {sorted(tags)} != filename tags {sorted(want)}")
            print(f"  tags:       {', '.join(sorted(tags))} (WHEEL file agrees: {tags == want})")

        sos = [n for n in zf.namelist() if n.endswith(".so")]
        if not sos:
            errors.append("no extension module (.so) in the wheel")
        for so in sos:
            with zf.open(so) as raw:
                elf = ELFFile(io.BytesIO(raw.read()))
            machine, bits = elf["e_machine"], elf.elfclass
            want_machine, want_bits = ELF_ARCH[arch]
            if (machine, bits) != (want_machine, want_bits):
                errors.append(f"{so} is ELF{bits} {machine}, expected ELF{want_bits} {want_machine}")
            if arch == "armv7l" and not elf["e_flags"] & EF_ARM_ABI_FLOAT_HARD:
                errors.append(f"{so} is not hard-float (armv7l wheels must be gnueabihf)")
            needed = []
            dyn = elf.get_section_by_name(".dynamic")
            if dyn is not None:
                needed = [t.needed for t in dyn.iter_tags() if t.entry.d_tag == "DT_NEEDED"]
            glibc = set()
            for sec in elf.iter_sections():
                if isinstance(sec, GNUVerNeedSection):
                    for _, aux in sec.iter_versions():
                        for a in aux:
                            if a.name.startswith("GLIBC_"):
                                glibc.add(a.name)
            newest = max(glibc, key=lambda v: version_tuple(v[len("GLIBC_"):]), default=None)
            print(f"  binary:     {so}: ELF{bits} {machine}, NEEDED {' '.join(needed) or '-'}, "
                  f"newest glibc symbol {newest or '-'}")
            if family == "manylinux":
                extra = set(needed) - GLIBC_SYSTEM_LIBS
                if extra:
                    errors.append(f"{so} needs non-system libraries {sorted(extra)}")
                if newest and version_tuple(newest[len("GLIBC_"):]) > baseline:
                    errors.append(f"{so} needs {newest}, newer than the {expected} baseline")
            else:
                extra = set(needed) - MUSL_SYSTEM_LIBS
                if extra:
                    errors.append(f"{so} needs libraries other than musl libc: {sorted(extra)}")
                if glibc:
                    errors.append(f"{so} references glibc symbol versions {sorted(glibc)}")

    if family == "manylinux":
        out = subprocess.run(["auditwheel", "show", "--json", path],
                             capture_output=True, text=True)
        if out.returncode != 0:
            errors.append(f"auditwheel show failed: {out.stderr.strip()[-500:]}")
        else:
            report = json.loads(out.stdout)
            tag, ext = report.get("overall_tag", ""), report.get("external_libs", {})
            print(f"  auditwheel: consistent with {tag}, external libs: {sorted(ext) or 'none'}")
            am = re.fullmatch(r"manylinux_(\d+_\d+)_(.+)", tag)
            if not am or am.group(2) != arch or version_tuple(am.group(1)) > baseline:
                errors.append(f"auditwheel says {tag}, which does not satisfy {expected}")
            if ext:
                errors.append(f"auditwheel found external libraries {sorted(ext)}")
    else:
        print("  auditwheel: skipped (cannot resolve musl libc on a glibc builder; ELF checks above apply)")

    for e in errors:
        print(f"  MISMATCH:   {e}")
    print(f"  result:     {'FAIL' if errors else 'OK'} (requested {expected})")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
