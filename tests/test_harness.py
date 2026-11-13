"""Two end-to-end checks, both against a real stdio MCP server (tests/fake_mcp.py):
  test_scripted  deterministic, scripted model, Chroma's built-in embedder. Needs nothing.
  test_local     real crew on a small Ollama model and embedder. Skipped when Ollama is down.
Override the local models with ANALYST_TEST_LLM / ANALYST_TEST_EMBED.
"""
import json
import os
import pathlib
import sys
import urllib.request

import pytest
import yaml
from crewai import BaseLLM
from fastapi.testclient import TestClient

from analyst import server
from analyst.harness import Harness

HERE = pathlib.Path(__file__).parent
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DB = {"command": sys.executable, "args": [str(HERE / "fake_mcp.py")]}


class Scripted(BaseLLM):
    replies: list = []

    def call(self, messages, *args, **kwargs):
        return self.replies.pop(0)

    def supports_function_calling(self):
        return False


def act(tool, **args):
    return f"Thought: I need data.\nAction: {tool}\nAction Input: {json.dumps(args)}"


def final(text):
    return f"Thought: I now know the final answer\nFinal Answer: {text}"


def test_scripted(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp": {"db": DB}}))
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "helper.md").write_text("---\nrole: Helper\ngoal: Help\n---\nYou help.")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "p.py").write_text(
        "def register(h):\n    h.kb.loaders['.note'] = lambda p: 'NOTE ' + p.read_text()\n")
    (tmp_path / "doc.note").write_text("The orders table holds one row per order with region and amount.")

    llm = Scripted(model="scripted")
    h = Harness(tmp_path / "config.yaml", llm=llm)
    try:
        assert h.mcp["db"]["error"] is None and h.plugins == {"p": None}
        assert h.kb.ingest_path(tmp_path / "doc.note") == {str(tmp_path / "doc.note"): 1}

        llm.replies = [
            act("kb_search", query="orders table"),
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
        assert h.history("s1")[0]["steps"] == events
        assert h.history("s1")[0]["question"] == "Which region {sells} most?"

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

        seen = []
        llm.replies = [act("use_skill", name="cohorts"), final("nothing found"), final("no answer"), final("No data.")]
        real_call = Scripted.call
        Scripted.call = lambda self, messages, *a, **k: (seen.append(str(messages)), real_call(self, messages))[1]
        try:
            body = c.post("/api/chat", json={"question": "anything?", "session": "s2"}).text
        finally:
            Scripted.call = real_call
        last = json.loads(body.strip().split("\n\n")[-1][6:])
        assert last["type"] == "done" and last["report"].startswith("> **No evidence")
        assert "Always name the period." in seen[0] and "cohorts: cohort analysis" in seen[0]
        assert "Group by signup month." in seen[1]                       # use_skill returned the body

        # assistants: saved from the form, its reference document is searched first, its type sets the format
        assert c.put("/api/assistants/finance", json={
            "domain": "Finance", "type": "brief", "prompt": "Revenue is net.", "mcp": ["db"],
            "docs": [str(tmp_path / "doc.note")]}).status_code == 200
        fin = next(a for a in c.get("/api/assistants").json() if a["name"] == "finance")
        assert fin["type"] == "brief" and fin["mcp"] == ["db"] and fin["prompt"] == "Revenue is net."
        assert h.kb.search("anything at all", prefer=fin["docs"])[0]["priority"] is True
        seen = []
        llm.replies = [final("x"), final("y"), final("z")]
        Scripted.call = lambda self, messages, *a, **k: (seen.append(str(messages)), real_call(self, messages))[1]
        try:
            h.research("Revenue?", session="s4", assistant="finance")
        finally:
            Scripted.call = real_call
        assert "Domain: Finance" in seen[0] and "Revenue is net." in seen[0]
        assert "at most five sentences" in seen[2]

        # human in the loop: the agent's question goes to `ask`, the reply comes back to it
        asked, seen = [], []
        llm.replies = [act("ask_user", question="Which period?"), final("x"), final("y"), final("z")]
        Scripted.call = lambda self, messages, *a, **k: (seen.append(str(messages)), real_call(self, messages))[1]
        try:
            h.research("How are sales?", session="s3", ask=lambda q: (asked.append(q), "last quarter")[1])
        finally:
            Scripted.call = real_call
        assert asked == ["Which period?"] and "The user replied: last quarter" in seen[1]

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
        "llm": "openai/" + os.environ.get("ANALYST_TEST_LLM", "qwen3:4b"),
        "llm_base_url": OLLAMA + "/v1",
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
    finally:
        h.close()
