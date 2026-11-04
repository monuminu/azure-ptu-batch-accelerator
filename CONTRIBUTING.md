# Contributing

Use Python 3.12 and `uv`. Keep the dependency lock updated when dependencies change.

```bash
uv sync --locked --all-extras
uv run --locked --all-extras ruff check .
uv run --locked --all-extras pytest -q
uv run --locked --all-extras python -m build
bicep build infra/main.bicep --stdout > /dev/null
```

Add tests for behavior and failure recovery, not copies of implementation details. Use local mode for most tests; real Azure tests must be manually opted in and run against customer-approved resources. Do not include customer IDs, credentials, or payloads in issues, logs, examples, or pull requests. Report security issues as described in [SECURITY.md](SECURITY.md).
