#!/usr/bin/env python3
"""The documentation check of the EasyMesh labs: the same file in every repository
(easymesh-labs and meta-cmf-bananapi-vcpe, prplmesh-lab, easymesh-medium,
easymesh-optimizer, emosa-lab, opensync-lab, easymesh-lab); change it in all of them. The
Pages workflow (checks.yml in easymesh-medium and easymesh-optimizer) runs it from the
repository's root:

    python3 pages/check-docs.py

It checks what must hold in every repository (easymesh-labs docs/alignment-plan.md,
step 7.5):
- the README's labs block is the one below, but for its Site line;
- every relative link in a tracked Markdown file names a file or directory that exists.
"""

import pathlib
import re
import subprocess
import sys
from urllib.parse import unquote

OPEN = (
    "<!-- labs block: the same in every repository of the EasyMesh labs, but for the Site line -->"
)
CLOSE = "<!-- /labs block -->"
BODY = """The [EasyMesh labs](https://boardfarmdevs.github.io/easymesh-labs/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://github.com/boardfarmdevs/easymesh-optimizer)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://boardfarmdevs.github.io/meta-cmf-bananapi-vcpe/),
[prplMesh](https://boardfarmdevs.github.io/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://boardfarmdevs.github.io/emosa-lab/), with the
[OpenSync lab](https://boardfarmdevs.github.io/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://boardfarmdevs.github.io/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://github.com/boardfarmdevs/easymesh-medium)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure and learning around them."""

LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)


def labs_block(root):
    readme = root / "README.md"
    if not readme.exists():
        return ["README.md: missing"]
    text = readme.read_text()
    start = text.find(OPEN)
    end = text.find(CLOSE, start)
    if start < 0 or end < 0:
        return ["README.md: no labs block (the markers are missing)"]
    lines = text[start + len(OPEN) : end].strip("\n").split("\n")
    if not lines[0].startswith("**Site:** "):
        return ["README.md: the labs block does not start with its Site line"]
    if "\n".join(lines[1:]) != BODY:
        return ["README.md: the labs block differs from the one in pages/check-docs.py"]
    return []


def relative_links(root):
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "*.md"], capture_output=True, text=True, check=True
    ).stdout.split()
    problems = []
    for name in listed:
        path = root / name
        if not path.exists():
            continue
        text = FENCE.sub("", path.read_text(errors="replace"))
        for match in LINK.finditer(text):
            target = match.group(1).split("#", 1)[0].split("?", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("<"):
                continue
            if not (path.parent / unquote(target)).exists():
                problems.append(f"{name}: broken link {match.group(1)}")
    return problems


def main():
    root = pathlib.Path.cwd()
    problems = labs_block(root) + relative_links(root)
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"check-docs: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
