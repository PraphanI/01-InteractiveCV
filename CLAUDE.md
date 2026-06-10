## interactive-CV — agent contract

### Project
Interactive HTML CV. Data flows from Google Sheets through a Python/DuckDB
pipeline to a static JSON file, which a self-contained HTML page reads at
runtime. Hosted for free on GitHub Pages.

The data schema, JOIN logic, and cv_data.json output shape are all defined
in shared/requirements_spec.html — the single source of truth produced by
Agent 1. Agent 2 reads the spec and builds from it. Agent 3 reads the spec
and tests against it. Neither agent hardcodes schema assumptions.

### Folder ownership
- agents/    system prompts — reference only, do not modify during a run
- shared/    handoff files between agents — the source of truth
- pipeline/  data pipeline code and local DuckDB database file
- docs/      GitHub Pages output — only the pipeline and Agent 2 write here

### Handoff contract
Agent 1 reads:    shared/requirements_spec_template.html
Agent 1 writes:   shared/requirements_spec.html
                  (user approves before Agent 2 starts)

Pipeline writes:  shared/cv_data.json
                  docs/cv_data.json
                  (runs after Agent 2 creates pipeline/build.py)

Agent 2 reads:    shared/requirements_spec.html
                  shared/cv_data.json
Agent 2 writes:   pipeline/build.py
                  pipeline/requirements.txt
                  docs/index.html

Agent 3 reads:    shared/requirements_spec.html
                  docs/index.html
Agent 3 writes:   shared/qa_report.md

### Rules
- Never commit: .env, service_account.json, *.db files
- Never edit docs/cv_data.json manually — always regenerate via build.py
- Never start Agent 2 without an approved requirements_spec.html in shared/
- Never deploy without a passing qa_report.md in shared/