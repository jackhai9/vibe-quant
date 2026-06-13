<!-- Input: 部署配置、运行环境、日志目录约定 -->
<!-- Output: 部署与运维步骤、日志/监控/备份指引 -->
<!-- Pos: 文档/部署指南 -->
<!-- 一旦我被更新，务必更新我的开头注释，以及所属文件夹的MD。 -->
# 部署指南

> binance-exit-executor 生产环境部署完整指南

---

## 目录

1. [系统要求](#系统要求)
2. [本地开发部署](#本地开发部署)
3. [生产环境部署（systemd）](#生产环境部署systemd推荐)
4. [Docker 部署](#docker-部署可选)
5. [监控与维护](#监控与维护)
6. [安全加固](#安全加固)

---

## 系统要求

### 硬件要求

| 配置 | 最低 | 推荐 |
|------|------|------|
| CPU | 1 核 | 2 核+ |
| 内存 | 512MB | 1GB+ |
| 磁盘 | 5GB | 20GB+（用于日志存储） |
| 网络 | 稳定连接 | 低延迟（< 100ms 到币安） |

### 软件要求

- **操作系统**: Linux（Ubuntu 20.04+、Debian 11+、CentOS 8+）或 macOS
- **Python**: 3.11 或更高版本，通过 uv 管理项目级 `.venv`
- **网络**: 可访问币安 API（`fapi.binance.com`、`fstream.binance.com`）

### 依赖项

依赖由 `pyproject.toml` 声明，并由 `uv.lock` 锁定：
- ccxt >= 4.0.0
- aiohttp >= 3.9.0
- PyYAML >= 6.0
- pydantic >= 2.0.0
- python-dotenv >= 1.0.0
- loguru >= 0.7.0

---

## 本地开发部署

适用于开发测试、调试、快速验证等场景。

### 1. 克隆代码

```bash
git clone https://github.com/jackhai9/binance-exit-executor.git
cd binance-exit-executor
```

### 2. 同步 uv 环境

```bash
uv sync
```

### 3. 配置环境变量

```bash
# 复制示例文件
cp .env.example .env

# 编辑 .env 文件
nano .env
```

填入真实凭证：
```bash
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# Telegram（可选）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

### 5. 配置参数文件

```bash
# 使用默认配置
cp config/config.example.yaml config/config-local.yaml

# 根据需要编辑
nano config/config-local.yaml
```

**测试网配置**（推荐首次使用）：
```yaml
global:
  testnet: true  # 使用测试网
  telegram:
    enabled: false  # 测试时关闭通知
```

### 6. 启动程序

```bash
# 加载环境变量并启动
python -m src.main config/config-local.yaml
```

**使用 python-dotenv 自动加载 .env**：
```bash
# .env 文件会被自动加载（如果代码中使用了 load_dotenv()）
python -m src.main config/config-local.yaml
```

### 7. 验证运行

检查日志输出：
- 查看 `logs/` 目录下的 `binance-exit-executor_YYYY-MM-DD.log` 与 `error_YYYY-MM-DD.log`（旧日志会压缩为 `.gz`）
- 确认 WebSocket 连接成功
- 查看仓位和市场数据是否正常获取

**停止程序**：按 `Ctrl+C`

---

## 生产环境部署（systemd，推荐）

适用于生产环境，支持自动重启、日志管理、开机自启等。

### 架构概览

```
/opt/binance-exit-executor/          # 代码目录
├── venv/                 # Python 虚拟环境
├── src/                  # 源代码
└── ...

/etc/binance-exit-executor/          # 配置目录
├── config.yaml           # 主配置文件
└── binance-exit-executor.env        # 环境变量（包含 API 密钥）

/var/log/binance-exit-executor/      # 日志目录
├── binance-exit-executor_YYYY-MM-DD.log  # 按天滚动的业务日志（旧日志压缩为 .gz）
└── error_YYYY-MM-DD.log       # 按天滚动的错误日志（旧日志压缩为 .gz）

/etc/systemd/system/      # systemd 服务
└── binance-exit-executor.service    # 服务单元文件
```

---

### 步骤1：安装代码与依赖

#### 1.1 创建部署目录

```bash
sudo mkdir -p /opt/binance-exit-executor
sudo chown $USER:$USER /opt/binance-exit-executor
```

#### 1.2 克隆代码

```bash
cd /opt
git clone https://github.com/jackhai9/binance-exit-executor.git
cd binance-exit-executor
```

#### 1.3 同步 uv 环境

```bash
uv sync --frozen --no-dev
```

---

### 步骤2：配置文件与环境变量

#### 2.1 创建配置目录

```bash
sudo mkdir -p /etc/binance-exit-executor
```

#### 2.2 复制配置文件

```bash
# 复制主配置文件
sudo cp /opt/binance-exit-executor/config/config.example.yaml /etc/binance-exit-executor/config.yaml

# 复制环境变量模板
sudo cp /opt/binance-exit-executor/deploy/systemd/binance-exit-executor.env.example /etc/binance-exit-executor/binance-exit-executor.env
```

#### 2.3 编辑环境变量

```bash
sudo nano /etc/binance-exit-executor/binance-exit-executor.env
```

填入真实凭证：
```bash
BINANCE_API_KEY=your_real_api_key
BINANCE_API_SECRET=your_real_api_secret

# Telegram（可选）
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

**设置文件权限**（重要！防止密钥泄露）：
```bash
sudo chmod 600 /etc/binance-exit-executor/binance-exit-executor.env
sudo chown root:root /etc/binance-exit-executor/binance-exit-executor.env
```

#### 2.4 编辑配置文件

```bash
sudo nano /etc/binance-exit-executor/config.yaml
```

根据实际需求调整参数，参考 [配置参数手册](configuration.md)。

---

### 步骤3：安装 systemd 服务

#### 3.1 复制服务文件

```bash
sudo cp /opt/binance-exit-executor/deploy/systemd/binance-exit-executor.service /etc/systemd/system/binance-exit-executor.service
```

#### 3.2 查看服务文件内容

```bash
cat /etc/systemd/system/binance-exit-executor.service
```

**服务文件说明**：
```ini
[Unit]
Description=binance-exit-executor (Binance Futures reduce-only executor)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/binance-exit-executor

# 环境变量文件（包含 API 密钥）
EnvironmentFile=/etc/binance-exit-executor/binance-exit-executor.env

# 日志目录
LogsDirectory=binance-exit-executor
Environment=VQ_LOG_DIR=/var/log/binance-exit-executor

# 启动命令
ExecStart=/opt/binance-exit-executor/.venv/bin/python -m src.main /etc/binance-exit-executor/config.yaml

# 自动重启策略
Restart=on-failure
RestartSec=2

# 优雅退出
TimeoutStopSec=20
KillSignal=SIGTERM

# 日志输出
StandardOutput=journal
StandardError=journal
SyslogIdentifier=binance-exit-executor

[Install]
WantedBy=multi-user.target
```

#### 3.3 重载 systemd 配置

```bash
sudo systemctl daemon-reload
```

---

### 步骤4：启动与自启

#### 4.1 启动服务

```bash
sudo systemctl start binance-exit-executor
```

#### 4.2 查看状态

```bash
sudo systemctl status binance-exit-executor
```

**预期输出**（运行中）：
```
● binance-exit-executor.service - binance-exit-executor (Binance Futures reduce-only executor)
   Loaded: loaded (/etc/systemd/system/binance-exit-executor.service; enabled; vendor preset: enabled)
   Active: active (running) since Thu 2025-12-19 10:00:00 UTC; 5s ago
 Main PID: 12345 (python)
   ...
```

#### 4.3 启用开机自启

```bash
sudo systemctl enable binance-exit-executor
```

#### 4.4 查看日志

**实时查看 systemd 日志**：
```bash
journalctl -u binance-exit-executor -f
```

**查看文件日志**：
```bash
tail -f /var/log/binance-exit-executor/binance-exit-executor_$(date +%Y-%m-%d).log
tail -f /var/log/binance-exit-executor/error_$(date +%Y-%m-%d).log
```

---

### 步骤5：验证自动重启

验证服务在异常退出时能自动重启：

```bash
# 强制杀死进程
sudo systemctl kill -s SIGKILL binance-exit-executor

# 等待 2-3 秒后查看状态
sudo systemctl status binance-exit-executor
```

**预期结果**：服务应该已自动重启，状态为 `active (running)`

---

### 常用 systemd 命令

```bash
# 启动服务
sudo systemctl start binance-exit-executor

# 停止服务
sudo systemctl stop binance-exit-executor

# 重启服务
sudo systemctl restart binance-exit-executor

# 查看状态
sudo systemctl status binance-exit-executor

# 启用开机自启
sudo systemctl enable binance-exit-executor

# 禁用开机自启
sudo systemctl disable binance-exit-executor

# 查看日志（最近100行）
journalctl -u binance-exit-executor -n 100

# 查看日志（实时）
journalctl -u binance-exit-executor -f

# 查看日志（指定时间范围）
journalctl -u binance-exit-executor --since "2025-12-19 10:00" --until "2025-12-19 12:00"
```

---

## Docker 部署（可选）

适用于需要容器化部署的场景。

### Dockerfile 示例

创建 `Dockerfile`：

```dockerfile
FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 设置工作目录
WORKDIR /app

# 安装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# 复制代码
COPY src/ ./src/

# 创建日志目录
RUN mkdir -p /var/log/binance-exit-executor

# 设置环境变量（仅默认值，实际使用时通过 -e 传递）
ENV VQ_LOG_DIR=/var/log/binance-exit-executor

# 启动命令（配置文件通过 volume 挂载）
ENTRYPOINT ["python", "-m", "src.main"]
CMD ["/etc/binance-exit-executor/config.yaml"]
```

### 构建镜像

```bash
docker build -t binance-exit-executor:latest .
```

### 运行容器

```bash
docker run -d \
  --name binance-exit-executor \
  --restart unless-stopped \
  -e BINANCE_API_KEY=your_key \
  -e BINANCE_API_SECRET=your_secret \
  -e TELEGRAM_BOT_TOKEN=your_token \
  -e TELEGRAM_CHAT_ID=your_chat_id \
  -v /path/to/config.yaml:/etc/binance-exit-executor/config.yaml:ro \
  -v /path/to/logs:/var/log/binance-exit-executor \
  binance-exit-executor:latest
```

### 使用 Docker Compose

创建 `docker-compose.yml`：

```yaml
version: '3.8'

services:
  binance-exit-executor:
    image: binance-exit-executor:latest
    container_name: binance-exit-executor
    restart: unless-stopped
    environment:
      - BINANCE_API_KEY=${BINANCE_API_KEY}
      - BINANCE_API_SECRET=${BINANCE_API_SECRET}
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
    volumes:
      - ./config/config.example.yaml:/etc/binance-exit-executor/config.yaml:ro
      - ./logs:/var/log/binance-exit-executor
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

启动：
```bash
# 创建 .env 文件包含 API 密钥
docker-compose up -d

# 查看日志
docker-compose logs -f binance-exit-executor
```

---

## 监控与维护

### 日志管理

#### 日志轮转

系统默认由 loguru 按天滚动日志，文件名为 `binance-exit-executor_YYYY-MM-DD.log` 与 `error_YYYY-MM-DD.log`，旧日志会压缩为 `.gz`，默认保留 30 天。通常无需额外 logrotate。若通过 `VQ_LOG_DIR` 修改日志目录，请将下述路径替换为实际目录。

**方法1：手动清理**
```bash
# 删除 30 天前的日志（包含 .log/.log.gz）
find /var/log/binance-exit-executor -type f \( -name "binance-exit-executor_*.log*" -o -name "error_*.log*" \) -mtime +30 -delete
```

**方法2（可选）：使用 logrotate（如需统一系统日志策略）**

创建 `/etc/logrotate.d/binance-exit-executor`：
```
/var/log/binance-exit-executor/binance-exit-executor_*.log /var/log/binance-exit-executor/error_*.log {
    daily
    rotate 30
    copytruncate
    nocompress
    missingok
    notifempty
}
```

注意：loguru 已负责轮转/压缩，若启用 logrotate，建议先在 `src/utils/logger.py` 调整 loguru 的 rotation/retention/compression 以避免双重轮转。

测试配置：
```bash
sudo logrotate -d /etc/logrotate.d/binance-exit-executor
```

#### 日志监控

**监控错误日志**：
```bash
# 实时监控错误日志
tail -f /var/log/binance-exit-executor/error_$(date +%Y-%m-%d).log

# 统计今日错误数
wc -l /var/log/binance-exit-executor/error_$(date +%Y-%m-%d).log
```

**监控重连事件**：
```bash
grep "WS重连" /var/log/binance-exit-executor/binance-exit-executor_$(date +%Y-%m-%d).log | wc -l
zgrep -h "WS重连" /var/log/binance-exit-executor/binance-exit-executor_*.log* | wc -l
```

---

### 性能监控

#### 系统资源监控

```bash
# CPU 和内存使用
top -p $(pgrep -f "src.main")

# 详细信息
ps aux | grep "src.main"
```

#### 网络监控

```bash
# 检查连接状态
netstat -an | grep fapi.binance.com
netstat -an | grep fstream.binance.com

# 测试延迟
ping fapi.binance.com
```

---

### 健康检查

创建健康检查脚本 `/opt/binance-exit-executor/healthcheck.sh`：

```bash
#!/bin/bash

# 检查进程是否运行
if ! systemctl is-active --quiet binance-exit-executor; then
    echo "CRITICAL: binance-exit-executor is not running"
    exit 2
fi

# 检查日志中是否有最近的心跳（例如最近5分钟内有日志更新）
# 如日志目录非 /var/log/binance-exit-executor，请同步调整 LOG_FILE
LOG_FILE="/var/log/binance-exit-executor/binance-exit-executor_$(date +%Y-%m-%d).log"
if [ ! -f "$LOG_FILE" ]; then
    echo "WARNING: Log file not found"
    exit 1
fi

LAST_LOG=$(stat -c %Y "$LOG_FILE")
NOW=$(date +%s)
DIFF=$((NOW - LAST_LOG))

if [ $DIFF -gt 300 ]; then
    echo "WARNING: No log activity in last 5 minutes"
    exit 1
fi

echo "OK: binance-exit-executor is healthy"
exit 0
```

设置权限：
```bash
chmod +x /opt/binance-exit-executor/healthcheck.sh
```

配置 cron 定期检查：
```bash
# 编辑 crontab
crontab -e

# 每5分钟检查一次
*/5 * * * * /opt/binance-exit-executor/healthcheck.sh >> /var/log/binance-exit-executor-health.log 2>&1
```

---

### 更新与回滚

#### 更新代码

```bash
# 停止服务
sudo systemctl stop binance-exit-executor

# 备份当前版本
cd /opt
sudo cp -r binance-exit-executor binance-exit-executor.backup.$(date +%Y%m%d)

# 拉取最新代码
cd /opt/binance-exit-executor
git pull origin main

# 更新依赖
uv sync --frozen --no-dev

# 启动服务
sudo systemctl start binance-exit-executor

# 查看日志确认正常
journalctl -u binance-exit-executor -f
```

#### 回滚

如果更新后出现问题：

```bash
# 停止服务
sudo systemctl stop binance-exit-executor

# 恢复备份
cd /opt
sudo rm -rf binance-exit-executor
sudo mv binance-exit-executor.backup.YYYYMMDD binance-exit-executor

# 启动服务
sudo systemctl start binance-exit-executor
```

---

## 安全加固

### 文件权限

```bash
# 配置文件权限（防止普通用户读取 API 密钥）
sudo chmod 600 /etc/binance-exit-executor/binance-exit-executor.env
sudo chown root:root /etc/binance-exit-executor/binance-exit-executor.env

# 配置文件
sudo chmod 644 /etc/binance-exit-executor/config.yaml
sudo chown root:root /etc/binance-exit-executor/config.yaml

# 日志目录
sudo chmod 755 /var/log/binance-exit-executor
```

### API Key 安全

1. **使用子账户**：
   - 创建币安子账户专门用于交易
   - 主账户资金转入适量到子账户
   - API Key 绑定到子账户

2. **权限最小化**：
   - 仅启用"期货交易"权限
   - **不启用**"提现"权限
   - **不启用**"现货交易"权限（如果不需要）

3. **IP 白名单**：
   - 在币安 API 管理中设置 IP 白名单
   - 仅允许服务器 IP 访问

4. **定期轮换**：
   - 每3-6个月轮换 API Key
   - 删除旧的 API Key

### 网络安全

```bash
# 配置防火墙（仅开放必要端口）
sudo ufw allow ssh
sudo ufw enable

# 如果使用 SSH，建议修改默认端口并禁用密码登录
sudo nano /etc/ssh/sshd_config
```

### 备份策略

**配置文件备份**：
```bash
# 每天备份配置文件
sudo crontab -e

# 添加定时任务
0 2 * * * cp /etc/binance-exit-executor/config.yaml /backup/config.yaml.$(date +\%Y\%m\%d)
```

**日志备份**：
```bash
# 每周归档日志
0 3 * * 0 tar -czf /backup/logs-$(date +\%Y\%W).tar.gz /var/log/binance-exit-executor/binance-exit-executor_*.log* /var/log/binance-exit-executor/error_*.log* && find /backup -name "logs-*.tar.gz" -mtime +60 -delete
```

---

## 故障恢复

### 服务无法启动

1. 查看 systemd 日志：
   ```bash
   sudo journalctl -u binance-exit-executor -n 50
   ```

2. 检查配置文件：
   ```bash
   python -c "import yaml; yaml.safe_load(open('/etc/binance-exit-executor/config.yaml'))"
   ```

3. 检查环境变量：
   ```bash
   sudo cat /etc/binance-exit-executor/binance-exit-executor.env
   ```

4. 手动运行测试：
   ```bash
   cd /opt/binance-exit-executor
   ./.venv/bin/python -m src.main /etc/binance-exit-executor/config.yaml
   ```

### 数据不一致

如果怀疑仓位数据不同步：

1. 停止服务
2. 手动调用交易所 API 确认实际仓位
3. 重启服务（会触发一次完整校准）

---

## 多实例部署

如果需要同时运行多个实例（不同配置或不同交易对）：

### 创建多个服务

```bash
# 复制服务文件
sudo cp /etc/systemd/system/binance-exit-executor.service /etc/systemd/system/binance-exit-executor-btc.service

# 编辑服务文件
sudo nano /etc/systemd/system/binance-exit-executor-btc.service
```

修改以下部分：
```ini
[Unit]
Description=binance-exit-executor-btc (BTC only)

[Service]
EnvironmentFile=/etc/binance-exit-executor/binance-exit-executor-btc.env
Environment=VQ_LOG_DIR=/var/log/binance-exit-executor-btc
ExecStart=/opt/binance-exit-executor/.venv/bin/python -m src.main /etc/binance-exit-executor/config-btc.yaml

[Install]
WantedBy=multi-user.target
```

启动多个实例：
```bash
sudo systemctl daemon-reload
sudo systemctl start binance-exit-executor-btc
sudo systemctl enable binance-exit-executor-btc
```

---

## 相关文档

- [配置参数手册](configuration.md)
- [故障排查指南](troubleshooting.md)
- [README](../README.md)

---
