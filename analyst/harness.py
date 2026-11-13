"""Research-analyst harness on pydantic-ai.

research(): researcher gathers evidence (knowledge base + MCP sources) -> analyst -> reporter.
catalog():  an agent explores one MCP source and writes notes about it into the knowledge base.
Extension points, all loaded from the directory holding config.yaml:
  mcp: section of config.yaml   data sources
  assistants/*.md               domain prompts (frontmatter: description, domain, type, mcp, docs)
  agents/*.md                   sub-agents the researcher can delegate to (role, goal, mcp, llm)
  rules/*.md                    always-on instructions for every agent
  skills/*.md                   instructions the researcher loads on demand (frontmatter: description)
  plugins/*.py                  register(harness): add tools, file loaders, MCP servers, agents

The harness is called from plain threads (the HTTP server, the CLI). Agents and MCP
connections live on one private asyncio loop; `_sync` hands work to it.
"""
import asyncio
import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass

import yaml

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # keep CLI output to the report
from pydantic_ai import (Agent, FunctionToolset, TextPart, ThinkingPart, Tool, ToolCallPart,
                         UsageLimits, WrapperToolset)

from .kb import KB, _pdf

# ponytail: keyword check on string arguments, easy to defeat (comments, CTEs, non-SQL
# stores). The real control is read-only credentials and the per-server `tools:` allowlist.
WRITE_SQL = re.compile(
    r"(^|;)\s*(insert|update|delete|drop|alter|truncate|create|grant|revoke|merge|replace)\b", re.I)

KINDS = {"assistants": ".md", "agents": ".md", "rules": ".md", "skills": ".md", "plugins": ".py"}

# How the report is laid out, chosen per assistant with `type:`.
FORMATS = {
    "report": "Write the report in markdown with these sections: Answer (two or three sentences), "
              "Findings (bullets, each ending with its evidence ids such as [E2]), Method (sources "
              "and queries used), Caveats (gaps, assumptions, confidence).",
    "brief": "Answer in at most five sentences of markdown, each claim followed by its evidence "
             "ids such as [E2]. End with one line on confidence and caveats.",
    "table": "Lead with a markdown table of the figures. Under it give short notes: what each "
             "column means, the evidence ids such as [E2] behind the rows, and caveats.",
}
LIMITS = UsageLimits(request_limit=40)


