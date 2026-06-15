<!-- Input: small-size production validation requirements and operator safety boundaries -->
<!-- Output: maintainer-facing mainnet validation walkthrough for v0.1.0 release readiness -->
<!-- Pos: small-size production validation guide -->
<!-- Update this header and docs/README.md when this document changes. -->

# Small-Size Production Validation

This walkthrough is for maintainer/operator validation on Binance USDT-M Futures mainnet with the smallest practical position size. It is not a simulation path.

Do not run these steps unless the operator explicitly accepts that real orders may be placed and canceled.

## Scope

Use this guide to validate release-relevant behavior that testnet cannot prove well:

- mainnet market data and user-data stream behavior;
- real exchange order placement, cancellation, fill, and rejection semantics;
- protective-stop visibility and external takeover boundaries;
- Telegram notification behavior in the operator's real runtime environment;
- small-size reduce-only convergence for one expected position.

This guide does not prove all market conditions, all symbols, all network failures, or all production host behavior.

## Preconditions

Before starting:

- Read [Operator Safety Guide](operator-safety.md).
- Use a dedicated API key.
- Confirm withdrawal permission is disabled.
- Confirm the account is in Hedge Mode.
- Confirm `global.testnet: false` is intentional.
- Use a single symbol and one expected small position.
- Keep `max_order_notional`, `base_mult`, panic-close tiers, and protective-stop settings conservative.
- Keep the Binance UI open for manual inspection and emergency intervention.
- Know how to stop the process and manually cancel open orders.
- Keep private evidence private. Do not paste raw order IDs, account identifiers, balances, API keys, `.env` files, or private logs into issues or pull requests.

## Recommended Configuration Shape

Use a separate local config file for the validation run. Do not commit private runtime config.

Minimum review points:

- `global.testnet: false`
- `global.execution.max_order_notional`
- `global.execution.base_mult`
- `global.risk.panic_close`
- `global.risk.protective_stop`
- symbol-level overrides for the one validation symbol
- Telegram enabled only if the target chat is private and expected

Example shape:

```yaml
global:
  testnet: false
  execution:
    base_mult: 1
    max_order_notional: 10
  telegram:
    enabled: false

symbols:
  "BTC/USDT:USDT":
    execution:
      base_mult: 1
```

Adjust values to the symbol's actual minimum quantity, minimum notional, and the operator's risk tolerance. The snippet is a shape example, not a recommendation to trade BTC specifically.

## Walkthrough

1. Confirm the working tree is clean:

   ```bash
   git status --short
   ```

2. Run local validation:

   ```bash
   uv run pyright src/
   uv run pytest -q
   ```

3. Prepare a dedicated `.env` and local config file. Do not commit either file.

4. Open one small Hedge Mode position manually on the exchange UI.

5. Confirm the expected side and symbol before starting the executor:

   - LONG position should only be reduced by SELL intents.
   - SHORT position should only be reduced by BUY intents.
   - The configured symbol override matches the exchange symbol.

6. Start the executor with the explicit local config:

   ```bash
   python -m src.main path/to/production-validation.yaml
   ```

7. Watch the console, log file, and exchange UI until one of these stopping conditions is reached:

   - position reaches zero or dust below the configured tradable threshold;
   - an unexpected order, side, symbol, or protective-stop state appears;
   - network, API, or Telegram behavior is unclear;
   - the operator decides to stop the run.

8. Stop with `Ctrl+C` and confirm own open orders are canceled or manually cancel any remaining orders from the exchange UI.

## Expected Evidence

Record only sanitized evidence:

- validation date and environment;
- commit hash;
- symbol and side, without balances or account identifiers;
- whether Hedge Mode was confirmed;
- whether `uv run pyright src/` passed;
- whether `uv run pytest -q` passed;
- whether order placement, cancellation, fill, cooldown, and protective-stop observations matched expectations;
- whether any manual intervention was required.

Do not record raw order IDs, account identifiers, screenshots with balances, `.env` values, or private logs in public artifacts.

## Release Decision

For `v0.1.0`, this walkthrough can support release readiness only after an operator explicitly performs the run and records sanitized evidence.

If the walkthrough is not performed before tagging, the release notes must say that fresh small-size production validation for the tagged commit has not been completed.
