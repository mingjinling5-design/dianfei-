"""分析引擎：日耗、充值识别、时段画像、异常检测、耗尽预测、成本核算。

全部纯 stdlib（statistics / math / datetime），不依赖 pandas/numpy。
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta
from typing import Any

# 单次余额上涨超过该值即判定为“充值”而非抖动
RECHARGE_THRESHOLD_KWH = 3.0
# 判定异常用的 MAD 倍数
ANOMALY_Z = 3.0
# 宿舍用电的物理上限：超过这个功率不可能是真实用电，
# 而是抄表修正 / 换表 / 系统对账（实测上游确实出现过 1 小时掉 68 度的情况）
ADJUST_RATE_KW = 3.5
# 相邻多久之内的多次充值合并为一次
RECHARGE_MERGE_HOURS = 3.0


# ---------------------------------------------------------------- 单位归一
# 上游可能以「元」或「度」计量。整套分析只在**一种**原生单位上做，
# 不去做元↔度的折算（校园多为阶梯电价，固定单价反推会失真）。
# 约定：分析层统一读写 readings[*]["balance_kwh"] 这个键，
#       native_unit == "cny" 时把「元」的值搬进去，单价按 1.0 处理
#       （即：1 元余额 - 1 元消耗 = 1 元剩余，数学上自洽）。

def native_view(readings: list[dict], native_unit: str) -> list[dict]:
    """把读数统一到原生单位视角。会就地修改并返回 readings。"""
    if native_unit == "cny":
        for r in readings:
            if r.get("balance_cny") is not None:
                r["balance_kwh"] = r["balance_cny"]
    return readings


def native_price(room: dict, native_unit: str) -> float:
    """原生单位下的“单价”：元制就是 1.0 元/元，度制才用电价。"""
    if native_unit == "cny":
        return 1.0
    return float(room.get("unit_price") or 0.538)


UNITS = {
    "cny": {"amount": "元", "rate": "元/天", "balance": "余额(元)"},
    "kwh": {"amount": "度", "rate": "度/天", "balance": "剩余电量(度)"},
}


def unit_labels(native_unit: str) -> dict:
    return UNITS.get(native_unit, UNITS["kwh"])


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _day(ts: str) -> date:
    return _parse(ts).date()


# ---------------------------------------------------------------- 片段化

def segments(readings: list[dict], recharge_threshold: float = RECHARGE_THRESHOLD_KWH) -> list[dict]:
    """把读数序列切成“用电片段”，识别充值点与数据修正点。

    返回每段：{start, end, kwh, hours, rate_kw, recharge, adjust}
      * recharge > 0 → 该段是充值（余额跳升），不计用电
      * adjust   > 0 → 该段是抄表修正/换表（余额跳降但功率不物理），不计用电
    """
    segs: list[dict] = []
    for prev, cur in zip(readings, readings[1:]):
        b0, b1 = prev.get("balance_kwh"), cur.get("balance_kwh")
        if b0 is None or b1 is None:
            continue
        t0, t1 = _parse(prev["ts"]), _parse(cur["ts"])
        hours = (t1 - t0).total_seconds() / 3600.0
        if hours <= 0:
            continue
        delta = b1 - b0
        base = {"start": t0, "end": t1, "hours": hours, "kwh": 0.0,
                "rate_kw": 0.0, "recharge": 0.0, "adjust": 0.0}
        if delta > recharge_threshold:
            base["recharge"] = round(delta, 3)
        else:
            kwh = max(0.0, -delta)
            rate = kwh / hours
            if rate > ADJUST_RATE_KW:
                # 物理上不可能：判为数据修正，不计入用电
                base["adjust"] = round(kwh, 3)
            else:
                base["kwh"] = kwh
                base["rate_kw"] = rate
        segs.append(base)
    return segs


def split_segments(segs: list[dict], unit: str = "hour") -> list[dict]:
    """把跨时钟边界的片段切开。

    为什么必须切：读数间隔不总是整点（比如采集中断后补采，会拿到一段
    10:51→13:28 的 2.62 小时区间）。如果不切，这整段的消耗会被算给 10 点，
    而算功率时分母又是 3.47 小时 —— 于是出现「一个小时花 0.91 元」却只有
    488W 这种对不上的数字。切开之后每个小时的分子分母才是一致的。

    unit="hour" 按小时切，unit="day" 按自然日切（跨零点的片段）。
    """
    def boundary_after(t: datetime) -> datetime:
        if unit == "day":
            return (t.replace(hour=0, minute=0, second=0, microsecond=0)
                    + timedelta(days=1))
        return t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    out: list[dict] = []
    for s in segs:
        total = (s["end"] - s["start"]).total_seconds()
        if total <= 0:
            continue
        t = s["start"]
        first = True
        while t < s["end"]:
            nxt = min(boundary_after(t), s["end"])
            frac = (nxt - t).total_seconds() / total
            piece = dict(s)
            piece["start"] = t
            piece["end"] = nxt
            piece["hours"] = (nxt - t).total_seconds() / 3600.0
            piece["kwh"] = s["kwh"] * frac
            piece["rate_kw"] = piece["kwh"] / piece["hours"] if piece["hours"] > 0 else 0.0
            # 充值/修正只在第一片保留，避免重复计数
            if not first:
                piece["recharge"] = 0.0
                piece["adjust"] = 0.0
            out.append(piece)
            first = False
            t = nxt
    return out


def _estimate_use_between(readings: list[dict], t0: datetime, t1: datetime,
                           unit_price: float = 1.0) -> float:
    """估算 [t0, t1) 这段时间的用电量。

    用途：修正充值金额。充值那一小时里余额既涨又被消耗，
    我们只能看到净值，所以要按本寝室的作息曲线把被吃掉的那部分补回去。
    """
    if t1 <= t0:
        return 0.0
    w = hour_weights(readings)                 # 24 个权重，和 = 1
    # 日耗基准：最近可用日的均值
    # ⚠️ 局部变量不能叫 daily —— 会遮住模块级的 daily() 函数
    dd = _fill_missing_eod(daily(readings, unit_price))
    use = usable_days(dd)
    if not use:
        return 0.0
    day_rate = statistics.fmean([d["kwh"] for d in use[-7:]])
    if day_rate <= 0:
        return 0.0

    total = 0.0
    t = t0
    while t < t1:
        nxt = min((t.replace(minute=0, second=0, microsecond=0)
                   + timedelta(hours=1)), t1)
        frac = (nxt - t).total_seconds() / 3600.0
        total += day_rate * w[t.hour] * frac
        t = nxt
    return total


# 充值金额取整的容差：修正后离整数多近就认为是整数充值
RECHARGE_SNAP_TOL = 0.75


def recharge_events(readings: list[dict], threshold: float = RECHARGE_THRESHOLD_KWH,
                    merge_hours: float = RECHARGE_MERGE_HOURS,
                    unit_price: float = 1.0,
                    correct: bool = True) -> list[dict]:
    """识别充值点，并修正金额。

    为什么要修正
    ------------
    余额是每小时刷新的。充值后到下一次读数之间**既充了钱又用了电**，
    我们只能看到净值，于是金额总是偏小一点。

        22:00  6.45 元
        23:00  80.60 元     +74.15
        00:00  155.35 元    +74.75
        ─────────────────────────────
        检测到 148.90 元   ← 但实际是 150 元
        那 2 小时用掉的 ~1.1 元被吞了

    修法：用本寝室的作息曲线估算这段区间的用电，补回净值上；
    修正后接近整数就取整（宿舍充值基本都是整数）。

    返回每项：{ts, kwh(修正后), kwh_raw(检测到的净值), estimated(补回的量), rounded}
    """
    raw: list[dict] = []
    for prev, cur in zip(readings, readings[1:]):
        b0, b1 = prev.get("balance_kwh"), cur.get("balance_kwh")
        if b0 is None or b1 is None:
            continue
        d = b1 - b0
        if d > threshold:
            raw.append({"ts": cur["ts"], "kwh": round(d, 3), "count": 1,
                        "from": prev["ts"]})

    merged: list[dict] = []
    for e in raw:
        if merged:
            last = merged[-1]
            gap = (_parse(e["ts"]) - _parse(last["ts"])).total_seconds() / 3600.0
            if gap <= merge_hours:
                last["kwh"] = round(last["kwh"] + e["kwh"], 3)
                last["count"] += 1
                last["ts"] = e["ts"]
                continue
        merged.append(dict(e))

    if not correct:
        for e in merged:
            e.setdefault("from", e["ts"])
            e["kwh_raw"] = e["kwh"]
            e["estimated"] = 0.0
            e["rounded"] = False
        return merged

    # 逐笔修正
    for e in merged:
        e["kwh_raw"] = e["kwh"]
        t0, t1 = _parse(e.get("from") or e["ts"]), _parse(e["ts"])
        est = _estimate_use_between(readings, t0, t1, unit_price)
        e["estimated"] = round(est, 3)
        total = e["kwh"] + est
        # 接近整数就取整 —— 宿舍充值基本都是整数
        nearest = round(total)
        if abs(total - nearest) <= RECHARGE_SNAP_TOL:
            e["kwh"] = round(float(nearest), 2)
            e["rounded"] = True
        else:
            e["kwh"] = round(total, 2)
            e["rounded"] = False
    return merged


def adjust_events(readings: list[dict]) -> list[dict]:
    """识别抄表修正 / 换表导致的余额跳降。"""
    return [{"ts": s["start"].isoformat(timespec="minutes"), "kwh": s["adjust"]}
            for s in segments(readings) if s["adjust"] > 0]


# ---------------------------------------------------------------- 数据卡住

# 余额连续完全不变多少小时算“卡住”
STALE_HOURS = 12
# 判定“一分钱都没动”的容差
STALE_EPS = 0.005


def detect_stale(readings: list[dict], hours: float = STALE_HOURS) -> list[dict]:
    """检测「数据卡住」：余额长时间一模一样，一点没变。

    这不是“没用电”，而是上游/电表没更新 —— 实测易校园出现过
    连续一周余额不变的故障。卡住期间所有消耗统计都是假的，必须单独标出来。

    返回 [{start, end, hours, value, ongoing}]
    """
    out: list[dict] = []
    if len(readings) < 2:
        return out

    def val(r):
        return r.get("balance_kwh")

    run_start = None
    run_val = None
    prev_t = None
    for r in readings:
        t = _parse(r["ts"])
        v = val(r)
        if v is None:
            continue
        if run_start is None:
            run_start, run_val, prev_t = t, v, t
            continue
        # 与基准值比：只要变了就结束当前段
        if abs(v - run_val) > STALE_EPS:
            span = (prev_t - run_start).total_seconds() / 3600.0
            if span >= hours:
                out.append({"start": run_start.isoformat(timespec="minutes"),
                            "end": prev_t.isoformat(timespec="minutes"),
                            "hours": round(span, 1), "value": round(run_val, 3),
                            "ongoing": False})
            run_start, run_val = t, v
        prev_t = t

    if run_start is not None:
        span = (prev_t - run_start).total_seconds() / 3600.0
        if span >= hours:
            out.append({"start": run_start.isoformat(timespec="minutes"),
                        "end": prev_t.isoformat(timespec="minutes"),
                        "hours": round(span, 1), "value": round(run_val, 3),
                        "ongoing": True})
    return out


def stale_now(readings: list[dict], hours: float = STALE_HOURS) -> dict | None:
    """当前是否正处于「卡住」状态（用于实时告警）。"""
    evs = detect_stale(readings, hours)
    for e in reversed(evs):
        if e["ongoing"]:
            return e
    return None


# ---------------------------------------------------------------- 缺口/质量

def detect_gaps(readings: list[dict], expect_minutes: int = 60,
                tolerance: float = 2.5) -> list[dict]:
    """检测采集缺口：相邻两点间隔超过预期。"""
    out: list[dict] = []
    limit = expect_minutes * tolerance
    for a, b in zip(readings, readings[1:]):
        gap = (_parse(b["ts"]) - _parse(a["ts"])).total_seconds() / 60.0
        if gap > limit:
            out.append({"start": a["ts"], "end": b["ts"], "minutes": round(gap, 1),
                        "missed": int(gap / expect_minutes) - 1})
    return out


def data_quality(readings: list[dict], expect_minutes: int = 60) -> dict:
    """数据质量指标：给用户一个“这份统计可不可信”的判断。"""
    if not readings:
        return {"score": 0, "level": "无数据", "readings": 0, "notes": ["还没有任何读数"]}

    t0, t1 = _parse(readings[0]["ts"]), _parse(readings[-1]["ts"])
    span_h = max(0.001, (t1 - t0).total_seconds() / 3600.0)
    expected = max(1, int(span_h / (expect_minutes / 60.0)) + 1)
    gaps = detect_gaps(readings, expect_minutes)
    missing = sum(g["missed"] for g in gaps)
    stale = detect_stale(readings, STALE_HOURS)
    stale_h = sum(s["hours"] for s in stale)

    coverage = max(0.0, min(1.0, len(readings) / expected))
    fresh = max(0.0, 1.0 - stale_h / span_h)
    gap_pen = max(0.0, 1.0 - missing / expected) if expected else 0.0
    score = round((coverage * 0.45 + fresh * 0.35 + gap_pen * 0.20) * 100)

    notes: list[str] = []
    if not any(abs(r.get("balance_kwh") or 0) > 0 for r in readings):
        notes.append("所有读数都是 0，可能电表离线或已被断电")
    if stale:
        notes.append(f"有 {len(stale)} 段数据卡住，合计 {round(stale_h,1)} 小时"
                     f"（最长 {max(s['hours'] for s in stale)} 小时），该时段的消耗统计不可信")
    if gaps:
        notes.append(f"有 {len(gaps)} 处采集中断，漏采约 {missing} 个点")
    if coverage < 0.8:
        notes.append(f"采样覆盖率只有 {round(coverage*100)}%")
    if not notes:
        notes.append("数据完整，统计可信")

    level = "良好" if score >= 85 else ("一般" if score >= 60 else "较差")
    return {
        "score": score, "level": level,
        "readings": len(readings),
        "span_hours": round(span_h, 1),
        "expected": expected,
        "missing": missing,
        "coverage_pct": round(coverage * 100, 1),
        "stale_segments": stale,
        "stale_hours": round(stale_h, 1),
        "gaps": gaps[-20:],
        "freshness_minutes": round((datetime.now() - t1).total_seconds() / 60.0, 1),
        "notes": notes,
    }


# ---------------------------------------------------------------- 小时级

def hourly_series(readings: list[dict], unit_price: float = 1.0) -> list[dict]:
    """小时粒度序列：每小时的用电量，并标注该小时是否属于卡住/修正段。"""
    segs = split_segments(segments(readings), "hour")
    buckets: dict[str, dict] = {}
    for s in segs:
        key = s["start"].replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        b = buckets.setdefault(key, {"ts": key, "kwh": 0.0, "recharge": 0.0,
                                     "adjust": 0.0, "samples": 0})
        b["kwh"] += s["kwh"]
        b["recharge"] += s["recharge"]
        b["adjust"] += s["adjust"]
        b["samples"] += 1

    # 每个小时的收盘余额
    eod: dict[str, float] = {}
    for r in readings:
        if r.get("balance_kwh") is None:
            continue
        t = _parse(r["ts"]).replace(minute=0, second=0, microsecond=0)
        eod[t.isoformat(timespec="seconds")] = r["balance_kwh"]

    for k, b in buckets.items():
        b["kwh"] = round(b["kwh"], 4)
        b["recharge"] = round(b["recharge"], 3)
        b["adjust"] = round(b["adjust"], 3)
        b["balance"] = eod.get(k)
        b["cny"] = round(b["kwh"] * unit_price, 3)

    stale_spans = detect_stale(readings, STALE_HOURS)
    def in_stale(ts: str) -> bool:
        t = _parse(ts)
        for s in stale_spans:
            if _parse(s["start"]) <= t <= _parse(s["end"]):
                return True
        return False

    return [{**buckets[k], "stale": in_stale(k)} for k in sorted(buckets)]


def distribution(days: list[dict], bins: int = 12) -> dict:
    """日消耗分布直方图 + 分位数，用来判断“多少算多”。

    只统计「可用日」—— 今天的半天和卡住日的 0 值会把分布拉歪。
    """
    vals = sorted(d["kwh"] for d in usable_days(days))
    if len(vals) < 3:
        return {"available": False, "reason": f"可用日不足（当前 {len(vals)} 天，至少需要 3 天）"}
    lo, hi = vals[0], vals[-1]
    if hi <= lo:
        hi = lo + 1.0
    width = (hi - lo) / bins
    hist = []
    for i in range(bins):
        a = lo + i * width
        b = a + width
        n = sum(1 for v in vals if (a <= v < b) or (i == bins - 1 and v == b))
        hist.append({"from": round(a, 2), "to": round(b, 2), "count": n})

    def q(p: float) -> float:
        if len(vals) == 1:
            return vals[0]
        k = (len(vals) - 1) * p
        f = int(k)
        c = min(f + 1, len(vals) - 1)
        return vals[f] + (vals[c] - vals[f]) * (k - f)

    return {
        "available": True, "bins": hist, "count": len(vals),
        "min": round(vals[0], 3), "max": round(vals[-1], 3),
        "mean": round(statistics.fmean(vals), 3),
        "median": round(statistics.median(vals), 3),
        "p25": round(q(0.25), 3), "p75": round(q(0.75), 3), "p90": round(q(0.90), 3),
        "p95": round(q(0.95), 3),
    }


def billing_cycles(readings: list[dict], unit_price: float = 1.0,
                   now: datetime | None = None) -> list[dict]:
    """充值周期分析：**每次充值之后**用了多少、撑了几天、还剩多少。

    两个容易搞错的地方
    ------------------
    1. **口径是「充值之后」**，不是「充值之前」。
       之前统计的是「上次充值 → 这次充值之间」，那是已经结束的历史，
       数字永远不会变（用户反馈「历次充值使用时长一直没变」就是这个原因）。

    2. **期初余额要带上结转**。
       充值前账户里往往还有几块钱没花完：

           充值前  6.45 元
           充值后  155.35 元    ← 充了 150，但可用其实是 155.35

       所以「撑了几天」必须按 155.35「算，只按 150 算会低估使用时长。

    返回 [{ts, recharge, carry, opening, used, remaining, days, per_day,
           lasts_days, ongoing, ...}]
    """
    rc = recharge_events(readings, unit_price=unit_price)
    if not rc:
        return []

    # 时间戳 → 余额，方便取「充值后那一刻」的实际余额
    bal_at: dict[str, float] = {}
    for r in readings:
        v = r.get("balance_kwh")
        if v is not None:
            bal_at[r["ts"]] = float(v)
    ts_sorted = sorted(bal_at)

    def balance_at(ts: str) -> float | None:
        if ts in bal_at:
            return bal_at[ts]
        prev = None
        for t in ts_sorted:
            if t <= ts:
                prev = t
            else:
                break
        return bal_at.get(prev) if prev else None

    segs = segments(readings)
    now = now or (_parse(readings[-1]["ts"]) if readings else datetime.now())

    out: list[dict] = []
    for i, e in enumerate(rc):
        start = _parse(e["ts"])
        end = _parse(rc[i + 1]["ts"]) if i + 1 < len(rc) else now

        amount = float(e["kwh"])                    # 本次充进去的
        opening = balance_at(e["ts"])               # 充值后的实际余额
        if opening is None:
            opening = amount
        carry = round(opening - amount, 2)          # 从上一期结转过来的

        # ⚠️ 期末余额不能直接取「下一次充值那一刻的余额」——
        #    那个读数已经包含了下一次充的钱，做减法会得到负的「已用」，
        #    再被 max(0,...) 截成 0（表现为每行都显示「已用 0.00」）。
        #    正确做法是把下一笔充值减掉。
        closing = None
        if i + 1 < len(rc):
            nxt = rc[i + 1]
            nxt_bal = balance_at(nxt["ts"])
            if nxt_bal is not None:
                closing = nxt_bal - float(nxt["kwh"])
        if closing is None:
            closing = bal_at.get(ts_sorted[-1]) if ts_sorted else opening

        used = max(0.0, opening - closing)
        days = (end - start).total_seconds() / 86400.0
        per_day = used / days if days > 0.1 else None

        out.append({
            "ts": e["ts"],
            "recharge": round(amount, 2),
            "recharge_raw": round(e.get("kwh_raw", amount), 2),
            "estimated": round(e.get("estimated", 0.0), 2),
            "rounded": bool(e.get("rounded")),
            "carry": carry,                          # 充值前没用完的
            "opening": round(opening, 2),            # 本期真正可用的总额
            "used": round(used, 2),
            "remaining": round(closing, 2),
            "days": round(days, 2),
            "per_day": round(per_day, 3) if per_day else None,
            "cny": round(used * unit_price, 2),
            "ongoing": i + 1 >= len(rc),
            "lasts_days": (round(closing / per_day, 1)
                           if per_day and per_day > 0 and closing > 0 else None),
        })
    return out


# ---------------------------------------------------------------- 对比（环比/同比）

def _sum_window(readings: list[dict], d0: date, d1: date,
                max_hour: int | None = None) -> float:
    """统计 [d0, d1] 区间内的用电量。

    max_hour 用于「同期对齐」：比如今天只到 14 点，那昨天也只算到 14 点，
    否则拿半天比整天，数字必然难看。
    """
    total = 0.0
    # ⚠️ 必须按小时切开：跨小时的片段如果整段算给起始小时，
    #    「截至 11 点」会把 11 点之后的部分也算进去。
    for s in split_segments(segments(readings), "hour"):
        d = s["start"].date()
        if not (d0 <= d <= d1):
            continue
        if max_hour is not None and s["start"].hour > max_hour:
            continue
        total += s["kwh"]
    return round(total, 3)


def _pct(cur: float, prev: float) -> float | None:
    if prev is None or abs(prev) < 1e-9:
        return None
    return round((cur - prev) / prev * 100, 1)


def _cmp_block(name: str, cur: float, prev: float, cur_label: str, prev_label: str,
               days_used: int) -> dict:
    return {
        "name": name,
        "current": round(cur, 2),
        "previous": round(prev, 2),
        "delta": round(cur - prev, 2),
        "pct": _pct(cur, prev),
        "current_label": cur_label,
        "previous_label": prev_label,
        "days_used": days_used,
        "available": True,
    }


def period_compare(days: list[dict], readings: list[dict],
                   today: date | None = None) -> dict:
    """环比 / 同比。

    结构：
      today     今天（截至此刻） vs 昨天同一时刻
      week      最近 7 天 vs 前 7 天
      month     最近 30 天 vs 前 30 天
      yoy       去年同期（数据满一年后自动出现）

    只统计「可用日」；被数据卡住或今天未结束的日子会按同期对齐处理，
    并在 days_used 里说明实际比了几天，避免"拿 3 天比 7 天"。
    """
    today = today or date.today()
    out: dict = {"today": None, "week": None, "month": None, "yoy": None,
                 "note": ""}

    if not readings:
        return out

    # ---- 今天 vs 昨天（同期对齐到小时）----
    now_hour = datetime.now().hour
    t = _sum_window(readings, today, today, max_hour=now_hour)
    y = _sum_window(readings, today - timedelta(days=1), today - timedelta(days=1),
                    max_hour=now_hour)
    if t > 0 and y > 0:
        out["today"] = _cmp_block("今天", t, y, "今天", "昨天", 1)
        out["today"]["scope"] = f"{today.isoformat()} 截至 {now_hour} 点"
        out["today"]["cur_from"] = out["today"]["cur_to"] = today.isoformat()
        yd = (today - timedelta(days=1)).isoformat()
        out["today"]["prev_from"] = out["today"]["prev_to"] = yd
    else:
        out["today"] = {
            "available": False, "name": "今天",
            "reason": "今天还没有用电记录" if t <= 0 else "昨天没有数据可以对比",
        }

    # ---- 环比：滚动窗口，只用可用日 ----
    # 数据少的时候自动缩短窗口（比如只有 5 个可用日就 2 天 vs 2 天），
    # 但两边天数必须一样，且至少各 2 天，否则不硬凑。
    use = usable_days(days)
    for key, n in (("week", 7), ("month", 30)):
        n_eff = min(n, len(use) // 2)
        if n_eff >= 2:
            cur_rows = use[-n_eff:]
            prev_rows = use[-2 * n_eff:-n_eff]
            cur = sum(d["kwh"] for d in cur_rows)
            prev = sum(d["kwh"] for d in prev_rows)
            b = _cmp_block("最近 %d 个完整日" % n_eff, cur, prev,
                           "最近 %d 天" % n_eff, "前 %d 天" % n_eff, len(cur_rows))
            b["previous_days"] = len(prev_rows)
            # 带上真实日期范围 —— 不然用户会以为「最近 N 天」包含今天
            b["cur_from"] = cur_rows[0]["date"]
            b["cur_to"] = cur_rows[-1]["date"]
            if prev_rows:
                b["prev_from"] = prev_rows[0]["date"]
                b["prev_to"] = prev_rows[-1]["date"]
            b["current_per_day"] = round(cur / len(cur_rows), 2) if cur_rows else 0
            b["previous_per_day"] = round(prev / len(prev_rows), 2) if prev_rows else 0
            b["window"] = n_eff
            b["shortened"] = n_eff < n
            out[key] = b
        else:
            need = (2 * n_eff + 1) if n_eff else 4
            out[key] = {
                "available": False,
                "name": "最近 %d 天" % n,
                "reason": f"还需要大约 {max(1, need - len(use))} 天数据才能对比",
                "days_used": len(use),
            }

    # ---- 同比：去年同期 ----
    ly_end = today - timedelta(days=364)
    ly_start = today - timedelta(days=371)
    ly = _sum_window(readings, ly_start, ly_end)
    if ly > 0 and use:
        n_eff = min(7, len(use))
        cur = sum(d["kwh"] for d in use[-n_eff:])
        out["yoy"] = _cmp_block("去年同期", cur, ly, f"最近 {n_eff} 天", "去年同期", n_eff)
    else:
        out["yoy"] = {"available": False, "name": "去年同期",
                      "reason": "需要满一年数据才能对比"}

    # 数据少的时候「最近一周」和「最近一月」会退化成同一个窗口，
    # 顶着两个标签显示同一个数字，看起来像重复了。同窗口只留靠前那条。
    wk, mo = out.get("week"), out.get("month")
    if (isinstance(wk, dict) and isinstance(mo, dict)
            and wk.get("available") and mo.get("available")
            and wk.get("window") == mo.get("window")):
        out["month"] = None
        out["merged"] = True

    if out.get("week", {}).get("available"):
        w = out["week"]
        extra = "（数据还在积累，对比窗口已自动缩短）" if w.get("shortened") else ""
        out["note"] = f"{w['days_used']} 天 vs 前 {w.get('previous_days', 0)} 天{extra}"
    return out


# ---------------------------------------------------------------- 智能分析

def _hhmm(h: int) -> str:
    return f"{h:02d}:00"


def _q(sorted_vals: list[float], p: float) -> float:
    """分位数（输入必须已排序）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _today_vs_yesterday(readings: list[dict]) -> dict:
    """今天到此刻 vs 昨天同一时刻 —— 最有时效性的对比。"""
    today = date.today()
    now_h = datetime.now().hour
    t = _sum_window(readings, today, today, max_hour=now_h)
    y = _sum_window(readings, today - timedelta(days=1), today - timedelta(days=1),
                    max_hour=now_h)
    if t <= 0 or y <= 0:
        return {"available": False}
    return {"available": True, "today": round(t, 2), "yesterday": round(y, 2),
            "pct": round((t - y) / y * 100, 1), "hour": now_h}


