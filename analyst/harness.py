"""Research-analyst harness on CrewAI.

research(): researcher gathers evidence (knowledge base + MCP sources) -> analyst -> reporter.
catalog():  an agent explores one MCP source and writes notes about it into the knowledge base.
Extension points, all loaded from the directory holding config.yaml:
  mcp: section of config.yaml   data sources
  assistants/*.md               domain prompts (frontmatter: description, mcp: [servers])
  agents/*.md                   sub-agents the researcher can delegate to (role, goal, mcp, llm)
  rules/*.md                    always-on instructions for every agent
  skills/*.md                   instructions the researcher loads on demand (frontmatter: description)
  plugins/*.py                  register(harness): add tools, file loaders, MCP servers, agents
"""
import importlib.util
import json
import os
import pathlib
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from typing import Any

import httpx
import yaml
from crewai import LLM, Agent, Crew, Process, Task
from crewai.tools import BaseTool, tool

from .kb import KB, _pdf

# ponytail: keyword check on string arguments, easy to defeat (comments, CTEs, non-SQL
# stores). The real control is read-only credentials and the per-server `tools:` allowlist.
WRITE_SQL = re.compile(
    r"(^|;)\s*(insert|update|delete|drop|alter|truncate|create|grant|revoke|merge|replace)\b", re.I)


KINDS = {"assistants": ".md", "agents": ".md", "rules": ".md", "skills": ".md", "plugins": ".py"}


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


def load_md(path):
    text = path.read_text(encoding="utf-8")
    try:
        return parse_md(path.stem, text)
    except Exception:  # hand-edited file with broken frontmatter: keep it usable
        return {"name": path.stem, "prompt": text}


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

    def call(self, inner, name, args, guard):
        with self.lock:
            self.calls += 1
            n = self.calls
        self.step("call", n=n, tool=name, input=args)
        if guard and any(isinstance(v, str) and WRITE_SQL.search(v) for v in args.values()):
            out = "Refused: this harness is read-only. Run a query that does not modify data."
            self.step("result", n=n, id=None, output=out)
            return out
        try:
            out = str(inner.run(**args))
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


