# Backend — Fred Lobster Race Rules 2027

Small FastAPI service that is the **single writer** for the rule Markdown files in
this repo. No database: the Markdown files and git are the storage.

- Binds `127.0.0.1:8100` only (localhost). Never `0.0.0.0`, no Funnel, no port
  forwarding. The public site is the static MkDocs output on GitHub Pages and contains
  no part of this backend.
- Storage: `docs/**/*.md`, `mkdocs.yml` (nav + footer version stamp), git history.
- Drafts live in `backend/drafts/` — backend-only, gitignored, never under `docs/`,
  so a half-finished rule can never reach the public site.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | version, draft state, git clean/sync state |
| GET | `/api/sections` | nav sections in `mkdocs.yml` order + file contents |
| PUT | `/api/draft/{section}` | save a draft (`{"content": "..."}`) |
| GET | `/api/diff` | draft vs published, unified diff per section |
| POST | `/api/publish` | validate → bump → build → commit → push → verify live |
| POST | `/api/rollback` | `git revert` the last publish commit, rebuild, redeploy |

Section ids are the nav path without `.md`, with `/` as `-`:
`rules-safety`, `rules-technical`, `quick-reference`, `changelog`, `index`, …

## Publish pipeline

One operation, stops at the first failure and publishes nothing:

1. **validate** — every nav section exists and is non-empty, no leftover include
   syntax, `mkdocs build --strict` (this is also the broken-internal-link check).
2. **stage** — drafts are written into `docs/`, the footer version is bumped
   (`v1.0` → `v1.1`, same `copyright:` mechanism as Phase 1), and the changelog
   entry is appended.
3. **build** — `mkdocs build --strict`; on failure the staged files are rolled back
   and nothing is committed.
4. **commit** — one commit, message `Publish v1.x: <sections> updated`, containing
   the drafts, the version stamp, and the changelog entry together.
5. **deploy** — `git push origin main`; the repo's existing GitHub Actions workflow
   (`deploy.yml`) builds and gh-deploys to `gh-pages`. No second deploy mechanism.
6. **verify** — polls the public site until it serves the new version string.

`POST /api/rollback` reverts the most recent `Publish v…` commit with `git revert`
(a clean commit), rebuilds, pushes, and waits for the live site to show the older
version again.

## Run it

```bash
cd ~/fredlobster-race-rules-2027
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m backend.app            # 127.0.0.1:8100 by default (FL_HOST / FL_PORT)
```

As a user service:

```bash
systemctl --user enable --now fredlobster-backend
systemctl --user status fredlobster-backend
curl -s http://127.0.0.1:8100/api/status
```

To expose it to the tailnet later, set `FL_HOST` to the Tailscale address
(`tailscale ip -4`) — never `0.0.0.0`.

## Notes / deliberate choices

- Section metadata (id, title, last-published version) is derived from `mkdocs.yml`
  nav plus the footer version rather than YAML front matter in the rule files: front
  matter without the `meta` extension would render as page text, and with it would
  leak into the include-markdown combined pages.
- `site/` is tracked in git upstream, so `mkdocs build` dirties it. The cleanliness
  check only looks at `docs/`, `mkdocs.yml` and `requirements.txt`, so builds don't
  block a publish.
- `publish` refuses to run when those published sources already have uncommitted
  changes, so the publish is always exactly one clean commit (and `rollback` can
  revert only that commit).
