---
name: vibe-quant-release
description: Prepare a release for vibe-quant by verifying tests and pyright, ensuring docs-as-code updates are complete, checking memory-bank sync, and producing a clean commit summary.
---

# vibe-quant-release

Use this skill when shipping changes in this repository.

## Workflow

1. Read `AGENTS.md` first.
2. Identify changed modules and choose matching validation commands.
3. Ensure docs-as-code updates are complete:
   - affected folder `README.md`
   - `memory-bank/architecture.md`
   - `memory-bank/progress.md`
   - file header comments when required
4. Run `pyright src/` when code changed.
5. Run targeted tests or full `pytest` depending on blast radius.
6. Summarize what changed, what was verified, what was not verified, and residual risks.

## Validation Heuristics

- 小范围单模块改动：优先跑对应测试文件
- 跨 execution / exchange / risk / ws 边界的改动：至少补 `pyright src/` 和多模块测试
- 涉及入口、配置、共享模型、状态机主路径：优先考虑全量 `pytest`

## Release Checklist

- docs-as-code 已同步
- `pyright src/` 已跑，或明确说明为什么没跑
- 至少一项与改动匹配的测试已跑，或明确说明为什么没跑
- 提交说明反映真实用户影响，而不是泛泛写“update”

## Notes

- 本仓库的 release 风险主要来自交易语义和文档漂移，不是版本号字段遗漏。
- 如果浏览器、交易所、Telegram 侧没有做集成验证，必须在最终说明里写清楚。
