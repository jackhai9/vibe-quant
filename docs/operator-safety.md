<!-- Input: operator safety requirements, credential boundaries, and pre-live validation workflow -->
<!-- Output: minimal-permission setup and non-production evaluation guide -->
<!-- Pos: operator safety guide -->
<!-- Update this header and docs/README.md when this document changes. -->

# Operator Safety Guide

This project can place and cancel Binance USDT-M Futures orders when it is connected to an account with trading permissions. Treat every runnable configuration as capable of affecting real positions unless it is explicitly pointed at Binance Futures testnet.

## Scope

Use this guide before running the executor against a real account. It covers:

- API key permissions.
- Configuration review before runtime.
- Testnet and small-size validation.
- The boundary between static review, testnet evaluation, and real-account execution.

This project does not provide a built-in mainnet paper-trading or dry-run switch. Do not run it on a real account expecting it to only simulate orders.

## Minimal API Permissions

Use a dedicated Binance API key for this service.

Required:

- Futures trading permission for Binance USDT-M Futures.
- IP whitelist when the runtime host has a stable outbound IP.

Do not enable:

- Withdrawal permission.
- Spot trading permission unless it is required for another separate workflow.
- Account-wide permissions unrelated to this executor.

Operational recommendations:

- Prefer a sub-account or isolated account for validation.
- Rotate the API key after public demos, shared-screen sessions, or suspected local exposure.
- Never paste API keys, secrets, account identifiers, private order IDs, or raw logs into issues or pull requests.

## Static Review Path

Use this path when you want to inspect the project without allowing it to submit orders.

1. Read [Configuration](configuration.md), especially `global.testnet`, `global.execution`, `global.risk`, `global.risk.protective_stop`, and symbol overrides.
2. Review `config/config.example.yaml` and prepare a separate local config file. Do not edit committed examples with private values.
3. Run local verification:

   ```bash
   uv sync
   uv run pyright src/
   uv run pytest -q
   ```

4. Review the effective operational assumptions:
   - Account must be in Hedge Mode.
   - The executor discovers active positions at runtime.
   - `symbols` are parameter overrides, not an execution allowlist.
   - Reduce-only semantics are enforced by `positionSide + side + qty <= position`.

Stop here if you only want a code and config review. Starting `python -m src.main ...` with a real-account key and `testnet: false` is an execution path, not a dry run.

## Testnet Validation Path

Use Binance Futures testnet for the first runnable validation.

1. Create a testnet API key with Futures trading permission.
2. Set the runtime config to testnet:

   ```yaml
   global:
     testnet: true
   ```

3. Use a separate testnet `.env` file. Do not reuse mainnet credentials.
4. Keep Telegram disabled until order behavior is understood, or use a private test chat.
5. Open a small testnet Hedge Mode position manually.
6. Start the executor with the explicit testnet config:

   ```bash
   python -m src.main path/to/testnet-config.yaml
   ```

7. Verify logs for:
   - market and user-data stream connectivity;
   - detected `symbol + positionSide`;
   - order placement, cancellation, fill, and cooldown events;
   - protective-stop behavior if enabled.

Testnet behavior is useful validation, but it does not prove mainnet liquidity, latency, rate limits, or stop-order behavior under production conditions.

## Real-Account Readiness

Before real-account execution:

- Confirm the account is in Hedge Mode.
- Confirm the API key has no withdrawal permission.
- Confirm `global.testnet: false` is intentional.
- Use the smallest practical position size.
- Start with one expected position and a conservative config.
- Keep the process awake during local runs, or use systemd for unattended runtime.
- Watch logs and exchange UI during the first run.
- Know how to stop the process and manually cancel open orders.

If any of these checks are uncertain, do not run against a real account.

## Issue And Support Hygiene

When reporting issues, include sanitized evidence only:

- config snippets with secrets removed;
- symbol, side, and mode;
- validation command output;
- relevant log lines with account identifiers, order IDs, and private values removed.

Never include `.env` files, API keys, secrets, raw private logs, shell history, or screenshots that reveal balances or account identifiers.
