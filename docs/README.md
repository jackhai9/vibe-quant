<!-- Input: docs 目录内的文档与变更需求 -->
<!-- Output: docs 目录架构概述与文件清单（含配置与自动发现持仓说明） -->
<!-- Pos: docs 文件夹级说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# docs 目录说明

部署/运维文档与项目运行方式保持一致。<br>
配置参数说明用于指导修改 YAML 与环境变量（含成交率反馈与自动发现持仓）。<br>
排障指南用于定位常见问题与恢复流程（含撤单失败、混合撤单与 reduce-only/min_notional 边界场景）。

## 文件清单

- `deployment.md`：部署与运维指南（systemd/Docker/日志/监控/备份）
- `configuration.md`：配置参数手册（global/symbol 覆盖，symbols 自动发现）
- `troubleshooting.md`：故障排查指南
