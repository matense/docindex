"""Convert agency-agents personalities into DocIndex's bundled persona JSON.

Usage:
    venv/Scripts/python.exe tools/convert_agency_personas.py <repo-dir>

<repo-dir> is a local clone of https://github.com/msitarzewski/agency-agents
(MIT License — see licenses/AGENCY-AGENTS-LICENSE.txt). Only top-level
division files with a YAML frontmatter (name + description) are real agent
personalities; strategy/ playbooks, examples/ and integrations/ are skipped.

Output: app/data/agency_personas.json
"""

import json
import re
import sys
from pathlib import Path

SKIP_DIRS = {"examples", "integrations", "scripts", "tools"}
OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "agency_personas.json"

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_frontmatter(text):
    """Tiny YAML-frontmatter reader for flat `key: value` pairs only."""
    m = _FM_RE.match(text)
    if not m:
        return None, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "-")):
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[m.end():]


def convert(repo_dir):
    repo = Path(repo_dir)
    personas = []
    for division_dir in sorted(p for p in repo.iterdir()
                               if p.is_dir() and not p.name.startswith(".")
                               and p.name not in SKIP_DIRS):
        for md in sorted(division_dir.glob("*.md")):
            if md.name.upper().startswith(("README", "CONTRIBUTING",
                                           "SECURITY")):
                continue
            meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
            if not meta or not meta.get("name") or not meta.get("description"):
                continue
            personas.append({
                "key": md.stem,
                "name": meta["name"],
                "division": division_dir.name.replace("-", " ").title(),
                "description": meta["description"],
                "emoji": meta.get("emoji", ""),
                "color": meta.get("color", ""),
                "vibe": meta.get("vibe", ""),
                "content": body.strip(),
            })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "source": {
            "project": "The Agency — AI agent personalities",
            "url": "https://github.com/msitarzewski/agency-agents",
            "license": "MIT",
            "copyright": "Copyright (c) 2025 AgentLand Contributors",
        },
        "personas": personas,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(personas)} personas -> {OUT} "
          f"({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    convert(sys.argv[1])
