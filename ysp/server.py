"""零依赖 HTTP 服务：REST 数据接口 + 仪表盘静态页。

只用标准库 http.server，`python -m ysp.server` 即可跑起来。
"""
from __future__ import annotations

import csv
import io
import json
import mimetypes
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analyze, db
from .auth import (
    AuthClient,
    AuthError,
    clear_session,
    load_session,
    resolve_phone,
    resolve_school,
    save_session,
)
from .collect import collect_once
from .config import ROOT, db_path, load_config
from .tenant_context import CURRENT

WEB_DIR = ROOT / "web"
CFG = CURRENT.get()[1] if CURRENT.get() else load_config()
analyze.set_vacancy_config(CFG)
DB_FILE = db_path(CFG)

_lock = threading.Lock()
_local = threading.local()


def conn():
    """每个线程一个独立连接。

    ⚠️ 这里必须用 thread-local：服务是 ThreadingHTTPServer，仪表盘一次会并发打
    十几个接口。共用同一个 sqlite3.Connection 会让并发查询互相串台
    （表现是接口偶发返回错的数据结构），所以每个线程各持一个连接。
    """
    c = getattr(_local, "conn", None)
    if c is None:
        c = db.connect(DB_FILE)
        _local.conn = c
    return c


def auth_client() -> AuthClient:
    src = CFG["source"]
    return AuthClient(str(src.get("base_url") or "https://tool.no-fish.cn"),
                      int(src.get("timeout", 20)),
                      bool(src.get("strict_tls", False)))


def _fail(exc: BaseException, where: str) -> dict:
    """把 500 的 traceback 打到控制台，避免只有一个光秃秃的错误名。"""
    import traceback

    tb = traceback.format_exc()
    print(f"\n[500] {where}\n{tb}", flush=True)
    return {"error": f"{type(exc).__name__}: {exc}",
            "where": where,
            "traceback": tb.strip().splitlines()[-6:]}


# ---------------------------------------------------------------- helpers

def _pick_room(q: dict) -> dict | None:
    key = (q.get("room") or [None])[0]
    rooms = db.list_rooms(conn(), enabled_only=True)
    if not rooms:
        return None
    if key:
        for r in rooms:
            if r["room_key"] == key:
                return r
    return rooms[0]


def _days(q: dict, default: int = 30) -> int:
    try:
        return max(1, min(730, int((q.get("days") or [default])[0])))
    except (TypeError, ValueError):
        return default


def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def _native_unit() -> str:
    """上游原生单位：cny=元 / kwh=度。由采集时写入 meta。"""
    return str(db.get_meta(conn(), "native_unit", "kwh"))


def _range(q: dict) -> tuple[str | None, str | None, str]:
    """解析时间范围。

    优先 ?from=YYYY-MM-DD&to=YYYY-MM-DD（自定义范围）；
    没有就退回 ?days=N（最近 N 天）。返回 (since, until, 描述)。
    """
    f = (q.get("from") or [""])[0].strip()
    t = (q.get("to") or [""])[0].strip()
    if f or t:
        since = f"{f}T00:00:00" if f else None
        until = f"{t}T23:59:59" if t else None
        if f and t:
            label = f if f == t else f"{f} ~ {t}"
        elif f:
            label = f"{f} 起"
        else:
            label = f"截至 {t}"
        return since, until, label
    d = _days(q, 30)
    return ((datetime.now() - timedelta(days=d)).isoformat(timespec="seconds"),
            None, f"最近 {d} 天")


def _load(room: dict, q: dict):
    """按请求里的时间范围取数，并统一到原生单位视角。"""
    since, until, _label = _range(q)
    readings = db.fetch_readings(conn(), room["room_key"], since=since, until=until)
    native = _native_unit()
    analyze.native_view(readings, native)
    price = analyze.native_price(room, native)
    days_data = analyze._fill_missing_eod(analyze.daily(readings, price))
    # 盖日类型（长假/寒暑假标签）：前端展示 + 占用锚点先验
    try:
        from . import model as _M
        _M.attach_kinds(days_data, conn())
    except Exception:  # noqa: BLE001 标签表不可用不影响主流程
        pass

    # 附加「折算度数」，但**不改动**原生字段
    if native == "cny":
        real = float(room.get("unit_price") or 0.538) or 0.538
        for r in readings:
            r["degree"] = round(r["balance_kwh"] / real, 3) if r.get("balance_kwh") is not None else None
        for d in days_data:
            d["degree"] = round(d["kwh"] / real, 3)
            d["amount"] = d["kwh"]
    else:
        for r in readings:
            r["degree"] = r.get("balance_kwh")
        for d in days_data:
            d["degree"] = d["kwh"]
            d["amount"] = d["kwh"]
    return readings, days_data, price


def _last_native_balance(room: dict) -> float | None:
    """取最新余额，按原生单位选列（元制下 balance_kwh 是 NULL）。"""
    last = db.last_reading(conn(), room["room_key"])
    if not last:
        return None
    if _native_unit() == "cny":
        return last.get("balance_cny") if last.get("balance_cny") is not None else last.get("balance_kwh")
    return last.get("balance_kwh")


# ---------------------------------------------------------------- API

def api_health(q):
    n = db.count_readings(conn())
    return {
        "ok": True,
        "version": "1.0.0",
        "config": CFG.get("_config_path"),
        "source": CFG["source"].get("type"),
        "readings": n,
        "db": str(DB_FILE),
        "server_time": datetime.now().isoformat(timespec="seconds"),
        # 寝室人数：分摊预估用
        "people": int(CFG["app"].get("people", 4) or 4),
        "people_options": list(CFG["app"].get("people_options") or [4, 5]),
    }


def api_rooms(q):
    rooms = db.list_rooms(conn())
    out = []
    for r in rooms:
        cov = db.coverage(conn(), r["room_key"])
        last = db.last_reading(conn(), r["room_key"])
        out.append({**r, "coverage": cov, "last": last})
    return {"rooms": out}


