# binance-exit-executor

<p align="center">
  <img src="assets/logo.png" alt="binance-exit-executor logo" width="180" />
</p>

<p align="center">
  <a href="README.zh-CN.md">简体中文</a> | English
</p>

![Python](https://img.shields.io/badge/Python-3.12-blue)
![Exchange](https://img.shields.io/badge/Exchange-Binance%20USDT--M%20Futures-f0b90b)
![Mode](https://img.shields.io/badge/Mode-Hedge%20Reduce--Only-47a042)
![Tests](https://img.shields.io/badge/Tests-pytest-111133)
![License](https://img.shields.io/badge/License-MIT-green)

`binance-exit-executor` is a Binance USDT-M Futures exit execution tool for Hedge Mode accounts. It is not an entry-signal bot. It focuses on reducing or closing existing LONG and SHORT positions with smaller slices, maker-first execution, escalation rules, and risk controls.

The project is designed for traders who want a testable Python service for exit execution rather than a manual collection of scripts and exchange-page actions.

> Disclaimer: This project is not financial advice and does not guarantee profitable or loss-free trading. Validate all behavior with minimal permissions, small size, testnet, or a read-only workflow before using it against a real account.

## What It Does

- Manages Binance USDT-M Futures Hedge Mode positions.
- Preserves reduce-only semantics through `positionSide + side + qty <= position`.
- Supports independent LONG and SHORT exit state machines.
- Uses maker-first limit orders and can escalate to aggressive limit orders after timeouts.
- Supports two signal modes: `orderbook_price` and `orderbook_pressure`.
- Adds safety layers for liquidation distance, forced slice exits, protective stops, and external stop/take-profit takeover.
- Sends Telegram notifications for fills, reconnects, risk events, and open-position alerts.
- Discovers active positions at runtime; `symbols` are used for parameter overrides, not as the only execution list.

## Who It Is For

This project fits users who:

- use Binance USDT-M Futures in Hedge Mode;
- already have positions and need more controlled exits;
- care about maker/taker behavior, orderbook pressure, liquidation distance, protective stops, and operational logs;
- want execution logic that can be tested, deployed, and reviewed.

This project is not for:

- automatic entry-signal discovery;
- spot, options, or non-Binance exchanges;
- running real trading automation without understanding API permissions, leverage, liquidation, and stop-order behavior.

## Quick Start

### Requirements

- Python 3.12
- uv
- A Binance USDT-M Futures account in Hedge Mode

### Install

```bash
uv sync
```

### Configure

```bash
cp .env.example .env
cp config/config.example.yaml config/config.yaml
```

Put Binance API credentials in `.env` and execution parameters in `config/config.yaml`.

Use the smallest practical API permissions. Do not enable withdrawal permissions.

### Run

```bash
python -m src.main
```

With an explicit config file:

```bash
python -m src.main path/to/config.yaml
```

On macOS, keep the process awake during local runs:

```bash
caffeinate -is python -m src.main
```

For production-style deployment, see [Deployment](docs/deployment.md).

## Safety Model

- The system does not rely on the Binance `reduceOnly` parameter for Hedge Mode safety.
- Reduce-only behavior is enforced by position side, close side, and quantity checks.
- Protective stops are exchange-side conditional orders used as a last-resort defense when the process crashes, sleeps, or loses connectivity.
- External stop/take-profit orders can take over protective-stop maintenance to avoid conflicting stop logic.
- Telegram is used for notifications and pause/resume control, not for transmitting exchange credentials.

## Strategy Modes

Each symbol can choose one of two mutually exclusive modes:

| Mode | Market data | Order intent | Sizing |
| --- | --- | --- | --- |
| `orderbook_price` | `aggTrade` + `bookTicker` | Price and momentum conditions with maker/aggressive rotation | `base_mult * roi_mult * accel_mult` |
| `orderbook_pressure` | `bookTicker` + `depth10@100ms` | Active one-level taking after sustained top-level size, otherwise one passive level | `min_qty * base_mult` |

`orderbook_pressure` requires both `strategy.mode: orderbook_pressure` and `pressure_exit` configuration. Stale `bookTicker` or `depth10` data pauses the current evaluation cycle.

## Architecture

```text
main.py
  |-- ConfigManager      loads YAML and symbol overrides
  |-- WSClient           streams market and user data
  |-- ExchangeAdapter    wraps ccxt REST calls
  |-- SignalEngine       evaluates exit signals
  |-- ExecutionEngine    manages per-side order state machines
  |-- RiskManager        handles liquidation-distance and rate limits
  |-- Notifier           sends Telegram notifications and bot commands
```

## Documentation

| Document | Description |
| --- | --- |
| [Configuration](docs/configuration.md) | Full configuration reference and tuning notes |
| [Operator Safety](docs/operator-safety.md) | Minimal permissions and pre-live validation workflow |
| [Production Validation](docs/production-validation.md) | Small-size mainnet validation workflow and evidence boundaries |
| [Deployment](docs/deployment.md) | Local, systemd, and Docker deployment notes |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and diagnostics |
| [Release Guide](docs/release.md) | Release gate and GitHub Release note checklist |
| [Architecture](memory-bank/architecture.md) | Detailed architecture and module responsibilities |
| [Progress](memory-bank/progress.md) | Development milestones and change notes |

## Release Status

`v0.1.0` is planned as an early operator-controlled release. Before a public tag is created, the release gate in [Release Guide](docs/release.md) must pass, including type checking, full tests, and release notes that clearly state validation scope and unverified integration areas.

## Development

```bash
uv sync
uv run pyright src/
uv run pytest -q
```

Targeted tests:

```bash
uv run pytest tests/test_execution.py -q
uv run pytest tests/test_exchange.py -q
uv run pytest tests/test_risk_manager.py tests/test_protective_stop.py -q
uv run pytest tests/test_ws_market.py tests/test_ws_user_data.py -q
```

## License

MIT. See [LICENSE](LICENSE).

This project is not affiliated with Binance.
