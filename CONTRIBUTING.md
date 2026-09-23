# Contributing to jevguard

Thanks for taking the time to help. This project is published on PyPI as **`jevguard-middleware`**
and imported as **`jevguard`**.

## Getting set up

```bash
git clone https://github.com/Shubs5758/jev-guard.git
cd jev-guard
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                              # optional: add a key for real Jev calls
pytest -q
```

No API key is needed to develop or to run the tests: without `TYPESAFE_API_KEY` the offline
`SimulatedJev` backend produces probability-shaped answers so the whole pipeline runs. It is
**not** Jev, and `jevguard doctor` will label it `simulator`.

## Before you open a pull request

```bash
pytest -q                                          # the full suite must pass
jevguard eval redteam_v1 --min-recall 0.85 --max-fpr 0.02
```

Both of these also run in CI, on Python 3.10 to 3.13.

If you change detection behaviour — a heuristic, a threshold, a question pack — run the eval
before and after and put both numbers in the pull request. A change that raises recall while
quietly raising the false-positive rate is not an improvement.

## Things worth knowing

- **Never let an unknown answer score zero.** A missing, unparseable or error-shaped Jev answer
  must degrade to heuristics and be visible as `heuristics_degraded`. A zero reads as "no risk"
  and waves everything through while looking healthy.
- **Heuristics first.** Anything that can be decided locally should never cost a Jev call.
- **Adapters stay thin.** Framework adapters translate that framework's hooks onto `Guard`;
  detection logic belongs in the engine, not in an adapter.
- **Sinks must never break the agent.** Emitting an event is best-effort and already wrapped.
- **Tests come with behaviour.** New detection or a new adapter path needs a test.

## Secrets

`.env` is git-ignored; `.env.example` is the tracked template and must only ever contain
placeholders. Please check your diff before pushing — a real key in a commit means rotating it.

## Reporting a vulnerability

Please do not open a public issue for a security problem in jevguard itself. See
[SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contributions are licensed under the MIT License.
