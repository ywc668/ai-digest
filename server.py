"""Local dashboard server — REST API + static frontend for the AI digest.

Run:  .venv/bin/python server.py          (http://localhost:8765)

Endpoints:
  GET  /api/items        — browse/sort/filter/search the archive
  POST /api/items/{id}/flag — star or hide an item
  GET  /api/sources      — distinct sources with item counts
  GET  /api/runs         — run history with token totals
  GET  /api/usage        — token usage grouped by stage
  GET  /api/stats        — global stats
  GET  /api/config       — raw config.yaml text + parsed view
  PUT  /api/config       — replace config.yaml (validated)
  PATCH /api/config      — targeted updates (thresholds, profile, backend...)
  POST /api/run          — trigger a pipeline run (subprocess)
  GET  /api/run/status   — live status + log tail of the current/last run
"""

import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("digest-server")

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="AI Digest Dashboard")


def get_store() -> Store:
    config = yaml.safe_load(CONFIG_PATH.read_text())
    db_file = config.get("state", {}).get("db_file", "digest.db")
    return Store(db_path=str(BASE_DIR / db_file))


# ── Pipeline runner (one at a time) ───────────────────────────

class RunState:
    def __init__(self):
        self.process: Optional[asyncio.subprocess.Process] = None
        self.log_lines: list[str] = []
        self.started: Optional[str] = None
        self.finished: Optional[str] = None
        self.exit_code: Optional[int] = None
        self.progress: Optional[dict] = None  # {done, total}

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None


run_state = RunState()
PROGRESS_RE = re.compile(r"Scoring progress: (\d+)/(\d+)")
SCORING_START_RE = re.compile(r"Scoring (\d+) items via")


async def _pump_output(proc: asyncio.subprocess.Process):
    from datetime import datetime, timezone
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        run_state.log_lines.append(line)
        if len(run_state.log_lines) > 500:
            run_state.log_lines = run_state.log_lines[-400:]
        m = PROGRESS_RE.search(line)
        if m:
            run_state.progress = {"done": int(m.group(1)), "total": int(m.group(2))}
        else:
            m = SCORING_START_RE.search(line)
            if m:
                run_state.progress = {"done": 0, "total": int(m.group(1))}
    await proc.wait()
    run_state.exit_code = proc.returncode
    run_state.finished = datetime.now(timezone.utc).isoformat()
    run_state.log_lines.append(f"[exit code {proc.returncode}]")


class RunRequest(BaseModel):
    no_email: bool = True


