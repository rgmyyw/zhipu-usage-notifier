import json
import logging
import os
import smtplib
import sys
import threading
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
_DATA_DIR = Path(os.environ.get("DATA_DIR", str(_SCRIPT_DIR / "data")))
SETTINGS_PATH = _DATA_DIR / "settings.json"
HISTORY_PATH = _DATA_DIR / "history.json"

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


# ── 推送设置与调度(频率可在仪表盘调整) ─────────────────────────────

ALLOWED_INTERVALS = [0, 30, 60, 120, 180, 360, 720]
WINDOW_START_HOUR = 8
WINDOW_END_HOUR = 23


def load_settings() -> dict:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        iv = int(raw.get("interval_minutes", 60))
    except Exception:
        iv = 60
    if iv not in ALLOWED_INTERVALS:
        iv = 60
    return {"interval_minutes": iv}


def save_settings(interval_minutes: int) -> dict:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"interval_minutes": interval_minutes}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return {"interval_minutes": interval_minutes}


def next_push_time(interval_min: int) -> Optional[float]:
    """下一个推送时刻:按 interval 分钟网格对齐,推送窗口 08:00-23:00(23:00 为当日最后一次)"""
    if interval_min <= 0:
        return None
    tz8 = timezone(timedelta(hours=8))
    now = datetime.now(tz8)
    step = timedelta(minutes=interval_min)
    cur = now
    for _ in range(3):
        midnight = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        candidate = midnight
        while candidate <= cur:
            candidate += step
        if candidate.hour < WINDOW_START_HOUR:
            candidate = candidate.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
        elif candidate.hour > WINDOW_END_HOUR or (candidate.hour == WINDOW_END_HOUR and candidate.minute > 0):
            cur = (candidate + timedelta(days=1)).replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
            continue
        return candidate.timestamp()
    return (datetime.now(tz8) + timedelta(days=1)).timestamp()


def _log_next(target: Optional[float], interval_min: int):
    if target is None:
        log.info("Push disabled (interval=0)")
    else:
        tz8 = timezone(timedelta(hours=8))
        dt = datetime.fromtimestamp(target, tz=tz8)
        wait = max(0, target - time.time())
        h, r = divmod(int(wait), 3600)
        m, s = divmod(r, 60)
        log.info("Push interval %dmin; next push at %s (%d小时%d分%d秒后)",
                 interval_min, dt.strftime('%m/%d %H:%M'), h, m, s)


def _push_once():
    try:
        main()
        return True
    except Exception as e:
        log.error("Push failed: %s", e)
        return False


# ── 数据缓存与历史采样(趋势/耗尽预测数据源) ────────────────────────

_fetch_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}
_history_lock = threading.Lock()
HISTORY_MAX = 2016  # 每 10 分钟一条 ≈ 两周


def _cfg():
    config = load_config()
    api_key = config.get("api_key") or os.environ.get("ZHIPUAI_API_KEY")
    endpoint = QUOTA_ENDPOINTS.get(config.get("endpoint", "intl"))
    return config, api_key, endpoint


def cached_fetch(ttl: float = 120.0) -> Optional[dict]:
    with _fetch_lock:
        if _cache["data"] is not None and time.time() - _cache["ts"] < ttl:
            return _cache["data"]
    _, api_key, endpoint = _cfg()
    data = fetch_quota(api_key, endpoint) if api_key and endpoint else None
    if data is not None:
        with _fetch_lock:
            _cache["ts"], _cache["data"] = time.time(), data
    return data


def load_history() -> list:
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except Exception:
        return []


def _append_history(data: dict):
    _, limits = _parse_limits(data)
    sample = {"ts": int(time.time()), "blocks": {}}
    for lim in limits:
        b = _limit_detail_block(lim)
        if b["visible"]:
            sample["blocks"][b["tag"]] = {
                "pct": b["pct"], "used": b["used"], "total": b["total"], "remaining": b["remaining"],
            }
    with _history_lock:
        history = load_history()
        history.append(sample)
        history = history[-HISTORY_MAX:]
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HISTORY_PATH)


