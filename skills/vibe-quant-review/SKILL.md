---
name: vibe-quant-review
version: "1.0"
description: Review vibe-quant changes for trading safety and doc drift.
---

# vibe-quant-review

Use this skill when reviewing code changes in this repository.

## Workflow

1. Read `AGENTS.md` first.
2. Inspect changed files and classify them by module: execution, exchange, risk, ws, signal, config, notify, docs.
3. Run the narrowest meaningful verification command, and expand to `pytest` / `pyright src/` when the change crosses module boundaries.
4. Review for trading safety and behavioral regressions before style concerns.
5. Output findings ordered by severity with file references.

## Review Priorities

### Trading Safety

- reduce-only 语义是否仍由仓位约束保证，而不是错误依赖交易所参数
- 是否存在反向开仓、超量下单、重复下单、漏撤单风险
- 状态机是否可能卡在中间态，或在 timeout / cancel / reconnect 时重复推进

### High-Risk Modules

- `src/execution/*`
  状态机推进、mode rotation、TTL、cooldown、撤单与成交反馈
- `src/exchange/*`
  订单参数、市场规则、仓位与 open order 读取
- `src/risk/*`
  panic close、保护止损、外部接管、rate limit
- `src/ws/*`
  stale 数据、重连、user data 顺序、ticker/trade 竞争
- `src/config/*`
  schema、默认值、向后兼容取舍是否明确

### Docs And Validation

- 改动是否同步更新 `memory-bank/architecture.md` / `memory-bank/progress.md`
- 目录职责变化后，目录级 `README.md` 是否同步
- 最终说明里是否交代已跑验证和未跑项

## Output Format

- 先写 `Findings`
- 再写 `Notes`
- 如果没有新的功能性问题，要明确写出来

不要把概述放在 findings 前面。