@app.post("/api/run")
async def trigger_run(req: RunRequest):
    from datetime import datetime, timezone
    if run_state.running:
        raise HTTPException(409, "A run is already in progress")
    cmd = [sys.executable, str(BASE_DIR / "main.py")]
    if req.no_email:
        cmd.append("--no-email")
    run_state.log_lines = [f"$ {' '.join(cmd)}"]
    run_state.progress = None
    run_state.exit_code = None
    run_state.finished = None
    run_state.started = datetime.now(timezone.utc).isoformat()
    run_state.process = await asyncio.create_subprocess_exec(
        *cmd, cwd=BASE_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.create_task(_pump_output(run_state.process))
    return {"status": "started"}


@app.get("/api/run/status")
async def run_status(tail: int = 40):
    return {
        "running": run_state.running,
        "started": run_state.started,
        "finished": run_state.finished,
        "exit_code": run_state.exit_code,
        "progress": run_state.progress,
        "log_tail": run_state.log_lines[-tail:],
    }


# ── Items ─────────────────────────────────────────────────────

@app.get("/api/items")
async def list_items(
    search: Optional[str] = None,
    category: Optional[str] = None,
    topic: Optional[str] = None,
    source: Optional[str] = None,
    min_score: Optional[float] = None,
    stage: Optional[str] = None,
    starred: Optional[bool] = None,
    include_hidden: bool = False,
    days: Optional[int] = None,
    run_id: Optional[int] = None,
    sort: str = "score",
    order: str = "desc",
    limit: int = 60,
    offset: int = 0,
):
    store = get_store()
    try:
        return store.query_items(
            search=search, category=category, topic=topic, source=source, min_score=min_score,
            stage=stage, starred=starred, include_hidden=include_hidden,
            days=days, run_id=run_id, sort=sort, order=order,
            limit=min(limit, 500), offset=offset,
        )
    finally:
        store.close()


class FlagRequest(BaseModel):
    field: str  # starred | hidden
    value: bool


@app.post("/api/items/{item_id}/flag")
async def flag_item(item_id: str, req: FlagRequest):
    store = get_store()
    try:
        ok = store.set_item_flag(item_id, req.field, req.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        store.close()
    if not ok:
        raise HTTPException(404, "Item not found")
    return {"ok": True}


@app.get("/api/sources")
async def list_sources():
    store = get_store()
    try:
        return store.list_sources()
    finally:
        store.close()


@app.get("/api/topics")
async def list_topics():
    config = yaml.safe_load(CONFIG_PATH.read_text())
    store = get_store()
    try:
        counts = {r["topic"]: r["n"] for r in store.topic_counts()}
    finally:
        store.close()
    return [
        {"topic": t, "count": counts.get(t, 0)}
        for t in config.get("topics", [])
    ]


# ── Intelligence: profile assistant, dig deeper ───────────────

def _scoring_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())["scoring"]


class ProfileAssistRequest(BaseModel):
    mode: str  # edit | questions | draft
    instruction: str = ""
    answers: Optional[list] = None


@app.post("/api/profile/assist")
async def profile_assist(req: ProfileAssistRequest):
    from intelligence import assist_profile
    config = yaml.safe_load(CONFIG_PATH.read_text())
    try:
        return await assist_profile(
            config["scoring"], config.get("interest_profile", ""),
            req.mode, req.instruction, req.answers,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("profile assist failed")
        raise HTTPException(502, f"LLM call failed: {type(e).__name__}")


@app.post("/api/items/{item_id}/dig")
async def dig_item(item_id: str, force: bool = False):
    from intelligence import dig_deeper
    store = get_store()
    try:
        item = store.get_item(item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        if item.get("deep_dive") and not force:
            import json as _json
            return _json.loads(item["deep_dive"])
        result = await dig_deeper(_scoring_config(), item)
        store.set_item_json(item_id, "deep_dive", result)
        return result
    finally:
        store.close()


# ── Stories ───────────────────────────────────────────────────

class StoryCreate(BaseModel):
    prompt: str


@app.post("/api/stories")
async def create_story(req: StoryCreate):
    from intelligence import build_story
    if not req.prompt.strip():
        raise HTTPException(400, "Story prompt required")
    store = get_store()
    story_id = store.create_story(req.prompt.strip())
    store.close()

    async def _build():
        s = get_store()
        try:
            await build_story(_scoring_config(), s, story_id, req.prompt.strip())
        finally:
            s.close()

    asyncio.create_task(_build())
    return {"id": story_id, "status": "building"}


@app.post("/api/stories/{story_id}/refresh")
async def refresh_story(story_id: int):
    from intelligence import build_story
    store = get_store()
    story = store.get_story(story_id)
    if not story:
        store.close()
        raise HTTPException(404, "Story not found")
    store.update_story(story_id, status="building", error=None)
    store.close()

    async def _build():
        s = get_store()
        try:
            await build_story(_scoring_config(), s, story_id, story["prompt"])
        finally:
            s.close()

    asyncio.create_task(_build())
    return {"id": story_id, "status": "building"}


@app.get("/api/stories")
async def list_stories():
    store = get_store()
    try:
        return store.list_stories()
    finally:
        store.close()


@app.get("/api/stories/{story_id}")
async def get_story(story_id: int):
    store = get_store()
    try:
        story = store.get_story(story_id)
    finally:
        store.close()
    if not story:
        raise HTTPException(404, "Story not found")
    return story


@app.delete("/api/stories/{story_id}")
async def delete_story(story_id: int):
    store = get_store()
    try:
        store.delete_story(story_id)
    finally:
        store.close()
    return {"ok": True}


# ── Reports ───────────────────────────────────────────────────

class ReportCreate(BaseModel):
    kind: str = "weekly"  # daily | weekly


@app.post("/api/reports")
async def create_report(req: ReportCreate):
    from datetime import datetime, timedelta, timezone
    from intelligence import generate_report
    if req.kind not in ("daily", "weekly"):
        raise HTTPException(400, "kind must be daily or weekly")
    days = 1 if req.kind == "daily" else 7
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).isoformat()
    config = yaml.safe_load(CONFIG_PATH.read_text())
    model = config["scoring"].get("ollama", {}).get("model", "?") \
        if config["scoring"].get("backend") == "ollama" \
        else config["scoring"].get("anthropic", {}).get("model", "?")

    store = get_store()
    report_id = store.create_report(req.kind, start, now.isoformat(), model)
    items = store.query_items(days=days, min_score=5, sort="score", limit=40)["items"]
    store.close()

    async def _gen():
        s = get_store()
        try:
            md = await generate_report(config["scoring"], items, req.kind, start, now.isoformat())
            s.finish_report(report_id, content_md=md)
        except Exception as e:
            logger.exception("report generation failed")
            s.finish_report(report_id, error=f"{type(e).__name__}: {e}")
        finally:
            s.close()

    asyncio.create_task(_gen())
    return {"id": report_id, "status": "building"}


@app.get("/api/reports")
async def list_reports():
    store = get_store()
    try:
        return store.list_reports()
    finally:
        store.close()


@app.get("/api/reports/{report_id}")
async def get_report(report_id: int):
    store = get_store()
    try:
        r = store.get_report(report_id)
    finally:
        store.close()
    if not r:
        raise HTTPException(404, "Report not found")
    return r


# ── Runs / usage / stats ──────────────────────────────────────

@app.get("/api/runs")
async def list_runs(limit: int = 30):
    store = get_store()
    try:
        return store.get_runs(limit)
    finally:
        store.close()


@app.get("/api/usage")
async def usage_summary(run_id: Optional[int] = None):
    store = get_store()
    try:
        return store.get_usage_summary(run_id)
    finally:
        store.close()


@app.get("/api/stats")
async def stats():
    store = get_store()
    try:
        return store.get_stats()
    finally:
        store.close()


# ── Config ────────────────────────────────────────────────────

REQUIRED_CONFIG_FIELDS = ["interest_profile", "feeds", "scoring"]


@app.get("/api/config")
async def get_config():
    raw = CONFIG_PATH.read_text()
    return {"yaml": raw, "parsed": yaml.safe_load(raw)}


class ConfigPut(BaseModel):
    yaml: str


@app.put("/api/config")
async def put_config(req: ConfigPut):
    try:
        parsed = yaml.safe_load(req.yaml)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}")
    if not isinstance(parsed, dict):
        raise HTTPException(400, "Config must be a YAML mapping")
    missing = [f for f in REQUIRED_CONFIG_FIELDS if f not in parsed]
    if missing:
        raise HTTPException(400, f"Missing required fields: {missing}")
    CONFIG_PATH.write_text(req.yaml)
    return {"ok": True}


class ConfigPatch(BaseModel):
    """Dot-path updates, e.g. {"scoring.stage1_threshold": 4, "scoring.backend": "ollama"}"""
    updates: dict


@app.patch("/api/config")
async def patch_config(req: ConfigPatch):
    raw = CONFIG_PATH.read_text()
    parsed = yaml.safe_load(raw)
    for path, value in req.updates.items():
        node = parsed
        keys = path.split(".")
        for key in keys[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value
    missing = [f for f in REQUIRED_CONFIG_FIELDS if f not in parsed]
    if missing:
        raise HTTPException(400, f"Patch would remove required fields: {missing}")
    # NOTE: dumping loses YAML comments; the raw editor (PUT) preserves full control
    CONFIG_PATH.write_text(yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True, width=100))
    return {"ok": True, "parsed": parsed}


# ── Source catalog & feeds ────────────────────────────────────

CATALOG_PATH = BASE_DIR / "catalog.yaml"


@app.get("/api/catalog")
async def get_catalog():
    """Curated optional sources + which feed URLs are currently enabled."""
    catalog = yaml.safe_load(CATALOG_PATH.read_text()) if CATALOG_PATH.exists() else {}
    config = yaml.safe_load(CONFIG_PATH.read_text())
    enabled = {
        feed["url"]: feed["name"]
        for feeds in (config.get("feeds") or {}).values()
        for feed in feeds
    }
    return {"catalog": catalog, "enabled": enabled}


class FeedsPut(BaseModel):
    """Full replacement of the feeds section: {category: [{name, url}, ...]}"""
    feeds: dict


@app.put("/api/feeds")
async def put_feeds(req: FeedsPut):
    if not req.feeds or not isinstance(req.feeds, dict):
        raise HTTPException(400, "feeds must be a non-empty mapping")
    for category, feeds in req.feeds.items():
        if not isinstance(feeds, list):
            raise HTTPException(400, f"feeds.{category} must be a list")
        for feed in feeds:
            if not isinstance(feed, dict) or not feed.get("name") or not feed.get("url"):
                raise HTTPException(400, f"every feed in {category} needs name and url")
            if not str(feed["url"]).startswith(("http://", "https://")):
                raise HTTPException(400, f"invalid url: {feed['url']}")
    parsed = yaml.safe_load(CONFIG_PATH.read_text())
    parsed["feeds"] = {
        cat: [{"name": f["name"], "url": f["url"]} for f in feeds]
        for cat, feeds in req.feeds.items() if feeds
    }
    CONFIG_PATH.write_text(
        yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True, width=100)
    )
    n = sum(len(v) for v in parsed["feeds"].values())
    return {"ok": True, "feed_count": n}


# ── Static frontend ───────────────────────────────────────────

@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"error": "frontend not built — static/index.html missing"}, 404)
    return FileResponse(index_file)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
