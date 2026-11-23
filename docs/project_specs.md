# Project Specs

## Metadata
| Key | Value |
| --- | --- |
| Generated (UTC) | 2026-11-16T08:59:39+00:00 |
| Commit/HEAD | 4441a980aa73fbd5ae4192ab53e92628e84face5 |
| Fingerprint | d2ef81bf93a2c1e06a0fb2bcef22a6aedf18faebc2572257ee421bee37bc4e32 |
| Freshness | fresh |
| Language | en |
| Workspace | ia |

## Quick Context for Agents
Workspace with repos=['.'], manifests=['requirements.txt'], entry points=['main.py', 'app/__init__.py', 'app/cli.py'].

## Business / What the Project Does
# Equipo de agentes multi-rol (LangChain + LangGraph + OmniRoute)

## Repositories / Structure
```text
.
├── .agents
│   ├── harness
│   └── skills
│       └── example-skill
├── .docs
├── .opencode
│   └── agents
├── agents
├── app
│   ├── agents
│   ├── graph
│   ├── qa
│   ├── research
│   └── tools
├── docs
└── tests
    └── graph_support
```

## Architecture / Data Flow
- Entry points observed: main.py, app/__init__.py, app/cli.py

## Domain Model / Rules
- (insufficient evidence)

## Ubiquitous Language
| Term | Meaning | Where used |
| --- | --- | --- |
| agentes | (inferred from repo vocabulary) | repository text |
| omniroute | (inferred from repo vocabulary) | repository text |
| research | (inferred from repo vocabulary) | repository text |
| agents | (inferred from repo vocabulary) | repository text |
| para | (inferred from repo vocabulary) | repository text |
| como | (inferred from repo vocabulary) | repository text |
| flujo | (inferred from repo vocabulary) | repository text |
| langgraph | (inferred from repo vocabulary) | repository text |
| ciclos | (inferred from repo vocabulary) | repository text |
| commit | (inferred from repo vocabulary) | repository text |
| estado | (inferred from repo vocabulary) | repository text |
| modelo | (inferred from repo vocabulary) | repository text |
| onboarding | (inferred from repo vocabulary) | repository text |
| aplica | (inferred from repo vocabulary) | repository text |
| base | (inferred from repo vocabulary) | repository text |

## Acronyms
- API
- CLI
- HITL
- JSON
- LLM
- SKILL
- SSRF
- TEMPERATURE
- URL
- YAML

## Stack / Versions
- requirements.txt

## Data Sources / Integrations
- (no explicit integration evidence found)

## Entry Points / Build / Test / Run
```text
main.py
app/__init__.py
app/cli.py
```

## Conventions / Patterns
- (not detected)

## Key Files Index
```text
.
├── .agents
│   ├── harness
│   │   ├── preferences.json
│   │   ├── preferences_backup.json
│   │   └── session.log
│   └── skills
│       └── example-skill
│           └── SKILL.md
├── .docs
│   └── REQUIREMENTS.md
├── .gitignore
├── .opencode
│   └── agents
│       ├── builder.md
│       ├── project-manager.md
│       ├── qa.md
│       ├── research.md
│       ├── reviewer.md
│       ├── security.md
│       └── translator.md
├── README.md
├── agents
│   ├── builder.md
│   ├── project-manager.md
│   ├── qa.md
│   └── research.md
├── main.py
└── requirements.txt
```

## Risks / Debt / Gotchas
- Synthesis may be partial when evidence is limited.

## FAQ / Where to Find
- Specs: docs/project_specs.md
- Digest: .agents/research/context.md

## Sources / Research Log
- Deterministic local workspace scan
- git: 4441a98 2026-11-16 test: add docs/head alignment and pre-commit flow tests (T4)
- git: edd1b8c 2026-11-16 T3: ensure docs updated before commit request/recording; coherence with HEAD/state, no auto git commit
- git: e59c7b6 2026-11-16 Add docs/HEAD alignment verification on workspace load
- Synthesis: partial (no_synthesizer_available)