def api_summary(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 60)
    readings, days_data, price = _load(room, q)
    bal = _last_native_balance(room)
    native = _native_unit()
    # 读模型状态（周几/气温修正是否启用），传给预测
    from . import model as _M
    _ov = _M.overrides(conn())
    s = analyze.summarize(days_data, readings, room, bal, native_unit=native,
                          overrides=_ov)
    # 钟点画像（去掉空调后的作息）+ 有没有"定时大负载"（如定时热水器）
    _prof = (_ov.get("weather_model") or {}).get("profile")
    if _prof and len(_prof) == 24:
        s["clock_profile"] = [round(float(x), 4) for x in _prof]
        s["clock_peak"] = _M.clock_peak(_prof)
    s["generated_at"] = datetime.now().isoformat(timespec="seconds")
    # 让前端能显示「数据更新于」—— 否则用户没法判断数据到底停没停
    last = db.last_reading(conn(), room["room_key"])
    s["last_reading_ts"] = last.get("ts") if last else None
    s["freshness_minutes"] = None
    if last and last.get("ts"):
        try:
            s["freshness_minutes"] = round(
                (datetime.now() - datetime.fromisoformat(last["ts"])).total_seconds() / 60, 1)
        except (ValueError, TypeError):
            pass
    s["expect_interval_minutes"] = int(CFG["collect"].get("interval_minutes", 60) or 60)
    # 采集调度状态：让页面能显示「下次什么时候采」，证明自动采集是活的
    try:
        from .collect import next_run_at
        nxt = next_run_at(CFG)
        s["next_collect_at"] = nxt.isoformat(timespec="seconds")
        s["auto_collect"] = bool(CFG["app"].get("auto_collect", True))
    except Exception:  # noqa: BLE001
        s["next_collect_at"] = None
        s["auto_collect"] = False
    s["minute_offset"] = int(CFG["collect"].get("minute_offset", 5) or 0)
    return s


def api_series(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 30)
    readings, days_data, price = _load(room, q)
    native = _native_unit()
    return {
        "room_key": room["room_key"],
        "label": room["label"],
        "unit_price": float(room.get("unit_price") or 0.538),
        "native_unit": native,
        # amount = 原生单位值；元制下 amount 就是元，kwh 是折算值
        "points": [{"ts": r["ts"], "amount": r["balance_kwh"],
                    "cny": r["balance_cny"], "degree": r.get("degree")}
                   for r in readings],
        "daily": days_data,
        "recharges": analyze.recharge_events(readings),
        "adjusts": analyze.adjust_events(readings),
    }


def api_daily(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 90)
    _, days_data, _ = _load(room, q)
    return {"daily": days_data, "anomalies": analyze.detect_anomalies(days_data)}


def api_hourly(q):
    """一天里每个小时的花费 + 平均功率。可选 ?date=YYYY-MM-DD。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 90)
    native = _native_unit()
    readings = db.fetch_readings(
        conn(), room["room_key"],
        since=(datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    analyze.native_view(readings, native)
    price = analyze.native_price(room, native)
    real_price = float(room.get("unit_price") or 0.538) or 0.538

    dates = analyze.available_days(readings)
    want = (q.get("date") or [""])[0].strip()
    if want not in dates:
        want = dates[0] if dates else None

    hours = analyze.hourly_profile(readings, price, day=want) if want else \
        analyze.hourly_profile(readings, price)
    total = sum(h["kwh"] for h in hours)

    # 功率（瓦）：用真实电价换算，和花费一起返回，保证两边日期一致
    p_rows = analyze.power_profile(readings, real_price, native, want) if want else \
        analyze.power_profile(readings, real_price, native)
    p_map = {r["hour"]: r for r in p_rows}
    for h in hours:
        p = p_map.get(h["hour"]) or {}
        h["watts"] = p.get("watts")
        h["power_level"] = p.get("level", "—")
        h["power_example"] = p.get("example", "")
    summary = analyze.power_summary(p_rows, real_price, native)

    elapsed = 24
    if want and want == datetime.now().date().isoformat():
        elapsed = datetime.now().hour + 1
    top = sorted([h for h in hours if h["kwh"] > 0], key=lambda x: -x["kwh"])[:3]
    return {
        "date": want,
        "dates": dates[:120],
        "hours": hours,
        "total": round(total, 3),
        "elapsed_hours": elapsed,
        "native_unit": native,
        "unit_price": real_price,
        "top_hours": [h["hour"] for h in top],
        "power": summary,
    }


def api_weekday(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 90)
    _, days_data, _ = _load(room, q)
    return {"weekdays": analyze.weekday_profile(days_data)}


def api_forecast(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 60)
    # 不传 horizon 就走自适应（预计耗尽 + 3 天余量）
    readings, days_data, price = _load(room, q)
    from . import model as _M
    kw = _M.overrides(conn())
    if "horizon" in q:
        kw["horizon"] = _days(q, 14)
    return analyze.forecast(days_data, _last_native_balance(room),
                            unit_price=price, readings=readings, **kw)


def api_anomalies(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 120)
    readings, days_data, _ = _load(room, q)
    return {
        "anomalies": analyze.detect_anomalies(days_data),
        "continuous_drain": analyze.detect_continuous_drain(readings),
        "recharges": analyze.recharge_events(readings),
        "adjusts": analyze.adjust_events(readings),
        "stale": analyze.detect_stale(readings, 12),
    }


def api_hourly_series(q):
    """小时级序列：每小时余额 + 每小时消耗，用于双轴图。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    hours = _days(q, 7)
    native = _native_unit()
    since, until, _lb = _range(q)
    readings = db.fetch_readings(conn(), room["room_key"], since=since, until=until)
    analyze.native_view(readings, native)
    price = analyze.native_price(room, native)
    series = analyze.hourly_series(readings, price)
    if native == "cny":
        real = float(room.get("unit_price") or 0.538) or 0.538
        for h in series:
            h["degree"] = round(h["kwh"] / real, 3)
            h["balance_degree"] = (round(h["balance"] / real, 3)
                                   if h.get("balance") is not None else None)
    return {"native_unit": native, "unit_price": float(room.get("unit_price") or 0.538),
            "days": hours, "hours": series}


