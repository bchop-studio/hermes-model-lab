# Contributing

Hermes Model Lab is intentionally narrow. V1 accepts work that helps people test configured models from Hermes Desktop without creating Hermes sessions or changing Hermes state.

## Before changing code

Open an issue describing the behavior and the safety boundary it touches. Keep each change small enough to review and prove with real tests.

## Scope rule

V1 has no agent loop, Hermes tools, code execution, filesystem access, saved conversations, or direct access to provider credentials. A change that adds one of those belongs in a later security design, not a V1 patch.

## Verification

Every change must include the command used to test it and the real result. Safety changes must prove that a run leaves Hermes sessions, memory, skills, project files, and model settings unchanged.

## Checks

Install the pinned development requirements once:

```
python -m pip install -r requirements-dev.txt
```

Run the same checks a maintainer runs, from the project root:

```
ruff check __init__.py dashboard/plugin_api.py scripts tests
node --check desktop/plugin.js
node --experimental-vm-modules tests/desktop_plugin_contract.mjs
pytest tests -q
```

The Python suite needs a local Hermes install on the path, because the backend imports the host's own `hermes_cli` package. The live proofs use the same host install:

```
HERMES_HOME=<hermes-home> python scripts/verify_isolation.py --project-root .
HERMES_HOME=<hermes-home> python scripts/verify_topology.py --project-root .
```

`.github/workflows/ci.yml` runs everything a hosted runner can run without a Hermes install: lint, the part of the Python suite that needs no host, the Desktop entry syntax check, and the Desktop plugin contract. It names the host-dependent tests it skips, so the gap stays visible instead of silently passing. `.github/workflows/codeql.yml` publishes code scanning results for `main` and for pull requests.

## Source of truth

The public V1 capability table lives in [README.md](README.md). T001 proved the stateless bridge and every V1 task is verified, so the project is release-ready: local release artifacts build deterministically via `scripts/build_release.py`, while the signed tag and GitHub release wait on maintainer approval.
