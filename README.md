# Hermes Model Lab

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Status](https://img.shields.io/badge/status-planning-lightgrey.svg)

A stateless model playground for Hermes Desktop.

Hermes Model Lab is an opt-in Desktop plugin for testing prompts against configured models without turning each run into a Hermes conversation. Lab runs must not write to Hermes chat history, memory, skills, project files, or model settings.

## Planned V1

| Capability | V1 |
|---|---|
| Right-side Hermes Desktop pane | Planned |
| Single-prompt model runs | Planned |
| Provider and model selection | Planned |
| Response time and token usage | Planned |
| Cancel and clear controls | Planned |
| Hermes tools or agent loops | Excluded |
| Code execution | Excluded |
| Saved conversations | Excluded |

## Safety promise

The first release is a model lab, not a general security sandbox. It makes one bounded model request at a time, keeps credentials in Hermes, disables tools, and avoids Hermes session and memory storage. Code execution will not be added unless it can run behind a proven container boundary.

## Status

The product boundary is locked. Implementation begins with a feasibility spike that must prove a Desktop plugin can make a host-owned model call without creating or changing Hermes state.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing a change.

## License

MIT © BChop
