"""告警通知：邮件（SMTP）+ Webhook。纯标准库，无第三方依赖。

邮件用到的都是 stdlib：smtplib / email.message / ssl。
常见邮箱配置：
    QQ 邮箱      smtp.qq.com   465 SSL   （密码填「授权码」，不是 QQ 密码）
    163 邮箱     smtp.163.com  465 SSL
    Gmail        smtp.gmail.com 465 SSL
    学校自建     smtp.xxx.edu.cn 587 STARTTLS
"""
from __future__ import annotations

import json
import smtplib
import ssl
import sys
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any


class NotifyError(RuntimeError):
    pass


# ------------------------------------------------------------------ 邮件

def send_email(cfg: dict, subject: str, body_text: str,
               body_html: str | None = None) -> dict:
    """按 config.alerts.email 发送邮件。返回 {ok, detail, to}。"""
    ec = (cfg.get("alerts") or {}).get("email") or {}
    if not ec.get("enabled"):
        return {"ok": False, "detail": "邮件未启用（alerts.email.enabled=false）"}

    host = str(ec.get("smtp_host") or "").strip()
    port = int(ec.get("smtp_port") or 465)
    user = str(ec.get("username") or "").strip()
    pwd = str(ec.get("password") or "").strip()
    sender = str(ec.get("from_addr") or user).strip()
    to_addrs = ec.get("to_addrs") or []
    if isinstance(to_addrs, str):
        to_addrs = [x.strip() for x in to_addrs.replace(";", ",").split(",") if x.strip()]

    missing = [k for k, v in (("smtp_host", host), ("username", user),
                              ("password", pwd), ("to_addrs", to_addrs)) if not v]
    if missing:
        return {"ok": False, "detail": f"邮件配置不完整，缺少：{'、'.join(missing)}"}

    prefix = str(ec.get("subject_prefix") or "")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"{prefix}{subject}", "utf-8")
    msg["From"] = formataddr((str(Header("寝室电费监测", "utf-8")), sender))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    use_ssl = bool(ec.get("use_ssl", port == 465))
    timeout = int(ec.get("timeout", 25))
    try:
        if use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as s:
                s.login(user, pwd)
                s.sendmail(sender, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass
                s.login(user, pwd)
                s.sendmail(sender, to_addrs, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "detail": f"SMTP 认证失败：{e}。"
                                       f"注意 QQ/163 邮箱必须用「授权码」而不是登录密码。"}
    except smtplib.SMTPException as e:
        return {"ok": False, "detail": f"SMTP 错误：{type(e).__name__}: {e}"}
    except (OSError, ssl.SSLError) as e:
        return {"ok": False, "detail": f"网络/SSL 错误：{e}"}
    return {"ok": True, "detail": f"已发送至 {', '.join(to_addrs)}", "to": to_addrs}


def test_email(cfg: dict) -> dict:
    return send_email(cfg, "测试邮件 · 配置正确",
                      "这是一封测试邮件。\n\n收到它说明 SMTP 配置没问题，"
                      "后续的电费告警会发到这个地址。\n\n— yischool-power")


# ------------------------------------------------------------------ Webhook

def send_webhook(cfg: dict, title: str, text: str) -> dict:
    """兼容企业微信 / 钉钉 / Bark / Server酱：同时带上多种字段名。"""
    url = str((cfg.get("alerts") or {}).get("webhook_url") or "").strip()
    if not url:
        return {"ok": False, "detail": "未配置 webhook_url"}
    payload = {
        "msgtype": "text",
        "text": {"content": f"{title}\n{text}"},
        "markdown": {"content": f"**{title}**\n\n{text}"},
        "title": title, "body": text, "content": f"{title}\n{text}",
        "message": f"{title}\n{text}",
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"ok": True, "detail": f"HTTP {r.status}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ 组合

def notify(cfg: dict, title: str, text: str, html: str | None = None) -> dict:
    """同时走邮件与 webhook（哪个配了就走哪个）。"""
    out: dict[str, Any] = {"title": title}
    ac = cfg.get("alerts") or {}
    if (ac.get("email") or {}).get("enabled"):
        out["email"] = send_email(cfg, title, text, html)
    if ac.get("webhook_url"):
        out["webhook"] = send_webhook(cfg, title, text)
    if not out.get("email") and not out.get("webhook"):
        out["skipped"] = "既没启用邮件，也没配 webhook"
    return out


def render_html(title: str, rows: list[tuple[str, str]],
                footer: str = "") -> str:
    """把 (标签, 值) 列表渲染成简单的 HTML 邮件正文。"""
    tr = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#666;white-space:nowrap">{k}</td>'
        f'<td style="padding:6px 0;font-weight:600">{v}</td></tr>' for k, v in rows)
    return f"""<div style="font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif;
            font-size:14px;color:#222;line-height:1.7;max-width:560px">
  <h2 style="margin:0 0 12px;font-size:17px">{title}</h2>
  <table style="border-collapse:collapse">{tr}</table>
  {f'<p style="color:#888;font-size:12px;margin-top:16px">{footer}</p>' if footer else ''}
</div>"""


def has_channel(cfg: dict) -> bool:
    ac = cfg.get("alerts") or {}
    return bool((ac.get("email") or {}).get("enabled")) or bool(ac.get("webhook_url"))
