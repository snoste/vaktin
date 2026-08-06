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
(fleet slugs, workflow names) live in the project that owns them — which also
keeps them out of this repository.

Point it at repositories one of two ways:

    VAKTIN_REPOS=/path/to/a:/path/to/b python3 vaktin.py

or list one absolute path per line in:

    ~/.config/vaktin/repos

A repo with no `.vaktin.json` still works — you get branches and tags, just no
build/ship join. See `.vaktin.example.json` for the schema.

── A note on exposure ───────────────────────────────────────────────────────
This binds 0.0.0.0 and has NO authentication. It renders branch names, commit
subjects, session names and release history. On a private network or a VPN
(Tailscale, WireGuard) that is usually what you want. Do not put it on a public
interface. See the README.
"""
import html
import json
import os
import re
import subprocess
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
    """`.vaktin.json` from the repo itself. Every key optional."""
    cfg = {}
    f = os.path.join(root, ".vaktin.json")
    if os.path.isfile(f):
        try:
            cfg = json.load(open(f)) or {}
        except Exception:
            cfg = {}
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
            "subject": run(["git", "log", "-1", "--format=%s", br], cwd=root)[:70],
            "age": int(run(["git", "log", "-1", "--format=%ct", br], cwd=root) or 0),
        })
    return sorted(out, key=lambda x: -x["age"])


def built_map(cfg):
    """What the deploy target actually has, keyed by version.

    Only balena is implemented, because that is what the first user deploys to.
    A repo with no `fleet` simply skips the join and still gets its tag list —
    which is why this returns (map, reachable) rather than raising.
    """
    fleet = cfg.get("fleet")
    if not fleet:
        return {}, None                       # not configured → not an error
    raw = run(["balena", "release", "list", fleet, "--json"], 40)
    if not raw:
        return {}, False                      # configured but unreachable
    built = {}
    try:
        for r in json.loads(raw):
            if r.get("semver"):
                # is_final matters as much as status: a DRAFT built fine but no
                # device will ever take it, so calling it "shipped" is a lie.
                built[r["semver"]] = (r.get("status", "?"), bool(r.get("is_final")))
    except Exception:
        return {}, False
    return built, True


def deploy_runs(root, cfg):
    """The deploy workflow's recent runs, indexed by the tag that triggered them.

    Needed to tell WHY a tag never built, because the two common causes look
    identical on GitHub and have OPPOSITE remedies (see classify_miss)."""
    wf = cfg.get("deploy_workflow")
    if not wf:
        return {}
    raw = run(["gh", "run", "list", "--workflow", wf, "--limit", "40", "--json",
               "conclusion,status,displayTitle,startedAt,updatedAt,databaseId,headBranch"],
              30, root)
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
            idx[key] = {"conclusion": r.get("conclusion") or "",
                        "status": r.get("status") or "",
                        "mins": mins, "id": r.get("databaseId")}
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


def releases(root, cfg):
    """Tags joined to what was actually built — the join nothing else does."""
    built, ok = built_map(cfg)
    runs = deploy_runs(root, cfg) if ok is not None else {}
    tags = run(["git", "tag", "-l", cfg["tag_glob"], "--sort=-v:refname"],
               cwd=root).splitlines()[:10]

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
            status, final = built[sem]
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
        out.append({"tag": t, "state": state, "note": note})
    return out, ok


def in_flight(root, cfg):
    """What CI is doing right now, plus an honest ETA from recent history."""
    fields = "status,name,displayTitle,startedAt,databaseId"
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
                             "title": r["displayTitle"][:60], "mins": mins,
                             "id": r["databaseId"]})
    except Exception:
        pass
    # ETA from the median of recent successful deploys — the honest number, not
    # a guess. Needs a workflow name, which is per-project, hence the config.
    eta = 0
    wf = cfg.get("deploy_workflow")
    if wf:
        raw = run(["gh", "run", "list", "--workflow", wf, "--status", "success",
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
    return runs, eta


_cache = {"at": 0, "data": None}


def gather():
    if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    roots = repo_list()
    projects = []
    for root in roots:
        cfg = repo_config(root)
        rel, ok = releases(root, cfg)
        runs, eta = in_flight(root, cfg)
        projects.append({
            "name": cfg["name"], "root": root,
            "branches": branches(root, cfg), "releases": rel,
            "built_ok": ok, "runs": runs, "eta": eta,
            "note": cfg.get("note", ""),
        })
    data = {"projects": projects, "sessions": sessions(roots),
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
.empty{padding:16px 14px;color:var(--muted)}
.hint{margin-top:8px;font-size:12px;color:var(--muted)}
.bar{height:3px;background:var(--line);border-radius:2px;overflow:hidden;width:120px;display:inline-block;
     vertical-align:middle;margin-left:8px}
.bar i{display:block;height:100%;background:var(--accent)}
code{font-family:'IBM Plex Mono',ui-monospace,Menlo,monospace;font-size:12px;
     background:rgba(0,0,0,.05);padding:1px 5px;border-radius:4px}
@media (prefers-color-scheme:dark){
 :root{--bg:#0f1116;--card:#171a21;--ink:#e8eaf0;--muted:#8c93a6;--line:#252a35;--accent:#8f9ddb}
 .p-ok{background:#12301f;color:#5fce93}.p-bad{background:#3a1512;color:#f2938a}
 .p-warn{background:#332508;color:#e0b25f}.p-busy{background:#1b2140;color:#9aa8e6}
 .p-idle{background:#1d212a;color:var(--muted)}
 code{background:rgba(255,255,255,.07)}}
"""


