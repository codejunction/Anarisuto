"""Knowledge base: documents chunked into a local Chroma collection."""
import hashlib
import pathlib
import urllib.request
from html.parser import HTMLParser

import chromadb

CHUNK, OVERLAP = 1500, 200


def _pdf(path):
    from pypdf import PdfReader
    return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


class _Text(HTMLParser):
    """Visible text of an HTML page."""

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ("script", "style")

    def handle_endtag(self, tag):
        self.skip -= tag in ("script", "style")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(data.strip())


class KB:
    def __init__(self, path, embed_model=None, embed_url="http://localhost:11434"):
        """Embeddings: Chroma's built-in MiniLM by default, or an Ollama model when
        `embed_model` is set. Each embedder gets its own collection, since vector sizes
        differ; switching embedder means ingesting again."""
        fn, name = None, "knowledge"
        if embed_model:
            from chromadb.utils.embedding_functions import OllamaEmbeddingFunction
            fn = OllamaEmbeddingFunction(url=embed_url, model_name=embed_model)
            name += "-" + "".join(c if c.isalnum() else "-" for c in embed_model).strip("-")
        self.col = chromadb.PersistentClient(str(path)).get_or_create_collection(name, embedding_function=fn)
        self.loaders = {".pdf": _pdf}  # plugins add more: ext -> fn(path) -> text

    def add(self, source, text, kind="document"):
        """Store `text` under `source`, replacing whatever was stored for it before."""
        self.delete(source)
        step = CHUNK - OVERLAP
        chunks = [c for c in (text[i:i + CHUNK] for i in range(0, len(text), step)) if c.strip()]
        if chunks:
            key = hashlib.sha1(source.encode()).hexdigest()[:12]
            self.col.add(
                ids=[f"{key}-{i}" for i in range(len(chunks))],
                documents=chunks,
                metadatas=[{"source": source, "kind": kind, "chunk": i} for i in range(len(chunks))],
            )
        return len(chunks)

    def delete(self, source):
        self.col.delete(where={"source": source})

    def ingest_path(self, path, base=None):
        """Ingest a file or every file under a directory. Returns {source: chunk count}.
        Sources are named relative to `base` when given, else by full path."""
        path = pathlib.Path(path)
        done = {}
        for f in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if not f.is_file():
                continue
            load = self.loaders.get(f.suffix.lower())
            try:
                text = load(f) if load else f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary file with no loader
            source = f.relative_to(base).as_posix() if base else str(f)
            done[source] = self.add(source, text)
        return done

    def ingest_url(self, url):
        if not url.startswith(("http://", "https://")):
            raise ValueError("only http and https URLs can be ingested")
        req = urllib.request.Request(url, headers={"User-Agent": "analyst-harness"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = "html" in r.headers.get_content_type()
            raw = r.read(20_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
        if html:
            parser = _Text()
            parser.feed(raw)
            raw = "\n".join(parser.out)
        return self.add(url, raw, "web page")

    def sources(self):
        # ponytail: reads every chunk's metadata; keep a side table past ~100k chunks
        counts = {}
        for m in self.col.get(include=["metadatas"])["metadatas"]:
            counts[m["source"], m["kind"]] = counts.get((m["source"], m["kind"]), 0) + 1
        return [{"source": s, "kind": k, "chunks": n} for (s, k), n in sorted(counts.items())]

    def search(self, query, k=6, prefer=None):
        """Best-matching chunks. Sources in `prefer` are searched first and fill up to half
        of the results, marked priority."""
        out, seen = [], set()
        for where, n in ([({"source": {"$in": list(prefer)}}, k // 2)] if prefer else []) + [(None, k)]:
            r = self.col.query(query_texts=[query], n_results=n, where=where)
            for i, d, m in zip(r["ids"][0], r["documents"][0], r["metadatas"][0]):
                if i not in seen and len(out) < k:
                    seen.add(i)
                    out.append({"source": m["source"], "text": d, "priority": where is not None})
        return out
