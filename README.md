# Vaktin

**One page that answers: who is working, what has landed, what is building, and what actually shipped.**

`vaktin` is Icelandic for *the watch* — the shift you stand so nothing goes past unnoticed.

A single-file, dependency-free status page for teams (and coding agents) working
across several repositories with real deploy targets. It does one join that
nothing else does:

> **git tag → was it actually built?**

A tag is not a build. Tagging only *asks* CI for one, and a build can be
cancelled, skipped, throttled or superseded — none of which ever reads as
"failed". The run says *cancelled*, the deploy job says *skipped*, and every
dashboard stays green while the version silently does not exist. Vaktin was
written after two releases were lost that way in a single afternoon, one of them
stacked on top of the other's hole.

## What it shows

Per watched repository:

- **Í vinnslu núna** — CI runs in flight, with an ETA taken from the *median of
  your last five successful deploys* rather than a guess.
- **Útgáfur** — the last ten tags, each joined to what the deploy target really
  has: `komin út` (shipped), `drög` (built but draft — no device will take it),
  `ALDREI BYGGÐ` (**tagged and never built**), `byggist`, or `óþekkt`.
- **Greinar sem eru ekki komnar á main** — branches with commits that have not
  landed, oldest first, because the oldest is the one everyone has forgotten.

And once, globally:

- **Lotur í gangi** — live coding sessions, which repo and branch each is on.
  A session in a worktree is on its own branch; one in the main checkout has no
  branch of its own, which is worth *seeing* rather than guessing at.

The UI is Icelandic. Everything else — config, API, code — is English.

## Install

No dependencies, no build step, stdlib only. Python 3.9+.

```bash
git clone https://github.com/<you>/vaktin.git && cd vaktin && python3 vaktin.py
```

Then tell it which repositories to watch, one absolute path per line:

```bash
mkdir -p ~/.config/vaktin && $EDITOR ~/.config/vaktin/repos
```

or per-invocation:

```bash
VAKTIN_REPOS=/code/alpha:/code/beta python3 vaktin.py
```

To keep it running on macOS, see [`install.sh`](install.sh):

```bash
./install.sh install
```

That writes a `launchd` user agent (`is.vaktin.agent`) with `KeepAlive`, so it
starts at login and comes back if it dies. `./install.sh status`, `logs`,
`restart` and `uninstall` do what they say.

## Configuration lives in the watched repo, not here

Vaktin itself is configured with nothing. Each repository carries its own
`.vaktin.json`, so this tool stays generic and every project's specifics live in
the project that owns them — which also keeps private identifiers out of a
public repository.

```json
{
  "name": "my-service",
  "fleet": "myorg/my-fleet",
  "deploy_workflow": "Deploy to Production",
  "trunk": "main",
  "tag_glob": "v*"
}
```

| key | meaning | default |
|---|---|---|
| `name` | shown as the section heading | the directory name |
| `fleet` | balena deploy target to join tags against | none → tags shown without build state |
| `cloud_run` | Cloud Run deploy target — `{service, region, project, image}` | none → as above |
| `deploy_workflow` | workflow name used for the ETA median | none → no ETA |
| `trunk` | the branch others are measured against | `main` |
| `tag_glob` | which tags are releases | `v*` |

Every key is optional. A repo with no `.vaktin.json` still gets branches and
tags — you simply lose the build/ship join.

**Deploy targets.** [balenaCloud](https://www.balena.io/) and
[Cloud Run](https://cloud.google.com/run). The seam is one function,
`built_map()`, which returns `{version: (status, is_final)}` — adding a third
means implementing that and nothing else.

Balena knows versions, so its join is a lookup. Cloud Run knows container
digests and nothing about your version numbers, so that join runs through git:
CI tags the image it builds with the commit sha, a git tag names a commit, and
a revision names a digest — so **tag → sha → digest → revision** is the chain,
and every link is a recorded fact rather than a naming convention. It therefore
requires that your workflow build `$IMAGE:${{ github.sha }}`, and an
authenticated `gcloud`.

One difference is worth stating, because getting it wrong makes the panel
useless: a Cloud Run image with **no revision standing on it** is the shape
balena calls a draft — the build succeeded, the deploy did not, nothing serves
it. A **superseded** revision is not that. It shipped and was later replaced,
which is what every healthy old release looks like; marking those amber would
paint nine green releases as warnings and teach you to ignore the colour. The
revision currently taking traffic is marked `í umferð núna`.

## For coding agents

If you are an agent working in one of these repos: **read `/api` before you
assert anything about release state.** One request answers what is building,
what shipped, what is tagged-but-never-built, and what has not landed:

```bash
curl -s http://localhost:8787/api | python3 -m json.tool
```

This exists so you do not re-derive it from five CLI calls, and so you do not
report "X is not shipped" from a stale assumption. If the page is wrong, the
correct response is to **fix the tool**, not to work around it in prose.

The JSON is the same structure the page renders: `projects[]` each with
`releases[]`, `runs[]`, `branches[]`, plus a global `sessions[]`.

## Requirements

`git` always. `gh` ([GitHub CLI](https://cli.github.com/), authenticated) for CI
state. `balena` for the build join. Each is optional — a missing or unauthenticated
CLI degrades that panel to "unknown" and never takes the page down. Every
subprocess runs under a timeout for the same reason: a status page that hangs is
worse than no status page.

## Security

**Vaktin binds `0.0.0.0` and has no authentication.** It renders branch names,
commit subjects, session names and release history — internal information, not
secrets, but not things to hand to the internet either.

Run it on a private network or a VPN. [Tailscale](https://tailscale.com/) is the
easy answer: the page is then reachable at `http://<machine>.<tailnet>.ts.net:8787`
for your devices and nobody else's. `tailscale serve` will put HTTPS and a real
hostname in front of it if you want that.

Do not expose it on a public interface. If you need to, put an authenticating
proxy in front — that is deliberately not this tool's job.

## Licence

MIT — see [LICENSE](LICENSE).
