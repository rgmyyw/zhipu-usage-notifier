import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

if sys.stdout.encoding and sys.stdout.encoding.upper() != "UTF-8":
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("zhipu-usage")

_SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", str(_SCRIPT_DIR / "config.json")))

QUOTA_ENDPOINTS = {
    "intl": "https://api.z.ai/api/monitor/usage/quota/limit",
    "cn": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
}


def load_config() -> dict:
    fallback = CONFIG_PATH.with_name("config.example.json")
    path = CONFIG_PATH if CONFIG_PATH.exists() else fallback
    if not path.exists():
        log.error("No config file found (tried %s and %s)", CONFIG_PATH, fallback)
        sys.exit(1)
    log.info("Loading config from %s", path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_quota(api_key: str, endpoint: str) -> Optional[dict]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "ZhipuUsageNotifier/1.0",
    }
    req = Request(endpoint, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("success") and data.get("code") == 200:
            return data["data"]
        log.error("API error: %s", data.get("msg", "Unknown error"))
        return None
    except URLError as e:
        log.error("Request failed: %s", e)
        return None


def format_tokens(val: int) -> str:
    if val >= 1_000_000:
        return f"{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"{val / 1_000:.1f}K"
    return str(val)


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "已重置"
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    parts = []
    if h > 0:
        parts.append(f"{h}小时")
    if m > 0:
        parts.append(f"{m}分")
    if s > 0 or not parts:
        parts.append(f"{s}秒")
    return "".join(parts)


TYPE_META = {
    "TOKENS_LIMIT": {"icon": "\U0001f504", "label": "Token 限额", "unit": "tokens"},
    "TIME_LIMIT":  {"icon": "\U0001f50d", "label": "搜索额度",   "unit": "次"},
    "WEEKLY_LIMIT": {"icon": "\U0001f4c5", "label": "周限额",     "unit": "tokens"},
    "MONTHLY_LIMIT": {"icon": "\U0001f4c8", "label": "月限额",    "unit": "tokens"},
}


PERIOD_MAP = {
    (3, 5): "5小时滚动",
    (6, 7): "7天滚动",
    (6, 1): "7天滚动",
    (5, 1): "月度",
    (5, None): "月度",
}


def _infer_period(lim: dict) -> str:
    u = lim.get("unit")
    n = lim.get("number")
    if n is not None:
        n = int(n)
    key = (u, n)
    p = PERIOD_MAP.get(key) or PERIOD_MAP.get((u, None))
    if p:
        return p
    if n:
        return f"{n}天滚动" if n > 1 else "月度"
    if u:
        return f"周期{u}"
    return ""


def _parse_limits(data: dict):
    raw = data.get("data", data)
    limits = raw.get("limits", [])
    level = raw.get("level", "")
    return level, limits


def _bar_emoji(pct: int, width: int = 10) -> str:
    remain = max(0, 100 - pct)
    filled = int(remain / 100 * width)
    filled = max(0, min(width, filled))
    if pct >= 80:
        fill_ch = "\U0001f7e5"
    elif pct >= 50:
        fill_ch = "\U0001f7e8"
    else:
        fill_ch = "\U0001f7e9"
    empty_ch = "\u2b1c"
    return fill_ch * filled + empty_ch * (width - filled)


def _bar_html(val: float, max_val: float) -> str:
    pct = (val / max_val * 100) if max_val > 0 else 0
    filled = max(0, min(100, pct))
    color = "#ef4444" if filled >= 80 else "#f59e0b" if filled >= 50 else "#22c55e"
    return (
        f'<div style="height:20px;width:100%;background:#e5e7eb;border-radius:10px;overflow:hidden;margin:4px 0">'
        f'<div style="height:100%;width:{filled}%;background:{color};border-radius:10px"></div>'
        f'</div>'
    )


def _bar_double_html(used: float, total: float, remaining: float) -> str:
    total_w = 100
    used_pct = (used / total * 100) if total > 0 else 0
    rem_pct = (remaining / total * 100) if total > 0 else 0
    used_color = "#ef4444" if used_pct >= 80 else "#f59e0b" if used_pct >= 50 else "#22c55e"
    rem_color = "#e5e7eb"
    return (
        f'<div style="height:20px;width:100%;background:{rem_color};border-radius:10px;overflow:hidden;margin:4px 0">'
        f'<div style="height:100%;width:{used_pct}%;background:{used_color};border-radius:10px"></div>'
        f'</div>'
    )


def _reset_str(reset_ms: int) -> str:
    if not reset_ms:
        return "无"
    remaining_sec = max(0, int(reset_ms / 1000 - time.time()))
    reset_dt = datetime.fromtimestamp(reset_ms / 1000, tz=timezone.utc) + timedelta(hours=8)
    return f"{reset_dt.strftime('%m/%d %H:%M')}（{format_duration(remaining_sec)}后）"


def _status_tag(pct: int) -> str:
    if pct >= 80:
        return "\U0001f534 紧张"
    if pct >= 50:
        return "\U0001f7e0 注意"
    if pct >= 20:
        return "\U0001f7e1 良好"
    return "\U0001f7e2 充裕"


def _limit_detail_block(lim: dict) -> dict:
    t = lim.get("type", "")
    meta = TYPE_META.get(t, {"icon": "\U0001f4ca", "label": t, "unit": ""})
    pct = lim.get("percentage", 0)
    used = lim.get("currentValue") or 0
    total = lim.get("usage") or 0
    remaining = lim.get("remaining") if lim.get("remaining") is not None else max(0, total - used)
    reset_ms = lim.get("nextResetTime")
    details = lim.get("usageDetails")
    period = _infer_period(lim)
    has_total = total > 0
    has_remaining = remaining > 0
    return {
        "icon": meta["icon"],
        "label": meta["label"],
        "period": period,
        "unit": meta["unit"],
        "pct": pct,
        "used": used,
        "total": total,
        "remaining": remaining,
        "reset_ms": reset_ms,
        "details": details,
        "tag": f"{t}-{period}" if period else t,
        "has_total": has_total,
        "has_remaining": has_remaining,
        "visible": True,
    }


def _overall_status(limits: list) -> str:
    if not limits:
        return "\U000026ab 未知"
    max_pct = max(l.get("percentage", 0) for l in limits)
    if max_pct >= 80:
        return "\U0001f534 用量紧张"
    if max_pct >= 50:
        return "\U0001f7e0 用量偏高"
    if max_pct >= 20:
        return "\U0001f7e1 用量正常"
    return "\U0001f7e2 用量充裕"


# ── DingTalk Markdown ──────────────────────────────────────────────

def build_dingtalk(data: dict, api_key: str) -> str:
    level, limits = _parse_limits(data)
    masked = api_key[:6] + "****" + api_key[-4:] if len(api_key) > 10 else "****"
    now = datetime.now(timezone(timedelta(hours=8)))

    blocks = [b for b in [_limit_detail_block(l) for l in limits] if b["visible"]]
    status = _overall_status(limits)

    lines = []
    lines.append("# \U0001f916 智谱 AI 用量报告")
    lines.append("")
    lines.append(f"> **账号** `{masked}`　　**套餐** `{level.upper() if level else '-'}`")
    lines.append(f"> **整体状态** {status}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for b in blocks:
        pct = b["pct"]
        hdr = f"{b['icon']} {b['label']}"
        if b["period"]:
            hdr += f"（{b['period']}）"
        lines.append(f"## {hdr}　{_status_tag(pct)}")
        lines.append("")
        lines.append(f"{_bar_emoji(pct)}　**剩余 {100 - pct}%**（已用 {pct}%）")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        if b["unit"] == "tokens":
            if b["has_total"]:
                lines.append(f"| 已消耗 | `{format_tokens(b['used'])}` |")
                lines.append(f"| 限额 | `{format_tokens(b['total'])}` |")
            lines.append(f"| 剩余 | **`{format_tokens(b['remaining'])}`** |")
        else:
            if b["has_total"]:
                lines.append(f"| 已消耗 | `{b['used']}` |")
                lines.append(f"| 限额 | `{b['total']}` |")
            lines.append(f"| 剩余 | **`{b['remaining']}`** |")
        if b["period"]:
            lines.append(f"| 周期 | {b['period']} |")
        lines.append(f"| 重置时间 | {_reset_str(b['reset_ms'])} |")
        lines.append("")

        if b["details"]:
            lines.append("**分模型明细：**")
            for d in b["details"]:
                code = d.get("modelCode", "?")
                u = d.get("usage", 0)
                lines.append(f"- `{code}`　{u} {b['unit'] if b['unit'] != 'tokens' else 'tokens'} {_bar_emoji(u / max(b['total'], 1) * 100, 6)}")
            lines.append("")

        if pct >= 80:
            lines.append(f"> \u26a0\ufe0f **该额度已使用 {pct}%，即将达到上限，请注意控制！**")
            lines.append("")

    lines.append("---")
    lines.append(f"\n*报告生成时间：{now.strftime('%Y-%m-%d %H:%M:%S')}*")
    return "\n".join(lines)


# ── Email HTML ─────────────────────────────────────────────────────

def build_email_html(data: dict, api_key: str) -> str:
    level, limits = _parse_limits(data)
    masked = api_key[:6] + "****" + api_key[-4:] if len(api_key) > 10 else "****"
    now = datetime.now(timezone(timedelta(hours=8)))
    blocks = [b for b in [_limit_detail_block(l) for l in limits] if b["visible"]]
    email_status = _overall_status(limits)

    body_rows = ""
    for b in blocks:
        detail_rows = ""
        if b["details"]:
            items = "".join(
                f'<tr><td style="padding:1px 12px;color:#6b7280;font-size:12px">{d.get("modelCode","?")}</td>'
                f'<td style="text-align:right;padding:1px 12px;font-size:12px">{d.get("usage",0)}</td>'
                f'<td style="padding:1px 0;width:80px">'
                f'<div style="height:10px;width:60px;background:#e5e7eb;border-radius:5px;overflow:hidden;display:inline-block">'
                f'<div style="height:100%;width:{min(100, d.get("usage",0)/max(b["total"],1)*100)}%;background:#6366f1;border-radius:5px"></div>'
                f'</div></td></tr>'
                for d in b["details"]
            )
            detail_rows = (
                '<tr><td colspan="3" style="padding:6px 0 2px;font-size:12px;color:#374151;font-weight:500">分模型明细</td></tr>'
                f'<tr><th style="text-align:left;padding:1px 12px;color:#9ca3af;font-size:11px;font-weight:400">模型</th>'
                f'<th style="text-align:right;padding:1px 12px;color:#9ca3af;font-size:11px;font-weight:400">用量</th>'
                f'<th style="width:80px"></th></tr>'
                f'{items}'
            )

        label = f"{b['icon']} {b['label']}"
        if b["period"]:
            label += f" · {b['period']}"
        status = _status_tag(b["pct"])
        total_str = format_tokens(b["total"]) if b["unit"] == "tokens" else str(b["total"])
        used_str = format_tokens(b["used"]) if b["unit"] == "tokens" else str(b["used"])
        rem_str = format_tokens(b["remaining"]) if b["unit"] == "tokens" else str(b["remaining"])

        usage_line = ""
        if b["has_total"]:
            usage_line = f"""
            <tr><td style="padding:2px 16px;color:#6b7280;font-size:13px">已用 / 总量</td>
              <td style="text-align:right;padding:2px 8px;font-size:13px;font-weight:500">{used_str}</td>
              <td style="text-align:right;padding:2px 16px;font-size:13px;color:#9ca3af">/ {total_str}</td>
            </tr>"""
        else:
            usage_line = f"""
            <tr><td style="padding:2px 16px;color:#6b7280;font-size:13px">使用率</td>
              <td colspan="2" style="text-align:right;padding:2px 16px;font-size:13px;font-weight:500">{b['pct']}%</td>
            </tr>"""

        body_rows += f"""
        <tr><td colspan="3" style="padding:14px 16px 4px;font-size:15px;font-weight:600;color:#1f2937">{label}　{status}</td></tr>
        <tr><td colspan="3" style="padding:2px 16px">{_bar_html(b['used'], b['total'])}</td></tr>
        {usage_line}
        <tr>
          <td style="padding:2px 16px;color:#6b7280;font-size:13px">剩余</td>
          <td style="text-align:right;padding:2px 8px;font-size:14px;font-weight:700;color:#059669">{rem_str}</td>
          <td style="text-align:right;padding:2px 16px;font-size:12px;color:#6b7280">{100 - b['pct']}%</td>
        </tr>
        <tr><td style="padding:2px 16px;color:#6b7280;font-size:13px">周期</td><td colspan="2" style="padding:2px 16px;font-size:13px">{b['period']}</td></tr>
        <tr><td style="padding:2px 16px;color:#6b7280;font-size:13px">重置时间</td><td colspan="2" style="padding:2px 16px;font-size:13px">{_reset_str(b['reset_ms'])}</td></tr>
        {detail_rows}
        <tr><td colspan="3" style="padding:2px 16px">{'' if b == blocks[-1] else '<hr style="border:none;border-top:1px solid #e5e7eb;margin:6px 0">'}</td></tr>"""

    warn_html = ""
    warn_limits = [b for b in blocks if b["pct"] >= 80]
    if warn_limits:
        items = "、".join(f"<b>{b['label']}</b>（{b['pct']}%）" for b in warn_limits)
        warn_html = f'''
        <tr><td colspan="3" style="padding:10px 16px;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;color:#dc2626;font-weight:600;text-align:center;font-size:14px">
            \u26a0\ufe0f 以下额度即将用尽：{items}
        </td></tr>
        <tr><td colspan="3" style="padding:4px"></td></tr>'''

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:20px;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 12px rgba(0,0,0,.08)">
  <div style="padding:24px 28px;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h1 style="margin:0;font-size:20px;font-weight:700">\U0001f916 智谱 AI 用量报告</h1>
      <span style="font-size:13px;background:rgba(255,255,255,.2);padding:4px 12px;border-radius:20px">{email_status}</span>
    </div>
    <p style="margin:8px 0 0;opacity:.8;font-size:13px">
      {masked} · {level.upper() if level else '-'} 套餐 · {now.strftime('%Y-%m-%d %H:%M')}
    </p>
  </div>
  <table style="width:100%;border-collapse:collapse">
    <tbody>
      {warn_html}
      {body_rows}
    </tbody>
  </table>
  <div style="padding:14px 28px;background:#f9fafb;border-top:1px solid #e5e7eb;font-size:11px;color:#9ca3af;text-align:center">
    下次自动更新将在 1 小时后 · <a href="https://bigmodel.cn" style="color:#6366f1;text-decoration:none">智谱 AI 控制台</a>
  </div>
</div></body></html>"""


# ── Console Text ───────────────────────────────────────────────────

def build_console(data: dict, api_key: str) -> str:
    level, limits = _parse_limits(data)
    masked = api_key[:6] + "****" + api_key[-4:] if len(api_key) > 10 else "****"
    now = datetime.now(timezone(timedelta(hours=8)))

    def row(label, val):
        return f"  {label:<14} {val}"

    lines = []
    lines.append("=" * 48)
    lines.append(f"  {_overall_status(limits)} · 智谱 AI 用量报告")
    lines.append("=" * 48)
    lines.append(row("账号", masked))
    lines.append(row("套餐", level.upper() if level else "Unknown"))
    lines.append(row("时间", now.strftime('%Y-%m-%d %H:%M:%S')))
    lines.append("")

    blocks = [b for b in [_limit_detail_block(l) for l in limits] if b["visible"]]
    for i, b in enumerate(blocks):
        status = _status_tag(b["pct"])
        total_str = format_tokens(b["total"]) if b["unit"] == "tokens" else str(b["total"])
        used_str = format_tokens(b["used"]) if b["unit"] == "tokens" else str(b["used"])
        rem_str = format_tokens(b["remaining"]) if b["unit"] == "tokens" else str(b["remaining"])

        n = len(blocks)
        lbl = b["label"]
        if b["period"]:
            lbl += f"·{b['period']}"
        lines.append(f"  [{i+1}/{n}] {b['icon']} {lbl}  {status}")
        lines.append(f"  {_bar_emoji(b['pct'])}  剩余 {100-b['pct']}%  ·  已用 {b['pct']}%")
        if b["has_total"]:
            lines.append(row("已用", f"{used_str} / {total_str}"))
        lines.append(row("剩余", rem_str))
        if b["period"]:
            lines.append(row("周期", b["period"]))
        lines.append(row("重置", _reset_str(b["reset_ms"])))

        if b["details"]:
            detail_parts = []
            for d in b["details"]:
                code = d.get("modelCode", "?")
                u = d.get("usage", 0)
                detail_parts.append(f"{code}({u})")
            lines.append(row("明细", "  ".join(detail_parts)))

        lines.append("")

    warn_blocks = [b for b in blocks if b["pct"] >= 80]
    if warn_blocks:
        names = "、".join(b["label"] for b in warn_blocks)
        lines.append(f"  \u26a0\ufe0f 警告：{names} 用量已达 80% 以上，请注意控制！")
        lines.append("")

    lines.append("=" * 48)
    return "\n".join(lines)


def send_dingtalk(webhook_url: str, msg: str, secret: Optional[str] = None):
    import hashlib
    import base64
    import hmac

    if secret:
        timestamp = str(round(time.time() * 1000))
        sign_string = f"{timestamp}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = base64.b64encode(digest).decode("utf-8")
        url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"
    else:
        url = webhook_url

    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": "智谱 AI 用量通知",
            "text": msg,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if result.get("errcode") != 0:
            log.error("DingTalk send failed: %s", result.get("errmsg", ""))
        else:
            log.info("DingTalk notification sent")
    except URLError as e:
        log.error("DingTalk request failed: %s", e)


def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    from_addr: str,
    to_addrs: list[str],
    msg_html: str,
    use_tls: bool = True,
):
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header("智谱用量通知", "utf-8")), from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = f"智谱 AI 用量报告 ({datetime.now().strftime('%m/%d %H:%M')})"
    msg.attach(MIMEText(msg_html, "html", "utf-8"))

    try:
        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        server.login(smtp_user, smtp_pass)
        server.sendmail(from_addr, to_addrs, msg.as_string())
        server.quit()
        log.info("Email notification sent to %s", ", ".join(to_addrs))
    except Exception as e:
        log.error("Email send failed: %s", e)


def notify(config: dict, data: dict, api_key: str):
    dingtalk_cfg = config.get("dingtalk")
    if dingtalk_cfg and dingtalk_cfg.get("webhook_url"):
        send_dingtalk(
            dingtalk_cfg["webhook_url"],
            build_dingtalk(data, api_key),
            dingtalk_cfg.get("secret"),
        )

    email_cfg = config.get("email")
    if email_cfg and email_cfg.get("smtp_host") and email_cfg.get("to"):
        send_email(
            smtp_host=email_cfg["smtp_host"],
            smtp_port=email_cfg.get("smtp_port", 587),
            smtp_user=email_cfg["smtp_user"],
            smtp_pass=email_cfg["smtp_pass"],
            from_addr=email_cfg.get("from", email_cfg["smtp_user"]),
            to_addrs=email_cfg["to"] if isinstance(email_cfg["to"], list) else [email_cfg["to"]],
            msg_html=build_email_html(data, api_key),
            use_tls=email_cfg.get("use_tls", True),
        )


def main():
    config = load_config()
    api_key = config.get("api_key") or os.environ.get("ZHIPUAI_API_KEY")
    if not api_key:
        log.error("No API key found. Set in config.json or ZHIPUAI_API_KEY env var")
        sys.exit(1)

    endpoint_key = config.get("endpoint", "intl")
    endpoint = QUOTA_ENDPOINTS.get(endpoint_key)
    if not endpoint:
        log.error("Invalid endpoint '%s'. Choose from: %s", endpoint_key, ", ".join(QUOTA_ENDPOINTS.keys()))
        sys.exit(1)

    log.info("Fetching quota from %s ...", endpoint_key)
    data = fetch_quota(api_key, endpoint)
    if not data:
        log.error("Failed to fetch quota data")
        sys.exit(1)

    console = build_console(data, api_key)
    print(console)

    notify(config, data, api_key)


def next_push_time() -> float:
    now = datetime.now(timezone(timedelta(hours=8)))
    minute = now.minute
    if minute < 30:
        next_minute = 30
    else:
        now += timedelta(hours=1)
        next_minute = 0
    candidate = now.replace(minute=next_minute, second=0, microsecond=0)
    hour = candidate.hour
    if hour < 8:
        candidate = candidate.replace(hour=8, minute=0, second=0, microsecond=0)
    elif hour > 23 or (hour == 23 and next_minute == 30):
        candidate = (candidate + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return candidate.timestamp()


def run_loop():
    log.info("Starting ZhipuAI usage notifier (schedule: 08:00-23:00 every 30min)")
    while True:
        target = next_push_time()
        now_ts = time.time()
        wait = target - now_ts
        if wait > 0:
            h, r = divmod(int(wait), 3600)
            m, s = divmod(r, 60)
            log.info("Next push at %s (%d小时%d分%d秒后)",
                     datetime.fromtimestamp(target, tz=timezone(timedelta(hours=8))).strftime('%H:%M'),
                     h, m, s)
            time.sleep(wait)
        try:
            main()
        except Exception as e:
            log.error("Unexpected error: %s", e)


if __name__ == "__main__":
    if "--once" in sys.argv:
        main()
    else:
        run_loop()
