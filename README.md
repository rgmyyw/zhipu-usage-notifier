# 智谱 AI 用量通知

定时查询智谱 API 用量，通过钉钉机器人和邮件推送通知。

## 功能

- 每小时自动查询（08:00-23:00 整点推送，频率可在仪表盘调整）
- 展示所有阶段额度：5小时滚动 / 7天滚动 / 月度
- 用量百分比 + 剩余量 + 重置倒计时
- 超 80% 自动告警
- 三渠道独立格式化：
  - 钉钉 Markdown（表格 + emoji 进度条）
  - 邮件 HTML（卡片布局 + CSS 进度条）
  - 控制台（纯文本）

## ZCode 重置卡（临期自动使用）

面板里的「可重置额度」重置卡（5 小时卡 / 周卡）过期即作废。本服务会：

- **列出剩余卡片**：仪表盘「🎴 ZCode 重置卡」卡片实时展示两类卡的数量、分组过期时间与倒计时
- **临期自动使用**：距过期不足 `threshold_minutes`（默认 20 分钟）时自动使用一张（同一过期批次只用一张——连续用第二张只是重置刚满的额度，没有收益）
- **触发即通知**：自动/手动使用成功后，立刻通过钉钉 + 邮件推送「重置卡已使用 + 剩余卡片清单」
- **手动使用**：仪表盘提供「使用一张5小时卡 / 周卡」按钮（带确认弹窗）
- **幂等安全**：自动使用带稳定幂等键，与 Mac 上 ZCode 客户端/其他实例并行运行不会重复消耗同一张卡

工作原理：调用 ZCode 官方接口 `zcode.z.ai/api/v1/coding-plan/reset/status|use`（与客户端点「重置」按钮完全相同），凭据来自 ZCode 的 `~/.zcode/v2/credentials.json`（AES-256-GCM 加密，本服务按相同密钥派生规则现场解密，文件更新后自动使用新 token）。

### 凭据准备

在**运行 ZCode 的那台 Mac** 上：

```bash
cp ~/.zcode/v2/credentials.json ./zcode-credentials.json   # 已加入 .gitignore
```

然后编辑 `config.json` 的 `reset_card` 段，把 `credential_home` / `credential_user` 填成那台 Mac 的实际值（默认 `darwin` 平台）。token 为长期会话 JWT，仅在 ZCode 重新登录后轮换——重新登录后再同步一次该文件即可。

也可以跳过文件、直接把两个 token 填进 `zcode_token` / `bigmodel_token`（可用 `~/.zcode/scripts/auto-reset-card.mjs` 里的解密逻辑导出）。

### 配置项

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `reset_card.enabled` | 总开关（false 时仪表盘仍可看，后台线程不启动） | `true` |
| `reset_card.auto_use` | 临期自动使用 | `true` |
| `reset_card.threshold_minutes` | 距过期多少分钟内触发 | `20` |
| `reset_card.check_interval_seconds` | 检查间隔 | `300` |
| `reset_card.notify_on_use` | 使用后推送钉钉/邮件 | `true` |
| `reset_card.credentials_file` | credentials.json 路径（容器内默认 `/app/data/zcode-credentials.json`） | 自动探测 |
| `reset_card.zcode_token` / `bigmodel_token` | 直配 token（优先于文件） | 空 |
| `reset_card.credential_platform/home/user` | 密钥派生所需的源机器信息 | `darwin` / 空 / 空 |

API：`/api/reset-cards`（卡片列表 + 最近触发记录）、`POST /api/reset-cards/use`（手动使用，body `{"type":"FIVE_HOUR"|"WEEK"}`）

## Web 仪表盘

访问 `http://<主机>:8060/`（内网无鉴权）：

- 用量卡片：环形仪表 + 进度条（50%/80% 告警线，≥80% 闪烁动画）+ 分模型明细 + 重置倒计时
- 重置时刻表：各窗口重置时间、实时倒计时、用量横条
- 近 24 小时趋势图（每 10 分钟采样，存 `data/history.json`）
- 耗尽预测：按近期采样速率推算"多久耗尽 / 能否撑到重置"（需接口返回绝对值）
- 推送频率设置：关闭 / 30 分钟 / 1 / 2 / 3 / 6 / 12 小时，保存后 30 秒内生效（存 `data/settings.json`）
- 立即推送 / 复制文本报告按钮

API：`/api/usage`、`/api/history`、`/api/settings`(GET/POST)、`/api/report`、`/api/reset-cards`(GET)、`/api/reset-cards/use`(POST)、`/healthz`

## 快速开始

### 1. 配置

```bash
cp config.example.json config.json
cp ~/.zcode/v2/credentials.json ./zcode-credentials.json   # 需要重置卡功能时
```

编辑 `config.json`：

```json
{
  "api_key": "your_api_key",
  "endpoint": "intl",

  "dingtalk": {
    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "secret": ""
  },

  "email": {
    "smtp_host": "smtp.qq.com",
    "smtp_port": 587,
    "smtp_user": "your_email@qq.com",
    "smtp_pass": "authorization_code",
    "from": "your_email@qq.com",
    "to": ["receiver@example.com"],
    "use_tls": true
  }
}
```

| 字段 | 说明 |
|------|------|
| `api_key` | 智谱 API Key（也可用环境变量 `ZHIPUAI_API_KEY`） |
| `endpoint` | `intl`（国际 `api.z.ai`）或 `cn`（国内 `open.bigmodel.cn`） |
| `dingtalk.webhook_url` | 钉钉机器人 Webhook 地址 |
| `dingtalk.secret` | 钉钉签名密钥（如启用加签） |
| `email.*` | SMTP 邮件配置 |

### 2. Docker 部署

```bash
docker-compose up -d
```

### 3. 本地运行

```bash
pip install -r requirements.txt   # 纯 stdlib，实际无需安装
python main.py --once             # 单次运行测试
python main.py                    # 持续运行（按调度推送）
```

## 定时调度

| 时段 | 行为 |
|------|------|
| 08:00 - 23:00 | 按仪表盘设置的频率推送（默认每小时，网格对齐） |
| 23:00 后 | 静默，等待次日 08:00 |
| 示例（1 小时档） | `08:00` `09:00` `10:00` ... `23:00` |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ZHIPUAI_API_KEY` | API Key（优先级高于 config.json） | - |
| `CONFIG_PATH` | 配置文件路径 | `./config.json` |
| `TZ` | 时区 | `Asia/Shanghai` |

## 贡献

```bash
# 克隆后先复制配置
cp config.example.json config.json
# config.json 已被 .gitignore 忽略，不会误提交
```