def sampler_loop():
    # 每 10 分钟采样一次,仅记录历史(趋势图/耗尽预测),不推送
    while True:
        data = cached_fetch(ttl=0)
        if data is not None:
            try:
                _append_history(data)
            except Exception as e:
                log.error("History append failed: %s", e)
        time.sleep(600)


# ── 仪表盘数据组装 ─────────────────────────────────────────────────

_PERIOD_WINDOW_HOURS = {"5小时滚动": 5, "7天滚动": 168}


def usage_payload() -> Optional[dict]:
    data = cached_fetch()
    if data is None:
        return None
    level, limits = _parse_limits(data)
    blocks = [b for b in (_limit_detail_block(l) for l in limits) if b["visible"]]
    now = time.time()
    return {
        "updated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "status": _overall_status(limits),
        "max_pct": max((b["pct"] for b in blocks), default=0),
        "interval_minutes": load_settings()["interval_minutes"],
        "blocks": [
            {
                "tag": b["tag"], "icon": b["icon"], "label": b["label"], "period": b["period"],
                "window_hours": _PERIOD_WINDOW_HOURS.get(b["period"]),
                "unit": b["unit"], "pct": b["pct"], "used": b["used"], "total": b["total"],
                "remaining": b["remaining"], "has_total": b["total"] > 0, "has_remaining": b["remaining"] > 0,
                "reset_at": (b["reset_ms"] / 1000) if b["reset_ms"] else None,
                "reset_in": max(0, int((b["reset_ms"] or 0) / 1000 - now)),
                "details": [
                    {"model": d.get("modelCode", "?"), "usage": d.get("usage", 0)}
                    for d in (b["details"] or [])
                ],
            }
            for b in blocks
        ],
    }


def report_payload() -> Optional[dict]:
    data = cached_fetch()
    if data is None:
        return None
    _, api_key, _ = _cfg()
    return {"text": build_console(data, api_key)}


