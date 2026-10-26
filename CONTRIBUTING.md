# Contributing to The Rehearsal

Thank you for contributing to The Rehearsal at [github.com/confidentlywrong/rehearsal](https://github.com/confidentlywrong/rehearsal). We welcome three kinds of contributions.

## 1. Multi-Stakeholder Simulation Scenarios

Simulation scenarios in [configs/quests/](https://github.com/confidentlywrong/rehearsal/tree/main/configs/quests) pair a transformation or AI rollout context with stakeholder roles across three organizational tiers. Scenarios are composites drawn from real organizational experience and stripped of anything that identifies a real person or company.

To contribute a new scenario, add a YAML definition under `configs/quests/` following the structure of the existing files and open a pull request.

## 2. Reports of Biased or Stereotyped Output

If a simulated role responds with a stereotype that flattens a function, an industry or a group into a caricature, open an issue using the Biased output template at [.github/ISSUE_TEMPLATE/biased-output.md](https://github.com/confidentlywrong/rehearsal/blob/main/.github/ISSUE_TEMPLATE/biased-output.md). Include the rollout draft you tested, the scenario or role definition and the output you received alongside what it got wrong.

## 3. Bug Fixes and Stack Improvements

Open an issue first if a change is larger than a small fix, so we can agree on the approach before you spend time building it.

## A Rule for Every Contribution

Never include real names, company names or confidential text in a scenario, an example, a pull request or an issue.

## Developer Workflow

### Prerequisites

Install the local prerequisites verified by `make check-prereqs`. The toolchain versions for Node.js, Go and Python are defined in `.nvmrc`, `go.mod` and `pyproject.toml`.

* Docker and Docker Compose run the local Redis, PostgreSQL and Pub/Sub emulators.
* Go builds the API gateway, frontend BFF, admin, billing and scenario tester services.
* Python and [uv](https://docs.astral.sh/uv/) run the Google ADK leadership and stakeholder agents.
* Node.js and npm build the Angular frontend, admin dashboard and scenario tester UI.
* Google Cloud SDK `gcloud` handles Vertex AI authentication and Cloud Build deployment.
* Terraform is required only when running `./scripts/teardown.sh` or local infrastructure targets.

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

1. Create a feature branch from `main` such as `git checkout -b feat/your-topic` or `fix/your-topic`.
2. Write commit messages following [Conventional Commits](https://www.conventionalcommits.org/).
3. Include the Apache 2.0 license header on every source file. New files created for this project use `Copyright 2026 Christian Haeussinger`. Modified files inherited from upstream [GoogleCloudPlatform/race-condition](https://github.com/GoogleCloudPlatform/race-condition) retain `Copyright 2026 Google LLC` and add `Modifications copyright 2026 Christian Haeussinger` along with a short `Changed for The Rehearsal` summary line.
4. Never commit `.env` files, API keys, service account credentials or personal data. To report a security vulnerability, follow [SECURITY.md](https://github.com/confidentlywrong/rehearsal/blob/main/SECURITY.md).
5. Follow our [CODE_OF_CONDUCT.md](https://github.com/confidentlywrong/rehearsal/blob/main/CODE_OF_CONDUCT.md) in all project interactions.