def _trend_run(days: list[dict]) -> dict:
    """连续几天在涨 / 在跌。"""
    use = usable_days(days)
    # 要 6 个可用日才敢谈"连续上升"。
    # 4 个点时"连着 3 天涨"太容易碰上了，报出来就是小题大做。
    if len(use) < 6:
        return {"available": False}
    vals = [d["kwh"] for d in use]
    up = down = 0
    for a, b in zip(vals, vals[1:]):
        if b > a * 1.06:
            up, down = up + 1, 0
        elif b < a * 0.94:
            down, up = down + 1, 0
        else:
            up = down = 0
    n = max(up, down)
    if n < 2:
        return {"available": False}
    recent = vals[-(n + 1):]
    return {"available": True, "dir": "up" if up >= 2 else "down", "days": n + 1,
            "from": round(recent[0], 2), "to": round(recent[-1], 2),
            "pct": round((recent[-1] - recent[0]) / recent[0] * 100) if recent[0] else 0}


def _best_worst_weekday(days: list[dict]) -> dict:
    """周内最好/最差的一天（要有点差距才值得说）。"""
    wk = [w for w in weekday_profile(days) if w["samples"] >= 2 and w["avg_kwh"] > 0]
    if len(wk) < 3:
        return {"available": False}
    hi = max(wk, key=lambda x: x["avg_kwh"])
    lo = min(wk, key=lambda x: x["avg_kwh"])
    if hi["avg_kwh"] < lo["avg_kwh"] * 1.35:
        return {"available": False}
    return {"available": True, "hi": hi, "lo": lo}


