"""告警规则引擎。

在设计上分成两层：
  * `collect.py` 负责「采集过程中能立刻发现的」告警（余额低、会话过期、采集失败）
  * `ysp/alerts.py` 负责「需要跑一遍分析才能发现的」告警（消耗异常、数据卡住、提前预警）

所有告警都会过一遍去重（`alerts.dedupe_hours`），避免每轮采集都刷你的邮箱。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import analyze, db
from .notify import has_channel, notify, render_html

# 去重键 → 上次发送时间由 DB 的 alerts 表承担，这里只定义规则聚合


def _level_emoji(level: str) -> str:
    return {"critical": "🔴", "warn": "🟠", "info": "🔵"}.get(level, "⚪")


def evaluate(conn: sqlite3.Connection, cfg: dict, room: dict,
             readings: list[dict], native_unit: str,
             balance: float | None) -> list[dict]:
    """跑一遍分析，产出应该触发的告警列表（已做去重判断）。"""
    rules = ((cfg.get("alerts") or {}).get("rules")) or {}
    unit = "元" if native_unit == "cny" else "度"
    price = analyze.native_price(room, native_unit)
    label = room.get("label") or room["room_key"]
    days_data = analyze._fill_missing_eod(analyze.daily(readings, price))
    fired: list[dict] = []

    def push(level: str, key: str, title: str, rows: list[tuple[str, str]],
             text_extra: str = "") -> None:
        dedupe = int((cfg.get("alerts") or {}).get("dedupe_hours", 12))
        if db.recent_alert(conn, room["room_key"], key, dedupe):
            return
        body_lines = [f"{k}：{v}" for k, v in rows]
        if text_extra:
            body_lines.append("")
            body_lines.append(text_extra)
        fired.append({
            "level": level,
            "key": key,
            "title": title,
            "text": "\n".join(body_lines),
            "html": render_html(f"{_level_emoji(level)} {title}", rows,
                                "来自 yischool-power 电费监测"),
            "rows": rows,
        })

    # ---------------- 1) 余额低 / 即将耗尽
    fc = analyze.forecast(days_data, balance, unit_price=price)
    warn_at = float(room.get("warn_kwh") or 20)
    crit_at = float(room.get("critical_kwh") or 5)

    if balance is not None and rules.get("low_balance", True):
        common = [
            ("房间", label),
            ("当前余额", f"{round(balance, 2)} {unit}"),
        ]
        if fc.get("available"):
            common += [("日均消耗", f"{fc['rate_kwh_per_day']} {unit}/天"),
                       ("预计耗尽", f"{fc['empty_date']}（还有 {fc['days_left']} 天）")]
        if balance <= crit_at:
            push("critical", "low_balance_critical",
                 f"{label} 电费告急",
                 common + [("告警线", f"{crit_at} {unit}（严重）")],
                 "请尽快充值，否则可能断电。")
        elif balance <= warn_at:
            push("warn", "low_balance_warn",
                 f"{label} 电费偏低",
                 common + [("告警线", f"{warn_at} {unit}")],
                 "建议提前充值。")

    # ---------------- 2) 提前预警：N 天内耗尽
    fdays = int(rules.get("forecast_days", 7) or 0)
    if fdays > 0 and fc.get("available") and fc.get("days_left") is not None:
        if fc["days_left"] <= fdays and balance is not None and balance > warn_at:
            push("warn", "forecast_soon",
                 f"{label} 预计 {fdays} 天内耗尽",
                 [("房间", label),
                  ("当前余额", f"{round(balance, 2)} {unit}"),
                  ("日均消耗", f"{fc['rate_kwh_per_day']} {unit}/天"),
                  ("预计耗尽", f"{fc['empty_date']}（还有 {fc['days_left']} 天）"),
                  ("消耗趋势", fc.get("trend", "—"))],
                 f"按当前速率，{fdays} 天内会用完，建议提前充值。")

    # ---------------- 3) 消耗异常（单日暴涨）
    if rules.get("spike", True):
        an = analyze.detect_anomalies(days_data)
        spikes = [a for a in an if a["kind"] == "spike"][-3:]
        if spikes:
            rows = [("房间", label)]
            for a in spikes:
                rows.append((a["date"], f"{a['kwh']} {unit}（基准 {a['expected']} {unit}，"
                                        f"偏离 {a['score']}σ）"))
            push("warn", "spike", f"{label} 用电量异常偏高", rows,
                 "某几天明显高于你的日常水平，可以看看是不是空调/取暖器忘关了。")

    # ---------------- 4) 数据卡住（易校园长时间不更新）
    if rules.get("stale", True):
        st = analyze.detect_stale(readings, float(rules.get("stale_hours", 12) or 12))
        ongoing = [s for s in st if s["ongoing"]]
        if ongoing:
            s = ongoing[-1]
            hint = ("这是易校园侧的数据没有更新，不是你没用电。"
                    "该时段的消耗统计会偏低，请不要据此判断用电情况。")
            if s["hours"] >= 72:
                hint += f" 已持续 {round(s['hours']/24, 1)} 天，建议在易校园 App 里核对一下电表。"
            push("warn" if s["hours"] < 72 else "critical", "stale",
                 f"{label} 电费数据卡住 {s['hours']} 小时",
                 [("房间", label),
                  ("卡住起始", s["start"].replace("T", " ")),
                  ("已持续", f"{s['hours']} 小时"),
                  ("保持不变的值", f"{s['value']} {unit}")],
                 hint)

    # ---------------- 5) 连续高耗（忘关电器）
    if rules.get("spike", True):
        drain = analyze.detect_continuous_drain(readings)
        recent = [d for d in drain
                  if (datetime.now() - datetime.fromisoformat(d["start"])).total_seconds() < 86400]
        if recent:
            d = recent[-1]
            push("info", "continuous_drain",
                 f"{label} 检测到连续高耗",
                 [("房间", label),
                  ("开始时间", d["start"].replace("T", " ")),
                  ("持续", f"{d['hours']} 小时"),
                  ("消耗", f"{d['kwh']} {unit}")],
                 "连续多小时保持较高功率，可能是某个电器一直没关。")

    return fired


def flush(conn: sqlite3.Connection, cfg: dict, fired: list[dict]) -> dict:
    """把触发的告警落库 + 推送。返回推送结果汇总。"""
    results: list[dict] = []
    for a in fired:
        db.add_alert(conn, a.get("room_key", ""), a["level"], a["title"])
        # 用 key 作为去重标记写进 alerts 表
        conn.execute(
            "INSERT INTO alerts (room_key,ts,level,message,acked) VALUES (?,?,?,?,0)",
            (a.get("room_key", ""), datetime.now().isoformat(timespec="seconds"),
             a["key"], a["title"]),
        )
        conn.commit()
        if has_channel(cfg):
            r = notify(cfg, a["title"], a["text"], a.get("html"))
            results.append({"title": a["title"], "notify": r})
        else:
            results.append({"title": a["title"], "notify": {"skipped": "未配置通知渠道"}})
    return {"sent": len(results), "results": results}


# ------------------------------------------------------------------ 运维告警

def send_ops_alert(conn: sqlite3.Connection, cfg: dict, key: str, level: str,
                   title: str, rows: list[tuple[str, str]], extra: str = "") -> bool:
    """运维类告警（会话过期、采集失败），与房间无关。

    返回 True 表示真的发出去了（未被去重）。
    """
    dedupe = int((cfg.get("alerts") or {}).get("dedupe_hours", 12))
    if db.recent_alert(conn, "__ops__", key, dedupe):
        return False
    conn.execute(
        "INSERT INTO alerts (room_key,ts,level,message,acked) VALUES (?,?,?,?,0)",
        ("__ops__", datetime.now().isoformat(timespec="seconds"), key, title))
    conn.commit()
    if has_channel(cfg):
        notify(cfg, title, "\n".join(f"{k}：{v}" for k, v in rows) + (f"\n\n{extra}" if extra else ""),
               render_html(f"{_level_emoji(level)} {title}", rows, "来自 yischool-power 电费监测"))
    return True
