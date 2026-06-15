<!-- Input: release readiness requirements and validation evidence -->
<!-- Output: release checklist and publication notes for tagged releases -->
<!-- Pos: release readiness guide -->
<!-- Update this header and docs/README.md when this document changes. -->

# Release Guide

This document defines the release gate for public tags and GitHub Releases.

## v0.1.0 Release Gate

`v0.1.0` should be treated as an early operator-controlled release of a Binance USDT-M Futures Hedge Mode reduce-only exit executor. It must not be described as a general-purpose trading bot, a strategy signal product, or a profit system.

The auditable release-candidate checklist and draft release notes are maintained in [v0.1.0 Release Checklist](releases/v0.1.0.md).

Before creating the tag and GitHub Release:

- Confirm the repository has no unrelated local changes.
- Confirm `pyproject.toml` version matches the release tag.
- Run `uv run pyright src/`.
- Run `uv run pytest -q`.
- Confirm README links and documentation links render correctly.
- Confirm reconnect and risk regression evidence is current in [Regression Evidence](regression-evidence.md).
- Confirm the release notes describe validation scope and unverified integration areas.
- Confirm the release notes do not include credentials, account identifiers, order IDs, private logs, or environment values.

## Suggested Release Notes Structure

Use this structure for GitHub Releases:

```md
## Scope

Early release of a Hedge Mode reduce-only exit executor for Binance USDT-M Futures.

## Safety Model

- Reduce-only semantics are enforced by `positionSide + side + qty <= position`.
- The project does not rely on the Binance `reduceOnly` order parameter for Hedge Mode safety.
- Protective stops and external takeover handling are documented as operational safeguards.

## Validation

- `uv run pyright src/`
- `uv run pytest -q`

## Not Yet Verified In This Release

- Fresh small-size production validation on the tagged commit, if required for the release decision.
- Production deployment on a newly provisioned host.
- Exchange, network, and Telegram behavior outside the documented environments.

## Upgrade Notes

- Review `config/config.example.yaml` and `docs/configuration.md` before using an existing local config.
- Use minimal API permissions and never enable withdrawal permissions.
```

## Public Roadmap Issues

Roadmap issues should describe concrete release-readiness work. Good issue topics:

- Small-size production validation walkthrough.
- Operator dry-run guide.
- Example configuration hardening.
- Risk-boundary documentation.
- WebSocket reconnect regression coverage.
- Panic close and protective stop validation evidence.

Do not create placeholder issues only to increase repository activity.

Tracked release-readiness issues for `v0.1.0`:

- [#18 Prepare v0.1.0 release checklist](https://github.com/jackhai9/binance-exit-executor/issues/18) - closed
- [#19 Add small-size production validation walkthrough](https://github.com/jackhai9/binance-exit-executor/issues/19) - closed
- [#20 Document operator dry-run and minimal-permission setup](https://github.com/jackhai9/binance-exit-executor/issues/20) - closed
- [#21 Expand reconnect and risk regression evidence before v0.1.0](https://github.com/jackhai9/binance-exit-executor/issues/21) - closed
