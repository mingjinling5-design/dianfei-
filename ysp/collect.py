"""采集器：抓取 → 入库 → 分析 → 告警。

设计要点：
  * 易校园**每小时**更新一次电费，所以调度默认按整点对齐（collect.minute_offset）
  * 采集失败会重试（collect.retry），连续失败会发运维告警
  * 分析类告警（消耗异常 / 数据卡住 / 提前预警）走 ysp.alerts 规则引擎
  * 运维类告警（会话过期 / 连续采集失败）单独走 __ops__ 通道
"""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from typing import Any

from . import alerts as alert_engine
from . import analyze, db
from .adapters import SourceError, build_adapter
from .auth import load_session, resolve_phone, resolve_school, save_session
from .config import db_path, load_config


def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 采集

def collect_once(cfg: dict | None = None, room_key: str | None = None,
                 verbose: bool = True, run_alerts: bool = True) -> dict[str, Any]:
    cfg = cfg or load_config()
    conn = db.connect(db_path(cfg))
    session = load_session(conn)
    adapter = build_adapter(cfg, session)

    rooms = db.list_rooms(conn, enabled_only=True)
    if room_key:
        rooms = [r for r in rooms if r["room_key"] == room_key]
    if not rooms:
        conn.close()
        return {"ok": False, "error": "还没有房间。请先在网页上完成登录并选择宿舍，"
                                      "或运行 python cli.py seed-demo 看演示。"}

    if getattr(adapter, "name", "") == "nofish" and not resolve_phone(cfg, conn):
        conn.close()
        return {"ok": False,
                "error": "尚未登录。请打开 http://127.0.0.1:8787 用手机验证码登录。",
                "need_login": True}

    # 本地几乎没数据时，先把上游自带的 7 天历史灌进来，避免仪表盘空白
    auto_backfill: dict | None = None
    if getattr(adapter, "name", "") == "nofish":
        try:
            thin = [r for r in rooms if db.count_readings(conn, r["room_key"]) < 3]
        except Exception:  # noqa: BLE001
            thin = []
        if thin:
            auto_backfill = backfill_from_nofish(cfg, days=7)
            if verbose and auto_backfill.get("ok"):
                print(f"[i] 已回填上游历史 {auto_backfill.get('inserted', 0)} 条"
                      f"（{auto_backfill.get('days')} 天逐小时）")

    retry = int(cfg["collect"].get("retry", 3) or 1)
    retry_delay = int(cfg["collect"].get("retry_delay_seconds", 20) or 20)

    results: list[dict] = []
    alerts_fired: list[dict] = []
    any_ok = False
    any_relogin = False
    last_error = ""

    for room in rooms:
        entry: dict[str, Any] = {"room_key": room["room_key"], "label": room["label"]}
        r = None
        err = None
        for attempt in range(1, max(1, retry) + 1):
            try:
                r = adapter.fetch(room)
                break
            except SourceError as e:
                err = e
                if attempt < retry:
                    if verbose:
                        print(f"[~] {room['label']} 第 {attempt} 次失败：{e}，{retry_delay}s 后重试")
                    time.sleep(retry_delay)

        if r is None:
            entry.update(ok=False, error=str(err))
            last_error = str(err)
            results.append(entry)
            if verbose:
                print(f"[x] {room['label']}: {err}")
            if _mark_relogin_if_needed(cfg, conn, adapter):
                entry["need_relogin"] = True
                any_relogin = True
            continue

        any_ok = True
        ts = r.ts or _now_ts()
        inserted = db.insert_reading(conn, room["room_key"], ts,
                                     r.balance_kwh, r.balance_cny,
                                     source=r.source, raw=r.raw)
        entry.update(ok=True, inserted=inserted, ts=ts,
                     balance_kwh=r.balance_kwh, balance_cny=r.balance_cny)
        if getattr(r, "unit", ""):
            db.set_meta(conn, "native_unit", r.unit)
        save_session(conn, last_ok_at=_now_ts(), need_relogin=False)

        results.append(entry)
        if verbose:
            mark = "ok" if inserted else "重复"
            unit = "元" if getattr(r, "unit", "") == "cny" else "度"
            val = r.balance_cny if unit == "元" else r.balance_kwh
            print(f"[✓] {room['label']}: {val} {unit} ({mark})")
        time.sleep(random.uniform(0.4, 1.2))

    # ---------------- 分析类告警
    if run_alerts and cfg.get("alerts", {}).get("enabled"):
        native = str(db.get_meta(conn, "native_unit", "kwh"))
        for room in rooms:
            entry = next((x for x in results if x["room_key"] == room["room_key"]), None)
            if not entry or not entry.get("ok"):
                continue
            readings = db.fetch_readings(conn, room["room_key"])
            if len(readings) < 2:
                continue
            analyze.native_view(readings, native)
            bal = _last_native(conn, room, native)
            fired = alert_engine.evaluate(conn, cfg, room, readings, native, bal)
            for a in fired:
                a["room_key"] = room["room_key"]
            if fired:
                alert_engine.flush(conn, cfg, fired)
                alerts_fired.extend(fired)
                entry["alerts"] = [a["title"] for a in fired]
                if verbose:
                    for a in fired:
                        print(f"[!] {a['title']}")

    # ---------------- 运维告警
    if cfg.get("alerts", {}).get("enabled"):
        rules = (cfg["alerts"].get("rules") or {})
        if any_relogin and rules.get("session_expired", True):
            alert_engine.send_ops_alert(
                conn, cfg, "session_expired", "critical",
                "易校园登录已过期，需要重新验证码登录",
                [("时间", _now_ts().replace("T", " ")),
                 ("状态", "采集失败：会话失效"),
                 ("处理", "打开网页点「重新登录」，收一次短信验证码即可")],
                "在会话恢复之前，采集会一直失败，电费数据不会更新。")
        if not any_ok and last_error and rules.get("collect_failed", True):
            key = db.get_meta(conn, "fail_streak", 0) or 0
            streak = int(key) + 1
            db.set_meta(conn, "fail_streak", streak)
            need = int(rules.get("collect_failed_times", 3) or 3)
            if streak >= need:
                alert_engine.send_ops_alert(
                    conn, cfg, "collect_failed", "warn",
                    f"连续 {streak} 次采集失败",
                    [("时间", _now_ts().replace("T", " ")),
                     ("连续失败", f"{streak} 次"),
                     ("最近错误", last_error[:300])],
                    "请检查网络、登录状态，或运行 python cli.py check 排查。")
        elif any_ok:
            db.set_meta(conn, "fail_streak", 0)

    if results:
        db.set_meta(conn, "last_collect", _now_ts())
    conn.close()
    return {"ok": any_ok, "at": _now_ts(), "results": results,
            "alerts": [{"level": a["level"], "title": a["title"]} for a in alerts_fired],
            "backfill": auto_backfill,
            "need_relogin": any_relogin}