def api_quality(q):
    """数据质量：覆盖率、缺口、卡住时长、新鲜度、会话状态。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 30)
    native = _native_unit()
    since, until, rlabel = _range(q)
    readings = db.fetch_readings(conn(), room["room_key"], since=since, until=until)
    analyze.native_view(readings, native)
    interval = int(CFG["collect"].get("interval_minutes", 60) or 60)
    out = analyze.data_quality(readings, expect_minutes=interval)
    out["expect_interval_minutes"] = interval
    out["last_collect"] = db.get_meta(conn(), "last_collect")
    out["fail_streak"] = int(db.get_meta(conn(), "fail_streak", 0) or 0)
    s = load_session(conn())
    out["session"] = {
        "logged_in": bool(s.get("phone")) and bool(s.get("logged_in_at")),
        "need_relogin": bool(s.get("need_relogin")),
        "last_ok_at": s.get("last_ok_at"),
        "phone_masked": _mask(str(s.get("phone") or "")),
    }
    return out


def api_distribution(q):
    """日花费分布直方图 + 分位数。

    注意：这里一律返回**原生单位**（元）的值，度数折算交给前端按用户选的单位做，
    否则前端再折一次会翻倍。
    """
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 120)
    _, days_data, _ = _load(room, q)
    return analyze.distribution(days_data)


def api_cycles(q):
    """充值周期：每次充值之后用了多少、撑了几天、还剩多少。

    ⚠️ 这张表固定读**全量历史**，不跟页面顶部的日期范围走。
    之前走 _load(room, q) 按 from/to 切读数，有两个问题：
      1. 最近 N 天没有充值时，整张表变空，看起来像「历史充值没了」；
      2. 窗口起点正好压在充值那一条读数上时，连这笔充值都识别不出来
         （识别充值要拿前一条读数做差，起点那条被切掉了）。
    这张表叫「历次充值」，历史就是它的全部意义，所以这里不做裁剪。
    """
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    readings = db.fetch_readings(conn(), room["room_key"], since=None, until=None)
    native = _native_unit()
    analyze.native_view(readings, native)
    price = analyze.native_price(room, native)
    cycles = analyze.billing_cycles(readings, price)
    if native == "cny":
        real = float(room.get("unit_price") or 0.538) or 0.538
        for c in cycles:
            c["recharge_degree"] = round(c["recharge"] / real, 2)
            c["used_degree"] = round(c["used"] / real, 2)
            c["per_day_degree"] = round(c["per_day"] / real, 3) if c.get("per_day") else None
    return {"cycles": cycles, "native_unit": native}


def api_power(q):
    """每小时平均功率（瓦）。可选 ?date=YYYY-MM-DD。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 90)
    native = _native_unit()
    readings = db.fetch_readings(
        conn(), room["room_key"],
        since=(datetime.now() - timedelta(days=days)).isoformat(timespec="seconds"))
    analyze.native_view(readings, native)
    price = float(room.get("unit_price") or 0.538) or 0.538

    dates = analyze.available_days(readings)
    want = (q.get("date") or [""])[0].strip()
    if want not in dates:
        want = dates[0] if dates else None

    rows = analyze.power_profile(readings, price, native, want) if want else \
        analyze.power_profile(readings, price, native)
    summary = analyze.power_summary(rows, price, native)
    return {
        "date": want,
        "dates": dates[:120],
        "hours": rows,
        "summary": summary,
        "unit_price": price,
        "bands": [{"level": n, "example": e} for _, _, n, e in analyze.POWER_BANDS],
    }
def api_stale(q):
    """数据卡住区间（易校园长时间不更新）。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 120)
    readings, _, _ = _load(room, q)
    hours = float((q.get("threshold") or [12])[0])
    return {"segments": analyze.detect_stale(readings, hours), "threshold_hours": hours}


def api_compare(q):
    """环比 / 同比：今天 vs 昨天、最近 7/30 天 vs 前 7/30 天、去年同期。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 400)
    readings, days_data, _ = _load(room, q)
    cmp_ = analyze.period_compare(days_data, readings)
    if _native_unit() == "cny":
        real = float(room.get("unit_price") or 0.538) or 0.538
        for k in ("today", "week", "month", "yoy"):
            b = cmp_.get(k)
            if not b:
                continue
            for f in ("current", "previous", "delta", "current_per_day", "previous_per_day"):
                if b.get(f) is not None:
                    b[f + "_degree"] = round(b[f] / real, 2)
    cmp_["native_unit"] = _native_unit()
    return cmp_


def api_insights(q):
    """智能分析：把数据翻译成人话建议（纯规则，不调用大模型）。"""
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 120)
    readings, days_data, _ = _load(room, q)
    native = _native_unit()
    bal = _last_native_balance(room)
    items = analyze.insights(days_data, readings, room, bal, native)
    price = float(room.get("unit_price") or 0.538) or 0.538
    if native == "cny":
        for it in items:
            it["text_degree_note"] = ""
    return {"insights": items, "native_unit": native,
            "generated_at": datetime.now().isoformat(timespec="seconds")}


def api_heatmap(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 30)
    readings, _, price = _load(room, q)
    segs = analyze.segments(readings)
    grid: dict[str, dict[int, float]] = {}
    for s in segs:
        d = s["start"].date().isoformat()
        grid.setdefault(d, {})
        h = s["start"].hour
        grid[d][h] = round(grid[d].get(h, 0.0) + s["kwh"], 3)
    dates = sorted(grid)
    data = [[h, i, round(grid[d].get(h, 0.0), 3)] for i, d in enumerate(dates) for h in range(24)]
    return {"dates": dates, "data": data, "unit_price": price}


def api_alerts(q):
    room = (q.get("room") or [None])[0]
    return {"alerts": db.list_alerts(conn(), room, limit=200)}


def api_ack(body):
    aid = int(body.get("id", 0))
    if aid:
        db.ack_alert(conn(), aid)
    return {"ok": True}


# ---------------------------------------------------------------- 登录相关

