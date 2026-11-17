"""Append-only record of every research data access and every experiment.

Why it exists: a result is only as honest as the number of things that were
tried before it. A Sharpe ratio picked as the best of forty variants means
something very different from the first and only one. The ledger makes the
denominator visible: every run of a study appends what it read, with which
code, under which configuration, and what came out — whether or not the
outcome is flattering.

Rules, enforced here:

* entries are only ever appended (the file is opened in append mode; there is
  no update or delete);
* every entry carries the git commit and whether the working tree had
  uncommitted changes, so a result can be traced to exact code;
* an experiment entry names the dataset segment it read (development or test)
  and the dataset identifiers from the manifest.

The ledger is versioned with the code (``research/experiments/ledger.jsonl``).
The sealed test period has its own, separate opening ledger next to the
dataset specification; this one never replaces it.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qts.core.errors import DataError
from qts.core.provenance import code_version

KINDS = ("data_access", "experiment")


class ExperimentLedger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Record one entry and return it (with its id and time)."""
        if kind not in KINDS:
            raise DataError(f"unknown ledger entry kind {kind!r} (expected one of {KINDS})")
        version = code_version()
        entry = {
            "id": fields.pop("id", None) or uuid.uuid4().hex[:12],
            "kind": kind,
            "at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "git_commit": version.get("git_commit"),
            "git_dirty": self._dirty(version.get("git_dirty")),
            **fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    def _dirty(self, reported: bool | None) -> bool | None:
        """Uncommitted changes to anything *but this ledger* (which a study run
        modifies itself, so counting it would mark every entry dirty)."""
        if not reported:
            return reported
        try:
            done = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
                cwd=self.path.resolve().parent,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
                cwd=self.path.resolve().parent,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return reported
        if done.returncode != 0 or top.returncode != 0:
            return reported
        mine = self.path.resolve().relative_to(Path(top.stdout.strip()).resolve()).as_posix()
        changed = [line[3:].strip() for line in done.stdout.splitlines() if line.strip()]
        return any(path != mine for path in changed)

    def entries(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [e for e in out if kind is None or e.get("kind") == kind]