def _last_native(conn, room: dict, native: str) -> float | None:
    last = db.last_reading(conn, room["room_key"])
    if not last:
        return None
    if native == "cny":
        return last.get("balance_cny") if last.get("balance_cny") is not None else last.get("balance_kwh")
    return last.get("balance_kwh")


def _mark_relogin_if_needed(cfg: dict, conn, adapter) -> bool:
    """采集失败时问一次上游：是会话过期还是别的问题。"""
    if getattr(adapter, "name", "") != "nofish":
        return False
    phone = resolve_phone(cfg, conn)
    if not phone:
        return False
    try:
        from .auth import AuthClient

        src = cfg["source"]
        client = AuthClient(str(src.get("base_url") or "https://tool.no-fish.cn"),
                            int(src.get("timeout", 20)), bool(src.get("strict_tls", False)))
        info = client.user_info(phone)
    except Exception:  # noqa: BLE001
        return False
    if info.get("need_relogin"):
        save_session(conn, need_relogin=True)
        return True
    return False


# ---------------------------------------------------------------- 整点调度

def next_run_at(cfg: dict, now: datetime | None = None) -> datetime:
    """下一次该跑的时刻：对齐到「整点 + minute_offset 分钟」。"""
    now = now or datetime.now()
    interval = int(cfg["collect"].get("interval_minutes", 60) or 60)
    offset = int(cfg["collect"].get("minute_offset", 5) or 0)

    base = now.replace(second=0, microsecond=0)
    # 先推到今天内偏移点
    candidate = base.replace(minute=0) + timedelta(minutes=offset)
    step = max(1, interval)
    while candidate <= now:
        candidate += timedelta(minutes=step)
    return candidate


def seconds_until(cfg: dict, now: datetime | None = None) -> float:
    nxt = next_run_at(cfg, now)
    return max(1.0, (nxt - (now or datetime.now())).total_seconds())


