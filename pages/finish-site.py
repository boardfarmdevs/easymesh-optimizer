#!/usr/bin/env python3
"""Finish a built Pages site: the labs bar on every page, .nojekyll and build.json.

Shared: the same file (pages/finish-site.py) in every repository of the EasyMesh labs with
a site; change it in all of them.

    python3 pages/finish-site.py [SITE]          (default dist/site)

- copies pages/labs-bar.js to the site root and adds
  <script src="<relative>labs-bar.js" data-project="<repo>" defer> to the head
  of every HTML page, except a page that says <meta name="labs-bar" content="off">
  (a full-screen tool);
- writes .nojekyll and build.json (repository, revision, pages with the bar);
- fails when the site has no index.html or a page has no head.

The repository name comes from GITHUB_REPOSITORY in Actions, else from the
origin remote.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BAR = "labs-bar.js"
OPT_OUT = re.compile(r"""<meta\s+name=["']labs-bar["']\s+content=["']off["']""", re.I)
HEAD_END = re.compile(r"</head\s*>", re.I)


def git(*args):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def repository():
    slug = os.environ.get("GITHUB_REPOSITORY") or re.sub(
        r"^.*github\.com[:/]|\.git$", "", git("remote", "get-url", "origin")
    )
    owner, _, name = slug.partition("/")
    if not owner or not name:
        raise SystemExit(f"cannot tell the repository ({slug!r})")
    return owner, name


def finish(site):
    if not (site / "index.html").is_file():
        raise SystemExit(f"{site}: no index.html; build the site first")
    owner, name = repository()
    shutil.copyfile(HERE / BAR, site / BAR)
    with_bar, without = [], []
    for page in sorted(site.rglob("*.html")):
        relative = page.relative_to(site).as_posix()
        html = page.read_text(encoding="utf-8")
        if OPT_OUT.search(html):
            without.append(relative)
            continue
        if f'data-project="{name}"' in html and BAR in html:
            with_bar.append(relative)  # already finished
            continue
        if not HEAD_END.search(html):
            raise SystemExit(f"{relative}: no </head> to add the labs bar to")
        prefix = "../" * relative.count("/")
        tag = f'<script src="{prefix}{BAR}" data-project="{name}" defer></script>\n'
        head = HEAD_END.search(html).start()
        page.write_text(html[:head] + tag + html[head:], encoding="utf-8")
        with_bar.append(relative)
    (site / ".nojekyll").touch()
    revision = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD")
    build = {
        "repository": f"{owner}/{name}",
        "revision": revision,
        "labs_bar_sha256": hashlib.sha256((HERE / BAR).read_bytes()).hexdigest(),
        "pages_with_bar": with_bar,
        "pages_without_bar": without,
    }
    (site / "build.json").write_text(json.dumps(build, indent=2) + "\n")
    print(
        f"{site}: labs bar on {len(with_bar)} pages, {len(without)} full-screen pages without; "
        f"{owner}/{name} @ {revision[:12]}; labs-bar.js sha256 {build['labs_bar_sha256'][:12]}"
    )


if __name__ == "__main__":
    finish(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "site").resolve())
