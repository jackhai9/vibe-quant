# 故障排查指南

> vibe-quant 常见问题与解决方案

---

## 目录

1. [启动问题](#启动问题)
2. [连接问题](#连接问题)
3. [API 权限与限速](#api-权限与限速)
4. [订单失败](#订单失败)
5. [WebSocket 断连](#websocket-断连)
6. [配置错误](#配置错误)
7. [日志查看](#日志查看)
8. [性能问题](#性能问题)

---

## 启动问题

### 问题：`环境变量 BINANCE_API_KEY 未设置`

**原因**：未配置必需的币安 API 凭证

**解决方案**：
1. 创建 `.env` 文件（本地开发）或配置 systemd EnvironmentFile（生产环境）
2. 设置以下环境变量：
   ```bash
   BINANCE_API_KEY=your_key_here
   BINANCE_API_SECRET=your_secret_here
   ```
3. 确保环境变量已加载：
   ```bash
   # 本地开发
   source .env  # 或使用 python-dotenv

   # systemd
   sudo systemctl restart vibe-quant
   ```

**参考**：[配置参数手册 - 环境变量](configuration.md#环境变量)

---

### 问题：`配置文件加载失败`

**原因**：配置文件路径错误或格式不合法

**解决方案**：
1. 检查配置文件路径：
   ```bash
   # 本地开发
   python -m src.main config/config.yaml

   # systemd
   ls -l /etc/vibe-quant/config.yaml
   ```

2. 验证 YAML 格式：
   ```bash
   # 使用 Python 验证
   python -c "import yaml; yaml.safe_load(open('config/config.yaml'))"
   ```

3. 检查 pydantic 验证错误（查看日志中的详细错误信息）

**常见格式错误**：
- 缩进错误（YAML 必须使用空格，不能使用 Tab）
- 缺少必需字段
- 类型错误（如字符串写成数字）
- Decimal 类型必须用引号（`"200"` 而非 `200`）

---

### 问题：`环境变量 TELEGRAM_BOT_TOKEN 未设置（telegram.enabled=true）`

**原因**：启用了 Telegram 通知但未配置 Bot Token

**解决方案**：
1. **选项1**：配置 Telegram 凭证
   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
   export TELEGRAM_CHAT_ID="123456789"
   ```

2. **选项2**：禁用 Telegram 通知
   ```yaml
   # config/config.yaml
   global:
     telegram:
       enabled: false
   ```

**获取 Telegram 凭证**：参考 [配置参数手册 - TELEGRAM_BOT_TOKEN](configuration.md#telegram_bot_token)

---

## 连接问题

### 问题：`连接币安 API 超时`

**症状**：
- 启动时长时间无响应
- 日志显示 `TimeoutError` 或 `Connection refused`

**原因**：网络无法访问币安 API（`api.binance.com` / `fapi.binance.com`）

**解决方案**：

#### 1. 检查网络连通性
```bash
# 测试连接
curl -I https://fapi.binance.com/fapi/v1/ping

# 测试 WebSocket
curl -I https://fstream.binance.com
```

#### 2. 配置代理
如果网络受限，需要通过代理访问：

**配置文件方式**（推荐，HTTP/HTTPS 代理）:
```yaml
# config/config.yaml
global:
  proxy: "http://127.0.0.1:7890"
```

**注意**：
- 系统**不会**读取 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量
- 只有 `config.yaml` 中的 `global.proxy` 配置有效

**socks5 代理**（需要额外工具）
```bash
# 安装 proxychains
sudo apt install proxychains4

# 编辑 /etc/proxychains4.conf
socks5 127.0.0.1 7890

# 运行
proxychains4 python -m src.main config/config.yaml
```

#### 3. 使用测试网
如果主网连接有问题，可以先测试使用测试网：
```yaml
global:
  testnet: true
```

**测试网端点**：`testnet.binancefuture.com`

---

### 问题：`WebSocket 连接失败`

**症状**：
- 日志显示 `WebSocket connection failed`
- 频繁重连

**原因**：
- 网络不稳定
- 代理配置错误
- 币安 WebSocket 端点临时故障

**解决方案**：
1. **检查网络**：
   ```bash
   # 测试 WebSocket 端点
   curl -I https://fstream.binance.com
   ```

2. **检查代理**：
   - WebSocket 需要 HTTP/HTTPS 代理支持 CONNECT 方法
   - socks5 代理需要使用 `proxychains` 或类似工具

3. **调整重连参数**：
   ```yaml
   global:
     ws:
       reconnect:
         initial_delay_ms: 2000    # 增加初始延迟
         max_delay_ms: 60000       # 增加最大延迟
   ```

4. **查看重连日志**：
   ```bash
   # 查找重连事件
   grep "WS重连" logs/*.log
   ```

---

## API 权限与限速

### 问题：`API Key 权限不足`

**症状**：
- 订单提交失败，错误码 `-2015`
- 日志显示 `Invalid API-key, IP, or permissions for action`

**原因**：API Key 未启用期货交易权限

**解决方案**：
1. 登录币安官网
2. 账户管理 → API 管理 → 编辑 API Key
3. 启用"期货交易"权限（**启用现货交易权限无效**）
4. 保存后等待 1-2 分钟生效
5. 重启 vibe-quant

**安全提示**：不要启用"提现"权限，降低资金风险

---

### 问题：`API 限速触发`

**症状**：
- 订单提交失败，错误码 `-1003` 或 `-1015`
- 日志显示 `Too many requests` 或 `Rate limit exceeded`

**原因**：超过币安 API 限速阈值

**币安限速规则**（U本位合约）：
- **订单权重限制**：1200/分钟（Order 类）
- **IP 限速**：2400/分钟（Weight 类）
- **单个订单权重**：NEW_ORDER=1, CANCEL_ORDER=1

**解决方案**：

#### 1. 调整内部限速参数
```yaml
global:
  rate_limit:
    max_orders_per_sec: 3    # 降低下单频率（默认5）
    max_cancels_per_sec: 5   # 降低撤单频率（默认8）
```

#### 2. 增加信号节流间隔
```yaml
global:
  execution:
    min_signal_interval_ms: 500  # 增加到500ms（默认200）
```

#### 3. 延长订单 TTL
```yaml
global:
  execution:
    order_ttl_ms: 5000  # 减少撤单重下频率
```

#### 4. 减少并发交易对
- 仅对必要的交易对启用平仓
- 避免同时监控过多 symbol

**监控限速**：
```bash
# 查看限速事件
grep "限速" logs/*.log
```

---

## 订单失败

### 问题：保护止损下单失败，错误码 `-4130`

**典型错误**：`An open stop or take profit order ... is existing.`<br>

**原因**：同侧已经存在 stop/tp 条件单（可能来自 Binance 客户端、手机端或网页端手动设置），交易所会拒绝再创建一张同侧的条件单。<br>

**系统行为（预期）**：检测到外部 stop/tp（`closePosition=true` 或 `reduceOnly=true`）时，会进入“外部接管”，撤销我方保护止损并暂停维护，直到外部单消失。<br>

**补充说明**：外部接管的释放以 REST 校验为准，可能发生在任意一次保护止损同步的 REST 调用中，不一定是 `external_takeover_verify` 触发的那一次。<br>
常见触发点：<br>
- 启动/重连校准后的同步
- 仓位更新或订单更新触发的同步
- 外部 stop/tp 的 WS 事件触发的同步
- 超时检查周期性触发的 `external_takeover_verify` 同步

**排查方法**：
```bash
# 查看外部接管状态变化（set/release/verify）
grep "\\[PROTECTIVE_STOP\\]" logs/*.log | grep "external_takeover" | tail -50

# 如果同侧出现多张外部 stop/tp，会打印摘要告警
grep "\\[PROTECTIVE_STOP\\]" logs/*.log | grep "external_stop_multiple" | tail -20
```

**解决方案**：
1. 在 Binance 客户端/网页端取消同侧的手动 stop/tp 条件单（或保留外部单，让系统继续外部接管）。<br>
2. 若确实希望由本程序维护保护止损：关闭/禁用外部条件单，并确保只有本程序在维护（避免多端同时设置）。<br>
3. （谨慎）临时禁用外部接管逻辑（不推荐在实盘长期使用）：<br>
   ```yaml
   global:
     risk:
       protective_stop:
         external_takeover:
           enabled: false
   ```<br>

### 问题：订单被拒绝，错误码 `-5022`

**典型错误**：`Post Only order will be rejected` / `Due to the order could not be executed as maker ...`<br>

**原因**：`MAKER_ONLY` 模式使用 Post-only（`timeInForce=GTX`）。当你挂出的价格会立即以 taker 方式成交时，交易所会直接拒绝该订单（不会进入订单历史）。<br>

**系统行为（预期）**：该类拒单会打印为 `ORDER_REJECT`（WARNING，`cn=下单被拒`，`reason=post_only_reject`），用于减少噪音与避免重复报错刷屏。<br>

**解决方案**：

#### 1. 增加 Maker 安全距离
```yaml
global:
  execution:
    maker_safety_ticks: 2  # 增加到2（默认1）
```

#### 2. 调整定价模式
```yaml
global:
  execution:
    maker_price_mode: "inside_spread_1tick"  # 更保守的定价
```

#### 3. 针对高波动品种特殊配置
```yaml
symbols:
  "ZEN/USDT:USDT":  # 示例：高波动币种
    execution:
      maker_safety_ticks: 3
      maker_price_mode: "custom_ticks"
      maker_n_ticks: 2
```

**原理**：增加安全距离可以防止订单挂在会立即成交的价格，但会降低成交概率

---

### 问题：订单被拒绝，错误码 `-2021`

**完整错误**：`Order would immediately trigger`

**原因**：STOP 订单的触发价格不合理（如 STOP_MARKET 的 stopPrice 已被触发）

**解决方案**：
1. 检查保护性止损配置：
   ```yaml
   global:
     risk:
       protective_stop:
         dist_to_liq: 0.015  # 增加触发距离
   ```

2. 查看日志中的止损订单参数：
   ```bash
   grep "保护止损" logs/*.log
   ```

3. 确认仓位数据准确（标记价格、强平价格）

---

### 问题：订单数量不符合交易所规则

**错误码**：`-1111`, `-1106`

**原因**：
- 数量小于 `minQty`
- 数量不符合 `stepSize` 精度
- 名义价值小于 `minNotional`

**解决方案**：
1. **系统会自动处理规整**，如果出现此错误，说明仓位已接近完结
2. 检查配置：
   ```yaml
   global:
     execution:
       base_lot_mult: 2  # 增加基础片大小
   ```

3. 查看日志中的仓位信息：
   ```bash
   grep "仓位更新" logs/*.log | tail -10
   ```

---

## WebSocket 断连

### 问题：WebSocket 频繁断线重连

**症状**：
- 日志大量 `WS断开` 和 `WS重连` 事件
- 系统反复进行校准

**原因**：
- 网络不稳定
- 代理不稳定
- 币安服务端重启（每24小时维护）

**解决方案**：

#### 1. 正常情况
币安 WebSocket 每 24 小时会断开一次（维护），系统会自动重连，**这是正常现象**。

#### 2. 异常频繁断线
- 检查网络稳定性：
  ```bash
  ping fstream.binance.com
  ```

- 检查代理稳定性（如果使用代理）

- 调整数据陈旧阈值：
  ```yaml
  global:
    ws:
      stale_data_ms: 3000  # 增加容忍度（默认1500）
  ```

#### 3. 监控重连状态
```bash
# 查看最近重连记录
grep "WS重连" logs/*.log | tail -20

# 统计今天重连次数
grep "WS重连" logs/$(date +%Y-%m-%d).log | wc -l
```

**预期重连次数**：1-3次/天属于正常

---

### 问题：数据陈旧警告

**症状**：
- 日志显示 `数据陈旧，暂停触发`
- 系统不执行平仓

**原因**：超过 `stale_data_ms` 未收到行情更新

**解决方案**：
1. **检查 WebSocket 连接状态**：
   ```bash
   grep "WS连接\|WS断开" logs/*.log | tail -10
   ```

2. **如果 WebSocket 已连接但仍显示陈旧**：
   - 可能是该交易对无交易（极低流动性）
   - 检查币安官网是否有该币对的最近成交

3. **调整陈旧阈值**（针对低流动性品种）：
   ```yaml
   global:
     ws:
       stale_data_ms: 5000  # 增加到5秒
   ```

---

## 配置错误

### 问题：`pydantic validation error`

**症状**：启动时抛出 `ValidationError` 异常

**原因**：配置文件中的值不符合类型要求

**常见错误**：

#### 1. Decimal 类型错误
```yaml
# ❌ 错误
max_order_notional: 200

# ✓ 正确
max_order_notional: 200   # 如果pydantic配置允许，可以不加引号
# 或者
max_order_notional: "200"
```

#### 2. 枚举类型错误
```yaml
# ❌ 错误
maker_price_mode: "invalid_mode"

# ✓ 正确（只能是以下三个值之一）
maker_price_mode: "at_touch"
maker_price_mode: "inside_spread_1tick"
maker_price_mode: "custom_ticks"
```

#### 3. 约束违反
```yaml
# ❌ 错误：maker_safety_ticks 必须 >= 1
maker_safety_ticks: 0

# ✓ 正确
maker_safety_ticks: 1
```

**解决方案**：
1. 查看错误信息中的字段名和约束条件
2. 参考 [配置参数手册](configuration.md) 确认正确格式
3. 查看 `src/config/models.py` 了解字段约束

---

### 问题：Symbol 配置不生效

**症状**：修改了 `symbols.<SYMBOL>` 配置，但运行时仍使用 global 配置

**原因**：
- Symbol 名称格式错误
- 配置路径错误

**解决方案**：
1. **确认 Symbol 格式**：必须使用 ccxt 统一格式
   ```yaml
   # ✓ 正确（注意大小写和冒号）
   symbols:
     "BTC/USDT:USDT":
       ...
     "ETH/USDT:USDT":
       ...

   # ❌ 错误
   symbols:
     "BTCUSDT":  # 缺少斜杠和后缀
       ...
   ```

2. **检查缩进**：YAML 对缩进敏感
   ```yaml
   symbols:
     "BTC/USDT:USDT":
       execution:          # 正确缩进
         order_ttl_ms: 3000
   ```

3. **查看日志确认加载**：
   ```bash
   grep "配置加载" logs/*.log | head -5
   ```

---

## 日志查看

### 日志文件位置

**本地开发**：
- 默认位置：`logs/`（相对于工作目录）
- 文件命名：`YYYY-MM-DD.log`（每天一个文件）

**systemd 部署**：
- 文件日志：`/var/log/vibe-quant/YYYY-MM-DD.log`
- systemd 日志：`journalctl -u vibe-quant`

---

### 常用日志查询命令

#### 实时查看日志
```bash
# 文件日志
tail -f logs/$(date +%Y-%m-%d).log

# systemd 日志
journalctl -u vibe-quant -f
```

#### 查找特定事件
```bash
# 查找错误
grep "错误" logs/*.log

# 查找订单成交
grep "已成交" logs/*.log

# 查找风险触发
grep "风险触发" logs/*.log

# 查找 WebSocket 重连
grep "WS重连" logs/*.log
```

#### 按时间筛选（systemd）
```bash
# 查看最近1小时
journalctl -u vibe-quant --since "1 hour ago"

# 查看今天的日志
journalctl -u vibe-quant --since today

# 查看指定时间范围
journalctl -u vibe-quant --since "2025-12-19 10:00" --until "2025-12-19 12:00"
```

#### 查看启动和关闭记录
```bash
grep "启动\|关闭" logs/*.log
```

---

### 日志级别说明

系统使用 loguru，默认输出以下级别：

| 级别 | 说明 | 示例 |
|------|------|------|
| INFO | 正常运行事件 | 启动、订单成交、信号触发 |
| WARNING | 警告（不影响运行） | 数据陈旧、限速临界 |
| ERROR | 错误（影响功能） | 订单失败、连接异常 |

**查看特定级别**：
```bash
grep "ERROR" logs/*.log
grep "WARNING" logs/*.log
```

---

## 性能问题

### 问题：延迟过高

**症状**：
- 从信号触发到订单提交 > 500ms
- 系统响应缓慢

**原因**：
- 网络延迟
- 系统负载过高
- 同时监控交易对过多

**解决方案**：

#### 1. 检查网络延迟
```bash
# 测试 API 延迟
time curl -X GET "https://fapi.binance.com/fapi/v1/time"
```

#### 2. 减少并发交易对
- 只监控必要的交易对
- 移除流动性极低的品种

#### 3. 优化配置
```yaml
global:
  ws:
    stale_data_ms: 1000  # 减少容忍度（默认1500）
  execution:
    min_signal_interval_ms: 100  # 减少节流间隔
```

#### 4. 检查系统资源
```bash
# CPU 使用率
top -p $(pgrep -f "src.main")

# 内存使用
ps aux | grep "src.main"
```

---

### 问题：内存占用持续增长

**症状**：
- 长时间运行后内存占用持续上升
- 可能导致 OOM

**原因**：
- 潜在内存泄漏
- 滑动窗口数据积累过多

**解决方案**：

#### 1. 定期重启（临时方案）
使用 systemd 定时重启：
```ini
# /etc/systemd/system/vibe-quant-restart.timer
[Unit]
Description=Daily restart of vibe-quant

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

#### 2. 监控内存
```bash
# 查看当前内存
ps aux | grep vibe-quant

# 持续监控
watch -n 5 'ps aux | grep vibe-quant'
```

#### 3. 报告问题
如果确认存在内存泄漏，请提供以下信息：
- 运行时长
- 监控的交易对数量
- 内存增长速率
- 日志文件

---

## 获取帮助

如果以上方案无法解决问题，请：

1. **查看完整日志**：
   ```bash
   # 收集最近的日志
   tail -1000 logs/$(date +%Y-%m-%d).log > debug.log
   ```

2. **提供以下信息**：
   - 系统版本：`python --version`
   - vibe-quant 版本：`git rev-parse HEAD`
   - 配置文件（脱敏后）
   - 错误日志
   - 复现步骤

3. **提交 Issue**：
   - GitHub: `https://github.com/your-repo/vibe-quant/issues`
   - 邮件: `your-email@example.com`

---

## 相关文档

- [配置参数手册](configuration.md)
- [部署指南](deployment.md)
- [README](../README.md)

---

*最后更新: 2025-12-21*
