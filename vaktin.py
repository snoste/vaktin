#!/usr/bin/env python3
"""Vaktin — one page showing who is working, what landed, what is building, what shipped.

    python3 vaktin.py              # → http://localhost:8787

Exists because the facts you need live in places that know nothing about each
other: your coding sessions know what is being MADE, GitHub knows what is
BUILDING, your deploy target knows what actually SHIPPED. A tag that never built
is invisible in each of them alone — which is exactly how two releases were lost
in one afternoon on the project this was written for.

The join it does that nothing else does: **git tag → was it actually built?**

Stdlib only. Read-only. No build step, no dependencies, no database. Every
external command runs with a timeout and degrades to "unknown" rather than
hanging the page, because a status page that hangs is worse than no status page.

── Configuration ────────────────────────────────────────────────────────────
Vaktin itself is configured with nothing. Each repository it watches carries its
own `.vaktin.json`, so the tool stays generic and every project's specifics
(fleet slugs, GCP project ids, workflow names) live in the project that owns
them — which also keeps them out of this repository.

That second half is a rule, not a side effect: THIS REPOSITORY IS PUBLIC AND THE
ONES IT WATCHES ARE NOT. No identifier from a watched project belongs in this
file or any other file here — examples use `my-service`, `myorg/my-fleet`,
`my-gcp-project`. See "What never goes in this repository" in the README.

Point it at repositories one of two ways:

    VAKTIN_REPOS=/path/to/a:/path/to/b python3 vaktin.py

or list one absolute path per line in:

    ~/.config/vaktin/repos

A repo with no `.vaktin.json` still works — you get branches and tags, just no
build/ship join. See `.vaktin.example.json` for the schema.

Deploy targets: balenaCloud (`fleet`) and Cloud Run (`cloud_run`). Both plug
into one seam, `built_map()`, which returns `{version: (status, is_final)}` —
adding a third target means implementing that and nothing else.

── A note on exposure ───────────────────────────────────────────────────────
This binds 0.0.0.0 and has NO authentication. It renders branch names, commit
subjects, session names and release history. On a private network or a VPN
(Tailscale, WireGuard) that is usually what you want. Do not put it on a public
interface. See the README.
"""
import glob
import html
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8787"))
CACHE_SECONDS = int(os.environ.get("VAKTIN_CACHE_SECONDS", "15"))
SESSION_DIR = os.path.expanduser(
    os.environ.get("VAKTIN_SESSION_DIR", "~/.claude/sessions"))
CONFIG_HOME = os.path.expanduser("~/.config/vaktin")


