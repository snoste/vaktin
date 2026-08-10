# CLAUDE.md — working on Vaktin

Read `README.md` first for what the tool is. This file is the part that is easy
to get wrong: which copy to edit, how to see a change without publishing your
employer's branch names, and what must never reach a commit.

## The tool is one file, and this checkout is the live one

`vaktin.py` is the whole application: stdlib only, no build step, no bundler,
CSS and page markup as strings inside it. Editing it is the entire workflow.

The installed instance is a launchd agent (`is.vaktin.agent`) that runs
`vaktin.py` **from this working tree, in place**. Two consequences worth
holding on to:

- Saving a change does not take effect until the agent restarts; use
  `./install.sh restart`, then `./install.sh status` or `logs`.
- Checking out a branch changes what the live page serves on the next restart.
  A fix left on an unmerged branch disappears when the tree returns to `main`.

**Never `pkill -f vaktin.py`.** That matches the user's agent, not just your
test process; launchd restarts it, but you have bounced their page for no
reason. Kill test instances by their own port or PID.

## An older fork of this script may exist inside a watched repo

Vaktin grew out of a single-repo status page, and a copy of that ancestor can
still be sitting in a project it now watches (a `scripts/status_server.py` or
similar). **This repository is canonical.** If you are asked to change Vaktin,
change `vaktin.py` here — patching the fork fixes a page nobody looks at, and
the two have diverged: multi-project support, per-workflow ETAs, the runner
panel and the Cloud Run join exist only here.

Watched repos own exactly one Vaktin file, `.vaktin.json`, and it is *their*
config, not this tool's — see README, "Configuration lives in the watched repo".

## Seeing a UI change without exposing real data

The live page renders branch names, commit subjects and session names from
private repositories. Do not screenshot it, paste it, or put its output in a
commit message, an issue or a PR description. Verify against data you invent
instead. Two ways, both cheap:

**Render the page with placeholder data.** Import the module and call `page()`
directly — no server, no watched repo, no network:

```python
import importlib.util
spec = importlib.util.spec_from_file_location("v", "vaktin.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

proj = {"name": "my-service", "root": "/home/dev/repos/my-service",
        "built_ok": True, "eta": 18, "note": "",
        "runs": [{"status": "in_progress", "name": "Deploy to my-fleet",
                  "title": "fix(widget): a subject long enough to clamp", "mins": 7}],
        "releases": [{"tag": "v1.4.2", "state": "shipped", "title": "…", "note": ""}],
        "branches": [{"name": "fix/a-branch", "ahead": 3, "when": "2 hours ago",
                      "subject": "fix(widget): …"}]}
d = {"projects": [proj], "configured": True, "at": "14:02:11", "sessions": [],
     "runners": {"list": [], "events": []}}
open("/tmp/preview.html", "w").write(
    f"<!doctype html><meta charset=utf-8>"
    f"<meta name=viewport content='width=device-width,initial-scale=1'>"
    f"<style>{m.CSS}</style>{m.page(d)}")
```

Note this skips the `<script>` the request handler adds, so anything that needs
the click-to-expand listener has to be tested on a served page.

**Or run a scratch instance against a throwaway repo**, which does exercise the
handler and its JavaScript:

```bash
mkdir -p /tmp/demo && cd /tmp/demo && git init -q && git commit -q --allow-empty -m "chore: seed"
VAKTIN_REPOS=/tmp/demo PORT=8794 python3 ~/vaktin/vaktin.py
```

Use a port that is not 8787 so the real agent keeps serving throughout.

## Layout: there are two of them

The page is a plain table on wide screens and a stack of small blocks under
720px (`@media (max-width:720px)` in `CSS`). The mobile half works by class, so
**a new `<td>` needs a class and a grid placement or it lands in the wrong slot
on a phone**:

- Put `class="stack"` on the `<table>` and `class="hd"` on its header row.
- Give each cell a `c-*` class, then place it with `grid-row` / `grid-column`
  in the media query.
- Wrap prose in `clip()`, which clamps to two lines and returns the whole text
  on tap or hover. `clip(text, 1)` is one line, and single-line clamping is
  scoped to the media query on purpose — a wide column has room to wrap.

Check both widths before committing. 375px is the case that breaks.

## Before you commit

Only files from this repository, and never `.vaktin.json` or anything under
`repos/` (both ignored — do not "fix" the ignore rules). Then read your own
diff for identifiers belonging to a watched project: fleet slugs, GCP project
ids, service or workflow names, repo names, hostnames. They do not belong in
code, in comments, in test fixtures, or **in a commit message** — a message is
as public as the diff. Placeholders are `my-service`, `myorg/my-fleet`,
`my-gcp-project`.

README, "What never goes in this repository", is the full list and the reason.
