"""Two end-to-end checks, both against a real stdio MCP server (tests/fake_mcp.py):
  test_scripted  deterministic, scripted model, Chroma's built-in embedder. Needs nothing.
  test_local     real agents on a small Ollama model and embedder. Skipped when Ollama is down.
Override the local models with ANALYST_TEST_LLM / ANALYST_TEST_EMBED.
"""
import json
import os
import pathlib
import sys
import urllib.request

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic_ai import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from analyst import server
from analyst.harness import Harness

HERE = pathlib.Path(__file__).parent
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DB = {"command": sys.executable, "args": [str(HERE / "fake_mcp.py")]}


class Scripted(FunctionModel):
    """Plays back `replies` one model call at a time and records what each call was sent."""

    def __init__(self):
        self.replies, self.seen = [], []
        super().__init__(self.reply, model_name="scripted")

    def reply(self, messages, info):
        self.seen.append(f"{info.instructions}\n{messages}")
        return self.replies.pop(0)


def act(tool, *before, **args):
    return ModelResponse(parts=[*before, ToolCallPart(tool, args)])


def final(text):
    return ModelResponse(parts=[TextPart(text)])


def test_scripted(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp": {"db": DB}}))
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "helper.md").write_text("---\nrole: Helper\ngoal: Help\n---\nYou help.")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "p.py").write_text(
        "def register(h):\n    h.kb.loaders['.note'] = lambda p: 'NOTE ' + p.read_text()\n")
    (tmp_path / "doc.note").write_text("The orders table holds one row per order with region and amount.")

    llm = Scripted()
    h = Harness(tmp_path / "config.yaml", llm=llm)
    try:
        assert h.mcp["db"]["error"] is None and h.plugins == {"p": None}
        assert h.kb.ingest_path(tmp_path / "doc.note") == {str(tmp_path / "doc.note"): 1}

        llm.replies = [
            act("kb_search", ThinkingPart("The dictionary should say what the table holds."),
                TextPart("Looking up the orders table first."), query="orders table"),
            act("db_query", sql="select region, sum(amount) from orders group by region"),
            act("db_query", sql="delete from orders"),
            final("North 150, south 70 [E2]."),
            final("North leads [E2]. Confidence high."),
            final("## Answer\nNorth leads with 150 [E2]. Product code E55 is unrelated. See also [E9]."),
        ]
        events = []
        out = h.research("Which region {sells} most?", session="s1", emit=events.append)

        ev = out["evidence"]
        assert [e["tool"] for e in ev] == ["kb_search", "db_query"]      # the delete was refused
        assert "NOTE The orders table" in ev[0]["output"]                # plugin loader + KB search
        assert "('north', 150.0)" in ev[1]["output"]                     # real MCP round trip
        assert "North leads with 150 [E2]" in out["report"]
        assert "does not exist: E9." in out["report"]                    # bad citation flagged, E55 ignored
        # the activity feed: stages in order, each call paired with a result, the refusal has no id
        assert [e["name"] for e in events if e["kind"] == "stage"] == ["research", "analysis", "report"]
        assert [e["name"] for e in events if e["kind"] == "output"] == ["research", "analysis", "report"]
        assert [e["id"] for e in events if e["kind"] == "result"] == ["E1", "E2", None]
        assert len([e for e in events if e["kind"] == "call"]) == 3
        assert [(e["kind"], e["text"]) for e in events if e["kind"] in ("thought", "say")] == [
            ("thought", "The dictionary should say what the table holds."),
            ("say", "Looking up the orders table first.")]                # model thinking surfaced
        assert h.history("s1")[0]["steps"] == events
        assert h.history("s1")[0]["question"] == "Which region {sells} most?"
        assert "North 150, south 70 [E2]." in llm.seen[4]                 # findings reach the analyst

        # agentic ingestion: explore a source, write catalog notes
        llm.replies = [act("db_query", sql="select * from orders limit 2"),
                       act("kb_save", title="orders", content="orders columns: id, region, amount"),
                       final("Saved orders.")]
        h.catalog("db")
        assert "catalog/db/orders" in [x["source"] for x in h.kb.search("orders columns")]

        # ---- the same harness through the HTTP API ----
        server.h = h
        c = TestClient(server.app)
        assert c.get("/api/meta").json()["mcp"] == [{"name": "db", "tools": 1, "error": None}]
        assert c.get("/api/sessions").json()[0]["title"] == "Which region {sells} most?"

        # library: a rule and a skill reach the prompt; bad input is rejected; plugin errors are reported
        assert c.put("/api/library/rules/periods", json={"content": "Always name the period."}).status_code == 200
        assert c.put("/api/library/skills/cohorts", json={
            "content": "---\ndescription: cohort analysis\n---\nGroup by signup month."}).status_code == 200
        assert c.put("/api/library/plugins/broken", json={"content": "def register(h:\n"}).status_code == 400
        assert c.put("/api/library/plugins/boom", json={
            "content": "def register(h):\n    raise RuntimeError('nope')\n"}).json()["error"] == "RuntimeError: nope"
        assert c.put("/api/library/rules/..%2Fevil", json={"content": "x"}).status_code in (400, 404)
        assert [i["name"] for i in c.get("/api/library/skills").json()] == ["cohorts"]
        assert h.rules[0]["prompt"] == "Always name the period." and "cohorts" in h.skills
        assert c.delete("/api/library/plugins/boom").status_code == 200 and "boom" not in h.plugins

        llm.seen = []
        llm.replies = [act("use_skill", name="cohorts"), final("nothing found"), final("still nothing"),
                       final("no answer"), final("No data.")]
        body = c.post("/api/chat", json={"question": "anything?", "session": "s2"}).text
        last = json.loads(body.strip().split("\n\n")[-1][6:])
        assert last["type"] == "done" and last["report"].startswith("> **No evidence")
        assert "Always name the period." in llm.seen[0] and "cohorts: cohort analysis" in llm.seen[0]
        assert "Group by signup month." in llm.seen[1]                    # use_skill returned the body

        # assistants: saved from the form, its reference document is searched first, its type sets the format
        assert c.put("/api/assistants/finance", json={
            "domain": "Finance", "type": "brief", "prompt": "Revenue is net.", "mcp": ["db"],
            "docs": [str(tmp_path / "doc.note")]}).status_code == 200
        fin = next(a for a in c.get("/api/assistants").json() if a["name"] == "finance")
        assert fin["type"] == "brief" and fin["mcp"] == ["db"] and fin["prompt"] == "Revenue is net."
        assert h.kb.search("anything at all", prefer=fin["docs"])[0]["priority"] is True
        llm.seen = []
        llm.replies = [final("x"), final("x again"), final("y"), final("z")]
        h.research("Revenue?", session="s4", assistant="finance")
        assert "Domain: Finance" in llm.seen[0] and "Revenue is net." in llm.seen[0]
        assert "You answered without calling any tool" in llm.seen[1]     # sent back once, then accepted
        assert "at most five sentences" in llm.seen[3]

        # human in the loop: the agent's question goes to `ask`, the reply comes back to it
        asked, llm.seen = [], []
        llm.replies = [act("ask_user", question="Which period?"), final("x"), final("x again"), final("y"), final("z")]
        h.research("How are sales?", session="s3", ask=lambda q: (asked.append(q), "last quarter")[1])
        assert asked == ["Which period?"] and "The user replied: last quarter" in llm.seen[1]

        # a report that cites none of the gathered evidence is flagged
        llm.replies = [act("kb_search", query="orders"), final("found it"), final("fine"), final("North sold 999.")]
        assert h.research("Sales?", session="s6")["report"].startswith("> **The report cites none")

        # sub-agent: the researcher hands a task to the helper, whose tool calls join the ledger
        llm.seen, events = [], []
        llm.replies = [act("ask_helper", task="Count the orders."),
                       act("db_query", sql="select count(*) from orders"),   # the helper's call
                       final("There are 3 orders [E1]."),                    # the helper's answer
                       final("3 orders [E1]."), final("Three [E1]."), final("Three orders [E1].")]
        out = h.research("How many orders?", session="s5", emit=events.append)
        assert [e["tool"] for e in out["evidence"]] == ["db_query"] and "(3,)" in out["evidence"][0]["output"]
        assert "You are the Helper. Help" in llm.seen[1] and "Count the orders." in llm.seen[1]
        assert "There are 3 orders [E1]." in llm.seen[3]                  # result returned to researcher
        assert ("stage", "Helper") in [(e["kind"], e.get("agent")) for e in events]

        # sources: add a second server from the API, persisted without touching the first
        r = c.put("/api/mcp/db2", json={**DB, "tools": ["query"]}).json()
        assert r == {"error": None, "tools": 1}
        assert set(yaml.safe_load((tmp_path / "config.yaml").read_text())["mcp"]) == {"db", "db2"}
        assert c.put("/api/mcp/dead", json={"command": "no-such-binary-xyz"}).json()["error"]
        assert c.delete("/api/mcp/db2").status_code == 200 and "db2" not in h.mcp

        # documents
        up = c.post("/api/documents/upload", files={"files": ("glossary.txt", b"Churn means cancelled.")}).json()
        assert up == {"glossary.txt": 1}
        assert {"source": "glossary.txt", "kind": "document", "chunks": 1} in c.get("/api/documents").json()
        c.delete("/api/documents", params={"source": "glossary.txt"})
        assert "glossary.txt" not in [d["source"] for d in c.get("/api/documents").json()]

        # management is refused for callers that are not local and have no token
        assert TestClient(server.app, client=("10.0.0.9", 5000)).get("/api/mcp").status_code == 403
    finally:
        h.close()