def insights(days: list[dict], readings: list[dict], room: dict,
             balance: float | None, native_unit: str = "kwh") -> list[dict]:
    """把数据翻译成几句人话建议。

    纯规则，但尽量说"你自己不会注意到"的事：

      * 拿这个寝室**自己**的历史分布当基准（分位数），不套通用阈值
      * 优先讲有时效性的：和昨天同期比、连续几天在涨、周内哪天花得多
      * 每条都带具体金额，能算省钱空间就算出来
      * 最多 6 条、按重要程度排序 —— 一次说十件事等于没说
    """
    price = native_price(room, native_unit)
    unit = "元" if native_unit == "cny" else "度"
    real_price = float(room.get("unit_price") or 0.538) or 0.538
    use = usable_days(days)
    out: list[dict] = []
    today_iso = date.today().isoformat()

    def add(level, icon, title, text, weight=50):
        out.append({"level": level, "icon": icon, "title": title,
                    "text": text, "w": weight})

    if not use:
        add("info", "🌱", "还在收集数据",
            "再积累几天就能给出花得快慢、什么时候该充值这类判断。", 0)
        out[0].pop("w", None)
        return out

    vals = sorted(d["kwh"] for d in use)
    med = statistics.median(vals)
    p25, p90 = _q(vals, .25), _q(vals, .90)

    # ---------- 1. 今天到此刻怎么样（最有时效性）----------
    tv = _today_vs_yesterday(readings)
    if tv.get("available"):
        h, t, y, pct = tv["hour"], tv["today"], tv["yesterday"], tv["pct"]
        if pct >= 25:
            add("warn", "📈", "今日用量高于昨日同期",
                f"截至 {h} 点累计 {t:.2f} {unit}，昨日同期 {y:.2f} {unit}，"
                f"增加 {pct:.0f}%。建议核查是否有额外用电。", 88)
        elif pct <= -25:
            add("good", "📉", "今日用量低于昨日同期",
                f"截至 {h} 点累计 {t:.2f} {unit}，昨日同期 {y:.2f} {unit}，"
                f"减少 {abs(pct):.0f}%。", 30)

    # ---------- 2. 连续上升 / 下降 ----------
    tr = _trend_run(days)
    if tr.get("available"):
        if tr["dir"] == "up" and tr["pct"] >= 15:
            add("warn", "📊", f"连续 {tr['days']} 天上升",
                f"从每天 {tr['from']:.2f} {unit} 升至 {tr['to']:.2f} {unit}"
                f"（+{tr['pct']:.0f}%），建议留意天气或用电习惯变化。", 80)
        elif tr["dir"] == "down" and tr["pct"] <= -15:
            add("good", "📊", f"连续 {tr['days']} 天下降",
                f"从每天 {tr['from']:.2f} {unit} 降至 {tr['to']:.2f} {unit}"
                f"（{tr['pct']:.0f}%）。", 28)

    # ---------- 3. 充值计划 ----------
    fc = forecast(days, balance, unit_price=price, readings=readings)
    if balance is not None and fc.get("available"):
        rate = fc["rate_kwh_per_day"]
        suggest = max(0.0, rate * 30 - balance)
        left = fc["days_left"]
        if left <= 0:
            add("crit", "🔴", "电费已经不够用了",
                f"余额只剩 {balance:.2f} {unit}，按每天 {rate:.1f} {unit} 的速度撑不了多久。"
                f"尽快充值，不然会断电。", 100)
        elif left <= 3:
            add("crit", "🔴", "电费快用完了",
                f"按当前 {rate:.1f} {unit}/天，预计 {fc['empty_date']} 用完"
                f"（剩余 {left:.1f} 天）。建议今日充值。", 100)
        elif left <= 10:
            add("warn", "🟠", f"还能用 {left:.0f} 天",
                f"预计 {fc['empty_date']} 用完。若需覆盖一个月用量，"
                f"建议充值 {suggest:.0f} {unit}。", 75)
        elif suggest > 1:
            add("info", "💰", f"充值 {suggest:.0f} {unit} 可用至月底",
                f"按日均 {rate:.1f} {unit} 计算，可用至 "
                f"{(date.today() + timedelta(days=30)).strftime('%m月%d日')}。", 55)

    # ---------- 4. 明显偏离自己平时的那天 ----------
    if len(use) >= 5:
        spikes = [d for d in use if d["kwh"] >= p90 and d["kwh"] > med * 1.5]
        if spikes:
            sp = spikes[-1]
            add("warn", "❗", f"{sp['date'][5:]} 费用明显偏高",
                f"当日 {sp['kwh']:.2f} {unit}；日常区间为 "
                f"{p25:.2f}~{p90:.2f} {unit}。", 70)

    # ---------- 5. 时段集中度 ----------
    prof = [h for h in hourly_profile(readings, price) if h["kwh"] > 0]
    if prof:
        top3 = sorted(prof, key=lambda x: -x["kwh"])[:3]
        share = sum(t["share"] for t in top3)
        hrs = "、".join(f"{t['hour']}点" for t in sorted(top3, key=lambda x: x["hour"]))
        if share >= 40:
            add("info", "⚡", f"{hrs} 占全天费用近 {share:.0f}%",
                f"这三个时段费用最集中。若其中有你不在宿舍的时段，建议核查。", 60)

    # ---------- 6. 周内规律 ----------
    bw = _best_worst_weekday(days)
    if bw.get("available"):
        hi, lo = bw["hi"], bw["lo"]
        add("info", "📅", f"{hi['label']}费用最高",
            f"{hi['label']} 日均 {hi['avg_kwh']:.2f} {unit}，{lo['label']} 最低"
            f"（{lo['avg_kwh']:.2f} {unit}），相差 {(hi['avg_kwh']/lo['avg_kwh']-1)*100:.0f}%。", 40)

    # ---------- 7. 夜间功率：量化省钱空间 ----------
    pdays = [d for d in available_days(readings) if d != today_iso]
    pday = pdays[0] if pdays else (available_days(readings) or [None])[0]
    if pday:
        ps = power_summary(power_profile(readings, real_price, native_unit, pday),
                           real_price, native_unit)
        if ps.get("available"):
            base = ps.get("baseline_watts")
            if base and base >= 150:
                monthly = base / 1000 * 24 * 30 * real_price
                add("warn", "🌙", f"凌晨平均功率 {base:.0f} 瓦",
                    f"以 {pday[5:]} 为例，凌晨 2~6 点平均功率 {base:.0f} 瓦，"
                    f"若持续整天则月费用约 {monthly:.0f} 元。", 72)
            pk = ps.get("peak_watts")
            if pk and pk >= 1200:
                add("info", "🔌", f"最高功率 {pk:.0f} 瓦",
                    f"出现在 {ps['peak_hour']} 点左右；该功率持续 1 小时约 "
                    f"{pk/1000*real_price:.2f} {unit}。", 45)

    # ---------- 8. 数据可信度 ----------
    st = detect_stale(readings, STALE_HOURS)
    on = [x for x in st if x["ongoing"]]
    if on:
        x = on[-1]
        add("warn", "⏸", "易校园数据未更新",
            f"自 {x['start'][:10]} 起余额未发生变化，已持续 {x['hours']/24:.1f} 天，"
            f"该时段用电量无法统计。", 90)
    elif st:
        # 旧缺口只在「刚结束」时提一句。
        # 缺口本身永远不会消失（数据是真的缺），但几周前的旧事不该天天挂在
        # 建议里 —— 它已经不影响当前判断了，下沉到「关于这些数据」即可。
        last = st[-1]
        try:
            days_ago = (date.today() - date.fromisoformat(last["end"][:10])).days
        except (ValueError, TypeError):
            days_ago = 999
        if days_ago <= 2:
            add("info", "ℹ️", "有几天数据缺失",
                f"{last['start'][:10]} 起中断约 {last['hours']/24:.1f} 天，"
                f"该时段未纳入统计，实际可能高于图示。", 35)

    out.sort(key=lambda x: -x.get("w", 0))
    for o in out:
        o.pop("w", None)
    return out[:6]