def run(cmd, timeout=25, cwd=None):
    """Never raise, never hang: a dead CLI must not take the page with it."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


# ── which repositories to watch ──────────────────────────────────────────────
def repo_list():
    """Configured repo roots, in order. Absent ones are skipped silently — a
    laptop that does not have every checkout is normal, not an error."""
    raw = os.environ.get("VAKTIN_REPOS", "")
    paths = [p for p in raw.split(":") if p.strip()]
    if not paths:
        f = os.path.join(CONFIG_HOME, "repos")
        if os.path.isfile(f):
            paths = [l.strip() for l in open(f)
                     if l.strip() and not l.startswith("#")]
    out = []
    for p in paths:
        p = os.path.expanduser(p.strip())
        if os.path.isdir(os.path.join(p, ".git")) or os.path.isfile(os.path.join(p, ".git")):
            out.append(p)
    return out


def repo_config(root):
    """`.vaktin.json` from the repo itself. Every key optional.

    Working tree first, so editing config locally takes effect immediately.
    Falling back to the tracked copy on a REF matters for how this is actually
    deployed: a clone Vaktin only ever `git fetch`es and never checks out. Its
    refs stay current while its working tree is frozen at whatever was cloned,
    so config read only from disk would go stale — silently — the first time
    someone changed it upstream.
    """
    cfg = {}
    f = os.path.join(root, ".vaktin.json")
    if os.path.isfile(f):
        try:
            cfg = json.load(open(f)) or {}
        except Exception:
            cfg = {}
    if not cfg:
        for ref in ("origin/main", "origin/master", "HEAD"):
            raw = run(["git", "show", f"{ref}:.vaktin.json"], 10, root)
            if raw:
                try:
                    cfg = json.loads(raw) or {}
                    break
                except Exception:
                    pass
    cfg.setdefault("name", os.path.basename(root.rstrip("/")))
    cfg.setdefault("tag_glob", "v*")
    cfg.setdefault("trunk", "main")
    return cfg


# ── data ─────────────────────────────────────────────────────────────────────
def sessions(roots):
    """Live coding sessions. A session in a worktree is on its own branch; one in
    the main checkout has no branch of its own, which is worth SEEING rather than
    guessing at."""
    out = []
    for fn in sorted(os.listdir(SESSION_DIR)) if os.path.isdir(SESSION_DIR) else []:
        if not fn.endswith(".json"):
            continue
        try:
            s = json.load(open(os.path.join(SESSION_DIR, fn)))
            os.kill(int(s["pid"]), 0)                      # alive?
        except Exception:
            continue
        cwd = s.get("cwd", "")
        # Attribute the session to a WATCHED repo when its path is inside one —
        # worktrees live outside the checkout, so a plain basename lies.
        repo = os.path.basename(os.path.dirname(cwd))
        for r in roots:
            if cwd.startswith(r.rstrip("/") + "/") or cwd == r:
                repo = repo_config(r)["name"]
                break
        else:
            common = os.path.basename(cwd)
            for r in roots:
                nm = repo_config(r)["name"]
                if nm and nm in cwd:
                    repo = nm
                    break
        out.append({
            "name": s.get("name") or "?",
            "cwd": os.path.basename(cwd) or cwd,
            "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"], 5, cwd) or "—",
            "repo": repo,
        })
    return sorted(out, key=lambda x: (x["repo"], x["name"]))


def branches(root, cfg):
    """What has NOT landed on trunk."""
    trunk = cfg["trunk"]
    run(["git", "fetch", "origin", "--prune", "--tags", "-q"], 30, root)
    refs = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"],
               cwd=root).splitlines()
    out = []
    for br in refs:
        if br == trunk:
            continue
        ahead = run(["git", "rev-list", "--count", f"origin/{trunk}..{br}"],
                    cwd=root) or "0"
        if ahead == "0":
            continue
        out.append({
            "name": br,
            "ahead": ahead,
            "when": run(["git", "log", "-1", "--format=%cr", br], cwd=root),
            # not cut here: the cell clamps it and hands the whole line back on a
            # tap, so a hard truncation would only throw away what the tap reveals
            "subject": run(["git", "log", "-1", "--format=%s", br], cwd=root),
            "age": int(run(["git", "log", "-1", "--format=%ct", br], cwd=root) or 0),
        })
    return sorted(out, key=lambda x: -x["age"])


def built_map(root, cfg):
    """What the deploy target actually has, keyed by version.

    The seam every deploy target plugs into: return `{version: (status,
    is_final)}` plus whether the target was reachable at all. A third element
    may carry a per-version note, which is how a target says something the two
    flags cannot — Cloud Run uses it to mark the revision serving traffic.

    (map, reachable) rather than raising, because the three answers are
    genuinely different and only one is a problem: no target configured
    (None → skip the join, still list tags), configured but unreachable
    (False → say "unknown", never "never built"), and configured and answered.
    """
    if cfg.get("fleet"):
        return _built_balena(cfg)
    if cfg.get("cloud_run"):
        return _built_cloud_run(root, cfg)
    return {}, None                           # not configured → not an error


def _built_balena(cfg):
    raw = run(["balena", "release", "list", cfg["fleet"], "--json"], 40)
    if not raw:
        return {}, False                      # configured but unreachable
    built = {}
    try:
        for r in json.loads(raw):
            sem = r.get("semver")
            if not sem:
                continue
            # is_final matters as much as status: a DRAFT built fine but no
            # device will ever take it, so calling it "shipped" is a lie.
            entry = (r.get("status", "?"), bool(r.get("is_final")))
            prev = built.get(sem)
            # A semver can have SEVERAL releases — a failed build plus a re-run
            # that succeeded (or a local push that errored + a CI push that
            # built). "Built" means ANY of them succeeded, so a success must
            # never be clobbered by a failed sibling. The list is newest-first,
            # so an older errored row arrives LAST and used to overwrite the good
            # one — reporting a shipped version as ALDREI BYGGÐ (v5.9.23,
            # 2026-08-10). Keep the first success we see; only upgrade toward it.
            if prev is None or (prev[0] != "success" and entry[0] == "success"):
                built[sem] = entry
    except Exception:
        return {}, False
    return built, True


def _built_cloud_run(root, cfg):
    """Cloud Run's answer to "was this tag actually built?".

    Balena knows versions, so its join is a lookup. Cloud Run knows container
    DIGESTS and nothing about your version numbers, so the join runs through
    git: CI tags the image it builds with the commit sha (`…/svc:<sha>`), a git
    tag names a commit, and a revision names a digest. tag → sha → digest →
    revision is the whole chain, and every link is a recorded fact rather than
    an assumption about naming.

    The distinction worth keeping: an image with NO revision standing on it is
    the Cloud Run shape of balena's draft — the build succeeded and the deploy
    did not, so the artifact exists and nothing serves it. A superseded revision
    is NOT that: it shipped, and was later replaced, which is what every healthy
    old release looks like. Calling those drafts would paint nine green releases
    amber and teach you to ignore the colour.
    """
    c = cfg.get("cloud_run") or {}
    service, region = c.get("service"), c.get("region")
    project, image = c.get("project"), c.get("image")
    if not (service and region and project and image):
        return {}, None                       # half-configured → same as absent

    revs = run(["gcloud", "run", "revisions", "list", "--service", service,
                "--region", region, "--project", project,
                "--format=json", "--limit", "100"], 45)
    svc = run(["gcloud", "run", "services", "describe", service, "--region", region,
               "--project", project, "--format=json"], 45)
    imgs = run(["gcloud", "container", "images", "list-tags", image,
                "--format=json", "--limit", "200"], 45)
    if not (revs and svc and imgs):
        return {}, False                      # unreachable → "unknown", not "never built"

    try:
        digest_revs = {}                      # digest → revisions standing on it
        for r in json.loads(revs):
            img = ((r.get("spec") or {}).get("containers") or [{}])[0].get("image", "")
            if "@" in img:
                digest_revs.setdefault(img.split("@")[-1], []).append(
                    (r.get("metadata") or {}).get("name", ""))
        live = {t.get("revisionName") for t in
                (json.loads(svc).get("status") or {}).get("traffic", [])
                if (t.get("percent") or 0) > 0}
        sha_digest = {}                       # commit sha (the image tag) → digest
        for im in json.loads(imgs):
            for t in im.get("tags", []):
                if re.fullmatch(r"[0-9a-f]{7,40}", t):
                    sha_digest[t] = im.get("digest", "")
    except Exception:
        return {}, False

    built = {}
    for tag in run(["git", "tag", "-l", cfg["tag_glob"]], cwd=root).splitlines():
        sha = run(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=root)
        if not sha:
            continue
        digest = sha_digest.get(sha) or next(
            (d for s, d in sha_digest.items()
             if s and (sha.startswith(s) or s.startswith(sha))), "")
        if not digest:
            continue                          # no image → absent, i.e. never built
        sem = tag[1:] if tag[:1] == "v" else tag
        names = digest_revs.get(digest, [])
        if not names:
            built[sem] = ("success", False, "byggð en engin keyrsla stendur á henni")
        elif live.intersection(names):
            built[sem] = ("success", True, "í umferð núna")
        else:
            built[sem] = ("success", True)
    return built, True


def deploy_runs(root, cfg):
    """The deploy workflow's recent runs, indexed by the tag that triggered them.

    Needed to tell WHY a tag never built, because the two common causes look
    identical on GitHub and have OPPOSITE remedies (see classify_miss)."""
    wf = cfg.get("deploy_workflow")
    if not wf:
        return {}
    raw = run(["gh", "run", "list", "--workflow", wf, "--limit", "40", "--json",
               "conclusion,status,displayTitle,startedAt,updatedAt,databaseId,"
               "headBranch,headSha"], 30, root)
    idx = {}
    try:
        for r in json.loads(raw or "[]"):
            key = (r.get("headBranch") or "").strip()
            if not key:
                m = re.search(r"(v\d+\.\d+\.\d+)", r.get("displayTitle") or "")
                key = m.group(1) if m else ""
            if not key or key in idx:
                continue                      # keep the most recent per tag
            mins = 0
            try:
                a = time.mktime(time.strptime(r["startedAt"], "%Y-%m-%dT%H:%M:%SZ"))
                b = time.mktime(time.strptime(r["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"))
                mins = int((b - a) / 60)
            except Exception:
                pass
            entry = {"conclusion": r.get("conclusion") or "",
                     "status": r.get("status") or "",
                     "mins": mins, "id": r.get("databaseId")}
            idx[key] = entry
            # Also reachable by commit: a project that deploys on PUSH TO TRUNK
            # and cuts tags afterwards has every run keyed "main", so a lookup
            # by tag finds nothing and the diagnosis column is blank forever.
            # The tag names a commit, and the run records the commit it ran on.
            sha = (r.get("headSha") or "").strip()
            if sha:
                idx.setdefault(sha, entry)
    except Exception:
        pass
    return idx


def job_minutes(root, run_id):
    """How long the longest job actually EXECUTED, in minutes.

    The run's own wall time is startedAt→updatedAt, which on a queue-bound runner
    includes waiting. `timeout-minutes` applies to the job, so only the job's own
    span can be compared against it. One extra call, made only for a tag that is
    already known to be missing a build."""
    raw = run(["gh", "run", "view", str(run_id), "--json", "jobs"], 25, root)
    best = 0
    try:
        for j in (json.loads(raw or "{}").get("jobs") or []):
            a, b = j.get("startedAt"), j.get("completedAt")
            if not (a and b):
                continue
            try:
                ta = time.mktime(time.strptime(a, "%Y-%m-%dT%H:%M:%SZ"))
                tb = time.mktime(time.strptime(b, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:
                continue
            best = max(best, int((tb - ta) / 60))
    except Exception:
        return 0
    return best


def classify_miss(tag, runs, cfg):
    """Why this tag has no build, and what to actually DO about it.

    A cancelled run is the trap. GitHub reports BOTH of these as "cancelled"
    with the deploy job "skipped", and nothing ever reads "failed":

      · superseded — a newer tag joined a shared concurrency group and evicted
        this pending run. The tag is sound, only its build never happened, so
        the fix is to RE-RUN. Burning a fresh version number wastes it forever.

      · timed out — the run hit the workflow's `timeout-minutes` and was killed.
        Re-running is USELESS here: `gh run rerun` re-reads the workflow file as
        of that tag, so it runs against the same ceiling and dies at the same
        minute. The fix is to raise the ceiling first, then dispatch.

    Measured on the project this was written for: a suite that grew ~30 → ~58
    min in four days against a 55-min ceiling killed two releases, and both were
    misdiagnosed as concurrency — twice — because the surface is identical.
    Duration is what separates them, so duration is what this reads.
    """
    r = runs.get(tag)
    if not r:
        return "", "engin keyrsla fannst fyrir þetta merki"
    if r["status"] in ("in_progress", "queued", "pending", "waiting"):
        return "", "byggist núna"
    if r["conclusion"] != "cancelled":
        return r["conclusion"], "skoðaðu keyrsluna"

    # EVIDENCE FIRST, verdict second — and only when the evidence carries it.
    #
    # Two things make a confident verdict here dishonest, and both bit the first
    # version of this function:
    #   · the RUN's wall time includes time QUEUED behind other jobs on a busy
    #     runner, while timeout-minutes applies to the JOB's own execution. On a
    #     single self-hosted runner the two differ by tens of minutes, so run
    #     duration alone called a 55-minute timeout "well under the ceiling".
    #   · the ceiling in .vaktin.json is TODAY's. An old run was judged against
    #     whatever it was then, which is usually why it died and was then raised.
    # So: prefer the job's own execution time, and say what the numbers are
    # rather than pretending to know which cause it was.
    mins = r.get("job_mins") or r["mins"]
    src = "vinnslutími" if r.get("job_mins") else "heildartími (bið meðtalin)"
    ceiling = cfg.get("gate_timeout_minutes")

    # The self-calibrating signal, and the most trustworthy one here: if two or
    # more cancelled runs stopped at the SAME minute, that is a ceiling, not a
    # coincidence — and it is the ceiling that was in force THEN, which is the
    # number that matters and the one the config cannot tell you (it holds
    # today's, which was usually raised BECAUSE of these very runs).
    if r.get("peers", 0) >= 2:
        return ("cancelled",
                f"hætt við á {mins}m — og {r['peers']} keyrslur stöðvuðust á sömu "
                "mínútu, sem er tímaþak en ekki tilviljun. Hækkaðu timeout-minutes; "
                "rerun les þakið eins og það var og deyr aftur")
    if ceiling and mins >= int(ceiling) - 2:
        return ("cancelled",
                f"hætt við á {mins}m ≈ þak {ceiling}m — LÍKLEGA TÍMAÞAK: "
                "hækkaðu timeout-minutes fyrst, rerun keyrir í sama þakið")
    if ceiling:
        return ("cancelled",
                f"hætt við á {mins}m, þak núna {ceiling}m — gæti verið útrýming "
                "(þá dugar rerun) EÐA lægra þak þá. Berðu saman við keyrsluna")
    return ("cancelled",
            f"hætt við — {src} {mins}m. Settu gate_timeout_minutes í "
            ".vaktin.json til að greina tímaþak frá útrýmingu")


def release_titles(root):
    """version → human title, read from RELEASE.md's own headings.

    The tag list said WHAT was building but never what it WAS — a version
    number is not an answer. The titles already exist as the release-notes
    headings (`# <Product> vX.Y.Z — Title`), so read them instead of inventing
    a second naming scheme. Fallback per tag: the annotated tag's own subject.
    """
    titles = {}
    # From origin/main's blob, not the mirror's working tree: branches() fetches
    # refs on every gather but nothing ever checks the tree out, so the on-disk
    # RELEASE.md is frozen at clone time and silently loses every new title.
    text = run(["git", "show", "origin/main:RELEASE.md"], cwd=root)
    for line in text.splitlines():
        m = re.match(r"#\s+.*?\bv?(\d+\.\d+\.\d+[\w.-]*)\s+—\s+(.+)", line)
        if m and m.group(1) not in titles:
            titles[m.group(1)] = m.group(2).strip()
    return titles


def tag_subject(root, tag):
    """The annotated tag's message subject — fallback when RELEASE.md has no
    heading for this version (e.g. a tag cut without notes)."""
    s = run(["git", "tag", "-l", "--format=%(contents:subject)", tag], cwd=root)
    return "" if s.startswith("Release v") else s   # the script's boilerplate says nothing


def releases(root, cfg):
    """Tags joined to what was actually built — the join nothing else does."""
    built, ok = built_map(root, cfg)
    titles = release_titles(root)
    runs = deploy_runs(root, cfg) if ok is not None else {}
    tags = run(["git", "tag", "-l", cfg["tag_glob"], "--sort=-v:refname"],
               cwd=root).splitlines()[:10]

    # Reach a push-deployed project's runs by tag: deploy_runs indexes those by
    # commit (see there), and everything below looks up by tag name.
    for t in tags:
        if t not in runs:
            sha = run(["git", "rev-parse", f"{t}^{{commit}}"], cwd=root)
            if sha and sha in runs:
                runs[t] = runs[sha]

    # Time every cancelled run FIRST, so each one can be told how many of its
    # siblings stopped at the same minute (see classify_miss). A verdict that
    # needs the whole set cannot be reached one row at a time.
    if ok:
        misses = [t for t in tags
                  if (t[1:] if t[:1] == "v" else t) not in built]
        for t in misses:
            r = runs.get(t)
            if r and r.get("conclusion") == "cancelled" and "job_mins" not in r:
                r["job_mins"] = job_minutes(root, r["id"])
        spans = [runs[t]["job_mins"] for t in misses
                 if runs.get(t) and runs[t].get("job_mins")]
        for t in misses:
            r = runs.get(t)
            if r and r.get("job_mins"):
                r["peers"] = sum(1 for s in spans if abs(s - r["job_mins"]) <= 1)

    out = []
    for t in tags:
        sem = t[1:] if t[:1] == "v" else t
        note = ""
        if ok is None:
            state = "unknown"
        elif sem in built:
            entry = built[sem]
            status, final = entry[0], entry[1]
            note = entry[2] if len(entry) > 2 else ""   # optional per-version note
            if status == "success" and final:
                state = "shipped"
            elif status == "success":
                state = "draft"
            elif status in ("running", "pending"):
                state = "running"
            else:
                state = "not-built"
                _, note = classify_miss(t, runs, cfg)
        elif ok:
            state = "not-built"
            _, note = classify_miss(t, runs, cfg)
        else:
            state = "unknown"
        out.append({"tag": t, "state": state, "note": note,
                    "title": titles.get(sem) or tag_subject(root, t)})
    return out, ok


def in_flight(root, cfg):
    """What CI is doing right now, plus an honest ETA from recent history."""
    # headBranch is the REF the run is for, and for a tag push that is the tag
    # itself (v1.4.2) — which is the only place the version being built appears.
    # Without it a release build is indistinguishable from any other CI run.
    fields = "status,name,displayTitle,startedAt,databaseId,headBranch"
    raw = run(["gh", "run", "list", "--limit", "15", "--json", fields], 30, root)
    runs = []
    try:
        for r in json.loads(raw or "[]"):
            if r["status"] in ("in_progress", "queued", "pending", "waiting"):
                started = r.get("startedAt") or ""
                mins = 0
                if started:
                    try:
                        t = time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
                        mins = max(0, int((time.time() - t) / 60))
                    except Exception:
                        pass
                runs.append({"status": r["status"], "name": r["name"],
                             "title": r["displayTitle"], "mins": mins,
                             "ref": r.get("headBranch") or "",
                             "id": r["databaseId"]})
    except Exception:
        pass
    # Per-WORKFLOW ETA from the median of its own recent successes — the honest
    # number, not a guess. Cached for a while: medians move slowly and each one
    # costs a gh call. Every running row can then show a real progress fill,
    # not just the configured deploy workflow.
    for r in runs:
        if r["status"] == "in_progress":
            r["eta"] = _workflow_eta(root, r["name"])
    eta = _workflow_eta(root, cfg.get("deploy_workflow")) if cfg.get("deploy_workflow") else 0
    return runs, eta


_eta_cache = {}                      # workflow name → (fetched_at, minutes)
ETA_CACHE_SECONDS = 600


def _workflow_eta(root, workflow):
    if not workflow:
        return 0
    hit = _eta_cache.get(workflow)
    if hit and time.time() - hit[0] < ETA_CACHE_SECONDS:
        return hit[1]
    eta = 0
    raw = run(["gh", "run", "list", "--workflow", workflow, "--status", "success",
               "--limit", "5", "--json", "startedAt,updatedAt"], 30, root)
    try:
        ds = []
        for r in json.loads(raw or "[]"):
            a = time.mktime(time.strptime(r["startedAt"], "%Y-%m-%dT%H:%M:%SZ"))
            b = time.mktime(time.strptime(r["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"))
            ds.append((b - a) / 60)
        eta = int(sorted(ds)[len(ds) // 2]) if ds else 0
    except Exception:
        pass
    _eta_cache[workflow] = (time.time(), eta)
    return eta


# ── runner watcher ────────────────────────────────────────────────────────────
# Why this lives in Vaktin: a self-hosted runner's listener can die silently
# (seen during a GitHub Actions outage) — GitHub then reports the runner
# "offline", jobs queue for an hour, and every dashboard shows "waiting" with
# nothing running. The one machine that can see the dead process is the machine
# the runner lives on, and Vaktin is already resident there — so it checks each
# installed runner's listener and REVIVES it via launchd.
#
# Guard rails: a revive per runner at most every RUNNER_REVIVE_COOLDOWN, and
# after RUNNER_GIVE_UP_AFTER consecutive failed revivals it stops trying and
# shows red — a runner that dies instantly every time needs a human, and a
# kickstart loop would just hide that.

RUNNER_GLOB = os.path.expanduser("~/actions-runner-*")
RUNNER_CHECK_SECONDS = int(os.environ.get("VAKTIN_RUNNER_CHECK_SECONDS", "120"))
RUNNER_REVIVE_COOLDOWN = 600
RUNNER_GIVE_UP_AFTER = 3

_runners = {"list": [], "events": []}     # updated by the watcher thread


def runner_installs():
    """Auto-discovered runner installs: every ~/actions-runner-*/ with a
    .service file (the file holds the launchd plist path; its basename is the
    service label). No config — a new runner install is watched by existing."""
    out = []
    for d in sorted(glob.glob(RUNNER_GLOB)):
        svc = os.path.join(d, ".service")
        try:
            with open(svc) as f:
                plist = f.read().strip()
        except OSError:
            continue
        label = os.path.basename(plist)
        label = label[:-6] if label.endswith(".plist") else label
        out.append({"dir": d, "label": label,
                    "name": os.path.basename(d).replace("actions-runner-", "")})
    return out


def gh_runners(root):
    """Self-hosted runners registered to this repo, from the GitHub API — the
    CROSS-MACHINE truth. The process/launchd view above only sees runners on
    THIS box (and can only revive those), so a runner on another machine (the
    Ubuntu server) is invisible to it; this fills that gap. [] on any error, so
    a repo with no runners or no `gh` auth just shows nothing."""
    nwo = run(["gh", "repo", "view", "--json", "nameWithOwner",
               "-q", ".nameWithOwner"], cwd=root)
    if not nwo:
        return []
    out = run(["gh", "api", f"repos/{nwo}/actions/runners", "--paginate",
               "-q", r'.runners[] | "\(.name)\t\(.status)\t\(.busy)\t'
                     r'\([.labels[].name] | join(","))"'], cwd=root)
    rows = []
    for line in (out or "").splitlines():
        p = line.split("\t")
        if len(p) == 4:
            rows.append({"name": p[0], "online": p[1] == "online",
                         "busy": p[2] == "true", "labels": p[3]})
    return rows


def _listener_alive(rdir):
    return subprocess.run(["pgrep", "-f", os.path.join(rdir, "bin", "Runner.Listener")],
                          capture_output=True).returncode == 0


def _runner_busy(rdir):
    """A runner executing a job has a Runner.Worker child. A stale-queue
    revival must NEVER kickstart a busy runner — that kills a live job."""
    return subprocess.run(["pgrep", "-f", os.path.join(rdir, "bin", "Runner.Worker")],
                          capture_output=True).returncode == 0


def _revive(label):
    return subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
        capture_output=True, text=True)


def _queue_is_stalled():
    """True when work is QUEUED but nothing at all is running — the second way
    a runner dies. A listener can survive as a process while its long-poll
    session goes stale (seen after a GitHub Actions outage + a cancelled run):
    the platform shows the runner idle, jobs queue forever, and no process
    check can see it. The signature is queued-with-nothing-in-progress across
    every watched project for a sustained spell."""
    data = _cache.get("data") or {}
    queued = running = 0
    for p in data.get("projects", []):
        for r in p.get("runs", []):
            if r["status"] == "in_progress":
                running += 1
            else:
                queued += 1
    return queued > 0 and running == 0


STALE_QUEUE_TICKS = 3        # consecutive watcher ticks (× RUNNER_CHECK_SECONDS)


def runner_watch_loop():
    fails = {}                       # label → {count, last_attempt}
    stalled_ticks = 0
    while True:
        # Keep the picture fresh even with no browser open — the stall check
        # reads the same cache the page does.
        try:
            gather()
        except Exception:
            pass
        # Stale-idle revival: sustained queued-but-nothing-running revives every
        # watched runner (rate-limited by the same cooldown bookkeeping below,
        # via a synthetic "dead" pass). One tick is normal scheduling latency;
        # several in a row is a wedged listener session.
        stalled_ticks = stalled_ticks + 1 if _queue_is_stalled() else 0
        force_revive = stalled_ticks >= STALE_QUEUE_TICKS
        if force_revive:
            stalled_ticks = 0
        rows = []
        for r in runner_installs():
            # force_revive treats an ALIVE-but-idle listener as dead (stale
            # session); a busy runner is exempt — it is provably not the problem.
            alive = _listener_alive(r["dir"]) and not (force_revive and not _runner_busy(r["dir"]))
            f = fails.setdefault(r["label"], {"count": 0, "last": 0.0})
            state = "ok"
            if alive:
                f["count"] = 0
            elif f["count"] >= RUNNER_GIVE_UP_AFTER:
                state = "gafst-upp"
            elif time.time() - f["last"] >= RUNNER_REVIVE_COOLDOWN or f["count"] == 0:
                f["last"] = time.time()
                f["count"] += 1
                res = _revive(r["label"])
                revived = res.returncode == 0 and _wait_alive(r["dir"])
                state = "endurræstur" if revived else "endurræsing-mistókst"
                if revived:
                    f["count"] = 0
                why = " (biðröð föst)" if force_revive else ""
                _runners["events"].insert(0, {
                    "ts": time.strftime("%H:%M:%S"), "runner": r["name"],
                    "action": ((f"endurræsti keyrarann{why}") if revived
                               else f"endurræsing mistókst ({f['count']}/{RUNNER_GIVE_UP_AFTER})")})
                del _runners["events"][20:]
            else:
                state = "dauður-bíð"
            rows.append({"name": r["name"], "label": r["label"],
                         "alive": alive or state == "endurræstur", "state": state})
        _runners["list"] = rows
        time.sleep(RUNNER_CHECK_SECONDS)


def _wait_alive(rdir, tries=10):
    for _ in range(tries):
        time.sleep(1)
        if _listener_alive(rdir):
            return True
    return False


_cache = {"at": 0, "data": None}


def gather():
    if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    roots = repo_list()
    projects = []
    gh_runner_rows, seen_runners = [], set()
    for root in roots:
        cfg = repo_config(root)
        rel, ok = releases(root, cfg)
        runs, eta = in_flight(root, cfg)
        # Registered self-hosted runners (deduped across repos — one physical
        # runner can serve several). This is what surfaces the Ubuntu server.
        for r in gh_runners(root):
            if r["name"] not in seen_runners:
                seen_runners.add(r["name"])
                gh_runner_rows.append(r)
        projects.append({
            "name": cfg["name"], "root": root,
            "branches": branches(root, cfg), "releases": rel,
            "built_ok": ok, "runs": runs, "eta": eta,
            "note": cfg.get("note", ""),
        })
    data = {"projects": projects, "sessions": sessions(roots),
            "runners": _runners, "github_runners": gh_runner_rows,
            "configured": bool(roots), "at": time.strftime("%H:%M:%S")}
    _cache.update(at=time.time(), data=data)
    return data


# ── page ─────────────────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box}
:root{--bg:#f4f5f9;--card:#fff;--ink:#12141c;--muted:#666e85;--line:#e2e5ee;
      --accent:#37478f;--ok:#1d7a4c;--warn:#9a6300;--bad:#b3261e;--busy:#37478f}
body{margin:0;background:var(--bg);color:var(--ink);
     font-family:'Public Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     font-size:14px;line-height:1.5}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:baseline;gap:14px;margin-bottom:22px}
h1{font-size:20px;margin:0;letter-spacing:-.01em}
.stamp{font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted)}
h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);
   margin:26px 0 10px;font-weight:600}
h3{font-size:15px;margin:34px 0 4px;letter-spacing:-.01em}
h3 .root{font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;font-size:11.5px;
         color:var(--muted);font-weight:400;margin-left:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse}
td,th{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:none}
.mono{font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;font-size:12.5px}
.muted{color:var(--muted)}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;
      letter-spacing:.02em;white-space:nowrap}
.p-ok{background:#e6f4ec;color:var(--ok)}
.p-bad{background:#fde9e7;color:var(--bad)}
.p-warn{background:#fdf1de;color:var(--warn)}
.p-busy{background:#e8ebf7;color:var(--busy)}
.p-idle{background:#eef0f5;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:7px;background:var(--busy)}
.ref-tag{color:var(--accent);font-weight:600}
/* The row IS the progress bar: the fill sweeps left to right as elapsed/ETA grows. */
tr.prog{background:linear-gradient(90deg,rgba(55,71,143,.13) var(--pct),transparent var(--pct))}
tr.over{background:rgba(154,99,0,.12)}
.empty{padding:16px 14px;color:var(--muted)}
.hint{margin-top:8px;font-size:12px;color:var(--muted)}
.bar{height:3px;background:var(--line);border-radius:2px;overflow:hidden;width:120px;display:inline-block;
     vertical-align:middle;margin-left:8px}
.bar i{display:block;height:100%;background:var(--accent)}
code{font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;font-size:12px;
     background:rgba(0,0,0,.05);padding:1px 5px;border-radius:4px}
/* A commit subject is one line of interest and five lines of height. Clamp it,
   and let hover (title=) or a tap give the whole thing back. */
.clip{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
      overflow-wrap:anywhere}
/* Chrome clamps block containers too now, so un-clamping needs the property reset
   as well as the display change — dropping to block alone leaves it clipped. */
.clip:focus,.clip:active,.clip.open{display:block;-webkit-line-clamp:initial;overflow:visible;
                                    outline:none}

/* Phones. 720 rather than 640 because in between the five-column table survives
   only by squeezing the job name to an ellipsis, which is what you scan for. */
@media (max-width:720px){
 body{font-size:13px}
 .wrap{padding:14px 12px 40px}
 header{flex-wrap:wrap;gap:6px;margin-bottom:14px}
 h1{font-size:17px}
 h2{margin:18px 0 8px}
 h3{margin:24px 0 4px;font-size:14px}
 h3 .root{display:block;margin-left:0;overflow-wrap:anywhere}
 td,th{padding:7px 10px}
 .pill{padding:1px 7px;font-size:10.5px}
 .mono{font-size:11.5px;overflow-wrap:anywhere}
 .empty,.hint{font-size:12px}
 /* one line for names and branches — but only here: on a wide screen the column
    has room to wrap them, and clamping there would hide a name for no gain */
 .clip1{-webkit-line-clamp:1}
 /* Five columns do not fit a phone, and the horizontal scroll that follows hides
    the very column you opened the page for — how long is left. So each row stops
    being a row: the facts you scan on line one, the prose underneath. */
 table.stack,table.stack tbody,table.stack tr,table.stack td{display:block}
 table.stack .hd{display:none}
 table.stack tr{display:grid;grid-template-columns:auto 1fr auto;align-items:center;
                column-gap:8px;row-gap:3px;padding:8px 10px;border-bottom:1px solid var(--line)}
 table.stack tr:last-child{border-bottom:none}
 table.stack td{padding:0;border:none;min-width:0}
 table.stack td:empty{display:none}
 .c-status{grid-row:1;grid-column:1}
 .c-job{grid-row:1;grid-column:2}
 .c-time{grid-row:1;grid-column:3;justify-self:end}
 /* the ref shares line two with the subject rather than costing a line of its
    own — "which version is building" is the question the panel exists for */
 /* capped: a grid column is shared by every row, so one long branch name would
    otherwise widen column one and shove the job names of all the others right */
 .c-ref{grid-row:2;grid-column:1;max-width:34vw}
 .c-what{grid-row:2;grid-column:2/-1}
 .c-eta{grid-row:3;grid-column:1/-1}
 .c-tag{grid-row:1;grid-column:1}
 .c-state{grid-row:1;grid-column:2;justify-self:start}
 .c-rtitle{grid-row:2;grid-column:1/-1}
 .c-note{grid-row:3;grid-column:1/-1}
 .c-branch{grid-row:1;grid-column:1/3}
 .c-ahead{grid-row:1;grid-column:3;justify-self:end}
 .c-subject{grid-row:2;grid-column:1/-1}
 .c-when{grid-row:3;grid-column:1/-1}
 .c-sess{grid-row:1;grid-column:1/3;white-space:nowrap}
 .c-proj{grid-row:1;grid-column:3;justify-self:end}
 .c-sbranch{grid-row:2;grid-column:1/3}
 .c-cwd{grid-row:2;grid-column:3;justify-self:end}
 .c-run{grid-row:1;grid-column:1/3}
 .c-rstate{grid-row:1;grid-column:3;justify-self:end}
 .c-rnote{grid-row:2;grid-column:1/-1}
 .c-evts{grid-row:1;grid-column:1}
 .c-evtx{grid-row:1;grid-column:2/-1}
 /* A three-line block tinted to 40% of its WIDTH reads as a column, not as
    progress. Same number, drawn as a bar along the bottom edge instead. */
 table.stack tr.prog{background:none;position:relative;padding-bottom:11px}
 table.stack tr.prog::after{content:"";position:absolute;left:0;right:0;bottom:0;height:3px;
   background:linear-gradient(90deg,var(--accent) var(--pct),var(--line) var(--pct))}
}
@media (prefers-color-scheme:dark){
 :root{--bg:#0f1116;--card:#171a21;--ink:#e8eaf0;--muted:#8c93a6;--line:#252a35;--accent:#8f9ddb}
 .p-ok{background:#12301f;color:#5fce93}.p-bad{background:#3a1512;color:#f2938a}
 .p-warn{background:#332508;color:#e0b25f}.p-busy{background:#1b2140;color:#9aa8e6}
 .p-idle{background:#1d212a;color:var(--muted)}
 code{background:rgba(255,255,255,.07)}}
"""


