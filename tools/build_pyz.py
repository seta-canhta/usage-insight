#!/usr/bin/env python3
"""Build ``insight-<version>.pyz`` -- the whole client as one file.

    python3 tools/build_pyz.py            -> dist/insight-0.3.0.pyz
    python3 tools/build_pyz.py --out /tmp/x.pyz

**Why a zipapp and not a package.** ``cli/insight.py`` finds its siblings with

    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_HERE)
    for _p in (_ROOT, _ROOT/"collector"): sys.path.insert(0, _p)

and ``zipimport`` accepts a ``sys.path`` entry of the form
``/path/to/archive.pyz/subdir``. So an archive whose internal layout *mirrors
the repo* -- ``cli/``, ``common/``, ``collector/`` at the archive root -- makes
``import common`` and ``import main as collector_main`` resolve with zero source
changes, on the laptop and in the checkout alike. ``cli/ship.py`` and
``cli/link_runs.py`` do the identical dance and work for the identical reason.

Restructuring into a proper package would have been the other answer, and it is
the wrong one: it would touch every module and break ``cli/tests/test_insight.py``,
which puts ``<repo>/cli`` on ``sys.path`` and does ``import insight``. The tests
run against the checkout; the archive is a packaging detail and should not be
able to change what is being tested.

**Contents are enumerated, never globbed.** A glob over ``cli/`` ships
``cli/tests/``, and a test fixture on fourteen laptops is fourteen copies of
something nobody meant to distribute. Nothing from ``importers/``, ``report/``,
``sql/`` or ``server/`` goes in either -- an engineer's laptop has no business
carrying the central pipeline.

**Determinism.** ``zipapp.create_archive`` records real mtimes, so the digest
changes on every build and the digest published in ``install.sh`` becomes
something to trust rather than something to check. Every entry here gets a fixed
timestamp and the paths are sorted, so two builds of one tree are byte-identical
and CI can prove it by building twice.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import stat
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

#: The zip epoch. Zip cannot represent anything earlier, and a build's digest
#: should depend on the bytes it packages and on nothing else -- not on when it
#: happened to run.
EPOCH = (1980, 1, 1, 0, 0, 0)

#: Every file that ships, listed by hand. Repo-relative path -> archive path.
#: The two are identical on purpose: the archive mirrors the repo, which is the
#: whole reason the ``sys.path`` dance in ``insight.py`` keeps working inside it.
PAYLOAD = [
    "cli/insight.py",
    "cli/identity.py",
    "cli/ship.py",
    "cli/schedule.py",
    "cli/vscode_setup.py",
    "cli/copilot_read.py",
    "cli/link_runs.py",
    "cli/version.py",
    "cli/update.py",
    "cli/hooks/prepare-commit-msg",
    # The two readers added for the surfaces the CLI journal cannot see.
    "cli/vscode_read.py",
    "cli/rtk_read.py",
    # The shared library. It lives at the repository root rather than inside
    # `pollers/` because cli/, importers/, report/ and collector/ all depend on
    # it -- see CLAUDE.md, "Known structural debt".
    "common/__init__.py",
    "collector/main.py",
]

#: Generated, not shipped from the tree, so there is one obvious answer to
#: "what runs when you execute the archive". ``sys.path.insert`` rather than
#: append: a stray ``insight.py`` in the working directory must not win.
MAIN = '''\
import os, sys
_ARCHIVE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ARCHIVE)                       # `import common`
sys.path.insert(0, os.path.join(_ARCHIVE, "cli"))
import insight
sys.exit(insight.main())
'''

#: ``#!`` line prepended to the archive. A zip reader locates the central
#: directory by scanning back from the end of the file, which is why an
#: arbitrary prefix is legal -- it is how zipapp works at all.
SHEBANG = b"#!/usr/bin/env python3\n"


def version(root: str = _ROOT) -> str:
    """Read ``VERSION`` out of ``cli/version.py`` without importing it.

    Importing it would run ``cli/__init__``-less module machinery against a tree
    this script is meant to be able to package even when it is mid-edit.
    """
    path = os.path.join(root, "cli", "version.py")
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), path)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "VERSION":
                    return str(ast.literal_eval(node.value))
    raise SystemExit("{} does not define VERSION".format(path))


def _entry(archive_path: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(archive_path, date_time=EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    # Pinned rather than inherited: ZipInfo picks create_system from the host
    # platform, so a build on macOS and one on the Linux runner would differ in
    # a byte nobody would think to look at.
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def build(out: str, root: str = _ROOT) -> str:
    """Write the archive. Returns its sha256."""
    missing = [p for p in PAYLOAD if not os.path.isfile(os.path.join(root, p))]
    if missing:
        raise SystemExit("missing from the tree: {}".format(", ".join(missing)))

    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "wb") as raw:
        raw.write(SHEBANG)
        # No .pyc anywhere in here, now or later. The bytecode magic number is
        # tied to one CPython minor version, and this archive has to run on
        # whatever 3.9 - 3.14 the machine happens to have. A "precompiled for
        # speed" archive is one that refuses to start on half the fleet.
        with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(_entry("__main__.py", 0o644), MAIN)
            for rel in sorted(PAYLOAD):
                with open(os.path.join(root, rel), "rb") as handle:
                    archive.writestr(_entry(rel, 0o644), handle.read())

    with open(tmp, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    os.replace(tmp, out)
    os.chmod(out, 0o755 if os.name != "nt" else stat.S_IREAD | stat.S_IWRITE)
    return digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="output path "
                                      "(default: dist/insight-<version>.pyz)")
    parser.add_argument("--root", default=_ROOT, help="repository to build from")
    parser.add_argument("--print-version", action="store_true",
                        help="print the version and exit, for the release job")
    args = parser.parse_args(argv)

    ver = version(args.root)
    if args.print_version:
        print(ver)
        return 0

    out = args.out or os.path.join(args.root, "dist",
                                   "insight-{}.pyz".format(ver))
    digest = build(out, args.root)
    print("{}  {}  {} bytes".format(digest, out, os.path.getsize(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
