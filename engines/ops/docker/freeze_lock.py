"""Filter pip freeze to the installed production dependency closure, offline."""

import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parents[1]
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
pending = [Requirement(item) for item in project["project"]["dependencies"]]
production = set()
visited = set()
while pending:
    requirement = pending.pop()
    name = canonicalize_name(requirement.name)
    key = (name, frozenset(requirement.extras))
    if key in visited:
        continue
    visited.add(key)
    production.add(name)
    distribution = importlib.metadata.distribution(requirement.name)
    for item in distribution.requires or []:
        dependency = Requirement(item)
        if dependency.marker is None or any(
            dependency.marker.evaluate({"extra": extra}) for extra in ["", *requirement.extras]
        ):
            pending.append(dependency)

frozen = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
lines = []
for line in frozen.splitlines():
    if "==" in line and canonicalize_name(line.split("==")[0]) in production:
        # Windows-only dependency in this source venv, never installed into Linux.
        lines.append(line + (' ; sys_platform == "win32"' if line.startswith("pywin32==") else ""))
(root / "requirements.lock").write_text(
    "# Installed production dependency closure from pip freeze; no resolution or network.\n"
    + "\n".join(lines)
    + "\n",
    encoding="utf-8",
)
