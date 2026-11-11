"""Fred Lobster Race Rules 2027 — backend core.

Thin FastAPI service that is the single writer for the rule Markdown files.
No database: the Markdown files and git are the storage.

Endpoints (exactly these):
    GET  /api/status            version, draft state, sync state
    GET  /api/sections          section list + file contents (nav order from mkdocs.yml)
    PUT  /api/draft/{section}   save a draft edit (backend-only, never in docs/)
    GET  /api/diff              draft vs published, unified diff per section
    POST /api/publish           validate -> bump -> build -> commit -> push -> verify live
    POST /api/rollback          git-revert the last publish, rebuild, redeploy

The service binds to localhost only (see the systemd unit). It never serves the
public site; the public site is the static MkDocs output on GitHub Pages.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
BACKEND = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
MKDOCS_YML = ROOT / "mkdocs.yml"
DRAFTS = BACKEND / "drafts"
MKDOCS_BIN = ROOT / ".venv" / "bin" / "mkdocs"
SITE_URL = "https://ruddyrid.github.io/race-rulebook/"
SITE_NAME = "Fred Lobster Race Rules 2027"
LIVE_TIMEOUT = 300  # seconds to wait for GitHub Pages to serve a new version
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

app = FastAPI(title="Fred Lobster Race Rules 2027 — backend", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------- helpers


def _git(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        env=GIT_ENV, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"git {' '.join(args)} failed")
    return proc


def _copyright_value() -> str:
    text = MKDOCS_YML.read_text()
    match = re.search(r'^copyright:\s*"(.*)"\s*$', text, re.M)
    return match.group(1) if match else ""


def _version() -> str:
    match = re.search(r"v(\d+)\.(\d+)", _copyright_value())
    return f"v{match.group(1)}.{match.group(2)}" if match else "v0.0"


def _bump(version: str) -> str:
    match = re.match(r"v(\d+)\.(\d+)$", version)
    if not match:
        return "v1.0"
    return f"v{match.group(1)}.{int(match.group(2)) + 1}"


def _stamp_version(version: str) -> None:
    """Rewrite the copyright line — the same footer mechanism Phase 1 used."""
    text = MKDOCS_YML.read_text()
    value = _copyright_value()
    if not value:
        raise RuntimeError("mkdocs.yml has no copyright line to stamp")
    today = date.today().strftime("%-d %B %Y")
    new_value = re.sub(r"v\d+\.\d+", version, value, count=1)
    if re.search(r"\(updated [^)]*\)", new_value):
        new_value = re.sub(r"\(updated [^)]*\)", f"(updated {today})", new_value)
    else:
        new_value = f"{new_value} (updated {today})"
    text = text.replace(f'copyright: "{value}"', f'copyright: "{new_value}"', 1)
    MKDOCS_YML.write_text(text)


def _nav() -> list[tuple[str, str]]:
    config = yaml.safe_load(MKDOCS_YML.read_text()) or {}
    leaves: list[tuple[str, str]] = []

    def walk(node) -> None:
        if isinstance(node, str):
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for title, value in node.items():
                if isinstance(value, str) and value.endswith(".md"):
                    leaves.append((title, value))
                else:
                    walk(value)

    walk(config.get("nav") or [])
    return leaves


def _section_id(path: str) -> str:
    return path[:-3].replace("/", "-")


def _sections() -> list[dict]:
    out = []
    for title, path in _nav():
        sid = _section_id(path)
        file = DOCS / path
        out.append({
            "id": sid,
            "title": title,
            "path": path,
            "file": str(file.relative_to(ROOT)),
            "exists": file.is_file(),
        })
    return out


def _section_or_404(sid: str) -> dict:
    for section in _sections():
        if section["id"] == sid:
            return section
    raise HTTPException(status_code=404, detail=f"unknown section '{sid}'")


def _draft_file(sid: str) -> Path:
    return DRAFTS / f"{sid}.md"


def _drafts() -> dict[str, dict]:
    DRAFTS.mkdir(parents=True, exist_ok=True)
    found = {}
    for file in sorted(DRAFTS.glob("*.md")):
        found[file.stem] = {
            "section": file.stem,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(file.stat().st_mtime)),
            "bytes": file.stat().st_size,
        }
    return found


def _changed_paths() -> list[str]:
    proc = _git("status", "--porcelain", "--", "docs", "mkdocs.yml", "requirements.txt", check=False)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _sync_state() -> dict:
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=ROOT,
                       capture_output=True, text=True, env=GIT_ENV, timeout=45)
        counts = _git("rev-list", "--left-right", "--count", "HEAD...origin/main").stdout.split()
        ahead, behind = int(counts[0]), int(counts[1])
        return {"in_sync": ahead == 0 and behind == 0, "ahead": ahead, "behind": behind, "error": None}
    except Exception as exc:  # network down, no remote, etc.
        return {"in_sync": None, "ahead": None, "behind": None, "error": str(exc)}


def _run_build(strict: bool = True) -> tuple[bool, str]:
    cmd = [str(MKDOCS_BIN), "build"] + (["--strict"] if strict else [])
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return False, "\n".join(output.splitlines()[-15:])
    return True, "mkdocs build --strict clean"


def _validate() -> str | None:
    """Return a plain-language reason when validation fails, else None."""
    for section in _sections():
        if not section["exists"]:
            return f"nav entry '{section['title']}' points at {section['path']}, which does not exist"
        body = (DOCS / section["path"]).read_text().strip()
        if not body:
            return f"nav entry '{section['title']}' ({section['path']}) is empty"
        if "{% include \"" in body or "![[" in body:
            return f"{section['path']} still contains unprocessed include syntax"
    ok, detail = _run_build(strict=True)
    return None if ok else f"mkdocs build failed:\n{detail}"


def _append_changelog(version: str, summary: str) -> None:
    changelog = DOCS / "changelog.md"
    text = changelog.read_text()
    today = date.today().strftime("%-d %B %Y")
    entry = f"### {today}\n- Published {version}: {summary}\n\n"

    season = re.search(r"^## (\d{4}) Season\s*$", text, re.M)
    if season and season.group(1) == str(date.today().year):
        insert_at = season.end() + 1
        if text[insert_at:insert_at + 1] == "\n":
            insert_at += 1
        text = text[:insert_at] + entry + text[insert_at:]
    elif season:
        new_season = f"## {date.today().year} Season\n\n{entry}"
        text = text[:season.start()] + new_season + text[season.start():]
    else:
        marker = text.find("\n## ")
        insert_at = marker + 1 if marker != -1 else len(text)
        text = text[:insert_at] + f"## {date.today().year} Season\n\n{entry}" + text[insert_at:]

    changelog.write_text(text)


def _live_version(timeout: int = LIVE_TIMEOUT) -> tuple[bool, str]:
    """Poll the public site until it serves the current version string."""
    wanted = _version()
    deadline = time.time() + timeout
    last = "no response"
    while time.time() < deadline:
        try:
            url = f"{SITE_URL}?_cb={int(time.time())}"
            with urllib.request.urlopen(url, timeout=20) as response:
                body = response.read().decode("utf-8", "replace")
                last = f"HTTP {response.status}, version {'present' if wanted in body else 'not yet'}"
                if response.status == 200 and wanted in body:
                    return True, f"live site serves {wanted}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(exc)
        time.sleep(10)
    return False, f"live site did not show {wanted} within {timeout}s ({last})"


# --------------------------------------------------------------------------- models


class DraftIn(BaseModel):
    content: str


class PublishIn(BaseModel):
    message: str | None = None


# --------------------------------------------------------------------------- endpoints


@app.get("/api/status")
def status() -> dict:
    sync = _sync_state()
    drafts = _drafts()
    return {
        "site": SITE_NAME,
        "site_url": SITE_URL,
        "version": _version(),
        "version_stamp": _copyright_value(),
        "drafts": {"count": len(drafts), "sections": list(drafts)},
        "git": {
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip(),
            "head": _git("rev-parse", "--short", "HEAD", check=False).stdout.strip(),
            "clean": not _changed_paths(),
            "uncommitted": _changed_paths(),
            **sync,
        },
    }


@app.get("/api/sections")
def sections() -> dict:
    items = []
    for section in _sections():
        body = (DOCS / section["path"]).read_text() if section["exists"] else ""
        items.append({**{k: v for k, v in section.items() if k != "file"},
                      "last_published_version": _version(),
                      "content": body})
    return {"version": _version(), "sections": items}


@app.put("/api/draft/{section}")
def save_draft(section: str, payload: DraftIn) -> dict:
    _section_or_404(section)
    if not payload.content.strip():
        raise HTTPException(status_code=422, detail="refusing to store an empty draft")
    DRAFTS.mkdir(parents=True, exist_ok=True)
    _draft_file(section).write_text(payload.content)
    return {"ok": True, "section": section, "bytes": len(payload.content),
            "drafts": len(_drafts())}


@app.get("/api/diff")
def diff() -> dict:
    entries, added, removed = [], 0, 0
    for sid, _meta in _drafts().items():
        section = _section_or_404(sid) if any(s["id"] == sid for s in _sections()) else None
        if section is None:
            continue
        published = (DOCS / section["path"]).read_text().splitlines(keepends=True)
        draft = _draft_file(sid).read_text().splitlines(keepends=True)
        text = "".join(difflib.unified_diff(
            published, draft,
            fromfile=f"published/{section['path']}",
            tofile=f"draft/{section['path']}",
        ))
        added += sum(1 for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed += sum(1 for line in text.splitlines() if line.startswith("-") and not line.startswith("---"))
        entries.append({"section": sid, "path": section["path"], "diff": text, "changed": bool(text)})
    return {"version": _version(), "added": added, "removed": removed, "entries": entries}


@app.post("/api/publish")
def publish(payload: PublishIn | None = None) -> dict:
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    drafts = _drafts()
    if not drafts:
        raise HTTPException(status_code=409, detail="nothing to publish: no drafts saved")

    dirty = _changed_paths()
    if dirty:
        raise HTTPException(status_code=409, detail=(
            "working tree has uncommitted changes in the published sources; "
            "commit or discard them before publishing so the publish stays one clean commit: "
            + ", ".join(line.strip().split()[-1] for line in dirty)))

    problem = _validate()
    if problem:
        step("validate", False, problem)
        return {"ok": False, "error": problem, "steps": steps}
    step("validate", True, "all nav sections present, non-empty, mkdocs build --strict clean")

    known = {s["id"]: s for s in _sections()}
    changed_titles = [known[sid]["title"] for sid in drafts if sid in known]
    summary = ", ".join(changed_titles) + " updated" if changed_titles else "content updated"

    old_version, new_version = _version(), _bump(_version())
    try:
        for sid, _meta in drafts.items():
            section = known.get(sid)
            if section is None:
                raise RuntimeError(f"draft '{sid}' has no matching section")
            (DOCS / section["path"]).write_text(_draft_file(sid).read_text())
        _stamp_version(new_version)
        _append_changelog(new_version, summary + (f" ({payload.message})" if payload and payload.message else ""))
        step("stage", True, f"drafts applied, footer stamped {new_version}, changelog entry added")
    except Exception as exc:
        _git("checkout", "--", "docs", "mkdocs.yml", check=False)
        return {"ok": False, "error": str(exc), "steps": steps}

    ok, detail = _run_build(strict=True)
    step("build", ok, detail)
    if not ok:
        _git("checkout", "--", "docs", "mkdocs.yml", check=False)
        return {"ok": False, "error": f"build failed, nothing published: {detail}", "steps": steps}

    try:
        _git("add", "--", "docs", "mkdocs.yml")
        message = f"Publish {new_version}: {summary}"
        if payload and payload.message:
            message += f" — {payload.message}"
        _git("commit", "-m", message)
        commit = _git("rev-parse", "--short", "HEAD").stdout.strip()
        step("commit", True, f"{commit} {message}")
    except Exception as exc:
        return {"ok": False, "error": f"commit failed: {exc}", "steps": steps}

    try:
        _git("push", "origin", "HEAD:main", timeout=180)
        step("push", True, "pushed main; the existing GitHub Actions workflow builds and deploys gh-pages")
    except Exception as exc:
        return {"ok": False, "error": f"push failed (commit {commit} is local only): {exc}",
                "steps": steps, "commit": commit}

    live_ok, live_detail = _live_version()
    step("live", live_ok, live_detail)

    if not live_ok:
        return {"ok": False, "error": live_detail, "version": new_version,
                "commit": commit, "steps": steps}

    for sid in list(drafts):
        _draft_file(sid).unlink(missing_ok=True)
    return {"ok": True, "version": new_version, "previous_version": old_version,
            "commit": commit, "site_url": SITE_URL + "?_cb=" + str(int(time.time())),
            "steps": steps}


@app.post("/api/rollback")
def rollback() -> dict:
    steps: list[dict] = []
    head_message = _git("log", "-1", "--pretty=%s").stdout.strip()
    if not head_message.startswith("Publish v"):
        raise HTTPException(status_code=409, detail=(
            f"HEAD is not a publish commit ('{head_message}'); refusing to revert it"))

    before_version = _version()
    revert = _git("revert", "--no-edit", "HEAD", check=False)
    if revert.returncode != 0:
        _git("revert", "--abort", check=False)
        return {"ok": False, "error": f"git revert failed; nothing changed: {revert.stderr.strip()}", "steps": steps}

    commit = _git("rev-parse", "--short", "HEAD").stdout.strip()
    version = _version()
    steps.append({"step": "revert", "ok": True, "detail": f"{commit} reverts {before_version} publish; version now {version}"})

    ok, detail = _run_build(strict=True)
    steps.append({"step": "build", "ok": ok, "detail": detail})
    if not ok:
        _git("reset", "--hard", "HEAD~1", check=False)
        return {"ok": False, "error": f"build failed after revert, revert undone: {detail}", "steps": steps}

    try:
        _git("push", "origin", "HEAD:main", timeout=180)
        steps.append({"step": "push", "ok": True, "detail": "pushed main; workflow rebuilds gh-pages"})
    except Exception as exc:
        return {"ok": False, "error": f"push failed (revert commit {commit} is local only): {exc}",
                "steps": steps, "commit": commit}

    live_ok, live_detail = _live_version()
    steps.append({"step": "live", "ok": live_ok, "detail": live_detail})
    if not live_ok:
        return {"ok": False, "error": live_detail, "commit": commit,
                "version": version, "steps": steps}
    return {"ok": True, "version": version, "commit": commit, "steps": steps}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("FL_HOST", "127.0.0.1"),
        port=int(os.environ.get("FL_PORT", "8100")),
        log_level="info",
    )
