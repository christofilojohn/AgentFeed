"""Write THIRD_PARTY_LICENSES.txt for a build.

Every MIT, BSD and Apache dependency asks the same thing of a binary
distribution: ship its copyright and licence text. This walks the installed
dependency tree of a distribution (default: agentfeed, with the `desktop`
extra) and writes one file with each package's name, version, licence and
the licence files it ships. Run it in the environment the app is built
from; the build scripts do.

    python scripts/third_party_licenses.py [dist[extra,...]] [-o out.txt]
"""
from __future__ import annotations

import argparse
import re
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.requirements import Requirement

LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)


def walk(root: str, extras: set[str]) -> list:
    seen: dict[str, object] = {}
    queue = [(root, extras)]
    while queue:
        name, ex = queue.pop()
        key = name.lower().replace("_", "-")
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        if key in seen:
            continue
        seen[key] = dist
        for spec in dist.requires or []:
            req = Requirement(spec)
            env_ok = (req.marker is None
                      or any(req.marker.evaluate({"extra": e}) for e in (ex or {""})))
            if env_ok:
                queue.append((req.name, set(req.extras)))
    return [d for k, d in sorted(seen.items())]


def licence_of(dist) -> str:
    md = dist.metadata
    return (md.get("License-Expression")
            or next((c.split("::")[-1].strip() for c in md.get_all("Classifier") or []
                     if c.startswith("License ::")), "")
            or (md.get("License") or "").splitlines()[0][:80]
            or "see licence text")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default="agentfeed[desktop]")
    ap.add_argument("-o", "--out", default="THIRD_PARTY_LICENSES.txt")
    args = ap.parse_args()
    root = Requirement(args.root)
    dists = walk(root.name, set(root.extras))
    own = root.name.lower().replace("_", "-")
    parts = ["Third-party software included in this application\n"
             "=================================================\n"]
    for d in dists:
        if d.metadata["Name"].lower().replace("_", "-") == own:
            continue
        parts.append(f"\n{'-' * 76}\n{d.metadata['Name']} {d.version}\n"
                     f"Licence: {licence_of(d)}\n"
                     f"{d.metadata.get('Home-page') or ''}\n")
        for f in d.files or []:
            if LICENSE_FILE.search(str(f)):
                try:
                    text = Path(f.locate()).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                parts.append(f"\n[{f.name}]\n{text.strip()}\n")
    Path(args.out).write_text("".join(parts), encoding="utf-8")
    print(f"wrote {args.out}: {len(dists) - 1} packages")


if __name__ == "__main__":
    main()
