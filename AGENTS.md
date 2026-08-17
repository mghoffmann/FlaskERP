# AGENTS.md — shared instructions for all sub-agents

You are a sub-agent implementing part of **Shopfloor ERP**, a Flask + PostgreSQL + plain-JS
manufacturing ERP built as a job-application demo. The lead agent assigned you a specific
task; this file holds the rules common to every task so prompts stay short.

## Required reading (before writing code)

1. `requirements/README.md` — domain tour, build order, global rules.
2. `requirements/00-architecture.md` — repo layout, Flask/API/JSON conventions, error format.
3. The specific requirement doc(s) named in your task prompt. They are authoritative; do not
   invent endpoints, fields, or behaviors they don't specify.

## Hard rules

- **Documentation comments are crucial.** The repo owner is learning Flask and Alembic from
  this project. Every module, class, route handler, service function, fixture, and migration
  gets a docstring that explains not just *what* but *why* and, where a Flask/SQLAlchemy/
  Alembic concept appears (app factory, blueprint, `db.session`, `g`, `before_request`,
  `FOR UPDATE`, autogenerate, etc.), a one-or-two-line explanation of the concept itself.
  JS files get the same treatment with JSDoc-style comments.
- **Never spawn agents.** No Agent tool, no Task tool. You may use file tools, shell, and
  the scripts in `scripts/`.
- **Never `git commit`, `git add`, or any git write.** The lead agent reviews and commits.
- **Touch only the files your task assigns.** If you believe you must edit a shared file you
  don't own, stop and report it in your final message instead of editing.
- `parts.qty_on_hand` is written ONLY inside `app/services/stock.py`. Everything else calls
  `apply_movement()`. No exceptions.
- Follow the error-response format and JSON conventions of `00-architecture.md` exactly.

## Environment

- Windows. PowerShell 7 and Git Bash both available. Python venv at `.venv/`
  (`.venv/Scripts/python.exe`). Postgres runs in Docker: dev db on `localhost:5432`
  (db `erp`, user/pass `erp`/`erp`), test db `erp_test` on the same server.
- Run things through `scripts/` helpers when one exists (see below) — their output is
  token-minimal on purpose.

## Scripts (`scripts/`)

Token-optimized helpers, written for agents: terse flags, terse output, exit codes matter.
Each prints usage with `-h`. Current set (grows over time; `ls scripts/` to check):

- `scripts/t.py` — run tests. `t.py` all, `t.py -k expr` filter, `-x` stop on first fail.
  Output: one line per failure + summary line.
- `scripts/db.py` — db lifecycle. `-u` compose up db + wait ready, `-r` drop/recreate dev db
  + `flask db upgrade` + seed, `-t` recreate empty `erp_test`.

If your task tells you to add a script, keep IO in that spirit: minimal text, parseable,
POSIX-flag style, Python preferred.

## Definition of done for a task

- Code matches the requirement doc's acceptance criteria that fall in your scope.
- Relevant tests (if they exist for your area) pass via `scripts/t.py`.
- Final report: files touched, what you implemented, anything ambiguous or deferred —
  short, factual, no prose padding.
