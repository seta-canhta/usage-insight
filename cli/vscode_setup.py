#!/usr/bin/env python3
"""Configure VS Code for the pilot, safely.

Backing this: doing it by hand went wrong three times in one afternoon, and
every failure was silent.

1. The settings went into workspace scope, which VS Code ignores for these keys.
2. A missing comma left `settings.json` invalid, so VS Code dropped **every**
   setting in the file and said nothing in the UI.
3. Absolute paths were used where only tilde-relative ones work; the rejection
   is a line in the renderer log and nothing else.

Since 2026-08-26 this also **removes** the Copilot OTel exporter settings it
used to write. Usage now comes from ``~/.copilot``, and an exporter left
switched on would go on writing prompts to a file nobody reads.

None of the three surfaced as an error. That is what makes this worth
automating rather than documenting: a manual step whose failure is invisible
gets done wrong and stays wrong.

So this writes, re-reads, re-parses, and **restores the backup if the result
does not parse**. A settings file this tool has touched is either correct or
unchanged.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def settings_path() -> Optional[str]:
    """Where VS Code keeps user settings on the two platforms we support."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Library", "Application Support", "Code", "User",
                     "settings.json"),                                  # macOS
        os.path.join(home, ".config", "Code", "User", "settings.json"),  # Linux
        os.path.join(home, ".config", "VSCodium", "User", "settings.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    for path in candidates:
        if os.path.isdir(os.path.dirname(path)):
            return path
    return None


def strip_jsonc(text: str) -> str:
    """Remove comments without touching anything inside a string.

    A naive regex eats the `//` in a URL and reports a valid file as broken,
    which is worse than not checking: it sends someone hunting for a syntax
    error that is not there.
    """
    out: List[str] = []
    index, length = 0, len(text)
    in_string = escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            closing = text.find("*/", index)
            index = length if closing == -1 else closing + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_jsonc(text: str) -> Dict[str, Any]:
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", strip_jsonc(text)))


def tilde(path: str) -> str:
    """Express a path relative to home.

    VS Code rejects absolute paths in the chat location settings -- "Skipping
    invalid path (glob patterns and absolute paths not supported)" in the
    renderer log, nothing in the UI.
    """
    home = os.path.expanduser("~")
    absolute = os.path.abspath(os.path.expanduser(path))
    if absolute == home:
        return "~"
    if absolute.startswith(home + os.sep):
        return "~/" + os.path.relpath(absolute, home)
    return absolute


#: Whole setting namespaces this tool used to write and now removes. Copilot's
#: OTel exporter was how token usage was collected until 2026-08-26; it is read
#: from ``~/.copilot`` instead, which needs no setting at all.
#:
#: They are actively deleted rather than merely no longer written. Left behind,
#: the exporter keeps appending prompts, system instructions and command output
#: to a file nothing reads any more -- microsoft/vscode#326254 means
#: ``captureContent: false`` does not stop it. Turning a collector off has to
#: include turning off what it was collecting.
#:
#: By **prefix**, not by name. This started as a list of the five keys this
#: tool wrote, and on the first real machine it ran against it left
#: ``github.copilot.chat.otel.otlpEndpoint`` behind -- a key somebody had set
#: by hand, pointing the exporter at a local collector, which would have gone
#: on exporting. An enumerated list is only as good as today's knowledge of
#: what is in somebody's settings file. The whole namespace is dead, so the
#: whole namespace goes.
OBSOLETE_PREFIXES = ("github.copilot.chat.otel.",)


def obsolete(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in OBSOLETE_PREFIXES)


def desired(insight_home: str, aiep: Optional[str]) -> Dict[str, Any]:
    """What this tool writes into VS Code settings.

    Nothing is required for usage collection any more: Copilot CLI journals
    every session under ``~/.copilot`` on its own. What remains here registers
    the platform's agents and skills, which VS Code cannot discover by itself.
    """
    wanted: Dict[str, Any] = {}
    if aiep:
        wanted["chat.agentFilesLocations"] = {
            tilde(os.path.join(aiep, "agents", "development")): True,
            tilde(os.path.join(aiep, "agents", "qualdev")): True,
        }
        wanted["chat.agentSkillsLocations"] = {
            tilde(os.path.join(aiep, ".skills")): True}
        wanted["chat.useAgentSkills"] = True
    return wanted


def merge(current: Dict[str, Any], wanted: Dict[str, Any]) -> Dict[str, Any]:
    """Add ours, keep theirs.

    Location settings are merged key by key rather than replaced: someone may
    already register agents of their own, and replacing the map would silently
    unregister them.
    """
    merged = dict(current)
    for key, value in wanted.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def apply(path: str, wanted: Dict[str, Any], dry_run: bool = False
          ) -> Tuple[bool, str, List[str]]:
    """Write the settings, verify, and restore the backup if anything is off.

    Returns ``(changed, message, changed_keys)``.
    """
    existing_text = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing_text = handle.read()

    try:
        current = parse_jsonc(existing_text) if existing_text.strip() else {}
    except ValueError as exc:
        # Refuse rather than overwrite. Their file is broken already and
        # replacing it would destroy settings we cannot read.
        return False, f"settings.json does not parse ({exc}); fix it first", []

    # Removal wins over writing. If a caller asks for a key that is on the
    # obsolete list, the list is right and the caller is stale -- and `wanted`
    # is filtered here rather than after the merge so the verification below
    # never demands a key we just deleted.
    wanted = {k: v for k, v in wanted.items() if not obsolete(k)}

    stale = sorted(k for k in current if obsolete(k))
    changed_keys = [k for k in wanted
                    if k not in current or current[k] != merge(current, wanted)[k]]
    changed_keys += stale
    if not changed_keys:
        return False, "already configured", []

    merged = merge(current, wanted)
    for key in stale:
        merged.pop(key, None)
    if dry_run:
        return True, "would change: " + ", ".join(sorted(changed_keys)), changed_keys

    backup = ""
    if existing_text:
        backup = "{}.bak.{}".format(
            path, datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy(path, backup)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = json.dumps(merged, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)

    # Read back from disk rather than trusting what we meant to write.
    try:
        with open(path, "r", encoding="utf-8") as handle:
            reparsed = parse_jsonc(handle.read())
        missing = [k for k in wanted if k not in reparsed]
        if missing:
            raise ValueError("keys did not land: " + ", ".join(missing))
        left = sorted(k for k in reparsed if obsolete(k))
        if left:
            raise ValueError("obsolete keys survived: " + ", ".join(left))
    except (ValueError, OSError) as exc:
        if backup:
            shutil.copy(backup, path)
        return False, f"write verified as broken and was rolled back ({exc})", []

    note = f"backup at {os.path.basename(backup)}" if backup else "new file"
    return True, note, changed_keys