def api_auth_status(q):
    """前端据此决定是显示登录向导还是仪表盘。"""
    src = CFG["source"]
    if src.get('type') == 'eschool':
        from .eschool_service import handle
        info = handle('status',{},CFG)
        return {'source_type':'eschool','login_required':True,'logged_in':info['logged_in'],
                'need_relogin':info['need_login'],'need_location':not info.get('room_bound')}
    kind = str(src.get("type", "demo")).lower()
    s = load_session(conn())
    phone = resolve_phone(CFG, conn())
    school_code, school_name = resolve_school(CFG, conn())
    out = {
        "source_type": kind,
        "login_required": kind == "nofish",
        # 必须真正走完 login 才算已登录；只发过验证码不算
        "logged_in": bool(phone) and bool(s.get("logged_in_at")),
        "phone": _mask(phone),
        "phone_raw_tail": phone[-4:] if phone else "",
        "school_code": school_code,
        "school_name": school_name,
        "need_relogin": bool(s.get("need_relogin")),
        "need_location": bool(s.get("need_location")),
        "room_key": s.get("room_key", ""),
        "room_label": s.get("room_label", ""),
        "logged_in_at": s.get("logged_in_at"),
        "last_ok_at": s.get("last_ok_at"),
        "pending_phone": s.get("pending_phone", ""),
        # 发码时间与次数：前端据此恢复倒计时、提示已等待多久
        "send_at": s.get("send_at"),
        "send_count": int(s.get("send_count") or 0),
    }
    if kind == "nofish" and phone:
        try:
            info = auth_client().user_info(phone)
            out["user_info"] = {
                "is_bound": info.get("is_bound"),
                "need_relogin": info.get("need_relogin"),
                "need_location": info.get("need_location"),
                "school_name": info.get("school_name") or school_name,
                "area_name": info.get("area_name"),
                "building_name": info.get("building_name"),
                "floor_name": info.get("floor_name"),
                "room_name": info.get("room_name"),
            }
            out["need_relogin"] = bool(info.get("need_relogin"))
            out["need_location"] = bool(info.get("need_location"))
        except AuthError as e:
            out["probe_error"] = str(e)
    return out


