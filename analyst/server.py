"""HTTP API and web app. Long jobs stream progress as server-sent events."""
import hmac
import json
import os
import pathlib
import queue
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import yaml
from pydantic import BaseModel

from .harness import KINDS, Harness, parse_md

STATIC = pathlib.Path(__file__).parent / "static"
h: Harness = None


@asynccontextmanager
async def lifespan(app):
    global h
    h = Harness(os.environ.get("ANALYST_CONFIG", "config.yaml"))
    yield
    h.close()


def admin(request: Request):
    """Management endpoints can run code (plugins, MCP commands). Allow them from this machine
    only, or from anywhere with the token in ANALYST_ADMIN_TOKEN."""
    token = os.environ.get("ANALYST_ADMIN_TOKEN")
    if token:
        ok = hmac.compare_digest(request.headers.get("x-admin-token", ""), token)
    else:
        ok = request.client and request.client.host in ("127.0.0.1", "::1", "testclient")
    if not ok:
        raise HTTPException(403, "admin token required")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
manage = [Depends(admin)]


@app.middleware("http")
async def revalidate(request, call_next):
    """Make browsers check for a newer page, script and stylesheet on every load."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def bad(e):
    return HTTPException(400, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "llm": h.cfg.get("llm", "anthropic/claude-opus-5-5"),
        "llm_base_url": h.cfg.get("llm_base_url", ""),
        "assistants": assistants(),
        "mcp": [{"name": n, "tools": len(s["tools"]), "error": s["error"]} for n, s in h.mcp.items()],
        "documents": h.kb.col.count(),
    }


# ---- assistants ----------------------------------------------------------------------
class AssistantIn(BaseModel):
    description: str = ""
    domain: str = ""
    type: str = "report"      # report | brief | table
    prompt: str = ""
    mcp: list[str] = []       # sources it may query; empty = all
    docs: list[str] = []      # knowledge-base sources searched first


@app.get("/api/assistants")
def assistants():
    return [{"name": a["name"], "description": str(a.get("description") or ""),
             "domain": str(a.get("domain") or ""), "type": a.get("type") or "report",
             "mcp": a.get("mcp") or [], "docs": a.get("docs") or [], "prompt": a["prompt"]}
            for a in h.assistants.values()]


@app.put("/api/assistants/{name}", dependencies=manage)
def assistant_put(name: str, a: AssistantIn):
    meta = {k: v for k, v in a.model_dump().items() if k != "prompt" and v}
    lib_path("assistants", name).write_text(
        "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + a.prompt.strip() + "\n",
        encoding="utf-8", newline="\n")
    h.reload()


# ---- conversations -------------------------------------------------------------------
class Job(BaseModel):
    question: str = ""
    session: str = "default"
    assistant: str | None = None
    catalog: str | None = None  # MCP server name: catalog it instead of answering a question


@app.get("/api/sessions")
def sessions():
    return h.sessions()


@app.get("/api/sessions/{session}")
def session(session: str):
    return h.history(session)


@app.delete("/api/sessions/{session}")
def forget(session: str):
    h.forget(session)


pending = {}  # question id -> queue the waiting agent reads its answer from


class Answer(BaseModel):
    id: str
    answer: str


@app.post("/api/answer")
def answer(a: Answer):
    if a.id not in pending:
        raise HTTPException(404, "that question is no longer waiting")
    pending[a.id].put(a.answer)


@app.post("/api/chat")
def chat(job: Job):
    if job.catalog and job.catalog not in h.mcp:
        raise HTTPException(404, "unknown source")
    events = queue.Queue()

    def ask(question):
        qid = uuid.uuid4().hex
        pending[qid] = queue.Queue()
        events.put({"type": "question", "id": qid, "text": question})
        try:
            return pending[qid].get(timeout=900)
        except queue.Empty:
            return ""
        finally:
            del pending[qid]

    def work():
        try:
            if job.catalog:
                result = h.catalog(job.catalog, job.question, events.put)
            else:
                result = h.research(job.question, job.session, job.assistant, events.put, ask)
            events.put({"type": "done", **result})
        except Exception as e:
            events.put({"type": "error", "message": f"{type(e).__name__}: {e}"})

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            e = events.get()
            yield f"data: {json.dumps(e, default=str)}\n\n"
            if e["type"] in ("done", "error"):
                return

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---- library: assistants, agents, rules, skills, plugins -----------------------------
class Content(BaseModel):
    content: str


def lib_path(kind, name):
    try:
        return h.path_for(kind, name)
    except ValueError as e:
        raise bad(e)


@app.get("/api/library/{kind}", dependencies=manage)
def library(kind: str):
    if kind not in KINDS:
        raise HTTPException(404)
    out = []
    for p in sorted((h.root / kind).glob("*" + KINDS[kind])):
        item = {"name": p.stem, "description": "", "error": None}
        if kind == "plugins":
            item["error"] = h.plugins.get(p.stem)
        else:
            try:
                meta = parse_md(p.stem, p.read_text(encoding="utf-8"))
                item["description"] = str(meta.get("description") or meta.get("role") or "")
            except Exception as e:
                item["error"] = str(e)
        out.append(item)
    return out


@app.get("/api/library/{kind}/{name}", dependencies=manage)
def library_get(kind: str, name: str):
    p = lib_path(kind, name)
    if not p.exists():
        raise HTTPException(404)
    return {"content": p.read_text(encoding="utf-8")}


@app.put("/api/library/{kind}/{name}", dependencies=manage)
def library_put(kind: str, name: str, body: Content):
    p = lib_path(kind, name)
    try:
        if kind == "plugins":
            compile(body.content, p.name, "exec")
        else:
            parse_md(name, body.content)
    except Exception as e:
        raise bad(f"{type(e).__name__}: {e}")
    p.write_text(body.content, encoding="utf-8", newline="\n")
    h.reload()
    return {"error": h.plugins.get(name) if kind == "plugins" else None}


@app.delete("/api/library/{kind}/{name}", dependencies=manage)
def library_delete(kind: str, name: str):
    lib_path(kind, name).unlink(missing_ok=True)
    h.reload()


# ---- sources: MCP servers ------------------------------------------------------------
@app.get("/api/mcp", dependencies=manage)
def mcp_list():
    return [{"name": n, "config": s["cfg"], "error": s["error"],
             "tools": [t.name for t in s["tools"]], "from_plugin": n not in (h.cfg.get("mcp") or {})}
            for n, s in h.mcp.items()]


@app.put("/api/mcp/{name}", dependencies=manage)
def mcp_put(name: str, config: dict):
    if not name.replace("-", "").replace("_", "").isalnum():
        raise bad("names may use letters, digits, - and _ only")
    if not (config.get("command") or config.get("url")):
        raise bad("a source needs a command or a url")
    h.cfg.setdefault("mcp", {})
    h.cfg["mcp"] = {**(h.cfg["mcp"] or {}), name: config}
    h.save_config()
    h.disconnect(name)
    h.reload()
    return {"error": h.mcp[name]["error"], "tools": len(h.mcp[name]["tools"])}


@app.post("/api/mcp/{name}/reconnect", dependencies=manage)
def mcp_reconnect(name: str):
    h.disconnect(name)
    h.reload()
    if name not in h.mcp:
        raise HTTPException(404)
    return {"error": h.mcp[name]["error"]}


@app.delete("/api/mcp/{name}", dependencies=manage)
def mcp_delete(name: str):
    (h.cfg.get("mcp") or {}).pop(name, None)
    h.save_config()
    h.reload()


# ---- documents -----------------------------------------------------------------------
class Url(BaseModel):
    url: str


@app.get("/api/documents", dependencies=manage)
def documents():
    return h.kb.sources()


@app.post("/api/documents/upload", dependencies=manage)
def upload(files: list[UploadFile]):
    done = {}
    uploads = h.data / "uploads"
    uploads.mkdir(exist_ok=True)
    for f in files:
        target = uploads / pathlib.Path(f.filename).name  # drop any client-supplied directories
        target.write_bytes(f.file.read())
        done.update(h.kb.ingest_path(target, base=uploads))
    return done


@app.post("/api/documents/url", dependencies=manage)
def add_url(body: Url):
    try:
        return {"chunks": h.kb.ingest_url(body.url)}
    except Exception as e:
        raise bad(f"{type(e).__name__}: {e}")


@app.delete("/api/documents", dependencies=manage)
def document_delete(source: str):
    h.kb.delete(source)


# ---- settings ------------------------------------------------------------------------
class Settings(BaseModel):
    llm: str
    llm_base_url: str = ""


@app.put("/api/settings", dependencies=manage)
def settings(s: Settings):
    h.cfg["llm"] = s.llm.strip()
    if s.llm_base_url.strip():
        h.cfg["llm_base_url"] = s.llm_base_url.strip()
    else:
        h.cfg.pop("llm_base_url", None)
    h.save_config()
    h.reload()
