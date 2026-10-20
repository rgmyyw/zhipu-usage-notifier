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

## Web 仪表盘

访问 `http://<主机>:8060/`（内网无鉴权）：

- 用量卡片：环形仪表 + 进度条（50%/80% 告警线，≥80% 闪烁动画）+ 分模型明细 + 重置倒计时
- 重置时刻表：各窗口重置时间、实时倒计时、用量横条
- 近 24 小时趋势图（每 10 分钟采样，存 `data/history.json`）
- 耗尽预测：按近期采样速率推算"多久耗尽 / 能否撑到重置"（需接口返回绝对值）
- 推送频率设置：关闭 / 30 分钟 / 1 / 2 / 3 / 6 / 12 小时，保存后 30 秒内生效（存 `data/settings.json`）
- 立即推送 / 复制文本报告按钮

API：`/api/usage`、`/api/history`、`/api/settings`(GET/POST)、`/api/report`、`/healthz`

## 快速开始

### 1. 配置

```bash
cp config.example.json config.json
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