# ---------------------------------------------------------------- 日粒度

def daily(readings: list[dict], unit_price: float = 0.538,
          today: date | None = None, mark: bool = True) -> list[dict]:
    """按自然日聚合：日末余额、当日用电、当日花费。

    同时给出三个质量标记，**统计时只该用 usable 的日子**：

      * `complete` —— 这一天是否已经过完（今天还没结束，消耗必然偏低）
      * `stale`    —— 这一天是否落在「数据卡住」区间内
                      （上游没更新 ≠ 没用电器，把它当成低消耗会严重拖低均值）
      * `usable`   —— 是否可用于计算日均 / 速率 / 预测

    踩过的坑：不剔除这两类日子时，150 元会被算成"能用 24.8 天"，
    而实际每天用 10 元、只能用 15 天。
    """
    if not readings:
        return []
    segs = split_segments(segments(readings), "day")
    by_day: dict[str, float] = {}
    for s in segs:
        d = s["start"].date().isoformat()
        by_day[d] = by_day.get(d, 0.0) + s["kwh"]

    eod: dict[str, float] = {}
    for r in readings:
        if r.get("balance_kwh") is None:
            continue
        eod[_day(r["ts"]).isoformat()] = float(r["balance_kwh"])

    today = today or date.today()
    stale_spans: list[tuple[date, date]] = []
    if mark:
        for s in detect_stale(readings, STALE_HOURS):
            stale_spans.append((_parse(s["start"]).date(), _parse(s["end"]).date()))

    days = sorted(set(eod) | set(by_day))
    out: list[dict] = []
    for d in days:
        kwh = round(by_day.get(d, 0.0), 3)
        dd = date.fromisoformat(d)
        is_today = dd >= today
        is_stale = any(a <= dd <= b for a, b in stale_spans)
        out.append({
            "date": d,
            "eod_kwh": round(eod[d], 3) if d in eod else None,
            "kwh": kwh,
            "cny": round(kwh * unit_price, 2),
            "complete": not is_today,
            "stale": is_stale,
            "usable": (not is_today) and (not is_stale) and kwh > 0,
        })
    return out


def usable_days(days: list[dict]) -> list[dict]:
    """可用于统计的日子：已过完 + 不在数据卡住区间 + 不是空置期 + 有用电。"""
    return [d for d in days if d.get("usable", d["kwh"] > 0)]


# 空置判定默认值（可被 config 的 vacancy 段覆盖）
VACANT_RATIO = 0.25       # 低到平时的 25% 以下
VACANT_RUN = 4            # 连续至少 4 天
VACANT_SPREAD = 3.0       # 段内最高/最低不超过 3 倍（一致性检查）

_VACANT_CFG: dict = {}


def set_vacancy_config(cfg: dict | None) -> None:
    """由 config 注入空置判定参数与手动指定的空置时段。"""
    global _VACANT_CFG
    _VACANT_CFG = ((cfg or {}).get("vacancy") or {}) if cfg else {}


def manual_vacancy_periods() -> list[dict]:
    """config 里手动写死的空置时段（最可靠 —— 你自己知道什么时候不在）。"""
    out = []
    for p in (_VACANT_CFG.get("periods") or []):
        if not isinstance(p, dict):
            continue
        f, t = str(p.get("from") or ""), str(p.get("to") or "")
        if f and t:
            out.append({"from": f, "to": t, "note": str(p.get("note") or "手动标记")})
    return out


