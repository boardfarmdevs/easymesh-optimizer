#!/usr/bin/env python3
"""The documentation check of the EasyMesh labs: the same file in every repository of the
labs; change it in all of them. The Pages workflow runs it from the repository's root:

    python3 pages/check-docs.py

It checks what must hold in every repository:
- the README's labs block is the one below, but for its Site line;
- the README has the labs' shape: a Components and a Documentation section;
- the documents live in docs/, indexed by docs/README.md, named in lowercase words joined
  by hyphens;
- every relative link in a tracked Markdown file names a file or directory that exists;
- no Markdown file links into another repository's files (link that project's site), nor
  into its own on GitHub (link it relatively).
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
BODY = """The [EasyMesh labs](https://mesh.vcpe.dev/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://vcpe.dev/easymesh-optimizer/)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://vcpe.dev/meta-cmf-bananapi-vcpe/),
[prplMesh](https://vcpe.dev/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://vcpe.dev/emosa-lab/), with the
[OpenSync lab](https://vcpe.dev/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://vcpe.dev/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://vcpe.dev/easymesh-medium/)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure, tools (the
[room builder](https://vcpe.dev/easymesh-room-builder/)) and learning
around them."""

LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
INTO_REPOSITORY = re.compile(
    r"https://github\.com/boardfarmdevs/([A-Za-z0-9._-]+)/(?:blob|tree|raw)/"
)
DOC_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
SECTIONS = ("## Components", "## Documentation")


def tracked(root, pattern):
    return subprocess.run(
        ["git", "-C", str(root), "ls-files", pattern], capture_output=True, text=True, check=True
    ).stdout.split()


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
    problems = []
    headings = [line.strip() for line in text.splitlines()]
    for section in SECTIONS:
        if section not in headings:
            problems.append(f"README.md: no '{section}' section")
    return problems


def docs_layout(root):
    problems = []
    if not (root / "docs" / "README.md").exists():
        problems.append("docs/README.md: missing (the documents' index)")
    for name in tracked(root, "docs/*.md"):
        base = name.rsplit("/", 1)[-1]
        if base != "README.md" and not DOC_NAME.match(base):
            problems.append(f"{name}: not named in lowercase words joined by hyphens")
    return problems


def links(root):
    repository = root.name
    remote = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True
    ).stdout.strip()
    if remote:
        repository = remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
    problems = []
    for name in tracked(root, "*.md"):
        path = root / name
        if not path.exists():
            continue
        text = FENCE.sub("", path.read_text(errors="replace"))
        for match in LINK.finditer(text):
            target = match.group(1)
            into = INTO_REPOSITORY.match(target)
            if into:
                where = (
                    "its own files: link them relatively"
                    if into.group(1) == repository
                    else (f"{into.group(1)}'s files: link its site")
                )
                problems.append(f"{name}: links into {where} ({target})")
                continue
            target = target.split("#", 1)[0].split("?", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("<"):
                continue
            if not (path.parent / unquote(target)).exists():
                problems.append(f"{name}: broken link {match.group(1)}")
    return problems


def main():
    root = pathlib.Path.cwd()
    problems = labs_block(root) + docs_layout(root) + links(root)
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"check-docs: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
