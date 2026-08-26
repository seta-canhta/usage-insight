#!/usr/bin/env python3
"""The one place the client's version number is written down.

Nothing is imported here, deliberately. ``tools/build_pyz.py`` reads this file
with ``ast.literal_eval`` rather than importing it, because importing anything
under ``cli/`` drags in ``pollers/common.py`` and ``collector/main.py`` through
the ``sys.path`` dance at the top of ``insight.py`` -- and a build script that
has to import the thing it is packaging cannot package a broken tree, which is
exactly when you want the build to run and tell you.

Bumping this is what a release is. The tag ``v<VERSION>`` triggers
``.github/workflows/release.yml``, which refuses to publish if the two disagree.
"""

VERSION = "0.3.0"