def detect_vacant(days: list[dict],
                  ratio: float | None = None,
                  run: int | None = None) -> list[dict]:
    """识别空置期：连续多天用电掉到「基础负载」水平。

    **关键是和"异常值"区分开**：上学期间宿舍一般都有人，偶尔一天特别低
    更可能是数据问题而不是没人。所以判定要求三条同时成立：

      1. 连续 ≥ 4 天（单天低不算）
      2. 每天都低于本寝室中位数的 25%（不是"略低"，是掉到底噪）
      3. 段内波动小（最高/最低 ≤ 3 倍）—— 稳定平台像"没人住"，
         忽高忽低像"数据抖动"，不算
      4. 不与「数据卡住」区间重叠（余额不动是上游问题，不是没人）

    另外 config 里的 `vacancy.periods` 可以手动指定，优先级最高。
    """
    cfg_ratio = _VACANT_CFG.get("ratio")
    cfg_run = _VACANT_CFG.get("min_days")
    ratio = float(ratio if ratio is not None else (cfg_ratio if cfg_ratio else VACANT_RATIO))
    run = int(run if run is not None else (cfg_run if cfg_run else VACANT_RUN))

    out: list[dict] = []

    # ---- 1) 手动指定的时段（无条件采用，但要核对当天确实低）----
    for p in manual_vacancy_periods():
        rows = [d for d in days if p["from"] <= d["date"] <= p["to"]]
        if not rows:
            continue
        out.append({
            "start": rows[0]["date"], "end": rows[-1]["date"], "days": len(rows),
            "avg": round(statistics.fmean([r["kwh"] for r in rows]), 2),
            "normal": None, "pct": None,
            "manual": True, "note": p["note"],
        })

    if not _VACANT_CFG.get("auto", True):
        return out

    # ---- 2) 自动识别 ----
    pool = [d for d in days
            if d.get("complete", True) and not d.get("stale") and d["kwh"] > 0]
    if len(pool) < run * 3:
        return out
    base = statistics.median([d["kwh"] for d in pool])
    if base <= 0:
        return out

    cur: list[dict] = []

    def flush() -> None:
        if len(cur) < run:
            return
        vals = [r["kwh"] for r in cur]
        # 一致性：稳定平台才像"没人住"
        if max(vals) > min(vals) * VACANT_SPREAD + 1e-9:
            return
        blk = _vacant_block(cur, base)
        # 别和手动标记重复
        if any(o["start"] == blk["start"] and o["end"] == blk["end"] for o in out):
            return
        out.append(blk)

    for d in pool:
        if d["kwh"] < base * ratio:
            cur.append(d)
        else:
            flush()
            cur = []
    flush()
    return sorted(out, key=lambda x: x["start"])


def _vacant_block(rows: list[dict], base: float) -> dict:
    avg = statistics.fmean([r["kwh"] for r in rows])
    return {
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "days": len(rows),
        "avg": round(avg, 2),
        "normal": round(base, 2),
        "pct": round(avg / base * 100),
        "manual": False,
        "note": "",
    }


def mark_vacant(days: list[dict]) -> list[dict]:
    """给每一天打上 vacant 标记，并同步刷新 usable。"""
    spans = detect_vacant(days)
    if not spans:
        return days
    for d in days:
        d["vacant"] = any(s["start"] <= d["date"] <= s["end"] for s in spans)
        if d["vacant"]:
            d["usable"] = False
    return days


def excluded_days(days: list[dict], today: date | None = None) -> dict:
    """说明哪些日子被排除了，便于在界面上把话说清楚。"""
    today = today or date.today()
    td = [d for d in days if not d.get("complete", True)]
    st = [d for d in days if d.get("stale")]
    vc = [d for d in days if d.get("vacant")]
    return {
        "today": td[0]["date"] if td else None,
        "today_kwh": round(td[0]["kwh"], 2) if td else 0.0,
        "stale_days": len(st),
        "stale_kwh": round(sum(d["kwh"] for d in st), 2),
        "stale_list": [d["date"] for d in st][-7:],
        "vacant_days": len(vc),
        "vacant_list": [d["date"] for d in vc][-10:],
        "vacant_spans": detect_vacant(days),
    }


def _fill_missing_eod(days: list[dict]) -> list[dict]:
    """把缺失的日末余额用前一日补齐，避免折线断裂。"""
    last = None
    for d in days:
        if d["eod_kwh"] is None:
            d["eod_kwh"] = last
        else:
            last = d["eod_kwh"]
    return days


# ---------------------------------------------------------------- 时段画像

def hourly_profile(readings: list[dict], unit_price: float = 0.538,
                   day: str | None = None) -> list[dict]:
    """一天里每个小时花了多少。

    day 传 'YYYY-MM-DD' 时只统计那一天；不传则统计所有读数。
    """
    segs = split_segments(segments(readings), "hour")
    if day:
        segs = [s for s in segs if s["start"].date().isoformat() == day]
    buckets: list[list[float]] = [[] for _ in range(24)]
    kwhs = [0.0] * 24
    for s in segs:
        h = s["start"].hour
        buckets[h].append(s["rate_kw"])
        kwhs[h] += s["kwh"]
    total = sum(kwhs) or 1.0
    return [
        {
            "hour": h,
            "avg_kw": round(statistics.fmean(buckets[h]), 4) if buckets[h] else 0.0,
            "kwh": round(kwhs[h], 3),
            "cny": round(kwhs[h] * unit_price, 2),
            "share": round(kwhs[h] / total * 100, 2),
        }
        for h in range(24)
    ]


def available_days(readings: list[dict], with_usage_only: bool = True) -> list[str]:
    """有数据（且当天有用电）的日期，从新到旧。用于前端日期选择器。"""
    by_day: dict[str, float] = {}
    for s in segments(readings):
        d = s["start"].date().isoformat()
        by_day[d] = by_day.get(d, 0.0) + s["kwh"]
    days = [d for d, v in by_day.items() if (v > 0 or not with_usage_only)]
    return sorted(days, reverse=True)


# ---------------------------------------------------------------- 功率换算
#
# 余额 ÷ 电价 = 度数；度数 ÷ 时长 × 1000 = 平均功率（瓦）。
#
# ⚠️ 两个必须讲清楚的前提：
#   1. 上游每小时才更新一次余额，所以这里算的是「这一小时的平均功率」，
#      不是瞬时功率。空调开 30 分钟，这一小时的平均值就是它满功率的一半。
#   2. 这是**整个房间所有电器加起来**的功率，不是某一台设备的功率。
#      1 小时的粒度分不出是哪台设备。
#
# 好处是：分组到「整点小时」再算，即使采集频率高于 1 小时也不会出现尖刺
# （没变化的那几分钟记 0 消耗但计时长，平均下来仍然正确）。

POWER_BANDS = [
    (0,    50,    "很低",   ""),
    (50,   150,   "低",     ""),
    (150,  350,   "中等",   ""),
    (350,  700,   "较高",   ""),
    (700,  1500,  "高",     ""),
    (1500, float("inf"), "很高", ""),
]


def classify_power(watts: float) -> tuple[str, str]:
    for lo, hi, name, eg in POWER_BANDS:
        if lo <= watts < hi:
            return name, eg
    return "—", ""


def _to_degree(value: float, price: float, native_unit: str) -> float:
    """把原生单位的花费换成度数。"""
    if native_unit == "cny":
        return value / price if price else 0.0
    return value


def power_profile(readings: list[dict], price: float = 0.538,
                  native_unit: str = "cny", day: str | None = None) -> list[dict]:
    """一天里每个小时的平均功率（瓦）。

    返回 24 条：{hour, watts, degree, cny, minutes, level, example}
    没有数据的小时 watts = None。
    """
    segs = split_segments(segments(readings), "hour")
    if day:
        segs = [s for s in segs if s["start"].date().isoformat() == day]

    out: list[dict] = []
    for h in range(24):
        hs = [s for s in segs if s["start"].hour == h]
        if not hs:
            out.append({"hour": h, "watts": None, "degree": 0.0, "cny": 0.0,
                        "minutes": 0, "level": "—", "example": ""})
            continue
        amount = sum(s["kwh"] for s in hs)          # 原生单位
        hours = sum(s["hours"] for s in hs)
        # 充电/修正片段也计入时长，否则会把基础功率算高
        fixed_hours = sum(s["hours"] for s in hs if s["recharge"] > 0 or s["adjust"] > 0)
        hours = max(0.0, hours - fixed_hours)
        degree = _to_degree(amount, price, native_unit)
        watts = round(degree * 1000 / hours, 1) if hours > 0.05 else None
        level, example = classify_power(watts) if watts is not None else ("—", "")
        out.append({
            "hour": h,
            "watts": watts,
            "degree": round(degree, 4),
            "cny": round(amount, 4),
            "minutes": round(hours * 60),
            "level": level,
            "example": example,
        })
    return out


def power_summary(rows: list[dict], price: float = 0.538,
                  native_unit: str = "cny") -> dict:
    """把 24 小时功率汇总成几句能用的结论。"""
    valid = [r for r in rows if r["watts"] is not None]
    if len(valid) < 2:
        return {"available": False, "reason": "这一天的数据还不够"}

    watts = [r["watts"] for r in valid]
    total_degree = sum(r["degree"] for r in valid)
    avg = round(statistics.fmean(watts), 1)
    peak = max(valid, key=lambda r: r["watts"])
    low = min(valid, key=lambda r: r["watts"])

    # 档位分布（按小时数）
    bands: dict[str, int] = {}
    for r in valid:
        bands[r["level"]] = bands.get(r["level"], 0) + 1
    band_list = [
        {"level": name, "hours": n,
         "share": round(n / len(valid) * 100),
         "example": next((eg for lo, hi, nm, eg in POWER_BANDS if nm == name), "")}
        for name, n in sorted(bands.items(),
                              key=lambda kv: -next((lo for lo, hi, nm, eg in POWER_BANDS
                                                    if nm == kv[0]), 0))
    ]

    # 夜间基础功率（凌晨 2~6 点），常开的电器会体现在这里
    night = [r["watts"] for r in valid if 2 <= r["hour"] <= 6]
    baseline = round(statistics.median(night), 1) if night else None

    return {
        "available": True,
        "avg_watts": avg,
        "peak_watts": peak["watts"],
        "peak_hour": peak["hour"],
        "peak_level": peak["level"],
        "min_watts": low["watts"],
        "min_hour": low["hour"],
        "baseline_watts": baseline,
        "total_degree": round(total_degree, 3),
        "total_cny": round(total_degree * price, 2),
        "bands": band_list,
        "hours_with_data": len(valid),
        "peak_estimate": _peak_estimate(peak["watts"]),
    }


def _peak_estimate(watts: float) -> str:
    """峰值时刻的功率档位描述。

    只描述功率大小本身，不猜是哪个电器 ——
    1 小时的粒度分辨不出设备，猜出来的都是假的。
    """
    if watts >= 1500:
        return "大功率"
    if watts >= 700:
        return "高功率"
    return ""


def weekday_profile(days: list[dict]) -> list[dict]:
    """周内画像：周一~周日平均日耗，用于识别周末规律。

    只统计「可用日」—— 把今天（未结束）或数据卡住日算进来会明显偏低。
    """
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    buckets: list[list[float]] = [[] for _ in range(7)]
    for d in usable_days(days):
        buckets[date.fromisoformat(d["date"]).weekday()].append(d["kwh"])
    return [
        {"weekday": i, "label": names[i],
         "avg_kwh": round(statistics.fmean(buckets[i]), 3) if buckets[i] else 0.0,
         "samples": len(buckets[i])}
        for i in range(7)
    ]


def hours_of_week(days: list[dict]) -> list[dict]:
    """按周几 × 小时聚合（供更细的画像用，未来扩展）。"""
    return []