# ── Web 仪表盘(HTTP,纯 stdlib) ────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智谱 AI 用量仪表盘</title>
<style>
:root{--bg:#0b1220;--card:#141d31;--line:#2a3a55;--txt:#e6edf7;--muted:#8fa3bf;
--green:#22c55e;--amber:#f59e0b;--red:#ef4444;--accent:#38bdf8}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:radial-gradient(1200px 600px at 80% -10%,#16233d,var(--bg));color:var(--txt);
font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:12px}
h1{font-size:19px;margin:0}
.pill{padding:2px 10px;border-radius:999px;background:var(--card);border:1px solid var(--line);font-size:12px;color:var(--muted)}
.muted{color:var(--muted);font-size:12px}
button{background:var(--accent);color:#06283d;border:0;border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer}
button:active{transform:translateY(1px)}
button.ghost{background:var(--card);color:var(--txt);border:1px solid var(--line)}
select{background:#0c1526;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px}
#banner{display:none;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.45);color:#fca5a5;
border-radius:10px;padding:8px 14px;margin-bottom:12px;font-size:13px;animation:blink 2s infinite}
@keyframes blink{50%{border-color:rgba(239,68,68,.2)}}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.card h2{font-size:14px;margin:0 0 10px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.top{display:flex;gap:14px;align-items:center}
.ring{position:relative;width:74px;height:74px;flex:none}
.ring svg{transform:rotate(-90deg)}
.ring .v{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px}
.big{font-size:24px;font-weight:700;margin-left:auto;text-align:right}
.big small{display:block;font-size:11px;color:var(--muted);font-weight:400}
.bar{position:relative;height:12px;border-radius:7px;background:#0a1120;overflow:hidden;margin:10px 0 2px}
.bar>i{display:block;height:100%;border-radius:7px;transition:width .5s}
.bar>i.crit{background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.18) 0 8px,transparent 8px 16px);animation:slide 1.2s linear infinite}
@keyframes slide{to{background-position:32px 0}}
.mark{position:absolute;top:-3px;width:1px;height:18px;background:#5b7194}
.kv{display:flex;justify-content:space-between;font-size:12.5px;color:var(--muted);margin:3px 0}
.kv b{color:var(--txt);font-weight:600}
.sub{margin-top:10px;border-top:1px dashed var(--line);padding-top:8px;font-size:12px;color:var(--muted)}
.mrow{display:grid;grid-template-columns:minmax(70px,120px) 1fr 64px;gap:8px;align-items:center;margin:4px 0}
.mrow span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mbar{height:6px;border-radius:4px;background:#0a1120;overflow:hidden}
.mbar>i{display:block;height:100%}
.spark{width:100%;height:36px;display:block;margin-top:8px}
.eta{margin-top:8px;padding:6px 10px;border-radius:8px;font-size:12.5px;background:#0a1120;border:1px solid var(--line)}
.tbar{height:5px;border-radius:3px;background:#0a1120;overflow:hidden;margin:4px 0 2px}
.tbar>i{display:block;height:100%;background:#64748b}
.rst{display:grid;grid-template-columns:minmax(96px,150px) 1fr 130px;gap:10px;align-items:center;margin:8px 0;font-size:12.5px}
.rst .cd{font-weight:700;color:var(--txt)}
svg text{fill:var(--muted);font-size:10px}
.legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.setbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
#msg{font-size:12px;color:var(--muted)}
footer{text-align:center;color:var(--muted);font-size:12px;margin-top:18px}
@media(max-width:560px){.big{font-size:19px}.rst{grid-template-columns:1fr 90px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>📊 智谱 AI 用量仪表盘</h1>
  <span class="pill" id="status">加载中…</span>
  <span class="muted" id="updated"></span>
  <span style="flex:1"></span>
  <button class="ghost" onclick="loadUsage()">刷新</button>
</header>

<div id="banner"></div>

<div class="card" style="margin-bottom:12px">
  <div class="setbar">
    <b>🔔 推送频率</b>
    <select id="interval">
      <option value="0">关闭推送</option>
      <option value="30">每 30 分钟</option>
      <option value="60">每 1 小时</option>
      <option value="120">每 2 小时</option>
      <option value="180">每 3 小时</option>
      <option value="360">每 6 小时</option>
      <option value="720">每 12 小时</option>
    </select>
    <button onclick="saveInterval(this)">保存</button>
    <button class="ghost" onclick="pushNow(this)">立即推送</button>
    <button class="ghost" onclick="copyReport(this)">复制文本报告</button>
    <span id="msg"></span>
  </div>
  <div class="muted">推送窗口 08:00–23:00,按所选频率对齐;调整后 30 秒内生效 · 数据直连智谱官方接口(本地无累计,数值为准接口侧口径)</div>
</div>

<div class="grid" id="blocks"></div>

<div class="card" style="margin-top:12px">
  <h2>⏰ 重置时刻表</h2>
  <div class="muted" id="resets">加载中…</div>
</div>

<div class="card" style="margin-top:12px">
  <h2>📈 近 24 小时用量趋势(已用 %)</h2>
  <div id="chart" class="muted">暂无历史数据(采样每 10 分钟一条,积累中…)</div>
  <div class="legend" id="legend"></div>
</div>

<footer>zhipu-usage dashboard · <a style="color:var(--muted)" href="/api/usage">/api/usage</a> · 历史 /api/history</footer>
</div>

<script>
const COLORS=['#38bdf8','#a78bfa','#34d399','#fbbf24','#f472b6','#94a3b8'];
const pctColor=p=>p>=80?'#ef4444':p>=50?'#f59e0b':'#22c55e';
const fmtTok=v=>v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(1)+'K':String(v);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum=(v,u)=>u==='tokens'?fmtTok(v):String(v);
function dur(sec){if(sec==null||!isFinite(sec))return'-';if(sec<=0)return'已重置';const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60);
return(d?d+'天':'')+(d||h?h+'小时':'')+m+'分'}
function ring(id,p,color,size){const r=(size||64)/2-6,C=2*Math.PI*r;
return `<div class="ring" style="width:${size||64}px;height:${size||64}px"><svg viewBox="0 0 ${size||64} ${size||64}">
<circle cx="${(size||64)/2}" cy="${(size||64)/2}" r="${r}" fill="none" stroke="#0a1120" stroke-width="7"/>
<circle cx="${(size||64)/2}" cy="${(size||64)/2}" r="${r}" fill="none" stroke="${color}" stroke-width="7"
stroke-linecap="round" stroke-dasharray="${(Math.min(p,100)/100*C).toFixed(1)} ${C.toFixed(1)}"/></svg>
<div class="v" id="${id}">${Math.round(p)}%</div></div>`}
function spark(series,color){if(!series||series.length<2)return'';
const W=280,H=36,P=3,pts=series.map((p,i)=>(P+i/(series.length-1)*(W-2*P)).toFixed(1)+','+(H-P-(Math.min(p,100)/100)*(H-2*P)).toFixed(1)).join(' ');
return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6"/></svg>`}
function etaText(tag,now){
  if(!now.has_total)return null; // 接口未给绝对值时无法测速
  const seg=currentSegment(tag);if(!seg)return null;
  const first=seg[0],last=seg[seg.length-1],dt=last.ts-first.ts;
  if(dt<1200||last.total<=0)return null;
  const rate=(last.used-first.used)/dt;
  if(rate<=0)return{ok:true,text:'消耗速率≈0,本周期内无耗尽风险'};
  const eta=last.remaining/rate,resetIn=now.reset_at?now.reset_at-Date.now()/1000:null;
  const t='按近 '+Math.round(dt/60)+' 分钟速率,约 <b>'+dur(eta)+'</b> 后耗尽';
  if(resetIn!=null&&eta<resetIn)return{ok:false,text:t+' —— <b>重置前会耗尽</b>,注意控制 ⚠️'};
  return{ok:true,text:t+',可撑到重置 ✅'};
}
let HISTORY=[],USAGE=null;
function currentSegment(tag){
  const s=HISTORY.filter(x=>x.blocks&&x.blocks[tag]&&x.blocks[tag].used!=null).sort((a,b)=>a.ts-b.ts);
  for(let i=s.length-1;i>0;i--)if(s[i].blocks[tag].used<s[i-1].blocks[tag].used)s=s.slice(i); // 截到最近一次重置之后
  return s.length>1?s:null;
}

async function loadUsage(){
  try{
    const r=await fetch('/api/usage');const j=await r.json();
    if(!r.ok){document.getElementById('status').textContent='获取失败';document.getElementById('updated').textContent=j.error||'';return}
    USAGE=j;
    document.getElementById('status').textContent=j.status;
    document.getElementById('updated').textContent='更新于 '+j.updated_at+(j.level?' · 套餐 '+esc(j.level):'');
    document.getElementById('interval').value=String(j.interval_minutes);
    const warns=j.blocks.filter(b=>b.pct>=80);
    const banner=document.getElementById('banner');
    banner.style.display=warns.length?'block':'none';
    if(warns.length)banner.innerHTML='⚠️ '+warns.map(b=>esc(b.label)+(b.period?'('+esc(b.period)+')':'')+' 已用 '+b.pct+'%').join('、')+' —— 超 80% 告警线';
    document.getElementById('blocks').innerHTML=j.blocks.map((b,i)=>{
      const c=pctColor(b.pct),col=COLORS[i%COLORS.length];
      const seg=currentSegment(b.tag),sparkData=seg?seg.map(x=>x.blocks[b.tag].pct):null;
      const eta=etaText(b,b);
      let h=`<div class="card"><h2>${esc(b.icon)} ${esc(b.label)}${b.period?` <span class="pill">${esc(b.period)}</span>`:''}</h2>
      <div class="top">${ring('rg'+i,b.pct,c)}
        <div class="big" style="color:${c}">${b.pct}%<small>已用${b.has_remaining?' · 剩余 '+fmtNum(b.remaining,b.unit)+(esc(b.unit)==='tokens'?' tokens':''):''}</small></div></div>
      <div class="bar"><i class="${b.pct>=80?'crit':''}" style="width:${Math.min(b.pct,100)}%;background:${c}"></i>
        <span class="mark" style="left:50%"></span><span class="mark" style="left:80%"></span></div>
      ${b.has_total?`<div class="kv"><span>已消耗 <b>${fmtNum(b.used,b.unit)}</b></span><span>限额 <b>${fmtNum(b.total,b.unit)}</b></span></div>`:''}
      <div class="kv"><span>重置</span><b><span class="cd" data-at="${b.reset_at||0}">-</span> · ${b.reset_at?new Date(b.reset_at*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'无'}</span></b></div>`;
      if(b.window_hours&&b.reset_at){
        const frac=Math.min(1,Math.max(0,1-(b.reset_at-Date.now()/1000)/(b.window_hours*3600)));
        h+=`<div class="kv"><span>本周期已过</span><b>${(frac*100).toFixed(0)}%</b></div><div class="tbar"><i style="width:${(frac*100).toFixed(0)}%"></i></div>`;
      }
      if(eta)h+=`<div class="eta" style="border-color:${eta.ok?'var(--line)':'rgba(239,68,68,.5)'}">🔮 ${eta.text}</div>`;
      if(sparkData)h+=`<div class="sub">近 24 小时${spark(sparkData,col)}</div>`;
      if(b.details&&b.details.length){
        h+=`<div class="sub"><b>分模型明细</b>`+b.details.map(d=>{
          const p=b.total>0?Math.min(100,d.usage/b.total*100):0;
          return `<div class="mrow"><span title="${esc(d.model)}">${esc(d.model)}</span><span class="mbar"><i style="width:${p.toFixed(1)}%;background:${col}"></i></span><span style="text-align:right;color:var(--muted)">${fmtTok(d.usage)}</span></div>`;
        }).join('')+`</div>`;
      }
      return h+`</div>`;
    }).join('');
    document.getElementById('resets').innerHTML=j.blocks.map(b=>
      `<div class="rst"><span>${esc(b.icon)} ${esc(b.label)}${b.period?'<span class="muted">('+esc(b.period)+')</span>':''}</span>
       <span class="tbar" style="margin:0"><i style="width:${(b.pct>=0?b.pct:0)}%;background:${pctColor(b.pct)}"></i></span>
       <span class="cd" data-at="${b.reset_at||0}" style="text-align:right">-</span></div>`).join('')
      +'<div class="muted" style="margin-top:4px">倒计时实时跳动;横条为当前用量占比</div>';
  }catch(e){document.getElementById('status').textContent='网络错误'}
}
async function loadHistory(){
  try{
    const r=await fetch('/api/history');const j=await r.json();
    HISTORY=(j.samples||[]).filter(x=>x.ts>Date.now()/1000-24*3600-1200);
    if(USAGE)loadUsage();
    const s=HISTORY;if(s.length<2)return;
    const t0=s[0].ts,t1=Math.max(s[s.length-1].ts,t0+1);
    if(t1-t0<1800){document.getElementById('chart').textContent='采样积累中(每 10 分钟一条),跨度满 30 分钟后展示趋势…';return}
    const tags=[...new Set(s.flatMap(x=>Object.keys(x.blocks||{})))];
    const W=1000,H=230,P=30;
    const X=t=>P+(t-t0)/(t1-t0)*(W-2*P),Y=p=>H-P-(p/100)*(H-2*P);
    let g='';
    [0,25,50,75,80,100].forEach(yy=>{g+=`<line x1="${P}" x2="${W-P}" y1="${Y(yy)}" y2="${Y(yy)}" stroke="${yy===80?'#7f1d1d':'#2a3a55'}" stroke-dasharray="${yy===0||yy===80?'0':'3 4'}"/>`+(yy===80?`<text x="${W-P+2}" y="${Y(yy)+3}" fill="#f87171">80%</text>`:`<text x="4" y="${Y(yy)+3}">${yy}%</text>`)});
    for(let i=0;i<=3;i++){const t=t0+(t1-t0)*i/3,d=new Date(t*1000);
      g+=`<text x="${X(t)}" y="${H-8}" text-anchor="middle">${String(d.getHours()).padStart(2,'0')}:00</text>`}
    let paths='',legend='';
    tags.forEach((tag,ti)=>{
      const col=COLORS[ti%COLORS.length];
      const pts=s.filter(x=>x.blocks[tag]).map(x=>X(x.ts).toFixed(1)+','+Y(x.blocks[tag].pct).toFixed(1)).join(' ');
      if(pts)paths+=`<polyline points="${pts}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>`;
      legend+=`<span><i style="background:${col}"></i>${esc(tag)}</span>`;
    });
    document.getElementById('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${g}${paths}</svg>`;
    document.getElementById('legend').innerHTML=legend;
  }catch(e){}
}
function busy(btn,txt,done){const old=btn.textContent;btn.disabled=true;btn.textContent=txt;
setTimeout(()=>{btn.disabled=false;btn.textContent=old;if(done)done()},done?400:1200)}
async function saveInterval(btn){
  const iv=parseInt(document.getElementById('interval').value,10),msg=document.getElementById('msg');
  busy(btn,'保存中…');msg.textContent='';
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interval_minutes:iv})});
    const j=await r.json();
    msg.textContent=r.ok?'✅ 已保存:'+(iv===0?'推送已关闭':'每 '+iv+' 分钟一次')+',30 秒内生效':'❌ '+(j.error||('HTTP '+r.status));
  }catch(e){msg.textContent='❌ 保存失败'}
}
async function pushNow(btn){
  const msg=document.getElementById('msg');busy(btn,'推送中…');msg.textContent='';
  try{
    const r=await fetch('/api/push',{method:'POST'});const j=await r.json();
    msg.textContent=r.ok?'✅ 已触发推送,请查看钉钉/邮箱':'❌ 触发失败';
  }catch(e){msg.textContent='❌ 触发失败'}
}
function copyText(t){
  if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(t);
  const ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{return Promise.resolve(document.execCommand('copy'))}finally{ta.remove()}
}
async function copyReport(btn){
  const msg=document.getElementById('msg');busy(btn,'复制中…');
  try{
    const r=await fetch('/api/report');const j=await r.json();
    if(!r.ok){msg.textContent='❌ 生成失败';return}
    await copyText(j.text);msg.textContent='✅ 文本报告已复制到剪贴板';
  }catch(e){msg.textContent='❌ 复制失败'}
}
function tickCd(){const now=Date.now()/1000;
document.querySelectorAll('.cd').forEach(el=>{const at=parseFloat(el.dataset.at);
el.textContent=at>0?dur(at-now):(at===0?'无':'-')})}
tickCd();setInterval(tickCd,1000);
loadUsage();loadHistory();
setInterval(loadUsage,60000);setInterval(loadHistory,300000);
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "ZhipuUsageDashboard/1.0"

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/usage":
            payload = usage_payload()
            self._json(payload, 200) if payload else self._json({"error": "获取用量失败,请稍后重试"}, 502)
        elif path == "/api/history":
            with _history_lock:
                samples = load_history()
            self._json({"samples": samples[-(24 * 6 + 2):]})
        elif path == "/api/settings":
            self._json(load_settings())
        elif path == "/api/report":
            payload = report_payload()
            self._json(payload, 200) if payload else self._json({"error": "生成失败"}, 502)
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            body = {}
        if path == "/api/settings":
            try:
                iv = int(body.get("interval_minutes"))
            except (TypeError, ValueError):
                return self._json({"error": "interval_minutes 必须为整数"}, 400)
            if iv not in ALLOWED_INTERVALS:
                return self._json({"error": f"interval_minutes 仅支持 {ALLOWED_INTERVALS}"}, 400)
            save_settings(iv)
            log.info("Dashboard set push interval to %dmin", iv)
            self._json(load_settings())
        elif path == "/api/push":
            threading.Thread(target=_push_once, daemon=True).start()
            self._json({"ok": True, "message": "已触发推送"})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def start_dashboard():
    port = int(os.environ.get("DASHBOARD_PORT", "8060"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("Dashboard listening on :%d", port)


def run_loop():
    start_dashboard()
    threading.Thread(target=sampler_loop, daemon=True, name="sampler").start()
    interval = load_settings()["interval_minutes"]
    target = next_push_time(interval)
    log.info("Starting ZhipuAI usage notifier (push window %02d:00-%02d:00)", WINDOW_START_HOUR, WINDOW_END_HOUR)
    _log_next(target, interval)
    while True:
        time.sleep(30)
        new_interval = load_settings()["interval_minutes"]
        if new_interval != interval:
            interval = new_interval
            target = next_push_time(interval)
            _log_next(target, interval)
            continue
        if target is None:
            continue
        if time.time() >= target:
            _push_once()
            target = next_push_time(interval)
            _log_next(target, interval)


if __name__ == "__main__":
    if "--once" in sys.argv:
        main()
    else:
        run_loop()
