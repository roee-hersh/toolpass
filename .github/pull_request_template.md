## What

## Why

## Documentation

- [ ] `README.md`, `agent-authorizer-py/README.md` and the pages under `docs/` show the new behaviour, not the old (`python3 test/docs/linkcheck.py` passes)

## Testing

- [ ] `ruff format --check src tests examples` and `ruff check src tests examples` pass (in `agent-authorizer-py`)
- [ ] `mypy --strict src/agent_authorizer` passes
- [ ] `python -m pytest -q` passes
