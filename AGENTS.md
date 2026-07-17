# ghost-sync

Follow [ponytail](https://github.com/DietrichGebert/ponytail) when changing this project:

1. Does this need to exist? If not, delete it.
2. Reuse what is already here before adding files or dependencies.
3. Prefer stdlib over new packages.
4. Keep webhook signature verification and error handling.
5. One file (`app.py`) is intentional — do not split without a hard reason.

## Dependencies & secrets

- New packages: install the current PyPI latest (`pip install <pkg>` / floor `@` latest in `requirements.txt`), never a version from memory. Run `/check-dep` first when adding anything.
- After every dependency change: `pip install -r requirements.txt && pip-audit` (Python 3.12+, same as Dockerfile). CI runs `pip-audit` and gitleaks on every push/PR.
- Periodically: `pip list --outdated` (or compare floors in `requirements.txt` to PyPI) and bump floors that matter.
- Secrets: never commit `.env` or keys. Install local guard: `brew install gitleaks pre-commit && pre-commit install` (uses `.pre-commit-config.yaml`).
