## Description

<!-- Explain what changed and why. Link the related issue if one exists. -->

Closes #

## Type of Change

<!-- Mark the applicable type with an "x". -->

- [ ] Bug fix
- [ ] New feature
- [ ] Performance improvement
- [ ] Refactor (no functional change)
- [ ] Documentation
- [ ] CI / build
- [ ] Chore (dependency bumps, config changes)

## Pre-Submission Checklist

<!-- All items must be checked before requesting review. -->

- [ ] `pre-commit run --all-files` passes
- [ ] `pytest` passes with coverage at or above the global 80% threshold
- [ ] `python scripts/module_coverage.py` passes (90% gate on critical modules)
- [ ] Tests added or updated for all changed logic
- [ ] `CHANGELOG.md` updated under `[Unreleased]` (skip for no operator-visible effect)
- [ ] Commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) specification