# ---------------------------------------------------------------- 异常检测

def detect_anomalies(days: list[dict], z: float = ANOMALY_Z) -> list[dict]:
    """基于 MAD 的稳健离群检测（比 3σ 抗极端值）。

    只在「可用日」上判断，避免今天（未结束）被误判成异常偏低。
    """
    pool = usable_days(days) or [d for d in days if d["kwh"] > 0]
    vals = [d["kwh"] for d in pool]
    if len(vals) < 5:
        return []
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) or 1e-9
    out = []
    for d in pool:
        score = 0.6745 * (d["kwh"] - med) / mad
        if abs(score) >= z:
            out.append({
                "date": d["date"], "kwh": d["kwh"],
                "expected": round(med, 3), "score": round(score, 2),
                "kind": "spike" if score > 0 else "drop",
            })
    return out


def detect_continuous_drain(readings: list[dict], window_hours: int = 6,
                            threshold_kw: float = 0.35,
                            ratio: float = 1.35,
                            min_history: int = 3) -> list[dict]:
    """找出「平时不会这样、这次一直没停」的时段。

    为什么不看绝对功率和时长就够了
    ------------------------------
    宿舍 24 小时开空调的话，「连续 10 小时有功率」天天都成立。
    只看时长，日志会被这种日常模式填满 —— 用户的原话是「容易污染日志」。

    而且也不能跟"全时段的中位数"比：夜间 0~340 W 会把中位数拉得很低，
    白天开空调的 1200 W 自然显得异常，可它其实就是这个寝室的日常。

    所以判据改成**自校准**：

      1. 先找出本寝室历史上所有的「连续段」（≥ window_hours）
      2. 算出这些段的平均功率中位数，当作"这个寝室的正常连续负载"
      3. 只有平均功率高出这个中位数 ratio 倍的段，才算「异常一直开着」

    历史段不足 min_history 条时不判断 —— 没基准就别猜。
    """
    segs = split_segments(segments(readings), "hour")
    if not segs:
        return []

    # ---- 第一步：收集所有连续段 ----
    runs: list[dict] = []
    start = None
    kwh = hours = 0.0
    for s in segs:
        if s["rate_kw"] >= threshold_kw and s["hours"] > 0:
            if start is None:
                start, kwh, hours = s["start"], 0.0, 0.0
            kwh += s["kwh"]
            hours += s["hours"]
        else:
            if start is not None and hours >= window_hours:
                runs.append({"start": start, "hours": hours,
                             "kwh": kwh, "avg_kw": kwh / hours if hours else 0.0})
            start = None
    if start is not None and hours >= window_hours:
        runs.append({"start": start, "hours": hours,
                     "kwh": kwh, "avg_kw": hours and kwh / hours or 0.0})

    if len(runs) < min_history:
        return []          # 基准不够，不判断

    # ---- 第二步：自校准基准 ----
    base = statistics.median([r["avg_kw"] for r in runs])
    if base <= 0:
        return []
    floor_kw = base * ratio

    # ---- 第三步：只留明显高于基准的 ----
    out = []
    for r in runs:
        if r["avg_kw"] >= floor_kw:
            out.append({
                "start": r["start"].isoformat(timespec="minutes"),
                "hours": round(r["hours"], 2),
                "kwh": round(r["kwh"], 3),
                "avg_kw": round(r["avg_kw"], 3),
                "base_kw": round(base, 3),
                "times": round(r["avg_kw"] / base, 2),
            })
    return out[-20:]


# ---------------------------------------------------------------- 预测

def hour_weights(readings: list[dict], days: list[dict] | None = None) -> list[float]:
    """算出「日内作息曲线」：24 个小时各自占全天消耗的比例，和为 1。

    这是把预测线从直线变成曲线的关键 —— 现在的预测只按"日耗 × 天数"平铺，
    画出来必然是一条斜直线；但真实用电是有峰谷的（晚上热水器 + 游戏本，
    凌晨只剩待机）。

    用本寝室的实测小时画像来分摊，预测线就会跟着自己的作息走。
    """
    prof = hourly_profile(readings, 1.0)
    vals = [max(0.0, float(p.get("kwh") or 0)) for p in prof]
    total = sum(vals)
    if len(vals) != 24 or total <= 0:
        return [1.0 / 24] * 24        # 没数据就平均分
    return [v / total for v in vals]