def pill(text, kind):
    return f'<span class="pill p-{kind}">{html.escape(text)}</span>'


def project_section(p, multi):
    s = []
    if multi:
        s.append(f'<h3>{html.escape(p["name"])}'
                 f'<span class="root">{html.escape(p["root"])}</span></h3>')

    # in flight first — it is the thing you are waiting on
    s.append('<h2>Í vinnslu núna</h2><div class="card">')
    if p["runs"]:
        s.append("<table><tr><th>Staða</th><th>Verk</th><th>Hvað</th>"
                 "<th>Tími</th><th>Áætlað eftir</th></tr>")
        for r in p["runs"]:
            busy = r["status"] == "in_progress"
            left = ""
            if busy and p["eta"]:
                rem = p["eta"] - r["mins"]
                pct = min(100, int(100 * r["mins"] / p["eta"])) if p["eta"] else 0
                left = (f'~{rem}m<span class="bar"><i style="width:{pct}%"></i></span>'
                        if rem > 0 else "að klárast")
            s.append(f'<tr><td>{pill("keyrir" if busy else "bíður", "busy" if busy else "idle")}</td>'
                     f'<td>{html.escape(r["name"])}</td>'
                     f'<td class="muted">{html.escape(r["title"])}</td>'
                     f'<td class="mono">{r["mins"]}m</td><td class="mono">{left}</td></tr>')
        s.append("</table>")
    else:
        s.append('<div class="empty">Ekkert í gangi.</div>')
    s.append("</div>")
    if p["eta"]:
        s.append(f'<div class="hint">Miðgildi útgáfukeyrslu: {p["eta"]} mín.</div>')

    # releases — the join that catches a tag which never built
    s.append('<h2>Útgáfur — merki → byggð?</h2><div class="card"><table>'
             "<tr><th>Merki</th><th>Staða</th><th></th></tr>")
    for r in p["releases"]:
        st = r["state"]
        kind, label, note = "idle", st, ""
        if st == "shipped":
            kind, label = "ok", "komin út"
        elif st == "draft":
            kind, label = "warn", "drög"
            note = "byggð en engin tæki taka hana"
        elif st == "not-built":
            kind, label = "bad", "ALDREI BYGGÐ"
            # The remedy is READ from the run, not assumed: a timeout kill and a
            # concurrency eviction look identical and need opposite actions.
            note = r.get("note") or "keyrðu bygginguna aftur — ekki hækka útgáfunúmer"
        elif st in ("running", "pending"):
            kind, label = "busy", "byggist"
        elif st == "unknown":
            kind, label = "idle", "óþekkt"
        s.append(f'<tr><td class="mono">{html.escape(r["tag"])}</td><td>{pill(label, kind)}</td>'
                 f'<td class="muted">{html.escape(note)}</td></tr>')
    s.append("</table></div>")
    if p["built_ok"] is False:
        s.append('<div class="hint">Náði ekki í byggingarstöðu — óþekkt.</div>')
    elif p["built_ok"] is None:
        s.append('<div class="hint">Enginn <code>fleet</code> í '
                 '<code>.vaktin.json</code> — merki eru sýnd án byggingarstöðu.</div>')

    # branches — what has NOT landed
    s.append('<h2>Greinar sem eru ekki komnar á main</h2><div class="card">')
    if p["branches"]:
        s.append("<table><tr><th>Grein</th><th>Framar</th><th>Síðast</th><th>Efni</th></tr>")
        for b in p["branches"]:
            s.append(f'<tr><td class="mono">{html.escape(b["name"])}</td>'
                     f'<td class="mono">+{b["ahead"]}</td>'
                     f'<td class="muted">{html.escape(b["when"])}</td>'
                     f'<td class="muted">{html.escape(b["subject"])}</td></tr>')
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

    multi = len(d["projects"]) > 1
    for p in d["projects"]:
        s.append(project_section(p, multi))

    # who is working — global, across every watched repo
    s.append('<h2>Lotur í gangi</h2><div class="card">')
    if d["sessions"]:
        s.append("<table><tr><th>Lota</th><th>Verkefni</th><th>Grein</th><th>Mappa</th></tr>")
        for x in d["sessions"]:
            s.append(f'<tr><td><span class="dot"></span>{html.escape(x["name"])}</td>'
                     f'<td class="muted">{html.escape(x["repo"])}</td>'
                     f'<td class="mono">{html.escape(x["branch"])}</td>'
                     f'<td class="muted">{html.escape(x["cwd"])}</td></tr>')
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
            js = ("<script>setInterval(async()=>{"
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
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
