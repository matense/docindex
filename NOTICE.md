# Third-party attributions

## The Agency — AI agent personalities

DocIndex bundles the AI persona library from
[The Agency](https://github.com/msitarzewski/agency-agents)
(`app/data/agency_personas.json`, converted with
`tools/convert_agency_personas.py`).

- Copyright (c) 2025 AgentLand Contributors
- License: MIT — full text in `licenses/AGENCY-AGENTS-LICENSE.txt`

The personas power the core Behaviors & personas page (`/behaviors`) and the
chat persona selector. All credit for the personality content goes to the
Agency Agents community.

## Dulwich — pure-Python Git implementation

DocIndex depends on [dulwich](https://github.com/jelmer/dulwich) (used by
the optional `gitdrives` module to clone and pull Git repositories with no
git binary required).

- License: Apache License 2.0 (dual-licensed Apache-2.0 OR GPL-2.0-or-later;
  DocIndex uses it under the Apache License 2.0)