def forecast(days: list[dict], balance_kwh: float | None,
             horizon: int | None = None, unit_price: float = 0.538,
             lookback: int = 14,
             readings: list[dict] | None = None,
             weekday_factors: dict | None = None,
             weather_model: dict | None = None,
             weather_feat: dict | None = None,
             weather_hours: dict | None = None,
             heater_rate: float = 0.0,
             heater_var: float = 0.0,
             heater_day: dict | None = None,
             occ_kinds: list | None = None,
             day_kinds: dict | None = None,
             bias_on: bool = False,
             bias_k: int = 3,
             bias_shrink: float = 4.0) -> dict:
    """预测未来余额与耗尽日期。

    做法：对最近 lookback 天的日耗做「加权线性回归 + EWMA 混合」，
    用残差标准差给出悲观/乐观带，比单纯取平均靠谱得多。
    若启用天气模型（weather_model 是度小时/折线参数、weather_feat 是每日特征），
    则日速率改由「近窗水平 + 天气修正」给出。

    占用/假期锚点（occ_kinds 给定时）：长假/寒暑假整段移出「近窗水平」，
    返校后锚点立刻回在校水平；假期进行中的预测用最近空置日水平。
    day_kinds 是 {日期: daytype.kind}（历史+未来都要有，过期缺的按普通日算）。

    热水器分解（heater_rate>0 时）：weather_model 已是**剥掉烧水后的余项**模型，
    这里在余项上算水平与天气差，再单独加回期望率 heater_rate=λ̂·Ê；
    速率带用泊松方差 heater_var=λ̂·Ê² 加宽，容纳「哪天多烧/少烧」的随机性。
    heater_day 是 {日期: 当天实际烧水(元)}，用来把历史日耗也归到余项口径。
    """
    recent = usable_days(days)[-lookback:]
    if not recent or balance_kwh is None:
        return {"available": False,
                "reason": "数据不足（需要至少 1 个完整、且不在数据卡住区间的用电日）"}

    ys = [d["kwh"] for d in recent]
    n = len(ys)
    xs = list(range(n))

    # 加权最小二乘（越近权重越大）
    w = [1.0 + i / max(1, n - 1) for i in range(n)]
    sw = sum(w)
    mx = sum(wi * xi for wi, xi in zip(w, xs)) / sw
    my = sum(wi * yi for wi, yi in zip(w, ys)) / sw
    denom = sum(wi * (xi - mx) ** 2 for wi, xi in zip(w, xs))
    slope = (sum(wi * (xi - mx) * (yi - my) for wi, xi, yi in zip(w, xs, ys)) / denom) if denom else 0.0

    # 样本少的时候压缩趋势项。
    # 只有 3~5 个点时，最小二乘拟合出的斜率基本是噪声，直接外推会得出
    # "每天涨 30%" 这种荒唐结论（之前的"150 块能用 24.8 天"就是这么来的）。
    # 用 n/(n+12) 做收缩：n=4 → 0.25，n=14 → 0.54，n=60 → 0.83，
    # 样本越多越相信趋势，样本少就退化成"取平均"。
    shrink = n / (n + 12.0)
    slope = slope * shrink
    intercept = my - slope * mx          # 截距跟着调整，保证仍过数据中心

    # EWMA 平滑基准
    alpha = 0.35
    ewma = ys[0]
    for y in ys[1:]:
        ewma = alpha * y + (1 - alpha) * ewma

    today0 = date.today()

    # ---- 当前水平 + 天气模型（度小时 / 折线）----
    # 之前的做法是「趋势外推 + 气温线性修正」。三个问题：
    #   1) 趋势外推把近期降温一路延伸，气温再减一遍 → 降温被计两次
    #   2) 线性关系在低温段外推成 0/负数
    #   3) 日均温抹掉下午峰值，分不清"整天 22°C"和"早上 18°C + 下午 26°C"
    # 现在改为「近窗水平 + 天气修正」：
    #   rate(某天) = 近窗日耗均值 + [模型(当天特征) − 模型(近窗平均特征)]
    # 特征含逐小时积分出来的冷/加热度小时，能反映"峰值有多热、热多久"。
    # 口径与 ysp/model.py 的 evaluate() / fit_temperature 完全一致。
    from . import model as _M

    anchor_rate = max(0.0, 0.6 * (intercept + slope * (n - 1)) + 0.4 * ewma)
    wm = weather_model or {}
    wfeat = weather_feat or {}
    _deg = wm.get("feature") == "deg"
    _ok = (all(k in wm for k in ("base", "c_cool", "c_max", "c_heat")) if _deg
           else all(k in wm for k in ("knee", "base", "hot", "cold")))
    weather_active = bool(wfeat) and _ok
    # 热水器分解：weather_model 是余项模型，历史/锚点都归到「剥掉烧水」的口径
    heater_on = bool(weather_active and heater_rate and heater_rate > 0)
    hday = heater_day or {}
    def _rem_kwh(d: dict) -> float:
        """日耗 - 当天实际烧水 = 余项（空调+基载）。"""
        return d["kwh"] - hday.get(d["date"], 0.0)

    level_feat = None
    level_kwh = None

    # 占用/假期锚点：kinds 来自 daytype（历史 + 未来都带），长假整段不进「近窗水平」
    kmap = {d["date"]: (d.get("kind") or "") for d in days if d.get("date")}
    for _k, _v in (day_kinds or {}).items():
        if _v:
            kmap[str(_k)] = str(_v)
    empties = {str(k) for k in (occ_kinds or [])}
    occ_on = False
    empty_level = None

    # 天气水平锚点用「最近 7 天」，不要用 14 天：
    # 14 天会把两周前的高温天算进来，把"当前水平"整体抬高。
    ANCHOR_N = 7
    anchor_days = usable_days(days)[-ANCHOR_N:]
    if weather_active and empties:
        nz = [d for d in usable_days(days)
              if kmap.get(d["date"], "") not in empties]
        if len(nz) >= 2:
            anchor_days = nz[-ANCHOR_N:]
            occ_on = True
    if weather_active:
        fs = [(_rem_kwh(d) if heater_on else d["kwh"], wfeat[d["date"]])
              for d in anchor_days if d["date"] in wfeat]
        if fs:
            level_kwh = statistics.fmean([p[0] for p in fs])
            level_feat = tuple(statistics.fmean([p[1][j] for p in fs]) for j in range(4))
        else:
            weather_active = False

    # 假期水平：最近空置日的日耗均值（第一次放假、还没有空置样本时为 None）
    if weather_active and occ_on:
        ev_vals = [(_rem_kwh(d) if heater_on else d["kwh"])
                   for d in days[-30:]
                   if kmap.get(d.get("date"), "") in empties
                   and d.get("complete", True)]
        ev_vals = [v for v in ev_vals if v > 0]
        if ev_vals:
            empty_level = statistics.fmean(ev_vals)

    # 滚动偏差校正：锚点常残留方向性系统偏差，用近期残差均值（收缩）拉回
    bias_adj = 0.0
    if bias_on and weather_active:
        bk = max(1, int(bias_k or 3))
        rk = []
        for d in anchor_days:
            pred = level_kwh
            f = wfeat.get(d["date"])
            if f is not None:
                pred += _M.model_value(wm, f) - _M.model_value(wm, level_feat)
            base_val = _rem_kwh(d) if heater_on else d["kwh"]
            rk.append(base_val - pred)
        if rk:
            last = rk[-bk:]
            shrink = len(last) / (len(last) + float(bias_shrink or 4.0))
            bias_adj = statistics.fmean(last) * shrink

    def _lead_w(lead: int) -> float:
        """预报权重：越远的预报越不可信，指数衰减、下限 0.15。

        度小时模型对"下午峰值"很敏感，而 10 天以后的逐小时峰值基本是噪声。
        不衰减就会出现"某天预测 10 元/天"这种离谱尖峰（实测踩过）。
        """
        if lead <= 2:
            return 1.0
        return max(0.15, 0.5 ** ((lead - 2) / 5.0))

    def _day_rate(d: date, trend_rate: float) -> float:
        """某一天的日速率。

        * 天气启用：近窗水平为锚 + 按预报提前量衰减的天气修正
        * 超出预报范围：修正项为 0，回落到近窗平均（而不是冻结在最后一天）
        * 否则：趋势外推（有周几系数再乘上去）
        """
        if weather_active:
            if (occ_on and empty_level is not None
                    and kmap.get(d.isoformat(), "") in empties):
                r = empty_level
            else:
                r = level_kwh
            if weekday_factors:
                r *= float(weekday_factors.get(d.weekday(), 1.0) or 1.0)
            f = wfeat.get(d.isoformat())
            if f is not None:
                w = _lead_w(max(0, (d - today0).days))
                r += w * (_M.model_value(wm, f) - _M.model_value(wm, level_feat))
            r += bias_adj
            return max(0.0, r)
        r = max(0.0, trend_rate)
        if weekday_factors:
            r *= float(weekday_factors.get(d.weekday(), 1.0) or 1.0)
        return r

    # 残差波动（投影上下沿要用它）。天气启用时针对天气模型、在锚点窗口上算。
    if weather_active:
        resid = []
        for d in anchor_days:
            pred = level_kwh
            if weekday_factors:
                pred *= float(weekday_factors.get(d.weekday(), 1.0) or 1.0)
            f = wfeat.get(d["date"])
            if f is not None:
                pred += _M.model_value(wm, f) - _M.model_value(wm, level_feat)
            base_val = _rem_kwh(d) if heater_on else d["kwh"]
            resid.append(base_val - max(0.0, pred))
        sd = statistics.pstdev(resid) if len(resid) > 1 else 0.0
        if heater_on:
            # 泊松：每天烧水次数随机，给速率带补上对应的不确定度
            sd = math.sqrt(sd * sd + heater_var)
    else:
        resid = [ys[i] - (intercept + slope * i) for i in range(n)]
        sd = statistics.pstdev(resid) if n > 1 else 0.0

    # ---- 先算速率，再决定投影多长 ----
    # 投影长度不再是写死的 14 天，而是「预计耗尽 + 3 天余量」，
    # 这样余额归零的交点永远落在图上，右边界也不会看起来一动不动。
    HORIZON_MARGIN = 3
    HORIZON_MIN, HORIZON_MAX = 7, 60
    # 天气启用时近几天的平均速率才算「当前速率」（可能有冷空气/回暖）
    rate_now = anchor_rate
    rate_extra = heater_rate if heater_on else 0.0
    if weather_active:
        near = [_day_rate(today0 + timedelta(days=i), 0.0) + rate_extra
                for i in range(1, 8)]
        if near:
            rate_now = statistics.fmean(near)
    horizon_auto = horizon is None          # 调用方有没有指定
    if horizon is None:
        if rate_now > 0.01 and balance_kwh:
            est_days = balance_kwh / rate_now
            horizon = int(min(HORIZON_MAX,
                              max(HORIZON_MIN, math.ceil(est_days) + HORIZON_MARGIN)))
        else:
            horizon = 14
    horizon = int(max(1, horizon))

    proj: list[dict] = []
    bal = balance_kwh
    bal_lo = balance_kwh      # 悲观：用得快，余额掉得快
    bal_hi = balance_kwh      # 乐观：用得慢
    base_day = today0
    # 波动带上下限系数：速率只允许在 [0.5×, 2×] 当前值之间摆动。
    # 不夹的话，当 sd 接近均值（快没电 / 换季波动大）时，乐观速率会掉到 ~0，
    # "乐观可用天数" 会被算成 10 年（实测复现过 2036 年）。sd 是"波动"，
    # 不该让预测跑出这个量级。
    BAND_LO, BAND_HI = 0.5, 2.0
    for i in range(1, horizon + 1):
        trend_rate = intercept + slope * (n - 1 + i)
        base_rate = 0.6 * trend_rate + 0.4 * ewma
        d = base_day + timedelta(days=i)
        rate = _day_rate(d, base_rate) + rate_extra
        rate_hi = min(rate * BAND_HI, rate + sd)     # 悲观
        rate_lo = max(rate * BAND_LO, rate - sd)     # 乐观
        bal = max(0.0, bal - rate)
        bal_lo = max(0.0, bal_lo - rate_hi)
        bal_hi = max(0.0, bal_hi - rate_lo)
        proj.append({
            "date": d.isoformat(),
            "kwh": round(rate, 3),
            "cny": round(rate * unit_price, 2),
            "balance": round(bal, 3),
            "balance_lo": round(bal_lo, 3),
            "balance_hi": round(bal_hi, 3),
        })

    # ---- 小时级投影：按日内作息曲线摊到 24 小时 ----
    # 日级投影是一天一个点，画出来必然是一条斜直线；
    # 摊到小时之后预测线会跟着这个寝室的作息走（晚上掉得快、凌晨几乎不动）。
    # 优先用**学到的钟点画像 + 当天逐小时气温的空调项**算日内权重；
    # 没有画像就退回实测作息曲线，再没有就平均分（退化成直线）。
    prof = wm.get("profile") if weather_active else None
    whours = weather_hours or {}
    _fallback_wts = hour_weights(readings) if readings else [1.0 / 24] * 24

    def _hour_wts(d: date):
        hs = whours.get(d.isoformat())
        if not (prof and len(prof) == 24 and hs and len(hs) >= 20):
            return None
        cc = float(wm.get("c_cool", 0.0) or 0.0)
        tc = float(wm.get("tc", 22.0) or 22.0)
        ch = float(wm.get("c_heat", 0.0) or 0.0)
        th = float(wm.get("th", 18.0) or 18.0)
        raw = [max(0.0, float(prof[h]) + cc * max(0.0, float(hs[h]) - tc)
                   + ch * max(0.0, th - float(hs[h]))) for h in range(24)]
        s = sum(raw)
        return [v / s for v in raw] if s > 0 else None

    hproj: list[dict] = []
    hb = hb_lo = hb_hi = balance_kwh
    t = datetime.now().replace(minute=0, second=0, microsecond=0)
    base_day = date.today()
    for _ in range(horizon * 24):
        t = t + timedelta(hours=1)
        # 这一小时属于预测的第几天（0 起），用来取当天的日速率
        di = (t.date() - base_day).days - 1
        di = max(0, min(horizon - 1, di))
        # ⚠️ 权重必须按「真实的小时数」取，不能用循环下标 ——
        #    起始点是从当前时刻开始的，第一轮处理的可能是 13:00 而不是 00:00。
        wts_day = _hour_wts(t.date()) or _fallback_wts
        frac = wts_day[t.hour]
        _base = max(0.0, 0.6 * (intercept + slope * (n - 1 + di + 1)) + 0.4 * ewma)
        day_rate = _day_rate(t.date(), _base) + rate_extra
        day_rate_hi = min(day_rate * BAND_HI, day_rate + sd)
        day_rate_lo = max(day_rate * BAND_LO, day_rate - sd)
        hb = max(0.0, hb - day_rate * frac)
        hb_lo = max(0.0, hb_lo - day_rate_hi * frac)
        hb_hi = max(0.0, hb_hi - day_rate_lo * frac)
        hproj.append({
            "ts": t.isoformat(timespec="seconds"),
            "balance": round(hb, 3),
            "balance_lo": round(hb_lo, 3),
            "balance_hi": round(hb_hi, 3),
            "hour": t.hour,
            "cny": round(day_rate * frac * unit_price, 3),
        })

    # 速率太小（宿舍长期没人、余额一直不动）时给出离谱的“永远用不完”，
    # 直接返回不可预测，避免 timedelta 溢出 / 展示 9999 年。
    MIN_RATE = 0.05            # 度/天
    MAX_DAYS = 3650.0          # 最多预测 10 年，超过就没有意义
    if not math.isfinite(rate_now) or rate_now < MIN_RATE:
        return {
            "available": False,
            "reason": f"近期几乎没有用电（日均 {round(rate_now, 4)} 度），无法预测耗尽时间",
            "rate_kwh_per_day": round(rate_now, 4),
            "trend": "平稳",
            "samples": n,
            "projection": proj,
            "hourly_projection": hproj,
        }

    # 整体速率带：同样夹到 [0.5×, 2×]，避免 sd≥均值时算出"还能用 10 年"
    rate_hi = min(rate_now * BAND_HI, rate_now + sd)     # 悲观：用得快
    rate_lo = max(rate_now * BAND_LO, rate_now - sd)     # 乐观：用得慢

    def _days_for(rate: float) -> float:
        if rate <= 0 or not math.isfinite(rate):
            return MAX_DAYS
        return min(MAX_DAYS, balance_kwh / rate)

    def _cross_days(key: str) -> float | None:
        for k, row in enumerate(proj):
            if row.get(key, 1.0) <= 1e-9:
                return float(k + 1)
        return None

    # 耗尽日期优先取投影曲线的过零点 —— 气温模型下速率每天在变，
    # 拿单一速率去除余额会明显偏。曲线没过零才退回除法估算。
    days_left = _cross_days("balance") or _days_for(rate_now)
    days_lo = _cross_days("balance_lo") or _days_for(rate_hi)   # 悲观：掉得快
    days_hi = _cross_days("balance_hi") or _days_for(rate_lo)   # 乐观：掉得慢

    now = datetime.now()
    empty_date = (now + timedelta(days=days_left)).date().isoformat()

    # 趋势：天气模型下看投影曲线的走向（跟着预报气温涨跌），
    # 否则看近窗拟合斜率。直接用 slope 会在"降温导致拟合斜率为负"时
    # 误报成"用量在涨/跌"，和实际投影对不上。
    if weather_active and len(proj) >= 2:
        wk = proj[min(6, len(proj) - 1)]["kwh"] - proj[0]["kwh"]
    else:
        wk = slope
    trend = "上升" if wk > 0.05 else ("下降" if wk < -0.05 else "平稳")

    return {
        "available": True,
        "rate_kwh_per_day": round(rate_now, 3),
        "rate_band": {"optimistic": round(rate_lo, 3), "pessimistic": round(rate_hi, 3)},
        "trend_slope": round(slope, 4),
        "trend": trend,
        "days_left": round(days_left, 1),
        "empty_date": empty_date,
        "empty_date_band": {
            "optimistic": (now + timedelta(days=days_hi)).date().isoformat(),
            "pessimistic": (now + timedelta(days=days_lo)).date().isoformat(),
        },
        "residual_sd": round(sd, 3),
        "projection": proj,
        "hourly_projection": hproj,
        "horizon_days": horizon,
        "horizon_auto": horizon_auto,
        "used_weekday": bool(weekday_factors),
        "used_temp": bool(weather_active),
        "used_heater": bool(heater_on),
        "used_occ": bool(occ_on),
        "used_bias": bool(bias_on and weather_active),
        "bias": round(bias_adj, 3),
        "heater_rate": round(rate_extra, 3),
        "heater_var": round(heater_var, 3),
        "samples": n,
    }


