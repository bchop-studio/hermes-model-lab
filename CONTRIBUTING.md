# Contributing

Hermes Model Lab is intentionally narrow. V1 accepts work that helps people test configured models from Hermes Desktop without creating Hermes sessions or changing Hermes state.

## Before changing code

Open an issue describing the behavior and the safety boundary it touches. Keep each change small enough to review and prove with real tests.

## Scope rule

V1 has no agent loop, Hermes tools, code execution, filesystem access, saved conversations, or direct access to provider credentials. A change that adds one of those belongs in a later security design, not a V1 patch.

## Verification

Every change must include the command used to test it and the real result. Safety changes must prove that a run leaves Hermes sessions, memory, skills, project files, and model settings unchanged.

## Source of truth

The public V1 capability table lives in [README.md](README.md). Until the feasibility spike passes, the project remains in planning status.