def pill(text, kind):
    return f'<span class="pill p-{kind}">{html.escape(text)}</span>'


def clip(text, lines=2):
    """Long prose in a narrow cell: clamped, with the full text one hover or tap away."""
    t = html.escape(text)
    cls = "clip clip1" if lines == 1 else "clip"
    return f'<span class="{cls}" tabindex="0" title="{t}">{t}</span>'


def project_section(p, multi):
    s = []
    if multi:
        s.append(f'<h3>{html.escape(p["name"])}'
                 f'<span class="root">{html.escape(p["root"])}</span></h3>')

    # in flight first — it is the thing you are waiting on
    s.append('<h2>Í vinnslu núna</h2><div class="card">')
    if p["runs"]:
        s.append('<table class="stack"><tr class="hd"><th>Staða</th><th>Verk</th>'
                 "<th>Merki/grein</th><th>Hvað</th>"
                 "<th>Tími</th><th>Áætlað eftir</th></tr>")
        # One self-hosted runner ⇒ one job at a time. A queued run is therefore
        # always waiting for THE RUNNER, and the thing holding it is whichever
        # run is in_progress — so say that, instead of a bare "bíður".
        holder = next((r for r in p["runs"] if r["status"] == "in_progress"), None)
        for r in p["runs"]:
            busy = r["status"] == "in_progress"
            left, row_style = "", ""
            if busy:
                # The ROW is the progress bar: a translucent fill sweeps left to
                # right as elapsed/ETA grows, so progress is visible at a glance
                # without a separate widget. Past the ETA the tint turns amber
                # and says by how much — an overdue run must look overdue, not
                # sit at 99% forever (that is how two timeout-killed releases
                # went unnoticed).
                eta = r.get("eta") or p["eta"]
                if eta:
                    pct = min(100, int(100 * r["mins"] / eta))
                    over = r["mins"] - eta
                    # the percentage travels as a custom property, not a finished
                    # gradient, so the phone can draw it as a bar instead (CSS)
                    if over <= 0:
                        left = f"~{eta - r['mins']}m eftir"
                        row_style = f' class="prog" style="--pct:{pct}%"'
                    else:
                        left = f"+{over}m yfir áætlun"
                        row_style = ' class="over"'
                else:
                    left = "keyrir"
            else:
                left = (f'bíður eftir keyrara — {html.escape(holder["name"])} heldur honum'
                        if holder else "bíður eftir keyrara")
            # A tag reads as the version being built and is highlighted as one;
            # a branch name is context, so it stays muted.
            ref, rcls = r.get("ref") or "", "c-ref mono"
            if re.match(r"^v?\d+\.\d+", ref):
                rcls += " ref-tag"
            else:
                rcls += " muted"
            s.append(f'<tr{row_style}><td class="c-status">'
                     f'{pill("keyrir" if busy else "bíður", "busy" if busy else "idle")}</td>'
                     f'<td class="c-job">{clip(r["name"], 1)}</td>'
                     f'<td class="{rcls}">{clip(ref, 1) if ref else ""}</td>'
                     f'<td class="c-what muted">{clip(r["title"])}</td>'
                     f'<td class="c-time mono">{r["mins"]}m</td>'
                     f'<td class="c-eta mono">{left}</td></tr>')
        s.append("</table>")
    else:
        s.append('<div class="empty">Ekkert í gangi.</div>')
    s.append("</div>")
    if p["eta"]:
        s.append(f'<div class="hint">Miðgildi útgáfukeyrslu: {p["eta"]} mín.</div>')

    # releases — the join that catches a tag which never built
    s.append('<h2>Útgáfur — merki → byggð?</h2><div class="card"><table class="stack">'
             '<tr class="hd"><th>Merki</th><th>Hvað</th><th>Staða</th><th></th></tr>')
    for r in p["releases"]:
        st = r["state"]
        kind, label, note = "idle", st, ""
        if st == "shipped":
            kind, label = "ok", "komin út"
            note = r.get("note") or ""      # e.g. Cloud Run's "í umferð núna"
        elif st == "draft":
            kind, label = "warn", "drög"
            note = r.get("note") or "byggð en engin tæki taka hana"
        elif st == "not-built":
            kind, label = "bad", "ALDREI BYGGÐ"
            # The remedy is READ from the run, not assumed: a timeout kill and a
            # concurrency eviction look identical and need opposite actions.
            note = r.get("note") or "keyrðu bygginguna aftur — ekki hækka útgáfunúmer"
        elif st in ("running", "pending"):
            kind, label = "busy", "byggist"
        elif st == "unknown":
            kind, label = "idle", "óþekkt"
        title = r.get("title") or ""
        s.append(f'<tr><td class="c-tag mono">{html.escape(r["tag"])}</td>'
                 f'<td class="c-rtitle">{clip(title) if title else ""}</td>'
                 f'<td class="c-state">{pill(label, kind)}</td>'
                 f'<td class="c-note muted">{clip(note) if note else ""}</td></tr>')
    s.append("</table></div>")
    if p["built_ok"] is False:
        s.append('<div class="hint">Náði ekki í byggingarstöðu — óþekkt.</div>')
    elif p["built_ok"] is None:
        s.append('<div class="hint">Ekkert <code>fleet</code> eða '
                 '<code>cloud_run</code> í <code>.vaktin.json</code> — merki eru '
                 'sýnd án byggingarstöðu.</div>')

    # branches — what has NOT landed
    s.append('<h2>Greinar sem eru ekki komnar á main</h2><div class="card">')
    if p["branches"]:
        s.append('<table class="stack"><tr class="hd"><th>Grein</th><th>Framar</th>'
                 "<th>Síðast</th><th>Efni</th></tr>")
        for b in p["branches"]:
            s.append(f'<tr><td class="c-branch mono">{clip(b["name"], 1)}</td>'
                     f'<td class="c-ahead mono">+{b["ahead"]}</td>'
                     f'<td class="c-when muted">{html.escape(b["when"])}</td>'
                     f'<td class="c-subject muted">{clip(b["subject"])}</td></tr>')
        s.append("</table>")
    else:
        s.append('<div class="empty">Allt komið á main.</div>')
    s.append("</div>")
    return "".join(s)


