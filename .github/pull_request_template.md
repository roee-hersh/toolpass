## What

## Why

## Documentation

- [ ] `README.md`, `toolpass-py/README.md` and the pages under `docs/` show the new behaviour, not the old (`python3 test/docs/linkcheck.py` passes)

## Testing

- [ ] `ruff format --check src tests examples` and `ruff check src tests examples` pass (in `toolpass-py`)
- [ ] `mypy --strict src/toolpass` passes
- [ ] `python -m pytest -q` passes
