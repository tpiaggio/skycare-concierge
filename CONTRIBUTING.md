# Contributing to SkyCare Concierge

Contributions to this project are welcome!

## Development Workflow

1. Follow the repository setup in [README.md](README.md) and [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
2. Keep all runtime behavior, security redactions, and HITL gates covered by unit and integration tests.
3. Run quality checks before opening a pull request:
   - `agents-cli lint` (`ruff check .`, `ruff format . --check`, `codespell`, `ty check .`)
   - `uv run pytest tests/unit tests/integration/test_offline_api_e2e.py -v`

## Contributor License Agreement (CLA)

Contributions to this project must be accompanied by a Contributor License Agreement (CLA). You (or your employer) retain the copyright to your contribution; this simply gives us permission to use and redistribute your contributions as part of the project.