# ---------------------------------------------------------------- 汇总

def summarize(days: list[dict], readings: list[dict], room: dict,
              balance_kwh: float | None = None,
              native_unit: str = "kwh",
              overrides: dict | None = None) -> dict:
    # 原生单位是元时，把元当作工作数值、单价=1.0，全流程只谈元
    price = native_price(room, native_unit)
    lab = unit_labels(native_unit)
    days = mark_vacant(_fill_missing_eod(days))

    # ⚠️ 统计只使用「可用日」：已过完 + 不在数据卡住区间 + 有用电。
    #    把今天（未结束）或卡住日算进来，会把日均严重拖低。
    nonempty = usable_days(days)
    excl = excluded_days(days)

    def window(k: int) -> list[dict]:
        return nonempty[-k:]

    def avg(rows: list[dict]) -> float:
        return round(statistics.fmean([r["kwh"] for r in rows]), 3) if rows else 0.0

    last7, prev7 = window(7), nonempty[-14:-7]
    a7, a14 = avg(last7), avg(prev7)
    wow = round((a7 - a14) / a14 * 100, 1) if a14 else None

    today = date.today().isoformat()
    today_kwh = next((d["kwh"] for d in days if d["date"] == today), 0.0)
    this_week = sum(d["kwh"] for d in nonempty[-7:])
    this_month = sum(d["kwh"] for d in nonempty[-30:])
    total_kwh = sum(d["kwh"] for d in days)

    fc = forecast(days, balance_kwh, unit_price=price, readings=readings,
                  **(overrides or {}))
    rate = fc.get("rate_kwh_per_day") if fc.get("available") else (a7 or 0.0)

    warn = float(room.get("warn_kwh") or 20)
    crit = float(room.get("critical_kwh") or 5)
    level = "ok"
    if balance_kwh is not None:
        if balance_kwh <= crit:
            level = "critical"
        elif balance_kwh <= warn:
            level = "warn"

    # 充值建议：撑到目标天数所需
    suggest = 0.0
    if rate and balance_kwh is not None:
        target_days = 30
        need = rate * target_days - balance_kwh
        suggest = round(max(0.0, need), 1)

    # ---- 充值时机 ----
    # 还剩多少天会跌到提醒线；再往后就来不及了，所以建议提前 2 天充。
    topup: dict = {"available": False}
    if rate > 0 and balance_kwh is not None:
        days_to_warn = (balance_kwh - warn) / rate if balance_kwh > warn else 0.0
        days_to_empty = balance_kwh / rate
        safe = max(0.0, days_to_warn - 2)
        topup = {
            "available": True,
            "days_to_warn": round(days_to_warn, 1),
            "days_to_empty": round(days_to_empty, 1),
            "safe_days": round(safe, 1),
            "by_date": (date.today() + timedelta(days=safe)).isoformat() if safe > 0 else date.today().isoformat(),
            "warn_at": warn,
            "amount": suggest,
            "amount_low": round(max(0.0, (fc.get("rate_kwh_per_day") or rate) * 30 * 0.9 - balance_kwh), 1),
            "amount_high": round(max(0.0, (fc.get("rate_kwh_per_day") or rate) * 30 * 1.15 - balance_kwh), 1),
            "urgent": safe <= 0,
        }

    # 元制下 balance 本身就是钱，balance_kwh/balance_cny 视为同一个数
    bal = round(balance_kwh, 3) if balance_kwh is not None else None

    # ---- 派生单位：元 → 度（按学校电价精确折算）
    # 上游按元计费，但电价已知（默认 0.538 元/度），所以同时给出度数统计。
    real_price = float(room.get("unit_price") or 0.538)
    derived: dict = {"available": False, "unit": "度", "price": real_price}
    if native_unit == "cny" and real_price > 0:
        def to_kwh(v: float | None) -> float | None:
            return round(v / real_price, 3) if v is not None else None

        derived = {
            "available": True,
            "unit": "度",
            "price": real_price,
            "balance": to_kwh(bal),
            "today": to_kwh(today_kwh),
            "avg7": to_kwh(a7),
            "week": to_kwh(this_week),
            "month": to_kwh(this_month),
            "total": to_kwh(total_kwh),
            "recharge_suggest": to_kwh(suggest),
            "rate_per_day": (round(fc["rate_kwh_per_day"] / real_price, 3)
                             if fc.get("available") else None),
            "recharges": [{"ts": x["ts"], "kwh": round(x["kwh"] / real_price, 2)}
                          for x in recharge_events(readings)],
            "adjusts": [{"ts": x["ts"], "kwh": round(x["kwh"] / real_price, 2)}
                        for x in adjust_events(readings)],
        }

    return {
        "room_key": room["room_key"],
        "label": room.get("label") or room["room_key"],
        "unit_price": real_price,
        "native_unit": native_unit,
        "unit_label": lab["amount"],  # "元" 或 "度"

        # ---- 主字段：一律是**原生单位**的值（元制就是元），供前端按当前单位换算
        "balance": bal,
        "today": round(today_kwh, 3),
        "avg7": a7,
        "avg14": a14,
        "week": round(this_week, 3),
        "month": round(this_month, 3),
        "total": round(total_kwh, 3),
        "recharge_suggest": suggest,
        "rate_per_day": fc.get("rate_kwh_per_day") if fc.get("available") else None,

        # ---- 派生单位（元→度），仅元制下可用
        "derived": derived,

        # ---- 兼容字段
        "balance_kwh": bal if native_unit == "kwh" else derived.get("balance"),
        "balance_cny": bal if native_unit == "cny" else (
            round(bal * real_price, 2) if bal is not None else None),
        "level": level,
        "warn_at": warn,
        "critical_at": crit,
        "today_kwh": derived.get("today") if native_unit == "cny" else round(today_kwh, 3),
        "today_cny": round(today_kwh, 2) if native_unit == "cny" else round(today_kwh * real_price, 2),
        "avg7_kwh": derived.get("avg7") if native_unit == "cny" else a7,
        "avg7_cny": round(a7, 2) if native_unit == "cny" else round(a7 * real_price, 2),
        "avg14_kwh": a14,
        "wow_pct": wow,
        # 统计口径说明：让用户知道这个速率是基于哪几天算的
        "rate_basis": {
            "usable_days": len(nonempty),
            "dates": [d["date"] for d in nonempty][-7:],
            "excluded_today": excl["today"],
            "excluded_today_kwh": excl["today_kwh"],
            "excluded_stale_days": excl["stale_days"],
            "excluded_stale_list": excl["stale_list"],
            "excluded_vacant_days": excl.get("vacant_days", 0),
            "excluded_vacant_list": excl.get("vacant_list", []),
            "note": f"基于 {len(nonempty)} 个完整用电日计算"
                    + (f"；已排除今天（{excl['today']}，仅统计到当前，{excl['today_kwh']}）"
                       if excl["today"] else "")
                    + (f"；已排除 {excl['stale_days']} 个数据卡住日" if excl["stale_days"] else "")
                    + (f"；已排除 {excl.get('vacant_days', 0)} 个空置日"
                       if excl.get("vacant_days") else ""),
        },
        "excluded": excl,
        "week_kwh": derived.get("week") if native_unit == "cny" else round(this_week, 3),
        "week_cny": round(this_week, 2) if native_unit == "cny" else round(this_week * real_price, 2),
        "month_kwh": derived.get("month") if native_unit == "cny" else round(this_month, 3),
        "month_cny": round(this_month, 2) if native_unit == "cny" else round(this_month * real_price, 2),
        "total_kwh": derived.get("total") if native_unit == "cny" else round(total_kwh, 3),
        "total_cny": round(total_kwh, 2) if native_unit == "cny" else round(total_kwh * real_price, 2),
        "days_left": fc.get("days_left"),
        "empty_date": fc.get("empty_date"),
        "forecast": fc,
        "recharge_suggest_kwh": derived.get("recharge_suggest") if native_unit == "cny" else suggest,
        "recharge_suggest_cny": round(suggest, 2) if native_unit == "cny" else round(suggest * real_price, 2),
        "topup": topup,
        "recharges": recharge_events(readings),
        "adjusts": adjust_events(readings),
        "coverage": {
            "days": len(days),
            "readings": len(readings),
            "first": readings[0]["ts"] if readings else None,
            "last": readings[-1]["ts"] if readings else None,
        },
    }
