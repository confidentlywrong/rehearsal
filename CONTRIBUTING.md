# Contributing to The Rehearsal

Thank you for contributing to **The Rehearsal** at [github.com/confidentlywrong/rehearsal](https://github.com/confidentlywrong/rehearsal). We welcome three kinds of contributions.

## 1. Simulation Scenarios and Personas

A persona in The Rehearsal describes a position in a company and the daily work done there. It is a composite drawn from real experience and stripped of anything that identifies a real person or company.

Copy this template into a new file in [`personas/`](https://github.com/confidentlywrong/rehearsal/tree/main/personas) named after the industry and role, such as `logistics-account-manager.md`, and open a pull request.

```markdown
# [Role] in [industry]

## Daily work
What this role does on an ordinary day, in concrete terms.

## What a change would cost
Time, standing, skills that lose value, control over their own work.

## What a change could offer
Less repetitive work, better information, recognition, new skills.

## Words that carry weight
Terms this group uses with a meaning outsiders may miss.

## What they ask first
The first question this group raises when a change is announced.

## Source
How this persona was built, for example "four interviews with account
managers at two logistics companies, 2026". No names, no company names.
```

See [`personas/logistics-account-manager.md`](https://github.com/confidentlywrong/rehearsal/blob/main/personas/logistics-account-manager.md) for a complete example.

```markdown
# Account manager in logistics

## Daily work
Prices shipments for a portfolio of customers, negotiates exceptions and
keeps the relationship steady when a quote has to change.

## What a change would cost
Pricing judgment built over years becomes less visible. Control over
exceptions may move to a model or a central team.

## What a change could offer
Less time on routine quotes, more time on the customers and exceptions
that need judgment.

## Words that carry weight
"Faster" can sound like a verdict on past speed. "Errors" can sound like
a verdict on past judgment. "Margin" is where the account manager's
reputation lives.

## What they ask first
Who decides when the model's price is wrong for a customer I know, and
will anyone listen?

## Source
Illustrative persona based on the composite case in Confidently Wrong.
Replace or refine it with interviews before relying on it.
```

If you are also wiring a persona into a multi-stakeholder simulation scenario in [`configs/quests/`](https://github.com/confidentlywrong/rehearsal/tree/main/configs/quests), include its YAML scenario definition alongside your Markdown persona.

## 2. Reports of biased output

If a persona responds with a stereotype that flattens a role, an industry or a group into a caricature, open an issue using the **Biased output** template at [`.github/ISSUE_TEMPLATE/biased-output.md`](https://github.com/confidentlywrong/rehearsal/blob/main/.github/ISSUE_TEMPLATE/biased-output.md). Include the announcement text you used, the persona definition and the output you received alongside what it got wrong.

## 3. Bugs and improvements

Open an issue first if the change is larger than a small fix, so we can agree on the shape before you spend time on it.

## A rule for every contribution

**Never include real names, company names or confidential text in a persona, an example or an issue.**

## Developer Workflow

### Prerequisites

Install the local prerequisites verified by `make check-prereqs`, with toolchain versions defined in `.nvmrc`, `go.mod` and `pyproject.toml`.

- **Docker and Docker Compose** for local Redis, PostgreSQL and Pub/Sub emulators
- **Go** for API gateway, frontend BFF, admin, billing and simulator services
- **Python** and [**uv**](https://docs.astral.sh/uv/) for ADK leadership agents
- **Node.js and npm** for the Angular frontend, admin dashboard and scenario tester UI

### Setup and Verification

```bash
# Verify prerequisites, install dependencies and configure local environment
make init

# Run Go, Python and Angular test suites
make test

# Run linters and license header checks
make lint
```

### Pull Request Guidelines

1. **Branching** uses a feature branch from `main` such as `git checkout -b feat/your-topic` or `fix/your-topic`.
2. **Commit messages** follow [Conventional Commits](https://www.conventionalcommits.org/).
3. **License headers** require the Apache 2.0 license header on every source file. New files created for this project use `Copyright 2026 The Confidently Wrong Authors`. Modified files inherited from upstream [`GoogleCloudPlatform/race-condition`](https://github.com/GoogleCloudPlatform/race-condition) retain `Copyright 2026 Google LLC` and add `Copyright 2026 The Confidently Wrong Authors` plus `Modified by The Confidently Wrong Authors`.
4. **Security** requires that you never commit `.env` files, API keys, service account credentials or personal data. To report a security vulnerability, follow [`SECURITY.md`](https://github.com/confidentlywrong/rehearsal/blob/main/SECURITY.md).
5. **Code of Conduct** applies to all contributors under [`CODE_OF_CONDUCT.md`](https://github.com/confidentlywrong/rehearsal/blob/main/CODE_OF_CONDUCT.md).
