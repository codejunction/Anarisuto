"""uv run analyst serve | ingest PATH... | ask QUESTION | catalog SERVER [HINT]"""
import argparse
import pathlib


def main():
    p = argparse.ArgumentParser(prog="analyst")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("ingest").add_argument("paths", nargs="+")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--assistant")
    c = sub.add_parser("catalog")
    c.add_argument("server")
    c.add_argument("hint", nargs="?", default="")
    args = p.parse_args()

    if args.cmd == "serve":
        import os
        import uvicorn
        os.environ["ANALYST_CONFIG"] = args.config
        return uvicorn.run("analyst.server:app", host=args.host, port=args.port)

    from .harness import Harness
    h = Harness(args.config)
    try:
        if args.cmd == "ingest":
            for path in map(pathlib.Path, args.paths):
                # name sources relative to the folder given, e.g. metric_definitions.md
                base = path if path.is_dir() else path.parent
                for source, n in h.kb.ingest_path(path, base=base).items():
                    print(f"{n:4} chunks  {source}")
            return
        show = lambda e: e.get("kind") == "call" and print(f"  {e['tool']} {e['input']}")
        if args.cmd == "ask":
            print(h.research(args.question, assistant=args.assistant, emit=show,
                             ask=lambda q: input(f"\n{q}\n> "))["report"])
        else:
            print(h.catalog(args.server, args.hint, emit=show)["report"])
    finally:
        h.close()


if __name__ == "__main__":
    main()
