"""jevguard command line: dashboard, scan, eval, init, demo."""

from __future__ import annotations

import argparse
import json
import sys

from jevguard.env import load_dotenv


def _dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from jevguard.server.app import create_app

    app = create_app(args.db, args.policy)
    print(f"jevguard dashboard -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _scan(args: argparse.Namespace) -> int:
    from jevguard import Guard, GuardContext, Stage

    guard = Guard(args.policy)
    text = args.text if args.text != "-" else sys.stdin.read()
    ctx = GuardContext()
    if args.stage == "tool_call":
        ctx.tool_name = args.tool or "tool"
        ctx.tool_args = json.loads(text) if text.strip().startswith("{") else {"input": text}
        text = ""
    d = guard.check(Stage(args.stage), text, ctx)
    if args.json:
        print(json.dumps(d.to_dict(), indent=2))
    else:
        shadow = "" if d.enforced else "  (shadow mode: recorded, not enforced)"
        print(f"{d.action.value.upper():9} risk={d.risk:.2f} source={d.source} latency={d.latency_ms:.1f}ms{shadow}")
        print(f"  {d.reason}")
        if d.redacted_text:
            print(f"  redacted: {d.redacted_text}")
    return 2 if d.blocked else 0


def _eval(args: argparse.Namespace) -> int:
    from jevguard import Guard
    from jevguard.evals import load_dataset, run_eval

    result = run_eval(Guard(args.policy), load_dataset(args.dataset))
    m = result["metrics"]
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"dataset={args.dataset} backend={m['backend']} cases={m['n']}")
        print(f"precision={m['precision']:.3f} recall={m['recall']:.3f} f1={m['f1']:.3f} fpr={m['fpr']:.3f} "
              f"p95={m['latency_p95_ms']}ms jev_calls={m['jev_calls']} cost=${m['cost_usd']:.6f}")
        misses = [c for c in result["cases"] if not c["correct"]]
        for c in misses:
            print(f"  x {c['id']:6} expected={c['expected']:5} got={c['action']:8} risk={c['risk']:.2f} {c['text'][:70]!r}")
    ok = m["recall"] >= args.min_recall and m["fpr"] <= args.max_fpr
    if not ok:
        print(f"FAILED: recall {m['recall']} < {args.min_recall} or fpr {m['fpr']} > {args.max_fpr}", file=sys.stderr)
    return 0 if ok else 1


def _init(args: argparse.Namespace) -> int:
    from jevguard import GuardPolicy

    GuardPolicy().save(args.path)
    print(f"wrote {args.path}")
    return 0


def _demo(args: argparse.Namespace) -> int:
    from jevguard import Guard
    from jevguard.demo import seed
    from jevguard.engine import run_sync
    from jevguard.store import EventStore

    events = run_sync(seed(Guard(args.policy), sessions=args.sessions))
    EventStore(args.db).add_events(events)
    print(f"added {len(events)} demo events to {args.db}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from jevguard import GuardPolicy
    from jevguard.diagnostics import diagnose, format_report
    from jevguard.engine import run_sync

    report = run_sync(diagnose(GuardPolicy.load(args.policy)))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("jevguard doctor")
        print()
        print(format_report(report))
    return 0 if report["status"] in ("ok", "simulator", "disabled") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="jevguard", description="Guardrails, evals and observability for AI agents.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=None, help="path to a .env file (default: nearest .env)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dashboard", help="run the dashboard and guard API", parents=[common])
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=7860)
    d.add_argument("--db", default="jevguard.db")
    d.add_argument("--policy", default=None)
    d.set_defaults(fn=_dashboard)

    s = sub.add_parser("scan", help="check one piece of text (exit code 2 when blocked)", parents=[common])
    s.add_argument("text", help="text to check, or - for stdin")
    s.add_argument("--stage", default="input", choices=["input", "tool_call", "tool_result", "retrieval", "output"])
    s.add_argument("--tool", default=None, help="tool name for --stage tool_call (text is the JSON args)")
    s.add_argument("--policy", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=_scan)

    e = sub.add_parser("eval", help="run a labelled dataset through the guard", parents=[common])
    e.add_argument("dataset", nargs="?", default="redteam_v1", help="built-in name or path to a .jsonl file")
    e.add_argument("--policy", default=None)
    e.add_argument("--min-recall", type=float, default=0.0)
    e.add_argument("--max-fpr", type=float, default=1.0)
    e.add_argument("--json", action="store_true")
    e.set_defaults(fn=_eval)

    i = sub.add_parser("init", help="write a starter policy file", parents=[common])
    i.add_argument("path", nargs="?", default="policy.yaml")
    i.set_defaults(fn=_init)

    m = sub.add_parser("demo", help="fill a database with realistic demo traffic", parents=[common])
    m.add_argument("--db", default="jevguard.db")
    m.add_argument("--policy", default=None)
    m.add_argument("--sessions", type=int, default=60)
    m.set_defaults(fn=_demo)

    doc = sub.add_parser("doctor", help="check the Jev connection and print what is being sent", parents=[common])
    doc.add_argument("--policy", default=None)
    doc.add_argument("--json", action="store_true")
    doc.set_defaults(fn=_doctor)

    args = p.parse_args(argv)
    load_dotenv(args.env_file)   # before any policy or server settings are read
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