def _mask(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone


def api_auth_schools(q):
    kw = (q.get("q") or [""])[0]
    return {"schools": auth_client().schools(kw)}


def api_auth_import_cookie(body):
    """方式二：粘贴易校园会话 Cookie（免抓包工具）。

    接受三种输入：
      * 纯 shiroJID 值      00000000-0000-0000-0000-000000000000
      * shiroJID=xxx
      * 从 DevTools 整段复制的 Cookie 字符串
    拿到后自动探 ymId 与房间信息，探到多少算多少。
    """
    from . import xiaofubao as xf

    raw = str(body.get("cookie") or body.get("shiro_jid") or "").strip()
    if not raw:
        raise AuthError("请粘贴 shiroJID 或整段 Cookie")
    cookie = xf.normalize_cookie(raw)

    try:
        xf.verify(cookie)
    except xf.UpstreamError as e:
        raise AuthError(f"该凭据无法访问易校园：{e}") from None

    found = xf.discover(cookie)
    ym_id = str(body.get("ym_id") or "").strip() or str(found.get("ym_id") or "")
    if not ym_id:
        raise AuthError("会话有效，但没能自动查到 ymId。请把 ymId 一并填上"
                        "（在易校园任意请求的参数里，是一串数字）。")

    room_fields = {}
    for k in ("area_id", "building_code", "floor_code", "room_code", "room_name",
              "building_name", "area_name"):
        v = str(body.get(k) or "").strip() or str(found.get(k) or "")
        if v:
            room_fields[k] = v

    save_session(conn(), source_type="xiaofubao", cookie=cookie, shiro_jid=cookie,
                 ym_id=ym_id, logged_in_at=datetime.now().isoformat(timespec="seconds"),
                 need_relogin=False, **room_fields)
    _kick_collect("导入会话")

    # 若探到了房间，直接登记成本地房间
    room = None
    if room_fields.get("room_code") or room_fields.get("room_name"):
        label = f"{room_fields.get('building_name','')} {room_fields.get('room_name','')}".strip()
        key = "-".join(str(x) for x in (
            room_fields.get("area_id") or "0",
            room_fields.get("building_code") or room_fields.get("building_name") or "0",
            room_fields.get("floor_code") or "0",
            room_fields.get("room_code") or room_fields.get("room_name") or "0",
        ))
        prev = (db.list_rooms(conn()) or [{}])[0]
        room = {
            "room_key": key,
            "label": label or "我的寝室",
            "school_code": "", "school_name": "",
            "area_id": room_fields.get("area_id", ""),
            "area_name": room_fields.get("area_name", ""),
            "building_code": room_fields.get("building_code", ""),
            "building_name": room_fields.get("building_name", ""),
            "floor_code": room_fields.get("floor_code", ""),
            "floor_name": "",
            "room_code": room_fields.get("room_code", ""),
            "room_name": room_fields.get("room_name", ""),
            "unit_price": float(prev.get("unit_price", 0.538)),
            "warn_kwh": float(prev.get("warn_kwh", 20)),
            "critical_kwh": float(prev.get("critical_kwh", 5)),
        }
        db.upsert_room(conn(), room)
        save_session(conn(), room_key=key, room_label=room["label"])

    return {"ok": True, "ym_id": ym_id, "room": room,
            "discovered": {k: v for k, v in found.items() if k not in ("user",)},
            "message": "会话已保存" + ("，房间已自动登记" if room else "；未能自动识别房间，请手工填写")}


def api_auth_diagnose(q):
    """自检：把「哪一步断了」讲清楚，不用去猜。"""
    src = CFG["source"]
    kind = str(src.get("type", "demo")).lower()
    phone = (q.get("phone") or [""])[0].strip() or resolve_phone(CFG, conn())
    steps: list[dict] = []

    def add(name, ok, detail):
        steps.append({"step": name, "ok": ok, "detail": detail})

    add("数据源类型", True, f"{kind}（{CFG.get('_config_path')}）")
    if kind != "nofish":
        add("提示", True, f"当前是 {kind} 模式，不需要短信登录。")
        return {"ok": True, "steps": steps}

    cli = auth_client()

    # 1) 上游可达性
    try:
        schools = cli.schools("")
        add("上游连通性", True, f"学校列表接口正常，返回 {len(schools)} 所学校")
    except AuthError as e:
        add("上游连通性", False, f"连不上上游：{e}")
        return {"ok": False, "steps": steps}

    # 2) 网络路径（是否走了代理）
    import os

    proxies = {k: v for k, v in os.environ.items() if k.lower() in ("http_proxy", "https_proxy", "all_proxy")}
    add("网络路径", True, ("经代理 " + "、".join(f"{k}={v}" for k, v in proxies.items())) if proxies
        else "直连（未检测到代理环境变量）")

    # 3) 手机号格式
    ok_phone = len(phone) == 11 and phone.isdigit()
    add("手机号", ok_phone, f"{_mask(phone) if phone else '（未填写）'}"
        + ("" if ok_phone else " —— 必须是 11 位数字"))

    # 4) 该号码在上游是否已有记录（没有也不代表不能登录）
    if ok_phone:
        try:
            info = cli.user_info(phone)
            add("账号记录", True,
                f"已在上游登记；已绑定宿舍={info.get('is_bound')}；"
                f"{info.get('school_name','')} {info.get('building_name','')} {info.get('room_name','')}".strip())
        except AuthError as e:
            add("账号记录", True,
                f"上游暂无该号码记录（{e}）。这不影响发码，首次登录后就会建立记录。")

    # 5) 发码冷却
    import time as _t

    last = AuthClient._last.get("send:" + phone, 0.0)
    left = 60 - (_t.time() - last)
    add("发码冷却", left <= 0, "可以发送" if left <= 0 else f"还需等待 {int(left) + 1} 秒（本地冷却）")

    # 6) 是否已发过码、等了多久
    s = load_session(conn())
    if s.get("send_at"):
        try:
            waited = int(_t.time() - datetime.fromisoformat(s["send_at"]).timestamp())
        except (ValueError, TypeError):
            waited = -1
        if waited >= 0:
            add("本次发码", True,
                f"已于 {s['send_at'].replace('T', ' ')} 发送，已等待 {waited // 60} 分 {waited % 60} 秒，"
                f"累计发送 {int(s.get('send_count') or 0)} 次"
                + ("　—— 易校园短信实测延迟可达 20 分钟，继续等待即可，也可以放心重发（发的是同一条）"
                   if waited < 1200 else ""))

    return {"ok": all(s["ok"] for s in steps), "steps": steps,
            "hint": "短信迟迟未到是易校园短信网关的延迟，实测可达 20 分钟。"
                    "重复获取发的是同一条验证码、不会覆盖，所以收不到时可以放心重发（上游限 60 秒一次）。"
                    "若等待超过 30 分钟仍未到，可改用「粘贴易校园会话」方式绕过短信。"}


def api_auth_send_code(body):
    phone = str(body.get("phone") or "").strip()
    r = auth_client().send_code(phone)
    # 只有上游真的回了成功信封才提示已发送；上游消息原样透出
    upstream_msg = ""
    if isinstance(r, dict):
        upstream_msg = str(r.get("message") or r.get("msg") or "")
    s = load_session(conn())
    count = int(s.get("send_count") or 0) + 1
    # 只记「待验证手机号」，不算登录成功 —— 否则发一次码就变成已登录
    save_session(conn(), pending_phone=phone, login_stage="code_sent",
                 send_at=datetime.now().isoformat(timespec="seconds"),
                 send_count=count)
    return {"ok": True,
            "message": upstream_msg or f"验证码已发送至 {_mask(phone)}，请查看短信",
            "send_at": datetime.now().isoformat(timespec="seconds"),
            "send_count": count,
            "upstream": r}


def api_auth_login(body):
    phone = str(body.get("phone") or "").strip()
    code = str(body.get("sms_code") or body.get("code") or "").strip()
    school_code = str(body.get("school_code") or "").strip()
    school_name = str(body.get("school_name") or "").strip()
    if not school_code:
        school_code, school_name = resolve_school(CFG, conn())

    auth_client().login(phone, code, school_code, school_name)
    save_session(conn(), phone=phone, school_code=school_code, school_name=school_name,
                 need_relogin=False, logged_in_at=datetime.now().isoformat(timespec="seconds"))

    info = auth_client().user_info(phone)
    need_location = bool(info.get("need_location"))
    # 已经绑定宿舍的话，这里就能开始采了
    if not need_location:
        _kick_collect("登录")
    save_session(conn(), need_location=need_location,
                 bound_school=info.get("school_name") or school_name)

    if not need_location and info.get("is_bound"):
        _register_room_from_info(info)

    return {
        "ok": True,
        "need_location": need_location,
        "is_bound": bool(info.get("is_bound")),
        "user_info": {
            "school_name": info.get("school_name") or school_name,
            "area_name": info.get("area_name"),
            "building_name": info.get("building_name"),
            "floor_name": info.get("floor_name"),
            "room_name": info.get("room_name"),
        },
    }


def api_auth_relogin_send(body):
    phone = resolve_phone(CFG, conn())
    if not phone:
        raise AuthError("还没有登录过，请先完整登录一次")
    auth_client().relogin_send_code(phone)
    return {"ok": True, "message": f"验证码已发送至 {_mask(phone)}"}


def api_auth_relogin(body):
    phone = resolve_phone(CFG, conn())
    if not phone:
        raise AuthError("还没有登录过，请先完整登录一次")
    code = str(body.get("sms_code") or body.get("code") or "").strip()
    auth_client().relogin(phone, code)
    save_session(conn(), need_relogin=False,
                 last_ok_at=datetime.now().isoformat(timespec="seconds"))
    info = auth_client().user_info(phone)
    if not info.get("need_location") and info.get("is_bound"):
        _register_room_from_info(info)
    # 会话刚恢复，立刻补一次，顺便把停更那段的数据接上
    _kick_collect("重登")
    return {"ok": True, "user_info": info}


def api_auth_logout(body):
    clear_session(conn())
    return {"ok": True}


def api_auth_areas(q):
    phone = resolve_phone(CFG, conn())
    code, _ = resolve_school(CFG, conn())
    return {"items": auth_client().areas(phone, code)}


def api_auth_buildings(q):
    phone = resolve_phone(CFG, conn())
    area_id = (q.get("area_id") or [""])[0]
    return {"items": auth_client().buildings(phone, area_id)}


def api_auth_floors(q):
    phone = resolve_phone(CFG, conn())
    area_id = (q.get("area_id") or [""])[0]
    bc = (q.get("building_code") or [""])[0]
    return {"items": auth_client().floors(phone, area_id, bc)}


def api_auth_rooms(q):
    phone = resolve_phone(CFG, conn())
    area_id = (q.get("area_id") or [""])[0]
    bc = (q.get("building_code") or [""])[0]
    fc = (q.get("floor_code") or [""])[0]
    return {"items": auth_client().rooms(phone, area_id, bc, fc)}


def api_auth_set_location(body):
    phone = resolve_phone(CFG, conn())
    if not phone:
        raise AuthError("尚未登录")
    payload = {
        "area_id": body.get("area_id"),
        "area_name": body.get("area_name", ""),
        "building_code": body.get("building_code"),
        "building_name": body.get("building_name", ""),
        "floor_code": body.get("floor_code"),
        "floor_name": body.get("floor_name", ""),
        "room_code": body.get("room_code"),
        "room_name": body.get("room_name", ""),
    }
    auth_client().set_location(phone, payload)
    info = auth_client().user_info(phone)
    room = _register_room_from_info(info)
    save_session(conn(), need_location=False)
    # 房间刚确定，立刻采一次（含 7 天历史回填），别让用户对着空页面等
    _kick_collect("选宿舍")
    return {"ok": True, "room": room, "user_info": info}


def _register_room_from_info(info: dict) -> dict:
    """把上游返回的宿舍信息登记成本地房间。"""
    building = info.get("building_name") or ""
    room_name = info.get("room_name") or ""
    label = f"{building} {room_name}".strip() or "我的寝室"
    key = "-".join(str(x) for x in (
        info.get("area_id") or "0",
        info.get("building_code") or building,
        info.get("floor_code") or "0",
        info.get("room_code") or room_name,
    ))

    rooms = db.list_rooms(conn())
    prev = rooms[0] if rooms else {}
    room = {
        "room_key": key,
        "label": label,
        "school_code": str(info.get("school_code") or ""),
        "school_name": info.get("school_name") or "",
        "area_id": str(info.get("area_id") or ""),
        "area_name": info.get("area_name") or "",
        "building_code": str(info.get("building_code") or ""),
        "building_name": building,
        "floor_code": str(info.get("floor_code") or ""),
        "floor_name": info.get("floor_name") or "",
        "room_code": str(info.get("room_code") or ""),
        "room_name": room_name,
        # 电价/阈值沿用上一次配置，避免每次登录都要重填
        "unit_price": float(prev.get("unit_price", 0.538)),
        "warn_kwh": float(prev.get("warn_kwh", 20)),
        "critical_kwh": float(prev.get("critical_kwh", 5)),
    }
    # 单一学校 / 单一寝室：清掉旧房间，只保留当前这个
    c = conn()
    for r in rooms:
        if r["room_key"] != key:
            c.execute("DELETE FROM rooms WHERE room_key=?", (r["room_key"],))
    c.commit()
    db.upsert_room(c, room)
    save_session(c, room_key=key, room_label=label)
    return room


def api_test_email(body):
    """发一封测试邮件，验证 SMTP 配置。"""
    from .notify import test_email as _t

    r = _t(CFG)
    return {"ok": bool(r.get("ok")), "detail": r.get("detail")}


def api_collect(body):
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "已有采集任务在跑"}
    try:
        return collect_once(CFG, room_key=body.get("room") or None, verbose=False)
    finally:
        _lock.release()


