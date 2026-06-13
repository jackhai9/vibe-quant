# Contributing

Thanks for your interest in improving `binance-exit-executor`.

This project controls Binance USDT-M Futures exit execution. Changes that look small can affect order placement, cancellation, protective stops, and reduce-only safety. Please keep contributions focused, tested, and explicit about trading risk.

## Before You Start

- Open an issue or discussion for behavior changes before writing a large PR.
- Do not submit API keys, account IDs, private configs, screenshots with balances, or real trade history.
- Use small, reviewable PRs. Separate docs-only changes from runtime behavior changes.
- Keep the project focused on exit execution. Strategy discovery, opening positions, backtesting, and portfolio management are out of scope.

## Development Setup

```bash
uv sync
```

Run the main verification commands before opening a PR:

```bash
uv run pyright src/
uv run pytest -q
```

For narrow changes, run the targeted tests first, then expand when the change crosses module boundaries.

## Pull Request Checklist

- Describe the user-visible behavior change.
- Explain any trading safety impact.
- List the validation commands you ran.
- Update README, docs, examples, or systemd templates when behavior or setup changes.
- Add or update tests for execution, exchange, risk, websocket, signal, config, or notification changes.

## Trading Safety Requirements

PRs that touch order placement, cancellation, sizing, risk, or exchange adapters must explain:

- how reduce-only semantics are preserved;
- why the change cannot increase or reverse a position unexpectedly;
- how stale position, websocket delay, retry, timeout, or cancel/fill races are handled;
- what happens when Binance rejects an order.

If you cannot validate a live exchange path, say so clearly in the PR.

## Issue Reports

Useful bug reports include:

- expected behavior;
- actual behavior;
- sanitized config snippets;
- relevant log lines with secrets removed;
- symbol, mode, and whether the account is in Hedge mode;
- reproduction steps.

Never include real API keys, secrets, or private account identifiers.