def run_forever(cfg: dict | None = None, verbose: bool = True) -> int:
    """常驻调度：整点对齐采集，Ctrl+C 退出。"""
    cfg = cfg or load_config()
    interval = int(cfg["collect"].get("interval_minutes", 60) or 60)
    offset = int(cfg["collect"].get("minute_offset", 5) or 0)
    jitter = int(cfg["collect"].get("jitter_seconds", 90) or 0)

    print(f"常驻采集已启动：每 {interval} 分钟一次，对齐到整点后第 {offset} 分钟"
          f"（易校园每小时更新一次电费）")
    if jitter:
        print(f"每次会附加 0~{jitter} 秒随机抖动，避免固定时刻打上游")
    print("Ctrl+C 退出\n")

    while True:
        try:
            nxt = next_run_at(cfg)
            wait = (nxt - datetime.now()).total_seconds() + random.uniform(0, jitter)
            print(f"[{datetime.now():%m-%d %H:%M:%S}] 下次采集：{nxt:%m-%d %H:%M:%S}"
                  f"（{int(wait // 60)} 分 {int(wait % 60)} 秒后）")
            time.sleep(max(1.0, wait))

            r = collect_once(cfg, verbose=verbose)
            if not r.get("ok"):
                print(f"[!] {r.get('error') or '采集未成功'}")
            elif verbose:
                ok = sum(1 for x in r["results"] if x.get("ok"))
                new = sum(1 for x in r["results"] if x.get("inserted"))
                print(f"[✓] {datetime.now():%H:%M:%S} 成功 {ok}/{len(r['results'])}，"
                      f"新增 {new} 条，告警 {len(r.get('alerts') or [])} 条")
            print()
        except KeyboardInterrupt:
            print("\nbye")
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"[!] 本轮异常：{type(e).__name__}: {e}，60 秒后重试")
            time.sleep(60)


# ---------------------------------------------------------------- 回填

def seed_demo(cfg: dict | None = None, days: int = 45) -> dict:
    """写入演示数据，方便立刻查看仪表盘。"""
    cfg = cfg or load_config()
    conn = db.connect(db_path(cfg))
    for room in cfg["rooms"]:
        db.upsert_room(conn, room)

    from .adapters.demo import DemoAdapter

    ad = DemoAdapter(cfg)
    total = 0
    for room in cfg["rooms"]:
        rows = ad.seed_history(room, days=days)
        total += db.bulk_insert(conn, rows)
    conn.close()
    return {"ok": True, "inserted": total, "days": days}


def backfill_from_nofish(cfg: dict | None = None, days: int = 7,
                         room_key: str | None = None) -> dict:
    """把上游自带的 7 天逐小时历史灌进本地库，仪表盘立刻就有曲线。

    上游 records 返回形如：
        [{"surplus_amount":107.32,"queried_at":"2026-09-19T03:00:00Z"}, ...]
    注意：surplus_amount 单位是**元**、queried_at 是 **UTC**，都要转换。
    """
    from .adapters import NoFishAdapter
    from .adapters.base import dig_cny, dig_kwh

    cfg = cfg or load_config()
    conn = db.connect(db_path(cfg))
    phone = resolve_phone(cfg, conn)
    if not phone:
        conn.close()
        return {"ok": False, "error": "尚未登录"}

    rooms = db.list_rooms(conn, enabled_only=True)
    if room_key:
        rooms = [r for r in rooms if r["room_key"] == room_key]
    if not rooms:
        conn.close()
        return {"ok": False, "error": "没有房间"}

    ad = NoFishAdapter(cfg, phone=phone)
    try:
        payload = ad.history(days)
    except SourceError as e:
        conn.close()
        return {"ok": False, "error": str(e)}
    if not isinstance(payload, list):
        conn.close()
        return {"ok": False, "error": f"历史接口返回异常：{str(payload)[:200]}"}

    rows: list[tuple] = []
    skipped = 0
    for item in payload:
        if not isinstance(item, dict):
            skipped += 1
            continue
        stamp = item.get("queried_at") or item.get("ts") or item.get("time")
        if not stamp:
            skipped += 1
            continue
        try:
            ts = _to_local_iso(str(stamp))
        except ValueError:
            skipped += 1
            continue
        cny = dig_cny(item)
        kwh = dig_kwh(item)
        if cny is None and kwh is None:
            skipped += 1
            continue
        for room in rooms:
            price = float(room.get("unit_price") or 0.538)
            if cny is not None:
                rows.append((room["room_key"], ts, None, cny, "nofish-history",
                             json.dumps(item, ensure_ascii=False)[:2000]))
            else:
                rows.append((room["room_key"], ts, kwh,
                             round(kwh * price, 2) if kwh is not None else None,
                             "nofish-history", json.dumps(item, ensure_ascii=False)[:2000]))

    inserted = db.bulk_insert(conn, rows)
    if rows:
        db.set_meta(conn, "native_unit", "cny")
    conn.close()
    return {"ok": True, "fetched": len(payload), "inserted": inserted,
            "skipped": skipped, "days": days}


def _to_local_iso(stamp: str) -> str:
    """把上游的 UTC 时间戳转成本地时间 ISO 串。"""
    s = stamp.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(stamp.strip()[:19], "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")
