<!-- Input: systemd 部署产物与运行环境 -->
<!-- Output: 部署步骤与文件说明 -->
<!-- Pos: deploy/systemd 目录说明 -->
<!-- 一旦我所属的文件夹有所变化，请更新我。 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# systemd 部署（推荐）

本目录提供 systemd 部署所需文件。<br>
服务单元定义启动命令与日志目录。<br>
环境变量模板用于密钥与可选配置。

## 文件清单

- `vibe-quant.service`：systemd 服务单元（启动命令/日志目录/环境变量）
- `vibe-quant.env.example`：环境变量模板（API Key/Telegram/日志目录）
- `README.md`：本目录说明与部署步骤

## 部署步骤

### 1) 安装代码与依赖

- 将仓库放在 `/opt/vibe-quant`
- 创建虚拟环境并安装依赖：
  - `python3.11 -m venv /opt/vibe-quant/venv`
  - `/opt/vibe-quant/venv/bin/pip install -r /opt/vibe-quant/requirements.txt`

### 2) 配置文件与环境变量

- 配置文件：建议放在 `/etc/vibe-quant/config.yaml`
  - 参考仓库中的 `config/config.example.yaml`
- 环境变量：建议放在 `/etc/vibe-quant/vibe-quant.env`
  - 参考 `deploy/systemd/vibe-quant.env.example`
  - 必需：`BINANCE_API_KEY` / `BINANCE_API_SECRET`
  - Telegram 可选：`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`

### 3) 安装 service

- 复制 service 文件：
  - `sudo cp /opt/vibe-quant/deploy/systemd/vibe-quant.service /etc/systemd/system/vibe-quant.service`
- 创建目录与环境文件：
  - `sudo mkdir -p /etc/vibe-quant`
  - `sudo cp /opt/vibe-quant/deploy/systemd/vibe-quant.env.example /etc/vibe-quant/vibe-quant.env`
  - `sudo cp /opt/vibe-quant/config/config.example.yaml /etc/vibe-quant/config.yaml`
  - 编辑 `/etc/vibe-quant/vibe-quant.env` 和 `/etc/vibe-quant/config.yaml`

### 4) 启动与自启

- `sudo systemctl daemon-reload`
- `sudo systemctl enable --now vibe-quant`
- 查看日志：
  - `journalctl -u vibe-quant -f`
  - 文件日志：默认写入 `/var/log/vibe-quant/`（`vibe-quant_YYYY-MM-DD.log`/`error_YYYY-MM-DD.log`，旧日志 `.gz`）

### 5) 验证“自动重启”

- `sudo systemctl kill -s SIGKILL vibe-quant`
- `sudo systemctl status vibe-quant` 应显示已自动拉起，并重新连接 WS（会触发一次重连后校准日志）

*最后更新: 2025-12-22*