def api_export(q):
    room = _pick_room(q)
    if not room:
        return {"error": "未配置房间"}
    days = _days(q, 365)
    readings, _, _ = _load(room, q)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "剩余金额(元)", "折算电量(度)", "单价(元/度)"])
    for r in readings:
        w.writerow([r["ts"], r.get("balance_cny"), r.get("degree"),
                    room.get("unit_price", 0.538)])
    return {"__csv__": buf.getvalue(), "filename": f"{room['room_key']}_readings.csv"}


def api_eschool(action, body):
    from .eschool_service import handle
    from .adapters import SourceError
    try:
        return handle(action,body,CFG)
    except SourceError as exc:
        return {'ok':False,'error':str(exc)}
    except (ValueError,KeyError,TypeError):
        return {'ok':False,'error':'易校园响应格式异常，请刷新验证码后重试。'}


ROUTES_GET = {
    '/api/eschool/status': lambda q: api_eschool('status',{}),
    "/api/health": api_health,
    "/api/rooms": api_rooms,
    "/api/summary": api_summary,
    "/api/series": api_series,
    "/api/daily": api_daily,
    "/api/hourly": api_hourly,
    "/api/weekday": api_weekday,
    "/api/forecast": api_forecast,
    "/api/anomalies": api_anomalies,
    "/api/heatmap": api_heatmap,
    "/api/alerts": api_alerts,
    "/api/hourly_series": api_hourly_series,
    "/api/quality": api_quality,
    "/api/distribution": api_distribution,
    "/api/cycles": api_cycles,
    "/api/stale": api_stale,
    "/api/power": api_power,
    "/api/compare": api_compare,
    "/api/insights": api_insights,
    "/api/export.csv": api_export,
    # 登录
    "/api/auth/status": api_auth_status,
    "/api/auth/schools": api_auth_schools,
    "/api/auth/diagnose": api_auth_diagnose,
    "/api/auth/areas": api_auth_areas,
    "/api/auth/buildings": api_auth_buildings,
    "/api/auth/floors": api_auth_floors,
    "/api/auth/rooms": api_auth_rooms,
}