def ollama_up():
    try:
        urllib.request.urlopen(OLLAMA + "/api/version", timeout=2)
        return True
    except OSError:
        return False


@pytest.mark.skipif(not ollama_up(), reason="Ollama is not running")
def test_local(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "llm": "ollama:" + os.environ.get("ANALYST_TEST_LLM", "qwen3:4b"),
        "llm_base_url": OLLAMA + "/v1",
        "llm_settings": {"extra_body": {"think": False}},
        "embed_model": os.environ.get("ANALYST_TEST_EMBED", "embeddinggemma:300m"),
        "embed_url": OLLAMA,
        "mcp": {"db": DB}}))
    h = Harness(tmp_path / "config.yaml")
    try:
        h.kb.add("dictionary", "The orders table has columns id, region and amount. "
                               "Total sales per region is sum(amount) grouped by region.")
        h.kb.add("menu", "The canteen serves soup on Mondays.")
        assert h.kb.search("how are sales totals computed", k=1)[0]["source"] == "dictionary"

        out = h.research("What is the total order amount for the north region? "
                         "Use the db_query tool with SQL on the orders table.")
        print(out["report"])
        tools = [e["tool"] for e in out["evidence"]]
        assert "db_query" in tools, tools                                # the model really queried
        assert "150" in out["report"]                                    # 100 + 50
        assert any(s["kind"] == "thought" for s in out["steps"])         # qwen3 reasoning surfaced
    finally:
        h.close()
