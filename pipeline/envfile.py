"""Load KEY=value lines from the project's .env into os.environ (no extra dependency).

Variables already set in the shell win over the file, so a one-off
`$env:BEDROCK_MODEL_ID = "..."` still overrides .env.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / ".env") -> list[str]:
    """Return the names of variables that were loaded from the file."""
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # trailing comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if key and value and key not in os.environ:  # blank values are ignored
            os.environ[key] = value
            loaded.append(key)
    return loaded
