<!-- Input: docs 目录内的文档与变更需求 -->
<!-- Output: docs 目录架构概述与文件清单（含配置、自动发现持仓、操作者安全、小额主网验证、发布门禁与候选发布清单说明） -->
<!-- Pos: docs 文件夹级说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# docs 目录说明

部署/运维文档与项目运行方式保持一致。<br>
配置参数说明用于指导修改 YAML 与环境变量（含成交率反馈与自动发现持仓）。<br>
操作者安全指南用于说明最小权限、静态审查、testnet 验证与真实账户运行边界。<br>
小额主网验证指南用于说明发布前真实交易环境验证的操作者流程与证据边界。<br>
排障指南用于定位常见问题与恢复流程（含撤单失败、混合撤单与 reduce-only/min_notional 边界场景）。
发布指南用于记录公开 tag 与 GitHub Release 前必须满足的验证门禁。
候选发布清单用于记录具体版本的发布状态、验证证据与 release notes 草案。

## 文件清单

- `deployment.md`：部署与运维指南（systemd/Docker/日志/监控/备份）
- `configuration.md`：配置参数手册（global/symbol 覆盖，symbols 自动发现）
- `operator-safety.md`：最小权限与真实账户运行前验证指南
- `production-validation.md`：小额主网验证流程与证据边界
- `troubleshooting.md`：故障排查指南
- `release.md`：发布门禁与 GitHub Release 说明清单
- `releases/v0.1.0.md`：`v0.1.0` 候选发布清单与 release notes 草案
