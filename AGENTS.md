# vibe-quant

## Scope

- 本仓库是 Binance U 本位永续合约 Hedge 模式 reduce-only 平仓执行器。
- 目标是低冲击平仓，不是通用交易机器人。
- 规则真源只保留在本文件；`CLAUDE.md` 应作为同内容入口，而不是第二份手册。

## Core Constraints

- 不依赖交易所 `reduceOnly` 参数来保证 reduce-only 语义；必须通过 `positionSide + side + qty <= position` 约束来保证。
- Hedge 模式必须显式区分 `positionSide=LONG/SHORT`。
- 任何执行、风控、下单逻辑改动，都优先保证不会扩大仓位、不会反向开仓、不会破坏 reduce-only 语义。
- 个人项目允许打破旧格式和 legacy 代码，但必须同步收口文档，不保留半新半旧状态。

## Architecture

关键入口与高风险模块：

- `src/main.py`
  入口、生命周期、优雅退出。
- `src/exchange/adapter.py`
  交易所 REST 适配，订单/持仓/市场元数据真源。
- `src/ws/market.py` / `src/ws/user_data.py`
  市场与用户数据流，时序和重连语义敏感。
- `src/execution/engine.py`
  执行状态机，最容易引入竞态、重复下单、撤单错误。
- `src/risk/manager.py` / `src/risk/protective_stop.py` / `src/risk/rate_limiter.py`
  风控兜底、保护止损、限速。
- `src/signal/engine.py`
  信号判断、滑动窗口、倍数计算。
- `src/config/*`
  配置 schema 与加载逻辑。
- `src/notify/*`
  Telegram 通知和暂停控制。

## Docs As Code

- 代码、架构、配置、行为变更完成前，必须同步更新对应文档。
- 重要功能或里程碑完成后，更新：
  - `memory-bank/architecture.md`
  - `memory-bank/progress.md`
- 若目录结构或文件职责变化，更新对应目录下的 `README.md`。
- 若文件实现变化且该文件受文件头注释约束，更新文件头注释。

文件头注释例外：

- `AGENTS.md`、`CLAUDE.md`
- `config/*.yaml`、`.env`、`.env.*`
- 自动生成目录/文件：`.pytest_cache/`、`logs/`、`__pycache__/`、`*.log`、`*.log.gz`
- 其他明确标注“自动生成/不要改动”的文件

## Hard Rules

- 回复统一使用中文。
- 修改前先理解真实架构，不要根据文件名猜行为。
- 重要编码前先阅读：
  - `memory-bank/architecture.md`
  - `memory-bank/design-document.md`
- 禁止生成恶意代码；必须注意密钥、token、环境变量和外部接口泄漏风险。
- 任何可能影响对外交互、下单、撤单、止损的改动，都不能只靠静态阅读自证正确。

## Validation

最低验证按改动范围选择，不要假装“没跑也算通过”：

- Python 类型检查：`pyright src/`
- 全量测试：`pytest`
- 定向测试：
  - 执行引擎：`pytest tests/test_execution.py -q`
  - 交易所适配：`pytest tests/test_exchange.py -q`
  - 风控/保护止损：`pytest tests/test_risk_manager.py tests/test_protective_stop.py -q`
  - WebSocket：`pytest tests/test_ws_market.py tests/test_ws_user_data.py -q`
  - 配置：`pytest tests/test_config.py -q`
  - 主流程/退出：`pytest tests/test_main_shutdown.py -q`

如果没有运行某项验证，最终说明必须明确写出未验证项和原因。

## Plan Gate

出现下列任一情况，先写 5 行计划再改代码：

- 改动 `execution`、`exchange`、`risk`、`ws`、`signal`、`config` 任一核心模块
- 改动订单状态机、撤单逻辑、保护止损、rate limiter、用户数据流处理
- 改动配置 schema、默认参数、运行入口、部署方式
- 改动 docs-as-code 规则或 memory-bank 文档结构

计划模板：

```md
Goal:
Files:
Risks:
Validation:
Out of scope:
```

小范围文案、注释、纯文档排版可跳过。

## Review Priorities

review 时优先找功能回归，不要先谈风格：

- 是否破坏 reduce-only 语义
- 是否可能导致重复下单、漏撤单、状态机卡死
- 是否引入 stale 数据、竞态、重连时序错误
- 是否让保护止损、外部接管、panic close 失效
- 是否更新了必要文档与文件头注释

输出顺序默认：

- `Findings`
- `Notes`

## Release Rules

- 改动完成后，确认相关文档已同步。
- 如果改动触及核心交易路径，至少运行一项针对性测试和 `pyright src/`，除非明确说明为什么没跑。
- 本仓库没有 userscript 那种 `@version` 头部约束；release 重点在验证、文档同步、提交说明清晰。

## Done When

- 代码或文档已落盘。
- 相关 `README.md` / memory-bank / 文件头注释已按规则同步。
- 已运行的验证命令和结果已记录。
- 未运行的验证与残余风险已明确说明。
