# Security Policy

## Reporting a Vulnerability

Please do not open a public issue for vulnerabilities that could expose credentials, private trading data, or unsafe order execution behavior.

Report security-sensitive issues privately through GitHub's private vulnerability reporting if available on this repository. If that is not available, contact the maintainer through the GitHub profile and include only sanitized details.

## Sensitive Data

Do not share:

- Binance API keys or secrets;
- Telegram bot tokens or chat IDs;
- `.env` files;
- private account identifiers;
- screenshots that reveal balances, positions, order IDs, or API permissions;
- full logs that may contain private trading context.

Use redacted snippets and minimal reproduction steps.

## Scope

Security-sensitive areas include:

- credential handling;
- order placement and cancellation safety;
- reduce-only enforcement;
- protective stop behavior;
- external stop/take-profit takeover detection;
- Telegram command handling;
- log output that may leak private account or trading data.

## Supported Versions

This is a personal open-source project. Security fixes target the latest `main` branch unless otherwise stated.

## Disclaimer

This project is provided without warranty. It is not financial advice and does not guarantee profitable or loss-free trading. Always validate behavior with minimal permissions and small size before running against a real account.