class Evidenced(BaseTool):
    """Wraps any CrewAI tool so its calls land in the run's ledger."""
    inner: Any
    ledger: Any
    guard: bool = True

    def _run(self, **kwargs):
        return self.ledger.call(self.inner, self.name, kwargs, self.guard)


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
        self.mcp = {}  # name -> {cfg, adapter, tools, error}
        self.reload_lock = threading.Lock()
        self.reload()

    def reload(self):
        """Re-read config and every extension directory; reconnect MCP servers that changed."""
        with self.reload_lock:
            c = expand(self.cfg)
            self.llm = self.llm_override or c.get("llm", "anthropic/claude-opus-5-5")
            if isinstance(self.llm, str) and c.get("llm_base_url"):
                # OpenAI-compatible endpoint, e.g. a local Ollama server
                self.llm = LLM(model=self.llm, base_url=c["llm_base_url"],
                               api_key=c.get("llm_api_key", "none"))
            for kind in KINDS:
                (self.root / kind).mkdir(exist_ok=True)
            md = lambda kind: [load_md(p) for p in sorted(self.root.glob(f"{kind}/*.md"))]
            self.assistants = {a["name"]: a for a in md("assistants")}
            self.subagents = md("agents")
            self.rules = md("rules")
            self.skills = {s["name"]: s for s in md("skills")}
            self.tools = []  # plugin tools, handed to the researcher
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
                    self._connect(name, server)

    def save_config(self):
        self.config_path.write_text(yaml.safe_dump(self.cfg, sort_keys=False), encoding="utf-8")

    def path_for(self, kind, name):
        if kind not in KINDS or not re.fullmatch(r"[\w-]+", name):
            raise ValueError("names may use letters, digits, - and _ only")
        return self.root / kind / (name + KINDS[kind])

    # ---- MCP -------------------------------------------------------------------------
    def _connect(self, name, cfg):
        from crewai_tools import MCPServerAdapter
        from mcp import StdioServerParameters
        s = expand(cfg)
        try:
            if s.get("command"):
                # "python" means the interpreter running the harness, so venvs work on any OS
                cmd = sys.executable if s["command"] in ("python", "python3") else s["command"]
                params = StdioServerParameters(command=cmd, args=s.get("args") or [],
                                               env={**os.environ, **(s.get("env") or {})})
            else:
                params = {k: s[k] for k in ("url", "transport", "headers") if s.get(k)}
            adapter = MCPServerAdapter(params, *(s.get("tools") or []),
                                       connect_timeout=s.get("timeout", 30))
            self.mcp[name] = {"cfg": cfg, "adapter": adapter, "tools": list(adapter.tools), "error": None}
        except Exception as e:  # one dead source must not take the harness down
            self.mcp[name] = {"cfg": cfg, "adapter": None, "tools": [], "error": str(e) or type(e).__name__}

    def disconnect(self, name):
        s = self.mcp.pop(name, None)
        if s and s["adapter"]:
            s["adapter"].stop()

    def close(self):
        for name in list(self.mcp):
            self.disconnect(name)

    @contextmanager
    def _llm(self, run):
        """The model for one run. On an OpenAI-compatible endpoint (llm_base_url) the raw
        responses are read as well, to show the model's reasoning text when it sends any."""
        c = expand(self.cfg)
        if self.llm_override or not c.get("llm_base_url"):
            yield self.llm
            return

        def read(response):
            try:
                response.read()
                msg = response.json()["choices"][0]["message"]
            except Exception:
                return
            thought = msg.get("reasoning") or msg.get("reasoning_content")
            if thought and thought.strip():
                run.step("thought", text=thought.strip())
            if msg.get("tool_calls") and (msg.get("content") or "").strip():
                run.step("say", text=msg["content"].strip())

        from openai import OpenAI
        with httpx.Client(event_hooks={"response": [read]}, timeout=1800) as client:
            llm = LLM(model=c.get("llm"), base_url=c["llm_base_url"], api_key=c.get("llm_api_key", "none"))
            # ponytail: CrewAI has no public way to give only the sync client an http_client
            # (client_params goes to the async client too, which rejects it), so swap it in.
            llm._client = OpenAI(base_url=c["llm_base_url"], api_key=c.get("llm_api_key", "none"),
                                 http_client=client)
            yield llm

    # ---- tools -----------------------------------------------------------------------
    def _wrap(self, run, t, name=None, guard=True):
        return Evidenced(name=name or t.name, description=t.description.split("Tool Description:")[-1].strip(),
                         args_schema=t.args_schema, inner=t, ledger=run, guard=guard)

    def _tools(self, run, servers=None, with_plugins=True, prefer=None):
        """kb_search + MCP tools (all servers, or only those named) + plugin tools.
        `prefer` lists knowledge-base sources to search first."""
        kb = self.kb

        @tool("kb_search")
        def kb_search(query: str) -> str:
            """Search ingested documents, data dictionaries and source catalogs. Use it first to
            learn which tables, collections, reports or pages matter and how fields are defined."""
            hits = kb.search(query, prefer=prefer)
            return "\n\n".join(f"source: {h['source']}{' (priority reference)' if h.get('priority') else ''}"
                               f"\n{h['text']}" for h in hits) or "No matches."

        out = [self._wrap(run, kb_search)]
        for name, s in self.mcp.items():
            if servers is None or name in servers:
                guard = not s["cfg"].get("allow_writes", False)
                out += [self._wrap(run, t, f"{name}_{t.name}", guard) for t in s["tools"]]
        if with_plugins:
            out += [self._wrap(run, t) for t in self.tools]
        return out

    # ---- research --------------------------------------------------------------------
    def research(self, question, session="default", assistant=None, emit=lambda e: None, ask=None):
        """`ask(question) -> answer` lets the researcher put a clarifying question to the user."""
        run = Run(emit, self.cfg.get("max_tool_output", 20000))
        with self._llm(run) as llm:
            return self._research(run, llm, question, session, assistant, ask)

    def _research(self, run, llm, question, session, assistant, ask):
        a = self.assistants.get(assistant or "default", {})
        domain = f"\n\nDomain: {a['domain']}" if a.get("domain") else ""
        if a.get("prompt"):
            domain += f"\n\nDomain guidance:\n{a['prompt']}"
        if self.rules:
            domain += "\n\nRules you must follow:\n" + "\n\n".join(r["prompt"] for r in self.rules)
        tools, skills_note = self._tools(run, a.get("mcp") or None, prefer=a.get("docs") or None), ""
        if self.skills:
            skills = self.skills

            @tool("use_skill")
            def use_skill(name: str) -> str:
                """Load the full instructions of a skill by name before doing the work it covers."""
                run.step("skill", name=name)
                return skills[name]["prompt"] if name in skills else f"No skill named {name}."

            tools.append(use_skill)
            skills_note = "\n\nSkills (load one with use_skill when the question calls for it):\n" + \
                "\n".join(f"- {n}: {s.get('description', '')}" for n, s in skills.items())
        past = "".join(f"\n\nEarlier question: {t['question']}\nEarlier answer: {t['report'][:1500]}"
                       for t in self.history(session)[-3:])
        if ask:
            @tool("ask_user")
            def ask_user(question: str) -> str:
                """Ask the user one short clarifying question and wait for the reply. Use it only
                when the request is ambiguous in a way that changes the answer (which period,
                which metric, which entity) and the knowledge base does not settle it."""
                run.step("ask", text=question)
                answer = ask(question)
                run.step("reply", text=answer or "(no reply)")
                return f"The user replied: {answer}" if answer else \
                    "No reply. Proceed with the most reasonable reading and state the assumption."

            tools.append(ask_user)
        subs = [Agent(role=s.get("role", s["name"]), goal=s.get("goal", s.get("description", s["name"])),
                      backstory=s["prompt"], tools=self._tools(run, s.get("mcp"), with_plugins=False),
                      llm=s.get("llm") or llm)
                for s in self.subagents]
        researcher = Agent(
            role="Evidence Researcher",
            goal="Collect the evidence needed to answer the question from the connected sources.",
            backstory="You are a careful data analyst. You never answer from memory: you look in the "
                      "knowledge base for what the data means, then query the sources themselves. "
                      "Access is read-only." + domain + skills_note,
            tools=tools, allow_delegation=bool(subs), llm=llm)
        analyst = Agent(
            role="Data Analyst",
            goal="Work out what the collected evidence does and does not show.",
            backstory="You reason only from the evidence given to you and say how sure you are." + domain,
            llm=llm)
        reporter = Agent(
            role="Report Writer",
            goal="Write a short, accurate report a business reader can act on.",
            backstory="You write plainly and cite the evidence id behind every claim." + domain,
            llm=llm)

        gather = Task(
            name="research", agent=researcher,
            description="Question: " + question + past + "\n\n"
                        "1. Search the knowledge base for the relevant tables, collections, reports "
                        "and definitions.\n2. Query the sources to get the actual figures or facts. "
                        "Check row counts, date ranges and filters so the numbers mean what you think.\n"
                        "3. If sources disagree, gather enough to show the disagreement.\n"
                        "Every tool result starts with an id like [E3].",
            expected_output="A list of findings. Each one states what was found, the exact query or "
                            "lookup used, and its evidence id. Then a list of what could not be found.")
        analyse = Task(
            name="analysis", agent=analyst, context=[gather],
            description="Question: " + question + "\n\nAnalyse the findings: calculate, compare, look "
                        "for contradictions and alternative explanations. Keep the evidence ids.",
            expected_output="The answer the evidence supports, the reasoning with evidence ids, a "
                            "confidence level (high, medium, low) with the reason, and open gaps.")
        report = Task(
            name="report", agent=reporter, context=[gather, analyse],
            description="Question: " + question + "\n\n" + FORMATS.get(a.get("type"), FORMATS["report"]) +
                        " Cite only ids that appear in the findings. If the evidence does not "
                        "answer the question, say so.",
            expected_output="Markdown.")

        stages = [("research", researcher.role), ("analysis", analyst.role), ("report", reporter.role)]

        def task_done(o):
            run.step("output", name=o.name, text=o.raw)
            nxt = [s[0] for s in stages].index(o.name) + 1
            if nxt < len(stages):
                run.step("stage", name=stages[nxt][0], agent=stages[nxt][1])

        crew = Crew(agents=[researcher, analyst, reporter, *subs], tasks=[gather, analyse, report],
                    process=Process.sequential, task_callback=task_done)
        run.step("stage", name=stages[0][0], agent=stages[0][1])
        text = crew.kickoff().raw

        ids = {e["id"] for e in run.evidence}
        cited = re.findall(r"E\d+", " ".join(re.findall(r"\[[^\]]*\]", text)))
        unknown = sorted(set(cited) - ids)
        if not ids:
            text = "> **No evidence was gathered. Treat this answer as unverified.**\n\n" + text
        elif unknown:
            text += f"\n\n> **Warning:** cites evidence that does not exist: {', '.join(unknown)}."
        with self.db_lock:
            self.db.execute(
                "insert into turns (session, question, report, evidence, steps) values (?,?,?,?,?)",
                (session, question, text, json.dumps(run.evidence, default=str),
                 json.dumps(run.steps, default=str)))
            self.db.commit()
        return {"report": text, "evidence": run.evidence, "steps": run.steps}

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
        with self._llm(run) as llm:
            return self._catalog(run, llm, server, hint)

    def _catalog(self, run, llm, server, hint):
        kb = self.kb

        @tool("kb_save")
        def kb_save(title: str, content: str) -> str:
            """Save one note to the knowledge base. Use one note per table, collection, dataset,
            report or page, titled with its exact name."""
            return f"saved '{title}' ({kb.add(f'catalog/{server}/{title}', content, 'catalog')} chunks)"

        tools = [t for t in self._tools(run, [server], with_plugins=False) if t.name != "kb_search"]
        cataloguer = Agent(
            role="Data Cataloguer",
            goal=f"Document the source '{server}' so analysts can find and query it later.",
            backstory="You explore data sources methodically and write precise notes. Read-only.",
            tools=[*tools, self._wrap(run, kb_save, guard=False)], llm=llm)
        task = Task(
            agent=cataloguer,
            description=f"Explore the source '{server}'. {hint}\nList what it contains (schemas, "
                        "tables, collections, datasets, reports, spaces or pages). For each object "
                        "save a note with kb_save: what it holds, its fields with types and meaning, "
                        "a few sample values, how it joins to other objects, and an example query.",
            expected_output="A list of the notes saved, and anything that could not be read.")
        run.step("stage", name="catalog", agent=cataloguer.role)
        text = Crew(agents=[cataloguer], tasks=[task]).kickoff().raw
        return {"report": text, "evidence": run.evidence, "steps": run.steps}