def expand(v):
    """Replace ${VAR} from the environment in every string of a config value."""
    if isinstance(v, str):
        return os.path.expandvars(v)
    if isinstance(v, dict):
        return {k: expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [expand(x) for x in v]
    return v


def parse_md(name, text):
    """Markdown with optional YAML frontmatter -> {name, ...frontmatter, prompt}."""
    meta = {}
    if text.startswith("---") and text.count("---") >= 2:
        _, front, text = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
        if not isinstance(meta, dict):
            raise ValueError("frontmatter must be key: value pairs")
    return {"name": name, **meta, "prompt": text.strip()}


def load_md(path):
    text = path.read_text(encoding="utf-8")
    try:
        return parse_md(path.stem, text)
    except Exception:  # hand-edited file with broken frontmatter: keep it usable
        return {"name": path.stem, "prompt": text}


def make_model(name, base_url=None, api_key=None):
    """A pydantic-ai model from config. `provider:model` names go to pydantic-ai as they are
    (e.g. anthropic:claude-opus-5-5); with a base URL the model is served from that endpoint,
    through the Ollama provider for `ollama:` names and as OpenAI-compatible otherwise."""
    if "/" in name.split(":")[0]:  # older `provider/model` spelling
        name = name.replace("/", ":", 1)
    if not base_url:
        return name
    # Local servers give small context windows (Ollama defaults to 4096 tokens). Sending the
    # model's earlier reasoning back with every request overflows that, and the server then
    # drops the start of the conversation: instructions, question and tool list.
    profile = {"openai_chat_send_back_thinking_parts": False}
    provider, _, model = name.partition(":")
    if provider == "ollama":
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider
        return OllamaModel(model, provider=OllamaProvider(base_url=base_url, api_key=api_key), profile=profile)
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    return OpenAIChatModel(model or name, provider=OpenAIProvider(base_url=base_url, api_key=api_key or "none"),
                           profile=profile)


class Run:
    """One question's record: the evidence ledger (every tool result gets an id the report
    must cite) and the step-by-step activity shown to the user."""

    def __init__(self, emit, max_output):
        self.evidence, self.steps, self.calls = [], [], 0
        self.emit, self.max_output = emit, max_output
        self.lock = threading.Lock()

    def step(self, kind, **data):
        s = {"type": "step", "kind": kind, **data}
        with self.lock:
            self.steps.append(s)
        self.emit(s)

    async def call(self, name, args, guard, invoke):
        with self.lock:
            self.calls += 1
            n = self.calls
        self.step("call", n=n, tool=name, input=args)
        if guard and any(isinstance(v, str) and WRITE_SQL.search(v) for v in args.values()):
            out = "Refused: this harness is read-only. Run a query that does not modify data."
            self.step("result", n=n, id=None, output=out)
            return out
        try:
            out = await invoke()
            out = out if isinstance(out, str) else json.dumps(out, default=str)
        except Exception as e:  # the agent should see the failure and try something else
            out = f"Tool error: {e}"
        cut = len(out) > self.max_output
        out = out[:self.max_output]
        with self.lock:
            item = {"id": f"E{len(self.evidence) + 1}", "tool": name, "input": args, "output": out}
            self.evidence.append(item)
        self.step("result", n=n, id=item["id"], output=out)
        note = "\n[output truncated: narrow the query or aggregate]" if cut else ""
        return f"[{item['id']}] cite this id for anything taken from this result\n{out}{note}"


@dataclass
class Evidenced(WrapperToolset):
    """Wraps any toolset so every call lands in the run's ledger."""
    run: Run = None
    guard: bool = True

    async def call_tool(self, name, tool_args, ctx, tool):
        return await self.run.call(name, tool_args, self.guard,
                                   lambda: self.wrapped.call_tool(name, tool_args, ctx, tool))


class Harness:
    def __init__(self, config="config.yaml", llm=None):
        self.config_path = pathlib.Path(config).resolve()
        self.root = self.config_path.parent
        # kept unexpanded so saving it back never writes secrets from the environment
        self.cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        self.llm_override = llm
        self.data = self.root / expand(self.cfg.get("data_dir", "data"))
        self.data.mkdir(exist_ok=True)
        c = expand(self.cfg)
        self.kb = KB(self.data / "kb", c.get("embed_model"), c.get("embed_url", "http://localhost:11434"))
        self.db = sqlite3.connect(self.data / "sessions.db", check_same_thread=False)
        self.db_lock = threading.Lock()
        self.db.execute("create table if not exists turns (session text, question text,"
                        " report text, evidence text, ts default current_timestamp)")
        if "steps" not in [r[1] for r in self.db.execute("pragma table_info(turns)")]:
            self.db.execute("alter table turns add column steps text")
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="analyst-loop", daemon=True).start()
        self.mcp = {}  # name -> {cfg, toolset, tools, error, stop, task}
        self.reload_lock = threading.Lock()
        self.reload()

    def _sync(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def reload(self):
        """Re-read config and every extension directory; reconnect MCP servers that changed."""
        with self.reload_lock:
            c = expand(self.cfg)
            self.llm = self.llm_override or make_model(
                c.get("llm", "anthropic:claude-opus-5-5"), c.get("llm_base_url"), c.get("llm_api_key"))
            # passed to every agent as pydantic-ai model settings: max_tokens, extra_body, ...
            self.llm_settings = c.get("llm_settings") or None
            for kind in KINDS:
                (self.root / kind).mkdir(exist_ok=True)
            md = lambda kind: [load_md(p) for p in sorted(self.root.glob(f"{kind}/*.md"))]
            self.assistants = {a["name"]: a for a in md("assistants")}
            self.subagents = md("agents")
            self.rules = md("rules")
            self.skills = {s["name"]: s for s in md("skills")}
            self.tools = []  # plugin tools (plain functions), handed to the researcher
            self.kb.loaders = {".pdf": _pdf}
            self.mcp_cfg = dict(self.cfg.get("mcp") or {})
            self.plugins = {}  # name -> error text, or None when loaded
            for p in sorted(self.root.glob("plugins/*.py")):
                try:
                    spec = importlib.util.spec_from_file_location(f"analyst_plugin_{p.stem}", p)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    mod.register(self)
                    self.plugins[p.stem] = None
                except Exception as e:  # a broken plugin is reported, not fatal
                    self.plugins[p.stem] = f"{type(e).__name__}: {e}"
            for name in list(self.mcp):
                if self.mcp[name]["cfg"] != self.mcp_cfg.get(name):
                    self.disconnect(name)
            for name, server in self.mcp_cfg.items():
                if name not in self.mcp:  # failed servers are retried only on request
                    self._sync(self._connect(name, server))

    def save_config(self):
        self.config_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False), encoding="utf-8")

    def path_for(self, kind, name):
        if kind not in KINDS or not re.fullmatch(r"[\w-]+", name):
            raise ValueError("names may use letters, digits, - and _ only")
        return self.root / kind / (name + KINDS[kind])

    # ---- MCP -------------------------------------------------------------------------
    async def _connect(self, name, cfg):
        """Open the server in a task that holds the connection until `disconnect`, so the
        connection is entered and left in the same task."""
        from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
        from pydantic_ai.mcp import MCPToolset
        s, entry = expand(cfg), {"cfg": cfg, "toolset": None, "tools": [], "error": None,
                                 "stop": asyncio.Event(), "task": None}
        self.mcp[name] = entry
        try:
            if s.get("command"):
                # "python" means the interpreter running the harness, so venvs work on any OS
                cmd = sys.executable if s["command"] in ("python", "python3") else s["command"]
                transport = StdioTransport(cmd, [str(a) for a in s.get("args") or []],
                                           env={**os.environ, **(s.get("env") or {})}, cwd=str(self.root))
            elif s.get("transport") == "sse":
                transport = SSETransport(s["url"], headers=s.get("headers") or None)
            else:
                transport = StreamableHttpTransport(s["url"], headers=s.get("headers") or None)
            toolset = MCPToolset(transport, id=name, tool_error_behavior="error")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            return
        ready = asyncio.get_running_loop().create_future()

        async def hold():
            try:
                async with toolset:
                    ready.set_result(await toolset.list_tools())
                    await entry["stop"].wait()
            except BaseException as e:
                if not ready.done():
                    ready.set_exception(e if isinstance(e, Exception) else RuntimeError(repr(e)))

        entry["task"] = asyncio.create_task(hold())
        try:
            tools = await asyncio.wait_for(asyncio.shield(ready), s.get("timeout", 30))
            allow = set(s.get("tools") or [])
            entry["tools"] = [t for t in tools if not allow or t.name in allow]
            entry["toolset"] = toolset
        except Exception as e:  # one dead source must not take the harness down
            entry["error"] = str(e) or type(e).__name__
            entry["stop"].set()
            entry["task"].cancel()

    async def _disconnect(self, entry):
        entry["stop"].set()
        if entry["task"]:
            try:
                await asyncio.wait_for(entry["task"], 10)
            except Exception:
                pass

    def disconnect(self, name):
        entry = self.mcp.pop(name, None)
        if entry:
            self._sync(self._disconnect(entry))

    def close(self):
        for name in list(self.mcp):
            self.disconnect(name)
        self.loop.call_soon_threadsafe(self.loop.stop)

    # ---- tools -----------------------------------------------------------------------
    def _toolsets(self, run, servers=None, with_plugins=True, prefer=None, kb_search=True):
        """Evidence-recording toolsets: kb_search, MCP sources (all, or those named), plugins.
        `prefer` lists knowledge-base sources to search first."""
        kb, funcs = self.kb, []
        if kb_search:
            def kb_search(query: str) -> str:
                """Search ingested documents, data dictionaries and source catalogs. Use it first to
                learn which tables, collections, reports or pages matter and how fields are defined."""
                hits = kb.search(query, prefer=prefer)
                return "\n\n".join(f"source: {h['source']}{' (priority reference)' if h.get('priority') else ''}"
                                   f"\n{h['text']}" for h in hits) or "No matches."
            funcs.append(kb_search)
        if with_plugins:
            funcs += self.tools
        out = [Evidenced(FunctionToolset(funcs), run=run, guard=False)] if funcs else []
        for name, s in self.mcp.items():
            if s["toolset"] and (servers is None or name in servers):
                allow = {t.name for t in s["tools"]}
                ts = s["toolset"].filtered(lambda ctx, td, allow=allow: td.name in allow).prefixed(name)
                out.append(Evidenced(ts, run=run, guard=not s["cfg"].get("allow_writes", False)))
        return out

    async def _drive(self, agent, prompt, run, toolsets=(), history=None, result=False):
        """Run one agent to its answer, reporting its thinking and commentary as steps.
        Returns the answer text, or the whole run result when `result` is set."""
        async with agent.iter(prompt, deps=run, usage_limits=LIMITS, toolsets=list(toolsets),
                              model_settings=self.llm_settings, message_history=history) as ar:
            async for node in ar:
                if Agent.is_call_tools_node(node):
                    parts = node.model_response.parts
                    calls = any(isinstance(p, ToolCallPart) for p in parts)
                    for p in parts:
                        if isinstance(p, ThinkingPart) and (p.content or "").strip():
                            run.step("thought", text=p.content.strip())
                        elif isinstance(p, TextPart) and calls and p.content.strip():
                            run.step("say", text=p.content.strip())
        return ar.result if result else ar.result.output

    def _model_for(self, name):
        if not name or self.llm_override:
            return self.llm
        c = expand(self.cfg)
        return make_model(name, c.get("llm_base_url"), c.get("llm_api_key"))

    # ---- research --------------------------------------------------------------------
    def research(self, question, session="default", assistant=None, emit=lambda e: None, ask=None):
        """`ask(question) -> answer` lets the researcher put a clarifying question to the user."""
        run = Run(emit, self.cfg.get("max_tool_output", 20000))
        past = self.history(session)[-3:]
        text = self._sync(self._research(run, question, past, assistant, ask))

        ids = {e["id"] for e in run.evidence}
        cited = re.findall(r"E\d+", " ".join(re.findall(r"\[[^\]]*\]", text)))
        unknown = sorted(set(cited) - ids)
        if not ids:
            text = "> **No evidence was gathered. Treat this answer as unverified.**\n\n" + text
        elif not set(cited) & ids:
            text = "> **The report cites none of the evidence gathered. Treat its figures as unverified.**\n\n" + text
        elif unknown:
            text += f"\n\n> **Warning:** cites evidence that does not exist: {', '.join(unknown)}."
        with self.db_lock:
            self.db.execute(
                "insert into turns (session, question, report, evidence, steps) values (?,?,?,?,?)",
                (session, question, text, json.dumps(run.evidence, default=str),
                 json.dumps(run.steps, default=str)))
            self.db.commit()
        return {"report": text, "evidence": run.evidence, "steps": run.steps}

    async def _research(self, run, question, past, assistant, ask):
        a = self.assistants.get(assistant or "default", {})
        domain = f"\n\nDomain: {a['domain']}" if a.get("domain") else ""
        if a.get("prompt"):
            domain += f"\n\nDomain guidance:\n{a['prompt']}"
        if self.rules:
            domain += "\n\nRules you must follow:\n" + "\n\n".join(r["prompt"] for r in self.rules)

        extra, notes = [], ""  # tools outside the evidence ledger
        if self.skills:
            skills = self.skills

            def use_skill(name: str) -> str:
                """Load the full instructions of a skill by name before doing the work it covers."""
                run.step("skill", name=name)
                return skills[name]["prompt"] if name in skills else f"No skill named {name}."

            extra.append(use_skill)
            notes += "\n\nSkills (load one with use_skill when the question calls for it):\n" + \
                "\n".join(f"- {n}: {s.get('description', '')}" for n, s in skills.items())
        if ask:
            def ask_user(question: str) -> str:
                """Ask the user one short clarifying question and wait for the reply. Use it only
                when the request is ambiguous in a way that changes the answer (which period,
                which metric, which entity) and the knowledge base does not settle it."""
                run.step("ask", text=question)
                answer = ask(question)
                run.step("reply", text=answer or "(no reply)")
                return f"The user replied: {answer}" if answer else \
                    "No reply. Proceed with the most reasonable reading and state the assumption."

            extra.append(ask_user)
        for s in self.subagents:
            role = s.get("role", s["name"])
            sub = Agent(self._model_for(s.get("llm")), retries=3, name=s["name"],
                        instructions=f"You are the {role}. {s.get('goal', '')}\n\n{s['prompt']}"
                                     "\nEvery tool result starts with an evidence id like [E3]; "
                                     "quote the ids behind what you report.")

            def delegator(sub, servers, role):
                async def delegate(task: str) -> str:
                    run.step("stage", name="delegated", agent=role)
                    return await self._drive(sub, task, run, self._toolsets(run, servers, with_plugins=False))
                return delegate

            extra.append(Tool(delegator(sub, s.get("mcp"), role), name=f"ask_{s['name']}",
                              description=f"Hand a self-contained task to the {role} "
                                          f"({s.get('goal', '')}). Returns their findings."))
            notes += f"\n\nYou can hand work to the {role} with ask_{s['name']}."

        researcher = Agent(
            self.llm, retries=3, name="researcher",
            instructions="You are an Evidence Researcher and a careful data analyst. You never answer "
                         "from memory: you look in the knowledge base for what the data means, then "
                         "query the sources themselves. Access is read-only." + domain + notes)
        analyst = Agent(self.llm, retries=3, name="analyst",
                        instructions="You are a Data Analyst. You reason only from the evidence given "
                                     "to you and say how sure you are." + domain)
        reporter = Agent(self.llm, retries=3, name="reporter",
                         instructions="You are a Report Writer. You write plainly and cite the evidence "
                                      "id behind every claim." + domain)

        earlier = "".join(f"\n\nEarlier question: {t['question']}\nEarlier answer: {t['report'][:1500]}"
                          for t in past)
        toolsets = self._toolsets(run, a.get("mcp") or None, prefer=a.get("docs") or None)
        if extra:
            toolsets.append(FunctionToolset(extra))

        run.step("stage", name="research", agent="Evidence Researcher")
        findings = await self._drive(researcher, (
            f"Question: {question}{earlier}\n\n"
            "1. Search the knowledge base for the relevant tables, collections, reports and definitions.\n"
            "2. Query the sources to get the actual figures or facts. Check row counts, date ranges and "
            "filters so the numbers mean what you think.\n"
            "3. If sources disagree, gather enough to show the disagreement.\n"
            "Every tool result starts with an id like [E3].\n\n"
            "Answer with a list of findings. Each one states what was found, the exact query or lookup "
            "used, and its evidence id. Then list what could not be found."), run, toolsets, result=True)
        if not run.evidence:  # answered from memory: send it back once to gather evidence
            run.step("say", text="No tool was used, so the researcher is asked to gather evidence first.")
            findings = await self._drive(researcher, (
                "You answered without calling any tool, so nothing you wrote is backed by evidence. "
                "Call kb_search and the data source tools now, then give your findings again with "
                "their evidence ids."), run, toolsets, history=findings.all_messages(), result=True)
        findings = findings.output
        run.step("output", name="research", text=findings)

        run.step("stage", name="analysis", agent="Data Analyst")
        analysis = await self._drive(analyst, (
            f"Question: {question}\n\nFindings:\n{findings}\n\n"
            "Analyse the findings: calculate, compare, look for contradictions and alternative "
            "explanations. Keep the evidence ids. Give the answer the evidence supports, the reasoning "
            "with evidence ids, a confidence level (high, medium, low) with the reason, and open gaps."), run)
        run.step("output", name="analysis", text=analysis)

        run.step("stage", name="report", agent="Report Writer")
        report = await self._drive(reporter, (
            f"Question: {question}\n\nFindings:\n{findings}\n\nAnalysis:\n{analysis}\n\n"
            + FORMATS.get(a.get("type"), FORMATS["report"]) +
            " Cite only ids that appear in the findings. If the evidence does not answer the "
            "question, say so."), run)
        run.step("output", name="report", text=report)
        return report

    def history(self, session):
        with self.db_lock:
            rows = self.db.execute("select question, report, evidence, ts, steps from turns"
                                   " where session=? order by rowid", (session,)).fetchall()
        return [{"question": q, "report": r, "evidence": json.loads(e), "ts": ts,
                 "steps": json.loads(st or "[]")} for q, r, e, ts, st in rows]

    def sessions(self):
        with self.db_lock:
            rows = self.db.execute(
                "select session, (select question from turns f where f.session = t.session"
                " order by rowid limit 1), max(ts) from turns t group by session"
                " order by max(rowid) desc").fetchall()
        return [{"id": s, "title": q, "ts": ts} for s, q, ts in rows]

    def forget(self, session):
        with self.db_lock:
            self.db.execute("delete from turns where session=?", (session,))
            self.db.commit()

    # ---- ingestion -------------------------------------------------------------------
    def catalog(self, server, hint="", emit=lambda e: None):
        """Have an agent explore one MCP source and save notes about it to the knowledge base."""
        if not self.mcp.get(server, {}).get("tools"):
            raise ValueError(f"MCP server '{server}' is not connected")
        run = Run(emit, self.cfg.get("max_tool_output", 20000))
        text = self._sync(self._catalog(run, server, hint))
        return {"report": text, "evidence": run.evidence, "steps": run.steps}

    async def _catalog(self, run, server, hint):
        kb = self.kb

        def kb_save(title: str, content: str) -> str:
            """Save one note to the knowledge base. Use one note per table, collection, dataset,
            report or page, titled with its exact name."""
            return f"saved '{title}' ({kb.add(f'catalog/{server}/{title}', content, 'catalog')} chunks)"

        toolsets = self._toolsets(run, [server], with_plugins=False, kb_search=False)
        toolsets.append(Evidenced(FunctionToolset([kb_save]), run=run, guard=False))
        cataloguer = Agent(self.llm, retries=3, name="cataloguer",
                           instructions=f"You are a Data Cataloguer documenting the source '{server}' so "
                                        "analysts can find and query it later. You explore data sources "
                                        "methodically and write precise notes. Read-only.")
        run.step("stage", name="catalog", agent="Data Cataloguer")
        return await self._drive(cataloguer, (
            f"Explore the source '{server}'. {hint}\nList what it contains (schemas, tables, "
            "collections, datasets, reports, spaces or pages). For each object save a note with "
            "kb_save: what it holds, its fields with types and meaning, a few sample values, how it "
            "joins to other objects, and an example query. Finish with a list of the notes saved and "
            "anything that could not be read."), run, toolsets)