def page(d):
    s = []
    s.append('<div class="wrap"><header><h1>Vaktin</h1>'
             f'<span class="stamp">uppfært {d["at"]} · sjálfvirkt á 20 s</span></header>')

    if not d["configured"]:
        s.append('<div class="card"><div class="empty">'
                 'Engin verkefni stillt. Settu slóðir í <code>~/.config/vaktin/repos</code> '
                 '(ein á línu) eða <code>VAKTIN_REPOS=/slóð/a:/slóð/b</code>.'
                 '</div></div></div>')
        return "".join(s)

    # Registered runners across ALL machines — GitHub's own view (online/busy),
    # so the Ubuntu server shows here even though the self-heal below is mac-only.
    ghr = d.get("github_runners") or []
    if ghr:
        s.append('<div class="card"><table class="stack"><tr class="hd">'
                 "<th>Keyrari (GitHub)</th><th>Staða</th><th></th></tr>")
        for r in ghr:
            if not r["online"]:
                kind, label = "bad", "aftengdur"
            elif r["busy"]:
                kind, label = "warn", "að vinna"
            else:
                kind, label = "ok", "laus"
            s.append(f'<tr><td class="c-run mono">{clip(r["name"], 1)}</td>'
                     f'<td class="c-rstate">{pill(label, kind)}</td>'
                     f'<td class="c-rnote muted">{clip(r.get("labels",""), 1)}</td></tr>')
        s.append("</table></div>")

    # CI runners on THIS machine — watched and self-healed (see runner_watch_loop)
    rn = d.get("runners") or {}
    if rn.get("list"):
        s.append('<div class="card"><table class="stack"><tr class="hd">'
                 "<th>Keyrari</th><th>Staða</th><th></th></tr>")
        for r in rn["list"]:
            if r["alive"]:
                kind, label, note = "ok", "á lífi", ""
            elif r["state"] == "gafst-upp":
                kind, label = "bad", "DAUÐUR"
                note = "endurræsing mistókst ítrekað — þarf handafl"
            else:
                kind, label, note = "warn", "dauður", "reyni endurræsingu sjálfkrafa"
            s.append(f'<tr><td class="c-run mono">{clip(r["name"], 1)}</td>'
                     f'<td class="c-rstate">{pill(label, kind)}</td>'
                     f'<td class="c-rnote muted">{clip(note) if note else ""}</td></tr>')
        for e in rn.get("events", [])[:5]:
            s.append(f'<tr><td class="c-evts mono muted">{e["ts"]}</td>'
                     f'<td colspan=2 class="c-evtx muted">{html.escape(e["runner"])}: '
                     f'{html.escape(e["action"])}</td></tr>')
        s.append("</table></div>")

    multi = len(d["projects"]) > 1
    for p in d["projects"]:
        s.append(project_section(p, multi))

    # who is working — global, across every watched repo
    s.append('<h2>Lotur í gangi</h2><div class="card">')
    if d["sessions"]:
        s.append('<table class="stack"><tr class="hd"><th>Lota</th><th>Verkefni</th>'
                 "<th>Grein</th><th>Mappa</th></tr>")
        for x in d["sessions"]:
            s.append(f'<tr><td class="c-sess"><span class="dot"></span>'
                     f'{html.escape(x["name"])}</td>'
                     f'<td class="c-proj muted">{clip(x["repo"], 1)}</td>'
                     f'<td class="c-sbranch mono">{clip(x["branch"], 1)}</td>'
                     f'<td class="c-cwd muted">{clip(x["cwd"], 1)}</td></tr>')
        s.append("</table>")
    else:
        s.append('<div class="empty">Engin lota keyrir.</div>')
    s.append("</div></div>")
    return "".join(s)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            body = json.dumps(gather(), ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        else:
            # Swap the content in place instead of <meta refresh>: a full reload
            # throws you back to the top every 20 s, which makes everything below
            # the fold unreadable — the sections you scrolled down to see.
            # Delegated, so it survives the content swap below replacing every row.
            # A tap on a clamped subject shows all of it; a second tap folds it.
            js = ("<script>document.addEventListener('click',e=>{"
                  "const c=e.target.closest('.clip');if(c)c.classList.toggle('open');});"
                  "setInterval(async()=>{"
                  "try{const r=await fetch('/',{cache:'no-store'});"
                  "const d=new DOMParser().parseFromString(await r.text(),'text/html');"
                  "const n=d.querySelector('.wrap'),o=document.querySelector('.wrap');"
                  "if(n&&o)o.innerHTML=n.innerHTML;}catch(e){}},20000);</script>")
            body = (f"<!doctype html><meta charset=utf-8>"
                    f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                    f"<title>Vaktin</title>"
                    f"<style>{CSS}</style>{page(gather())}{js}").encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Vaktin → http://localhost:{PORT}   (Ctrl-C to stop)")
    for r in repo_list():
        print(f"  watching {r}")
    for r in runner_installs():
        print(f"  watching runner {r['name']} ({r['label']})")
    threading.Thread(target=runner_watch_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