ROUTES_POST = {
    **{'/api/eschool/'+action: (lambda body, a=action: api_eschool(a,body))
       for action in ('captcha','send-code','login','locations','room','authorize','auto-room')},
    "/api/collect": api_collect,
    "/api/alerts/ack": api_ack,
    "/api/alerts/test": api_test_email,
    # 登录
    "/api/auth/send-code": api_auth_send_code,
    "/api/auth/login": api_auth_login,
    "/api/auth/relogin-send-code": api_auth_relogin_send,
    "/api/auth/relogin": api_auth_relogin,
    "/api/auth/location": api_auth_set_location,
    "/api/auth/import-cookie": api_auth_import_cookie,
    "/api/auth/logout": api_auth_logout,
}


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "yischool-power/1.0"

    def log_message(self, fmt, *args):  # 降噪
        if "/api/" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    # -- utils
    def _json(self, obj, status=200):
        if isinstance(obj, dict) and "__csv__" in obj:
            data = obj["__csv__"].encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{obj["filename"]}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str):
        if path == '/login' and CFG['source'].get('type') == 'eschool':
            path = '/eschool'
        rel = path.lstrip("/") or "index.html"
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return
        if target.is_dir():
            target = target / "index.html"
        # 无扩展名时尝试补 .html，让 /login 这类干净路径也能用
        if not target.is_file() and not target.suffix:
            alt = target.with_suffix(".html")
            if alt.is_file():
                target = alt
        if not target.is_file():
            self._json({"error": "not found", "path": path}, 404)
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text") or ctype.endswith("javascript")
                         else ctype)
        self.send_header("Content-Length", str(len(data)))
        # 静态资源不缓存，避免改了前端却看到旧页面
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    # -- verbs
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ROUTES_GET:
            q = parse_qs(u.query)
            try:
                self._json(ROUTES_GET[u.path](q))
            except AuthError as e:
                self._json({"error": str(e), "auth": True}, 400)
            except Exception as e:  # noqa: BLE001
                self._json(_fail(e, u.path), 500)
            return
        self._static(u.path)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith('/api/eschool/'):
            allowed = {'http://127.0.0.1:' + str(CFG['app']['port']), 'http://localhost:' + str(CFG['app']['port'])}
            if self.headers.get('Host') not in {urlparse(v).netloc for v in allowed} or self.headers.get('Origin') not in allowed | {None} or self.headers.get('X-Eschool-Request') != '1':
                self._json({'error':'请从本机易校园登录页面操作'},403)
                return
        fn = ROUTES_POST.get(u.path)
        if not fn:
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 24 * 1024 * 1024:
            self._json({'error': '请求过大，请只导出余额查询抓包'}, 413)
            self.close_connection = True
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        try:
            self._json(fn(body))
        except AuthError as e:
            self._json({"error": str(e), "auth": True}, 400)
        except Exception as e:  # noqa: BLE001
            self._json(_fail(e, u.path), 500)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def _collector_loop(stop: threading.Event) -> None:
    """后台采集线程：整点对齐，和网页共用一个进程。

    之前只有 `cli.py serve` 会跑服务、采集要另开 `cli.py loop` —— 用户很自然会
    以为"部署了就会自己采"，结果数据一直停在最后一次手动采集的时间。
    现在 serve 默认自带采集，单进程即可 24 小时运行。
    """
    from .collect import collect_once, next_run_at

    interval = int(CFG["collect"].get("interval_minutes", 60) or 60)

    # 启动后先补一次：数据已经落后超过一个采集周期就立刻采，不用干等到整点
    if stop.wait(10):
        return
    try:
        c = db.connect(DB_FILE)
        rooms = db.list_rooms(c, enabled_only=True)
        last = db.last_reading(c, rooms[0]["room_key"]) if rooms else None
        c.close()
        lag = None
        if last and last.get("ts"):
            lag = (datetime.now() - datetime.fromisoformat(last["ts"])).total_seconds()
        if lag is None or lag > interval * 60 * 0.8:
            r = collect_once(CFG, verbose=False)
            new = sum(1 for x in (r.get("results") or []) if x.get("inserted"))
            tail = f"（落后 {lag/60:.0f} 分钟）" if lag else "（此前无数据）"
            print(f"[采集] 启动补采{tail} {'成功' if r.get('ok') else '失败'} "
                  f"新增 {new} 条", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[采集] 启动补采异常 {type(e).__name__}: {e}", flush=True)

    maybe_backup()
    maybe_weather()
    maybe_daytype()
    maybe_fit_model()

    while not stop.is_set():
        try:
            nxt = next_run_at(CFG)
            wait = max(5.0, (nxt - datetime.now()).total_seconds())
            if stop.wait(wait):
                break
            r = collect_once(CFG, verbose=False)
            ok = bool(r.get("ok"))
            new = sum(1 for x in (r.get("results") or []) if x.get("inserted"))
            print(f"[采集] {datetime.now():%H:%M:%S} {'成功' if ok else '失败'} "
                  f"新增 {new} 条 告警 {len(r.get('alerts') or [])} 条", flush=True)
            maybe_backup()
            maybe_weather()
            maybe_daytype()
            maybe_fit_model()
            if r.get("need_login"):
                print("[采集] 需要重新登录：打开网页点「重新登录」", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[采集] 异常 {type(e).__name__}: {e}", flush=True)
            if stop.wait(300):
                break


def _kick_collect(tag: str = "") -> None:
    """后台立刻采一次，别让用户对着空页面等下一个整点。

    登录、选完房间、导入 Cookie 之后都会触发。
    7 天历史回填也在这条路径上（房间读数 < 3 条时自动触发）。
    """
    def run() -> None:
        try:
            r = collect_once(CFG, verbose=False)
            new = sum(1 for x in (r.get("results") or []) if x.get("inserted"))
            bf = r.get("backfill") or {}
            extra = f"，回填历史 {bf.get('inserted')} 条" if bf.get("inserted") else ""
            print(f"[采集] {tag}后立即补采：新增 {new} 条{extra}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[采集] {tag}后补采失败：{type(e).__name__}: {e}", flush=True)

    threading.Thread(target=run, daemon=True, name="kick-collect").start()


def maybe_backup() -> None:
    """每天做一次数据库快照。上游只给 7 天历史，本地库丢了就找不回来了。"""
    bcfg = (CFG["app"].get("backup") or {})
    if not bcfg.get("enabled", True):
        return
    d = Path(str(bcfg.get("dir") or "data/backup"))
    if not d.is_absolute():
        d = Path(__file__).resolve().parent.parent / d
    try:
        r = db.backup_db(DB_FILE, d, keep=int(bcfg.get("keep", 30) or 30))
    except Exception as e:  # noqa: BLE001
        print(f"[备份] 失败 {type(e).__name__}: {e}", flush=True)
        return
    if r.get("ok") and not r.get("skipped"):
        print(f"[备份] 已生成 {Path(r['path']).name}"
              f"（{r['size']//1024} KB，保留 {r.get('kept')} 份）", flush=True)
    if r.get("removed"):
        print(f"[备份] 清理旧份：{', '.join(r['removed'])}", flush=True)


def maybe_weather() -> None:
    """每天拉一次逐小时气温。失败不影响采集。"""
    wcfg = (CFG["app"].get("weather") or {})
    if not wcfg.get("enabled", True):
        return
    from . import weather as W

    c = conn()
    # 今天已经拉过就跳过（预报一天更新几次就够）
    last = db.get_meta(c, "weather_fetched_at") or ""
    if last[:10] == datetime.now().strftime("%Y-%m-%d"):
        return
    try:
        rows = W.fetch(
            float(wcfg.get("latitude", 30.27)),
            float(wcfg.get("longitude", 120.16)),
            str(wcfg.get("timezone") or "Asia/Shanghai"),
            int(wcfg.get("past_days", 14) or 14),
            int(wcfg.get("forecast_days", 16) or 16),
        )
        W.save(c, rows)
        db.set_meta(c, "weather_fetched_at", datetime.now().isoformat(timespec="seconds"))
        past = sum(1 for r in rows if r["kind"] == "obs")
        print(f"[气温] 已更新 {len(rows)} 小时"
              f"（已发生 {past} / 预报 {len(rows)-past}）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[气温] 拉取失败（不影响采集）：{type(e).__name__}: {e}", flush=True)


def maybe_daytype() -> None:
    """给日期区间打日类型标签。标签天天在变，所以每次采集都刷一遍。"""
    dcfg = (CFG["app"].get("daytype") or {})
    if not dcfg.get("enabled", True):
        return
    from . import daytype as D

    c = conn()
    cd = Path(str(dcfg.get("cache_dir") or "data/holidays"))
    if not cd.is_absolute():
        cd = Path(__file__).resolve().parent.parent / cd
    try:
        today = date.today()
        d0 = today - timedelta(days=int(dcfg.get("lookback_days", 30) or 30))
        d1 = today + timedelta(days=int(dcfg.get("lookahead_days", 120) or 120))
        rows = D.for_range(d0, d1, cd, str(dcfg.get("term") or "2026-2027-1"))
        D.save(c, rows)
        db.set_meta(c, "daytype_updated_at", datetime.now().isoformat(timespec="seconds"))
    except Exception as e:  # noqa: BLE001
        print(f"[日类型] 更新失败（不影响采集）：{type(e).__name__}: {e}", flush=True)


def maybe_fit_model() -> None:
    """每天跑一次模型评估：够数据就拟合、回测、决定是否启用。

    之前 decide() 只有手动跑 cli.py model 才会执行 ——
    判断逻辑写了但没人调用，等于没接上。
    """
    from . import model as M
    from . import weather as W

    c = conn()
    last = db.get_meta(c, "model_fitted_at") or ""
    if last[:10] == datetime.now().strftime("%Y-%m-%d"):
        return

    rooms = db.list_rooms(c, enabled_only=True)
    if not rooms:
        return
    room = rooms[0]
    try:
        r = db.fetch_readings(c, room["room_key"])
        if len(r) < 24:
            return
        native = _native_unit()
        analyze.native_view(r, native)
        dd = analyze._fill_missing_eod(analyze.daily(r, 1.0))
        M.attach_kinds(dd, c)
        wx = W.load(c)
        dec = M.decide(dd, r, wx,
                       unit_price=float(room.get("unit_price") or 0.538) or 0.538)
        st = M.save_state(c, dec)
        db.set_meta(c, "model_fitted_at", datetime.now().isoformat(timespec="seconds"))

        act = []
        if st.get("weekday_active"):
            act.append("周几")
        if st.get("temp_active"):
            act.append("气温")
        if st.get("occ_active"):
            act.append("占用")
        if st.get("bias_active"):
            act.append("偏差")
        print(f"[模型] 已评估：{'、'.join(act) + ' 修正已启用' if act else '暂不启用任何修正'}"
              f"（可用日 {len([d for d in dd if d.get('usable')])}）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[模型] 评估失败（不影响采集）：{type(e).__name__}: {e}", flush=True)


def start_collector() -> threading.Event | None:
    app = CFG.get("app") or {}
    if not app.get("auto_collect", True):
        print("→ 自动采集：已关闭（app.auto_collect=false）")
        return None
    src = str(CFG["source"].get("type", "demo")).lower()
    if src == "demo":
        print("→ 自动采集：跳过（演示数据源无需采集）")
        return None
    stop = threading.Event()
    t = threading.Thread(target=_collector_loop, args=(stop,), daemon=True, name="collector")
    t.start()
    mins = int(CFG["collect"].get("interval_minutes", 60) or 60)
    off = int(CFG["collect"].get("minute_offset", 5) or 0)
    print(f"→ 自动采集：已启动（每 {mins} 分钟一次，整点后第 {off} 分钟）")
    return stop


def main():
    host = str(CFG["app"].get("host", "127.0.0.1"))
    port = int(CFG["app"].get("port", 8787))
    kind = str(CFG["source"].get("type", "demo")).lower()

    rooms = db.list_rooms(conn())
    if not rooms and kind == "demo":
        print("[!] 尚未配置房间，正在用 config 中的 rooms 初始化…")
        for r in CFG["rooms"]:
            db.upsert_room(conn(), r)
        rooms = db.list_rooms(conn())

    n = db.count_readings(conn())
    print(f"yischool-power · 数据源={kind} · 读数={n} 条 · 房间={len(rooms)} 个")

    if kind == "nofish":
        phone = resolve_phone(CFG, conn())
        if not phone:
            print("→ 尚未登录，请打开下面的地址用手机验证码登录：")
        else:
            s = load_session(conn())
            print(f"→ 已登录：{_mask(phone)}"
                  + (f" · {s.get('school_name','')}" if s.get("school_name") else "")
                  + (f" · {s.get('room_label','')}" if s.get("room_label") else ""))
            if s.get("need_relogin"):
                print("  ⚠️ 会话已过期，打开网页点「重新登录」重新获取验证码")
    elif n == 0:
        print("[!] 库里没有数据。先执行：python cli.py seed-demo  或  python cli.py collect")

    start_collector()

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"→ 仪表盘  http://{host}:{port}/")
    if kind == "nofish":
        print(f"→ 登录页  http://{host}:{port}/login")
    print(f"→ 接口    http://{host}:{port}/api/summary")
    print("Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
