"""模型评估与自动启用。

核心思路
--------
**先有度量，再谈模型。**

加任何参数（气温、日类型、日照…）之前，必须先能回答「加了到底有没有变准」。
没有这个，加参数就是玄学 —— 之前那次「150 块能用 24.8 天」就是这么来的。

设计成「自动拟合 + 自动验证」：
  1. 数据攒够（默认 ≥14 个可用日）才尝试拟合
  2. 拟合完立刻跑滚动回测，对比「不加气温」的基线
  3. **只有在回测里真的更准，才启用**；否则保持关闭并说明原因

这样不需要任何人盯着 —— 条件够了它自己启用，不够就一直关着。
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta

# 启用气温模型所需的最少可用日
MIN_DAYS_FOR_TEMP = 14
# 启用周几模型所需的最少可用日（保证每个周几都能摊到几个样本）
MIN_DAYS_FOR_WEEKDAY = 14
# 至少要比基线好这么多（%）才启用，避免噪声导致的"假胜出"
MIN_IMPROVEMENT_PCT = 8.0

# 偏差校正：锚点里常残留一个方向性的系统偏差（如降温后近期水平整体漂移），
# 用「滚动近期残差均值（收缩）」把它拉回来。窗口要短、收缩要强——
# 太长会把稳定的气温关系也削掉（实测 K=7 反而变差）。
BIAS_K = 3            # 看最近几天（残差窗口）
BIAS_SHRINK_K = 4.0   # 残差样本少就强烈收缩（n/(n+k)）

# 即热式热水器事件：额定功率 ~2000W 但常不满载（实测 1000~2400W）。
# 宿舍差异很大：有的定时烧水（固定时段，已被钟点画像吃掉），
# 有的每次洗澡才烧、时间随机 → 逐小时/气温模型都解释不了，只能拆出来
# 当泊松「事件率」单独预测，并给速率带加上对应不确定性。
HEATER_TH_W = 1000.0    # 视为烧水的下限功率（2000W 额定，用户确认肯定 ≥1000W）
HEATER_HI_W = 2400.0    # 超过这个基本是空调/大功率电器，不算烧水
HEATER_CAP_H = 4        # 连续同一事件最多算几小时（晚高峰会被 PC 拉高，得截断）
HEATER_MIN_RUN = 0.3    # 单事件至少要消耗这么多（元）才算一次
HEATER_LOOKBACK = 4     # 期望率用最近几天的事件均值（短窗，适应行为切换/假期）

# 占用/假期锚点：长假（≥5 天）与寒暑假期间学生几乎都离校，
# 这些日子不能进「近窗水平」——否则返校后锚点还泡在假期低用量里，
# 要拖一周才追上（实测返校首周系统低估 3~8 元/天）。
EMPTY_KINDS = frozenset({"long_break", "vacation"})


# ---------------------------------------------------------------- 回测
def walk_forward(days: list[dict], readings: list[dict],
                 train_min: int = 4, horizon: int = 1,
                 rate_fn=None) -> dict:
    """滚动回测：在每个切分点用历史预测未来 horizon 天的累计消耗，和实际比。

    返回 {samples, mae, mape, bias, detail}

    * **mae**  平均绝对误差（元）—— 预测的"这么多天要花多少"平均差多少
    * **mape** 平均绝对百分比误差 —— 相对误差，跨量级可比
    * **bias** 平均偏差 —— 正数表示系统性高估（预测比实际多花）
    """
    use = _usable(days)
    if len(use) < train_min + horizon:
        return {"available": False,
                "reason": f"可用日不足（需要 {train_min + horizon} 天，实际 {len(use)} 天）"}

    rate_fn = rate_fn or (lambda hist: _simple_rate(hist))

    detail: list[dict] = []
    for i in range(train_min, len(use) - horizon + 1):
        hist = use[:i]
        fut = use[i:i + horizon]
        d0, d1 = fut[0]["date"], fut[-1]["date"]

        # 实际消耗直接取自日聚合 —— 它内部已经正确处理了小时切分、充值、修正。
        # （之前自己拿余额相减，结果算成了「前一天」的消耗，差了整整一天。）
        actual = sum(d["kwh"] for d in fut)
        if actual <= 0:
            continue

        rate = rate_fn(hist)
        if rate is None:
            continue
        pred = rate * horizon

        err = pred - actual
        detail.append({
            "from": d0, "to": d1,
            "pred": round(pred, 3), "actual": round(actual, 3),
            "err": round(err, 3),
            "rate": round(rate, 3),
        })

    if not detail:
        return {"available": False, "reason": "没有可用的回测窗口（读数缺口或充值干扰）"}

    errs = [d["err"] for d in detail]
    pcts = [abs(d["err"]) / d["actual"] * 100 for d in detail]
    return {
        "available": True,
        "samples": len(detail),
        "mae": round(statistics.fmean(abs(e) for e in errs), 3),
        "mape": round(statistics.fmean(pcts), 1),
        "bias": round(statistics.fmean(errs), 3),
        "horizon": horizon,
        "train_min": train_min,
        "detail": detail[-20:],
    }


def _prev_day(use: list[dict], i: int) -> str:
    return use[i - 1]["date"]


def _usable(days: list[dict]) -> list[dict]:
    return [d for d in days if d.get("usable", d.get("kwh", 0) > 0)]


def _empty(d: dict) -> bool:
    """长假/寒暑假（基本没人）——占用先验里要从「近窗水平」里剔掉的日类型。"""
    return str(d.get("kind") or "") in EMPTY_KINDS


def attach_kinds(days: list[dict], conn) -> int:
    """把 daytype 表的 kind 盖到日记录上（占用锚点用）。没有表就静默跳过。"""
    try:
        from . import daytype as _D
        km = {r["date"]: r["kind"] for r in _D.load(conn) if r.get("date")}
    except Exception:  # noqa: BLE001 日类型表不可用就当它不存在
        return 0
    n = 0
    for d in days or []:
        k = km.get(d.get("date"))
        if k:
            d["kind"] = k
            n += 1
    return n


def _simple_rate(hist: list[dict], lookback: int = 7) -> float | None:
    """基线模型：最近若干天的均值。"""
    v = [d["kwh"] for d in hist[-lookback:] if d.get("kwh", 0) > 0]
    if not v:
        return None
    return statistics.fmean(v)


# ---------------------------------------------------------------- 气温模型
def _temp_by_day(readings: list[dict], weather: list[dict],
                 native_unit: str = "cny") -> dict[str, float]:
    """把逐小时气温聚合成每日均温。"""
    by: dict[str, list[float]] = {}
    for w in weather or []:
        t = w.get("ts") or ""
        v = w.get("temp")
        if len(t) >= 10 and v is not None:
            by.setdefault(t[:10], []).append(float(v))
    return {d: statistics.fmean(vs) for d, vs in by.items() if vs}


def _good_dates(days: list[dict]) -> set | None:
    """可用（已过完 + 非空置 + 非卡住 + 有用电）的日期集合。"""
    try:
        from . import analyze as _A
        marked = _A.mark_vacant(_A._fill_missing_eod(days))
        return {d["date"] for d in _A.usable_days(marked)}
    except Exception:  # noqa: BLE001
        return None


def _stale_spans(readings: list[dict]) -> list:
    """上游卡住（余额长时间不动）的时间区间，用来剔除脏小时。"""
    try:
        from . import analyze as _A
        return _A.detect_stale(readings) or []
    except Exception:  # noqa: BLE001
        return []


def _heater_per_day(readings: list[dict], unit_price: float,
                    th_w: float = HEATER_TH_W, hi_w: float = HEATER_HI_W,
                    cap_h: int = HEATER_CAP_H,
                    min_run: float = HEATER_MIN_RUN):
    """把「即热式热水器」事件从逐小时读数里剥出来。

    readings 需先经 analyze.native_view（balance_kwh = 原生余额，元制即元）。
    逐小时差分 → 隐含功率 W = 元/h ÷ 单价 × 1000；落在 [th_w, hi_w] 且
    连续不超过 cap_h 小时的段记为一个烧水事件（≥min_run 元才计数）。

    返回 (per_day, runs)：
      per_day = {date: {"count": 次数, "energy": 元}}   # 只含被判定为烧水的
      runs    = 全部事件单次能量（元）列表，供估算单次中位 Ê
    """
    price = float(unit_price or 0.538) or 0.538
    rows = [r for r in (readings or [])
            if r.get("balance_kwh") is not None and r.get("ts")]
    rows.sort(key=lambda r: r["ts"])
    hrs: dict[str, dict[int, float]] = {}
    for a, b in zip(rows, rows[1:]):
        try:
            ta = datetime.fromisoformat(a["ts"])
            tb = datetime.fromisoformat(b["ts"])
        except (ValueError, TypeError):
            continue
        gap = (tb - ta).total_seconds() / 3600.0
        if not (0.5 <= gap <= 1.5):
            continue
        d = float(a["balance_kwh"]) - float(b["balance_kwh"])
        if d < -0.001 or d > 20.0:
            continue
        k = ta.date().isoformat()
        hrs.setdefault(k, {})[ta.hour] = hrs.get(k, {}).get(ta.hour, 0.0) + d

    per_day: dict[str, dict] = {}
    runs: list[float] = []
    for date, hmap in hrs.items():
        seg_energy = []
        cur = 0.0
        curlen = 0
        for h in range(24):
            v = hmap.get(h, 0.0)
            w = v / price * 1000.0
            if (th_w <= w <= hi_w) and curlen < cap_h:
                cur += v
                curlen += 1
            else:
                if curlen:
                    seg_energy.append(cur)
                cur = 0.0
                curlen = 0
        if curlen:
            seg_energy.append(cur)
        seg_energy = [r for r in seg_energy if r >= min_run]
        per_day[date] = {"count": len(seg_energy), "energy": sum(seg_energy)}
        runs.extend(seg_energy)
    return per_day, runs


def _heater_stats(per_day: dict, runs: list, days: list[dict],
                  lookback: int = HEATER_LOOKBACK) -> dict:
    """由检测结果给出期望率 λ̂·Ê 与泊松方差。

    λ̂ = 最近 lookback 个可用日事件数均值（向全期均值收缩，防样本少时过拟合）；
    Ê = 单次能量中位数；var = λ̂·Ê² 是「每天烧水」的泊松方差，给速率带加不确定性用。
    """
    if not runs or not per_day:
        return {"active": False}
    E = statistics.median(runs)
    use = _usable(days)
    allc = [per_day.get(d["date"], {"count": 0})["count"] for d in use] or [0.0]
    rec = allc[-lookback:]
    lam_all = statistics.fmean(allc)
    lam_rec = statistics.fmean(rec) if rec else lam_all
    k = len(rec) / (len(rec) + 2.0)           # 短窗向全期收缩，样本极少时拉回全期
    lam = lam_rec * k + lam_all * (1.0 - k)
    if lam <= 0.05 or E <= 0:
        return {"active": False}
    return {
        "active": True,
        "rate": round(lam * E, 4),
        "lambda": round(lam, 3),
        "E": round(E, 3),
        "var": round(lam * E * E, 4),
        "events_per_day": round(lam_all, 3),
        "energy_per_event": round(E, 3),
    }


def _hourly_by_day(weather: list[dict]) -> dict[str, list[tuple[int, float]]]:
    """把逐小时气温整理成 {日期: [(小时, 气温), ...]}。"""
    by: dict[str, list[tuple[int, float]]] = {}
    for w in weather or []:
        ts = w.get("ts") or ""
        v = w.get("temp")
        if len(ts) >= 13 and v is not None:
            try:
                h = int(ts[11:13])
            except ValueError:
                continue
            by.setdefault(ts[:10], []).append((h, float(v)))
    return by


# ---------------------------------------------------------------- 铰链气温模型
# 物理形状：不开空调时有一段「基载」（电脑/灯/热水器），只有气温高过某个
# 拐点（空调启动温度）之后才随温度陡升；拐点以下基本平。
# 用单直线拟会在低温段外推成 0 甚至负数（实测 12°C → -4.7 元），所以用折线。
MIN_POINTS_PER_SIDE = 3          # 拐点两侧各至少几天，否则拐点不可辨识
HINGE_KNEE_STEP = 0.5            # 拐点搜索步长（°C）
TEMP_CLAMP = (-10.0, 45.0)       # 气温夹取范围（防风/脏数据/极端外推）


def _hinge(t: float, knee: float, base: float, hot: float, cold: float) -> float:
    """折线：拐点以上 = base + hot×(T−knee)，以下 = base + cold×(knee−T)。"""
    if t >= knee:
        return base + hot * (t - knee)
    return base + cold * (knee - t)


def _solve(A, B):
    """解 k×k 线性方程组（带主元的高斯消元）。奇异返回 None。"""
    k = len(B)
    M = [list(A[i]) + [B[i]] for i in range(k)]
    for i in range(k):
        p = max(range(i, k), key=lambda r: abs(M[r][i]))
        if abs(M[p][i]) < 1e-12:
            return None
        M[i], M[p] = M[p], M[i]
        piv = M[i][i]
        for r in range(i + 1, k):
            f = M[r][i] / piv
            for c in range(i, k + 1):
                M[r][c] -= f * M[i][c]
    x = [0.0] * k
    for i in range(k - 1, -1, -1):
        x[i] = (M[i][k] - sum(M[i][j] * x[j] for j in range(i + 1, k))) / M[i][i]
    return x


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _robust_scale(resid: list[float]) -> float:
    """1.4826×MAD：抗单点异常的标准差估计（普通 std 会被一个异常日带飞）。"""
    if not resid:
        return 0.0
    a = sorted(abs(r) for r in resid)
    med = a[len(a) // 2]
    if med > 1e-9:
        return 1.4826 * med
    return statistics.pstdev(resid) if len(resid) > 1 else 0.0


def _rmse(resid: list[float]) -> float:
    """均方根误差 —— 用于**选模型**（比 MAD 公平：MAD 会偏袒"平"的模型）。"""
    if not resid:
        return 0.0
    return math.sqrt(sum(r * r for r in resid) / len(resid))


def _irls(cols, ys, iters: int = 10, c: float = 1.345):
    """Huber-IRLS 稳健最小二乘（通用版）。

    cols 是逐样本的设计向量列表，例如折线用 (1, max(0,T−拐点), max(0,拐点−T))。
    普通最小二乘对单天异常很敏感（16 个点里一天趴窝就能把斜率带偏），
    Huber 会把大残差的权重压下来。c=1.345 是 95% 正态效率的标准取值。
    """
    n = len(ys)
    k = len(cols[0])
    w = [1.0] * n
    coef = None
    for _ in range(iters):
        A = [[sum(w[p] * cols[p][i] * cols[p][j] for p in range(n)) for j in range(k)]
             for i in range(k)]
        B = [sum(w[p] * cols[p][i] * ys[p] for p in range(n)) for i in range(k)]
        coef = _solve(A, B)
        if coef is None:
            return None
        resid = [ys[p] - sum(coef[i] * cols[p][i] for i in range(k)) for p in range(n)]
        s = _robust_scale(resid) or 1.0
        w = [(1.0 if abs(r) / s <= c else c / (abs(r) / s)) for r in resid]
    return coef


def _nn_hinge_fit(ts, ys, knee):
    """给定拐点的**非负**铰链拟合，返回 (稳健尺度, [base,hot,cold])。

    做法：枚举"哪些项为零"（全自由 / cold=0 / hot=0 / 常数），各自做稳健
    最小二乘，取稳健尺度最小且系数非负者。
    不能先自由拟合再把负系数硬压成 0 —— 那样 base 会和斜率一起被压歪，
    残差看似变小其实是假象（踩过这个坑）。
    """
    h = [max(0.0, t - knee) for t in ts]
    cc = [max(0.0, knee - t) for t in ts]
    cands = []
    co = _irls([(1.0, h[i], cc[i]) for i in range(len(ts))], ys)
    if co:
        cands.append(co)                                   # 全自由
    co = _irls([(1.0, h[i]) for i in range(len(ts))], ys)
    if co:
        cands.append([co[0], co[1], 0.0])                  # 冷段为 0
    co = _irls([(1.0, cc[i]) for i in range(len(ts))], ys)
    if co:
        cands.append([co[0], 0.0, co[1]])                  # 热段为 0
    cands.append([_median(ys), 0.0, 0.0])                  # 常数
    best = None
    for co in cands:
        if co[1] < -1e-9 or co[2] < -1e-9:
            continue
        co = [max(0.0, co[0]), max(0.0, co[1]), max(0.0, co[2])]
        resid = [ys[i] - _hinge(ts[i], knee, *co) for i in range(len(ts))]
        sc = _rmse(resid)
        if best is None or sc < best[0]:
            best = (sc, co)
    return best


def _fit_hinge_pairs(ts: list[float], ys: list[float],
                     min_side: int = MIN_POINTS_PER_SIDE) -> dict | None:
    """拟合「气温→日耗」：在直线和折线（铰链）之间选一个。

    * 直线：局部近似。夏天只有高温数据时，它比强行折线更稳。
    * 折线：有低温数据、能定出拐点时，低温段不会外推成 0/负数。

    选择标准：用稳健尺度（MAD）比较，但折线要多两个参数，
    必须**明显更优**（默认好 10% 以上）才采用 —— 否则用直线。
    这样夏天不会因为"凑不出拐点"而假拟一条折线，等冬天数据进来再切换。
    """
    n = len(ts)
    if n < 4:
        return None
    ts = list(ts)
    tmin, tmax = min(ts), max(ts)
    COMPLEXITY = 0.90        # 折线稳健尺度要 < 直线×0.9 才值得多两个参数

    # 候选一：稳健直线，写成「最冷处以下保持平」的折线形式（cold=0）
    line_coef = None
    line_scale = None
    lin = _irls([(1.0, t) for t in ts], ys)
    if lin:
        a, b = lin
        b = max(0.0, b)                       # 物理：越热不应越省
        line_coef = [max(0.0, a + b * tmin), b, 0.0]
        line_scale = _rmse(
            [ys[i] - _hinge(ts[i], tmin, *line_coef) for i in range(n)])

    # 候选二：折线（拐点 grid 搜索，系数非负）
    best = None
    k = tmin
    while k <= tmax + 1e-9:
        low = sum(1 for t in ts if t < k)
        high = n - low
        if low >= min_side and high >= min_side:
            r = _nn_hinge_fit(ts, ys, k)
            if r and (best is None or r[0] < best[0]):
                best = (r[0], k, r[1])
        k += HINGE_KNEE_STEP

    if best is not None and line_scale is not None and best[0] < line_scale * COMPLEXITY:
        sc, knee, coef = best
        form = "hinge"
    elif line_coef is not None:
        sc, knee, coef = (line_scale, tmin, line_coef)
        form = "line"
    elif best is not None:
        sc, knee, coef = best
        form = "hinge"
    else:
        return None

    base, hot, cold = coef
    pred = [_hinge(t, knee, base, hot, cold) for t in ts]
    my = statistics.fmean(ys)
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, pred))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    return {
        "active": True, "feature": "mean", "form": form,
        "knee": round(knee, 2), "base": round(base, 3),
        "hot": round(hot, 4), "cold": round(cold, 4),
        "r2": round(r2, 3), "rmse": round(math.sqrt(ss_res / n), 4),
        "samples": n, "robust_scale": round(sc, 3),
        "temp_range": [round(tmin, 1), round(tmax, 1)],
    }


# ---------------------------------------------------------------- 度小时模型
# 空调取决于"下午有多热、热多久"，用逐小时气温积分（CDH）比日均温贴近物理：
# "早上 18°C + 下午 26°C"的一天，CDH 很大，日均温却不高 —— 这正是不同寝室
# 对温度耐受不同、以及"开不开空调看峰值"的根源。
# 再加「最高温」刻画峰值强度、「加热度小时」刻画冷天热水器。
DEG_BASE_MIN, DEG_BASE_MAX, DEG_BASE_STEP = 18.0, 26.0, 0.5   # 冷却基准网格
HEAT_BASE = 18.0                                              # 加热基准（固定）
DEG_IMPROVE = 0.95    # 度小时模型要比均值折线好 5% 以上才采用（多几个参数）


def _hours_ok(hourly: dict, day: str) -> bool:
    hs = hourly.get(day)
    return bool(hs) and len(hs) >= 20


def day_features(hourly: dict, day: str, tc: float,
                 th: float = HEAT_BASE):
    """某天的特征 (均温, 最高温, CDH, HDH)。缺数据返回 None。"""
    hs = hourly.get(day)
    if not hs or len(hs) < 20:
        return None
    temps = [t for _, t in hs]
    cdh = sum(max(0.0, t - tc) for t in temps)
    hdh = sum(max(0.0, th - t) for t in temps)
    return (statistics.fmean(temps), max(temps), cdh, hdh)


def model_value(model: dict, feat) -> float:
    """按模型和当天特征算日耗。feat = (均温, 最高, CDH, HDH)。"""
    if not feat or not model:
        return 0.0
    if model.get("feature") == "deg":
        mean, mx, cdh, hdh = feat
        return (float(model["base"]) + float(model["c_cool"]) * cdh
                + float(model["c_max"]) * mx + float(model["c_heat"]) * hdh)
    t = feat[0]
    k = float(model["knee"])
    return (float(model["base"]) + float(model["hot"]) * max(0.0, t - k)
            + float(model["cold"]) * max(0.0, k - t))


def _nn_deg_fit(rows):
    """非负拟合 y = base + c_cool·CDH + c_max·最高温 + c_heat·HDH。

    rows = [(均温, 最高温, CDH, HDH, y)]。枚举哪些项为零，取 RMSE 最小的非负解。
    """
    n = len(rows)
    cols_all = [(1.0, r[2], r[1], r[3]) for r in rows]   # 1, CDH, 最高, HDH
    ys = [r[4] for r in rows]
    cand_idx = [
        (0, 1, 2, 3), (0, 1, 2), (0, 1, 3), (0, 2, 3),
        (0, 1), (0, 2), (0, 3), (0,),
    ]
    best = None
    for idx in cand_idx:
        co = _irls([tuple(cols_all[p][i] for i in idx) for p in range(n)], ys)
        if co is None:
            continue
        full = [0.0, 0.0, 0.0, 0.0]
        ok = True
        for j, i in enumerate(idx):
            if co[j] < -1e-9:
                ok = False
                break
            full[i] = max(0.0, co[j])
        if not ok:
            continue
        pred = [full[0] + full[1] * rows[p][2] + full[2] * rows[p][1]
                + full[3] * rows[p][3] for p in range(n)]
        sc = _rmse([ys[p] - pred[p] for p in range(n)])
        if best is None or sc < best[0]:
            best = (sc, full)
    return best


def _fit_degree_pairs(hourly: dict, pairs: list):
    """grid 冷却基准 + 非负拟合，返回 (rmse, model) 或 None。pairs=[(day,kwh)]。"""
    best = None
    tc = DEG_BASE_MIN
    need = max(5, len(pairs) - 2)
    while tc <= DEG_BASE_MAX + 1e-9:
        rows = []
        for day, y in pairs:
            f = day_features(hourly, day, tc)
            if f:
                rows.append((f[0], f[1], f[2], f[3], y))
        if len(rows) >= need:
            r = _nn_deg_fit(rows)
            if r and (best is None or r[0] < best[0]):
                best = (r[0], tc, r[1])
        tc += DEG_BASE_STEP
    if not best:
        return None
    sc, tc, (base, c_cool, c_max, c_heat) = best
    return sc, {
        "feature": "deg", "base": round(base, 3),
        "c_cool": round(c_cool, 6), "c_max": round(c_max, 4),
        "c_heat": round(c_heat, 6), "tc": round(tc, 1), "th": HEAT_BASE,
        "rmse": round(sc, 4),
    }


def _hourly_samples(readings: list[dict], tkey: dict,
                    good_dates: set | None = None,
                    stale_spans: list | None = None):
    """逐小时训练样本 [(日期, 钟点, 气温, 用量)]。

    用量 = 原生余额的逐小时差分；跳过充值(负跳)/跨天/过大跳变。
    good_dates 给定时只保留这些日期 —— 空置/卡住周里那些接近 0 的小时
    会混进钟点画像，把峰值压低、形状带歪，必须剔掉。
    stale_spans 给定时，落在"余额一直不动"区间里的小时也跳过。
    """
    rows = [r for r in (readings or [])
            if r.get("balance_kwh") is not None and r.get("ts")]
    rows.sort(key=lambda r: r["ts"])
    stale = []
    for s in (stale_spans or []):
        try:
            stale.append((datetime.fromisoformat(s["start"]),
                          datetime.fromisoformat(s["end"])))
        except (ValueError, TypeError, KeyError):
            continue

    out = []
    for a, b in zip(rows, rows[1:]):
        try:
            ta = datetime.fromisoformat(a["ts"])
            tb = datetime.fromisoformat(b["ts"])
        except (ValueError, TypeError):
            continue
        gap = (tb - ta).total_seconds() / 3600.0
        if not (0.5 <= gap <= 1.5):
            continue
        ds = ta.date().isoformat()
        if good_dates is not None and ds not in good_dates:
            continue
        if stale and any(x <= ta <= y for x, y in stale):
            continue
        d = float(a["balance_kwh"]) - float(b["balance_kwh"])
        if d < -0.001 or d > 20.0:          # 负=充值/修正；过大=异常
            continue
        hk = ta.replace(minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        t = tkey.get(hk)
        if t is None:
            continue
        out.append((ds, ta.hour, float(t), round(d, 4)))
    return out


def _fit_hourly_model(samples: list, th: float = HEAT_BASE,
                      base_lo: float = 18.0, base_hi: float = 28.0,
                      base_step: float = 1.0, iters: int = 4):
    """拟合逐小时模型 u_h = 钟点画像[h] + c_cool·max(0,T−tc) + c_heat·max(0,th−T)。

    交替迭代：先估钟点画像（把"热水器晚上烧、游戏本晚上开"这类**时钟规律**
    吃进去），再在残差上拟合空调/加热项（**气温规律**），反复几轮让两者分开。
    用逐小时数据拟合，样本量是日级的 ~24 倍，空调系数估得准得多。
    返回 {profile, tc, c_cool, c_heat, rmse, samples} 或 None。
    """
    n = len(samples)
    if n < 48:
        return None
    prof = []
    for h in range(24):
        vs = [u for (_, hh, _, u) in samples if hh == h]
        prof.append(_median(vs) if vs else 0.0)

    c_cool = c_heat = 0.0
    tc = 22.0
    sc = None
    for _ in range(iters):
        resid = [u - prof[h] for (_, h, _, u) in samples]
        best = None
        k = base_lo
        while k <= base_hi + 1e-9:
            cl = [max(0.0, t - k) for (_, _, t, _) in samples]
            ht = [max(0.0, th - t) for (_, _, t, _) in samples]
            # 枚举列组合，避免某一列全 0（如夏天没有加热度小时）导致矩阵奇异
            for idx in ((0, 1), (0,), (1,)):
                cols = [tuple((cl[i], ht[i])[j] for j in idx) for i in range(n)]
                co = _irls(cols, resid)
                if co is None or any(c < -1e-9 for c in co):
                    continue
                cc = max(0.0, co[idx.index(0)]) if 0 in idx else 0.0
                ch = max(0.0, co[idx.index(1)]) if 1 in idx else 0.0
                pred = [prof[samples[i][1]] + cc * cl[i] + ch * ht[i] for i in range(n)]
                s = _rmse([samples[i][3] - pred[i] for i in range(n)])
                if best is None or s < best[0]:
                    best = (s, k, cc, ch)
            k += base_step
        if not best:
            break
        sc, tc, c_cool, c_heat = best
        newprof = []
        for h in range(24):
            vs = [samples[i][3] - c_cool * max(0.0, samples[i][2] - tc)
                  - c_heat * max(0.0, th - samples[i][2])
                  for i in range(n) if samples[i][1] == h]
            newprof.append(max(0.0, _median(vs)) if vs else 0.0)
        prof = newprof
    if sc is None:
        return None
    return {"profile": prof, "tc": tc, "c_cool": c_cool, "c_heat": c_heat,
            "rmse": sc, "samples": n}


def _hourly_model_dict(hm: dict) -> dict:
    """把逐小时模型转成和 deg 同构的日级模型（base=Σ钟点画像）。"""
    return {
        "feature": "deg", "form": "hourly",
        "base": round(sum(hm["profile"]), 3),
        "c_cool": round(hm["c_cool"], 6), "c_max": 0.0,
        "c_heat": round(hm["c_heat"], 6),
        "tc": round(hm["tc"], 1), "th": HEAT_BASE,
        "profile": [round(v, 4) for v in hm["profile"]],
        "rmse": round(hm["rmse"], 4), "samples": hm["samples"],
    }


def _clock_profile(samples: list, c_cool: float, tc: float,
                   c_heat: float, th: float = HEAT_BASE) -> list:
    """给定空调/加热系数，反解出纯「钟点画像」（去掉气温项后的每小时中位用量）。

    这样画像里只剩"几点钟干什么"（热水器 19~21 烧、晚上游戏本、待机），
    空调那部分不混进来 —— 日内预测时再把空调按当天逐小时气温加回去。
    """
    prof = []
    for h in range(24):
        vs = [u - c_cool * max(0.0, t - tc) - c_heat * max(0.0, th - t)
              for (_, hh, t, u) in samples if hh == h]
        prof.append(max(0.0, round(_median(vs), 4)) if vs else 0.0)
    return prof


def _clock_profile(samples: list, c_cool: float, tc: float,
                   c_heat: float, th: float = HEAT_BASE) -> list:
    """固定空调系数，反推每个钟点的"基载画像"（元/小时，24 维）。

    用**日级**模型里更准的空调系数，把逐小时用量里的空调部分扣掉，
    剩下的就是纯作息画像：待机 + 晚上热水器/游戏本。
    """
    prof = []
    for h in range(24):
        vs = [u - c_cool * max(0.0, t - tc) - c_heat * max(0.0, th - t)
              for (_, hh, t, u) in samples if hh == h]
        prof.append(max(0.0, _median(vs)) if vs else 0.0)
    return [round(v, 4) for v in prof]


def _daily_rmse(model: dict, hourly: dict, pairs: list):
    """把任一模型放到**日级**上算 RMSE，用来公平比较（小时模型也降到日级）。"""
    tc = float(model.get("tc", 22.0))
    resid = []
    for day, y in pairs:
        f = day_features(hourly, day, tc)
        if f:
            resid.append(y - model_value(model, f))
    return _rmse(resid) if resid else None


def _fit_weather_pairs(hourly: dict, pairs: list, hsamples: list | None = None):
    """在「逐小时模型」「日级度小时」「均值折线」之间选日级 RMSE 最小的一个。"""
    cands = []
    if hsamples:
        hm = _fit_hourly_model(hsamples)
        if hm:
            cands.append(_hourly_model_dict(hm))
    deg = _fit_degree_pairs(hourly, pairs)
    if deg:
        cands.append(deg[1])
    hp = [(statistics.fmean([t for _, t in hourly[day]]), y)
          for day, y in pairs if _hours_ok(hourly, day)]
    mean_fit = (_fit_hinge_pairs([p[0] for p in hp], [p[1] for p in hp])
                if len(hp) >= 4 else None)
    if mean_fit:
        cands.append(mean_fit)
    if not cands:
        return None
    scored = []
    for m in cands:
        r = _daily_rmse(m, hourly, pairs)
        if r is not None:
            scored.append((r, m))
    if not scored:
        return cands[0]
    scored.sort(key=lambda x: x[0])
    best_r, best_m = scored[0]
    # 逐小时模型在日级上通常和日级模型"打平"（功能同构），但它样本量 24 倍、
    # 还能给出钟点画像，所以只要不差过 10% 就优先用它。
    hourly_c = next((m for m in cands if m.get("form") == "hourly"), None)
    if hourly_c is not None:
        hr = next((r for r, m in scored if m is hourly_c), None)
        if hr is not None and hr <= best_r * 1.10:
            best_m, best_r = hourly_c, hr
    best_m["daily_rmse"] = round(best_r, 4)
    return best_m



# 启用周几模型所需的每周几最少样本数
MIN_SAMPLES_PER_WEEKDAY = 3


def weekday_factors(days: list[dict], min_days: int = 14,
                    shrink_k: float = 4.0) -> dict | None:
    """算出每个星期几的消耗系数（相对全期平均）。

    为什么周几是好特征
    ------------------
    宿舍作息按周几高度重复：周二上午有课宿舍没人、周四下午没课在打游戏。
    这种模式**整个学期都一样**，不是随机波动，所以是可学的结构特征。

    （课表的价值只在"同一个周几在不同周安排不同"时才体现 —— 比如单双周。
     没有单双周的话，周几就等价于课表，而且不用人工提供。）

    收缩
    ----
    某个星期几只有 1~2 个样本时，系数不可信。用 n/(n+shrink_k) 把它
    往 1.0（不做调整）拉，和斜率收缩是同一套思路。

    返回 {0..6: 系数}，或者 None（样本不够）。
    """
    use = _usable(days)
    if len(use) < min_days:
        return None

    overall = statistics.fmean([d["kwh"] for d in use])
    if overall <= 0:
        return None

    by_wd: dict[int, list[float]] = {}
    for d in use:
        try:
            wd = date.fromisoformat(d["date"]).weekday()
        except (ValueError, TypeError):
            continue
        by_wd.setdefault(wd, []).append(d["kwh"])

    out: dict[int, float] = {}
    for wd in range(7):
        vals = by_wd.get(wd) or []
        if not vals:
            out[wd] = 1.0
            continue
        raw = statistics.fmean(vals) / overall
        k = len(vals) / (len(vals) + shrink_k)      # 样本少就收缩
        out[wd] = round(1.0 + (raw - 1.0) * k, 4)
    return out


def weekday_report(days: list[dict], factors: dict | None = None) -> list[dict]:
    """每个星期几的样本数和平均消耗，用来看规律值不值得建模。"""
    use = _usable(days)
    by_wd: dict[int, list[float]] = {}
    for d in use:
        try:
            wd = date.fromisoformat(d["date"]).weekday()
        except (ValueError, TypeError):
            continue
        by_wd.setdefault(wd, []).append(d["kwh"])
    labels = "一二三四五六日"
    out = []
    for wd in range(7):
        vals = by_wd.get(wd) or []
        out.append({
            "weekday": wd,
            "label": f"周{labels[wd]}",
            "samples": len(vals),
            "avg": round(statistics.fmean(vals), 2) if vals else None,
            "factor": (factors or {}).get(wd),
        })
    return out


def fit_temperature(days: list[dict], readings: list[dict], weather: list[dict],
                    min_days: int = MIN_DAYS_FOR_TEMP,
                    unit_price: float | None = None) -> dict:
    """拟合「天气 → 日耗」模型，在两种形式里选更好的一个：

    * **度小时（deg）**：base + c_cool·CDH + c_max·最高温 + c_heat·HDH
      CDH/HDH 用**逐小时**气温积分，能分辨"整天 22°C"和"早上 18°C + 下午 26°C"，
      契合"空调看峰值"的物理。冷却基准 tc 逐房间 grid 搜索 = 各寝室各自的耐受。
    * **均值折线（mean）**：单一直线或铰链，只在度小时不够好时用（数据太少的退路）。

    顺带做「即热式热水器」分解（unit_price 给定时）：
    从读数里剥出烧水事件，得到期望率 λ̂·Ê 与泊松方差 var，并在剥掉烧水的
    「余项」上重拟合温度模型（避免烧水噪声污染空调系数）。
    是否真的拆，由 decide() 的 8% 回测门槛决定 —— 定时烧水的宿舍拆了不会变准，
    会保持关闭。

    ⚠️ 样本不够时**不拟合**，直接返回 active=False 并说明还差多少天。
    """
    use = _usable(days)
    if len(use) < min_days:
        return {"active": False, "reason": "样本不足",
                "have": len(use), "need": min_days,
                "note": f"还差 {min_days - len(use)} 个可用日"}

    hourly = _hourly_by_day(weather)
    have = sum(1 for d in use if _hours_ok(hourly, d["date"]))
    if have < min_days:
        return {"active": False, "reason": "逐小时气温覆盖不足",
                "have": have, "need": min_days,
                "note": f"可用日里只有 {have} 天有逐小时气温"}

    pairs = [(d["date"], d["kwh"]) for d in use if _hours_ok(hourly, d["date"])]
    tkey = {w["ts"]: w["temp"] for w in weather
            if w.get("ts") and w.get("temp") is not None}
    hsamples = _hourly_samples(readings, tkey,
                               _good_dates(days), _stale_spans(readings))
    fit = _fit_weather_pairs(hourly, pairs, hsamples)
    if not fit:
        return {"active": False, "reason": "气温没有变化，无法拟合"}
    fit["active"] = True
    # 用选中的（日级）空调系数反解出纯钟点画像，供日内预测用
    if hsamples and fit.get("feature") == "deg":
        cp = _clock_profile(hsamples,
                            float(fit.get("c_cool", 0.0) or 0.0),
                            float(fit.get("tc", 22.0) or 22.0),
                            float(fit.get("c_heat", 0.0) or 0.0))
        if any(cp):
            fit["profile"] = cp

    ys = [y for _, y in pairs]
    my = statistics.fmean(ys)
    if fit.get("feature") == "deg":
        tc = fit["tc"]
        pred = []
        for day, y in pairs:
            f = day_features(hourly, day, tc)
            pred.append(model_value(fit, f))
        ss_res = sum((y - p) ** 2 for y, p in zip(ys, pred))
        ss_tot = sum((y - my) ** 2 for y in ys)
        fit["r2"] = round(1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0, 3)
        fit["samples"] = len(pairs)
        if fit.get("form") == "hourly":
            fit["note"] = (f"{len(pairs)} 天 / {fit.get('samples', 0)} 小时，"
                           f"逐小时模型：空调基准 {tc:.0f}°C，日基载 {fit['base']:.1f} 元，"
                           f"CDH 系数 {fit['c_cool']:.4f}，HDH 系数 {fit['c_heat']:.4f}，"
                           f"r²={fit['r2']:.2f}")
        else:
            fit["note"] = (f"{len(pairs)} 天，度小时：冷却基准 {tc:.0f}°C，"
                           f"基载 {fit['base']:.1f} 元，CDH 系数 {fit['c_cool']:.4f}，"
                           f"最高温系数 {fit['c_max']:.2f}，HDH 系数 {fit['c_heat']:.4f}，"
                           f"r²={fit['r2']:.2f}")
    else:
        if fit.get("form") == "hinge":
            fit["note"] = (f"{fit['samples']} 天，折线：拐点 {fit['knee']:.1f}°C，"
                           f"基载 {fit['base']:.1f} 元，热段 +{fit['hot']:.2f}/°C，"
                           f"冷段 {fit['cold']:+.2f}/°C，r²={fit['r2']:.2f}")
        else:
            fit["note"] = (f"{fit['samples']} 天，单一直线（数据还不够定拐点）："
                           f"斜率 +{fit['hot']:.2f}/°C，r²={fit['r2']:.2f}")

    # ---- 热水器（即热式烧水）分解：只做检测与参数，启用与否由 decide 门槛决定 ----
    heat: dict = {"active": False}
    if unit_price:
        try:
            per_day, runs = _heater_per_day(readings, unit_price)
        except Exception:  # noqa: BLE001  检测失败就当没有
            per_day, runs = {}, []
        if runs:
            hs = _heater_stats(per_day, runs, days)
            rem_pairs = [(d, max(0.0, y - per_day.get(d, {"energy": 0.0})["energy"]))
                         for d, y in pairs]
            rem_model = (_fit_weather_pairs(hourly, rem_pairs, None)
                         if len(rem_pairs) >= 4 else None)
            if hs.get("active") and rem_model:
                heat = {
                    "active": True,
                    "rate": hs.get("rate", 0.0),
                    "lambda": hs.get("lambda", 0.0),
                    "E": hs.get("E", 0.0),
                    "var": hs.get("var", 0.0),
                    "events_per_day": hs.get("events_per_day", 0.0),
                    "energy_per_event": hs.get("energy_per_event", 0.0),
                    "day": {k: v["energy"] for k, v in per_day.items()},
                    "model": rem_model,
                }
    fit["heater"] = heat
    return fit


def _fit_weather_from(rows: list[dict], hourly: dict,
                      hsamples: list | None = None, min_pairs: int = 4,
                      heater_day: dict | None = None) -> dict:
    """只用给定历史拟合天气模型（滚动验证专用，不偷看未来）。

    heater_day 给定时，把烧水能量从日耗里剥掉再拟 —— 余项 = 空调 + 基载，
    供「分解 + λ̂·Ê」预测（forecast 先预测余项再加烧水期望）。
    """
    if heater_day:
        pairs = []
        for d in rows:
            if _hours_ok(hourly, d["date"]):
                rem = d["kwh"] - (heater_day.get(d["date"], {"energy": 0.0})["energy"]
                                 if heater_day.get(d["date"]) else 0.0)
                pairs.append((d["date"], max(0.0, rem)))
    else:
        pairs = [(d["date"], d["kwh"]) for d in rows if _hours_ok(hourly, d["date"])]
    if len(pairs) < min_pairs:
        return {"active": False, "reason": "样本不足"}
    fit = _fit_weather_pairs(hourly, pairs, hsamples)
    if not fit:
        return {"active": False, "reason": "气温没有变化"}
    fit["active"] = True
    return fit


def _weekday_factors_from(rows: list[dict], shrink_k: float = 4.0) -> dict:
    """只用给定历史算周几系数（滚动验证专用）。"""
    if len(rows) < 5:
        return {}
    overall = statistics.fmean([d["kwh"] for d in rows])
    if overall <= 0:
        return {}
    by: dict[int, list[float]] = {}
    for d in rows:
        try:
            by.setdefault(date.fromisoformat(d["date"]).weekday(), []).append(d["kwh"])
        except (ValueError, TypeError):
            continue
    out: dict[int, float] = {}
    for wd in range(7):
        vals = by.get(wd) or []
        if not vals:
            out[wd] = 1.0
            continue
        raw = statistics.fmean(vals) / overall
        k = len(vals) / (len(vals) + shrink_k)
        out[wd] = 1.0 + (raw - 1.0) * k
    return out


def evaluate(days: list[dict], readings: list[dict], weather: list[dict],
             min_train: int = 6, lookback: int = 7,
             unit_price: float | None = None) -> dict:
    """滚动验证四种组合，看哪个真的更准。

        base       只用近期均值
        +weekday   叠加周几系数
        +temp      叠加速度修正
        +both      两个都叠
        +decomp    剥掉烧水事件后：余项(气温) + 烧水期望 λ̂·Ê
                    （unit_price 给定时才会测这一项）

    ⚠️ 每一步都**只用该步之前的历史**拟合系数 —— 这是关键。
       如果拿全量数据拟合再在全量上评估，等于偷看答案，
       连纯噪声都会显示「改善 20%+」，那是假的。
    """
    use = _usable(days)
    hourly = _hourly_by_day(weather)

    if len(use) < min_train + 2:
        return {"available": False,
                "reason": f"可用日不足（需要 {min_train + 2} 天，实际 {len(use)} 天）"}

    per_day = runs = E = None
    if unit_price:
        per_day, runs = _heater_per_day(readings, unit_price)
        E = statistics.median(runs) if runs else 0.0

    errs: dict[str, list[float]] = {
        "base": [], "weekday": [], "temp": [], "both": [], "decomp": [],
        "occ": [], "bias": []}
    resid_t: list[float] = []   # 气温变体的带符号残差，供偏差校正使用（只用过去）

    for i in range(min_train, len(use)):
        hist = use[:i]                 # ← 只有历史，不含待预测那天
        cur = use[i]
        # 实际消耗直接用日聚合值（见 walk_forward 里的说明）
        actual = cur["kwh"]
        if actual <= 0:
            continue

        hs = [d["kwh"] for d in hist[-lookback:] if d.get("kwh", 0) > 0]
        if not hs:
            continue
        base_rate = statistics.fmean(hs)

        # 周几系数：只用 hist
        wf = _weekday_factors_from(hist[-21:])
        try:
            wd = date.fromisoformat(cur["date"]).weekday()
        except (ValueError, TypeError):
            wd = None
        wf_v = wf.get(wd, 1.0) if wd is not None else 1.0

        # 天气模型：只用 hist 拟合（日级；逐小时模型留给最终 fit_temperature，
        # 回测不需要它，逐窗拟合小时模型太慢），再平移到近窗水平上
        tf = _fit_weather_from(hist, hourly)
        t_adj = 0.0
        if tf.get("active"):
            tc = float(tf.get("tc", 22.0))
            f_now = day_features(hourly, cur["date"], tc)
            fs = [day_features(hourly, d["date"], tc) for d in hist[-lookback:]]
            fs = [f for f in fs if f]
            if f_now and fs:
                f_level = tuple(statistics.fmean([f[j] for f in fs]) for j in range(4))
                t_adj = model_value(tf, f_now) - model_value(tf, f_level)

        # 分解预测：余项(剥掉烧水)均值 + 气温差 + 烧水期望 λ̂·Ê
        d_pred = None
        if per_day is not None and runs and E and E > 0:
            rem_vals = []
            for d in hist[-lookback:]:
                rem = d["kwh"] - (per_day.get(d["date"], {"energy": 0.0})["energy"]
                                 if per_day.get(d["date"]) else 0.0)
                rem_vals.append(max(0.0, rem))
            rem_level = statistics.fmean(rem_vals) if rem_vals else 0.0
            rf = _fit_weather_from(hist, hourly, heater_day=per_day)
            r_adj = 0.0
            if rf.get("active"):
                tc = float(rf.get("tc", 22.0))
                f_now = day_features(hourly, cur["date"], tc)
                fs = [day_features(hourly, d["date"], tc) for d in hist[-lookback:]]
                fs = [f for f in fs if f]
                if f_now and fs:
                    f_level = tuple(statistics.fmean([f[j] for f in fs]) for j in range(4))
                    r_adj = model_value(rf, f_now) - model_value(rf, f_level)
            counts = [per_day.get(d["date"], {"count": 0})["count"] for d in hist[-lookback:]]
            lam = statistics.fmean(counts) if counts else 0.0
            d_pred = rem_level + r_adj + lam * E

        # 占用/假期锚点：长假整段移出「近窗水平」。
        # 返校后锚点立刻回到在校水平，不再被假期低用量拖一周；
        # 假期当天没有可用的空置样本时退回普通在校水平。
        occ_pred = None
        occ_hist = [d for d in hist if not _empty(d)]
        hs2 = [d["kwh"] for d in occ_hist[-lookback:] if d.get("kwh", 0) > 0]
        if hs2:
            if _empty(cur):
                ev_vals = [d["kwh"] for d in hist if _empty(d) and d.get("kwh", 0) > 0]
                occ_level = (statistics.fmean(ev_vals[-14:]) if ev_vals
                             else statistics.fmean(hs2))
            else:
                occ_level = statistics.fmean(hs2)
            of = _fit_weather_from(occ_hist if len(occ_hist) >= 4 else hist, hourly)
            o_adj = 0.0
            if of.get("active"):
                tc = float(of.get("tc", 22.0))
                f_now = day_features(hourly, cur["date"], tc)
                fs = [day_features(hourly, d["date"], tc) for d in occ_hist[-lookback:]]
                fs = [f for f in fs if f]
                if f_now and fs:
                    f_level = tuple(statistics.fmean([f[j] for f in fs]) for j in range(4))
                    o_adj = model_value(of, f_now) - model_value(of, f_level)
            occ_pred = occ_level + o_adj

        # 偏差校正：近期残差均值（收缩）修正锚点的系统偏差。
        # 只吃「过去」的气温残差，不偷看未来；窗口短、收缩强。
        p_temp = max(0.0, base_rate + t_adj)
        bias = 0.0
        if resid_t:
            recent = resid_t[-BIAS_K:]
            bias = statistics.fmean(recent) * (len(recent) / (len(recent) + BIAS_SHRINK_K))
        p_bias = max(0.0, p_temp - bias)
        resid_t.append(p_temp - actual)

        for key, pred in (
            ("base", base_rate),
            ("weekday", base_rate * wf_v),
            ("temp", p_temp),
            ("both", base_rate * wf_v + t_adj),
            ("decomp", d_pred if d_pred is not None else p_temp),
            ("occ", occ_pred if occ_pred is not None else p_temp),
            ("bias", p_bias),
        ):
            errs[key].append(abs(max(0.0, pred) - actual))

    if not errs["base"]:
        return {"available": False, "reason": "没有可用的回测窗口"}

    out: dict = {"available": True, "samples": len(errs["base"]), "min_train": min_train}
    for k, v in errs.items():
        out[k] = round(statistics.fmean(v), 3) if v else None
    b = out["base"]
    for k in ("weekday", "temp", "both"):
        out[f"{k}_improve"] = (round((b - out[k]) / b * 100, 1)
                               if (b and out[k] is not None) else None)
    # 「周几在气温之上」的边际改善 —— 周几该不该启用，要和实际会跑的
    # 预测器（气温启用时就是 temp）比，而不是和裸基线比，否则口径不一致。
    if out.get("temp") and out.get("both") is not None:
        out["weekday_over_temp_improve"] = round(
            (out["temp"] - out["both"]) / out["temp"] * 100, 1)
    else:
        out["weekday_over_temp_improve"] = None
    # 「拆掉烧水」在气温之上还能再改善多少 —— 决定热水器分解要不要启用。
    if out.get("temp") and out.get("decomp") is not None:
        out["heater_over_temp_improve"] = round(
            (out["temp"] - out["decomp"]) / out["temp"] * 100, 1)
    else:
        out["heater_over_temp_improve"] = None
    # 「长假移出锚点」在气温之上还能再改善多少 —— 决定占用锚点要不要启用。
    if out.get("temp") and out.get("occ") is not None:
        out["occ_over_temp_improve"] = round(
            (out["temp"] - out["occ"]) / out["temp"] * 100, 1)
    else:
        out["occ_over_temp_improve"] = None
    # 「滚动偏差校正」在气温之上还能再改善多少 —— 决定偏差校正要不要启用。
    if out.get("temp") and out.get("bias") is not None:
        out["bias_over_temp_improve"] = round(
            (out["temp"] - out["bias"]) / out["temp"] * 100, 1)
    else:
        out["bias_over_temp_improve"] = None
    return out


def decide(days: list[dict], readings: list[dict], weather: list[dict],
           force: bool = False, unit_price: float | None = None) -> dict:
    """决定气温 / 周几 / 热水器分解 特征要不要启用。

    流程：滚动验证各种组合 → 各自看改善幅度 → 超过门槛才启用。

    为什么用滚动验证而不是"拟合全量再评估全量"：
    后者等于偷看答案，连纯噪声都会显示"改善 20%"。
    实测（20 次随机实验）：
        纯噪声数据  →  平均改善 -2.4%（负的，不会误启用）
        强周几规律  →  平均改善 +28%
    """
    ev = evaluate(days, readings, weather, unit_price=unit_price)
    fit = fit_temperature(days, readings, weather, unit_price=unit_price)

    out: dict = {
        "evaluate": ev,
        "fit": fit,
        "active": False,
        "temp": {"active": False, "why": ""},
        "weekday": {"active": False, "why": ""},
        "heater": {"active": False, "why": ""},
        "occ": {"active": False, "why": ""},
        "bias": {"active": False, "why": ""},
    }

    if not ev.get("available"):
        why = f"未启用：{ev.get('reason')}"
        out["temp"]["why"] = why
        out["weekday"]["why"] = why
        out["heater"]["why"] = why
        out["occ"]["why"] = why
        out["bias"]["why"] = why
        out["why"] = why
        return out

    # ---- 气温 ----
    ti = ev.get("temp_improve")
    if not fit.get("active"):
        out["temp"]["why"] = f"未启用：{fit.get('note') or fit.get('reason')}"
    elif ti is None:
        out["temp"]["why"] = "未启用：没有可对比的窗口"
    elif force or ti >= MIN_IMPROVEMENT_PCT:
        out["temp"] = {"active": True,
                       "why": f"已启用：气温让日均误差改善 {ti:.1f}%"
                              f"（{ev['base']:.2f} → {ev['temp']:.2f} 元）"}
    else:
        out["temp"]["why"] = (f"未启用：加了气温只改善 {ti:.1f}%，"
                              f"不到 {MIN_IMPROVEMENT_PCT:.0f}% 的门槛")

    # ---- 周几 ----
    # 门槛口径必须和「实际会跑的预测器」一致：
    #   气温已启用 → 周几要在气温之上还能再改善，才值得加；
    #   气温没启用 → 和裸基线比。
    wf = weekday_factors(days)
    temp_on = out["temp"]["active"]
    wi = (ev.get("weekday_over_temp_improve") if temp_on
          else ev.get("weekday_improve"))
    ref = "气温之上" if temp_on else "基线之上"
    if not wf:
        out["weekday"]["why"] = (f"未启用：可用日不足"
                                 f"（需要 {MIN_DAYS_FOR_WEEKDAY} 天）")
    elif wi is None:
        out["weekday"]["why"] = "未启用：没有可对比的窗口"
    elif force or wi >= MIN_IMPROVEMENT_PCT:
        out["weekday"] = {"active": True, "factors": wf,
                          "why": f"已启用：周几在{ref}让日均误差再改善 {wi:.1f}%"}
    else:
        out["weekday"]["why"] = (f"未启用：周几在{ref}只改善 {wi:.1f}%，"
                                 f"不到 {MIN_IMPROVEMENT_PCT:.0f}% 的门槛")

    # ---- 热水器（即热式烧水）分解 ----
    # 在气温之上比。拆掉烧水后余项用气温模型、烧水单独按 λ̂·Ê 加回来；
    # 只有回测证明真的更准才启用（定时烧水的宿舍拆了反而更差，自动关）。
    heat = fit.get("heater") or {}
    hi = ev.get("heater_over_temp_improve")
    hinfo = {
        "rate": heat.get("rate", 0.0),
        "lambda": heat.get("lambda", 0.0),
        "E": heat.get("E", 0.0),
        "var": heat.get("var", 0.0),
        "events_per_day": heat.get("events_per_day", 0.0),
        "energy_per_event": heat.get("energy_per_event", 0.0),
    }
    hmodel = heat.get("model")
    if not (heat.get("active") and hmodel):
        out["heater"] = {**hinfo, "active": False,
                         "why": "未启用：烧水事件不足或无法分离"}
    elif hi is None:
        out["heater"] = {**hinfo, "active": False,
                         "why": "未启用：没有可对比的窗口"}
    elif force or hi >= MIN_IMPROVEMENT_PCT:
        # 启用：保存时用「余项温度模型」，forecast 再单独加 λ̂·Ê
        fit = hmodel
        out["fit"] = fit
        out["heater"] = {**hinfo, "active": True,
                         "why": f"已启用：拆掉烧水事件让日均误差再改善 {hi:.1f}%"}
    else:
        out["heater"] = {**hinfo, "active": False,
                         "why": (f"未启用：拆烧水只改善 {hi:.1f}%，"
                                 f"不到 {MIN_IMPROVEMENT_PCT:.0f}% 的门槛")}

    # ---- 占用/假期锚点 ----
    # 长假整段移出「近窗水平」，门槛口径同样 = 在气温之上还能再改善多少。
    # 数据里没有 daytype.kind（没接日类型表）时 occ 与 temp 逐日相同、
    # 改善 0%，自然保持关闭。
    oi = ev.get("occ_over_temp_improve")
    kinds = sorted(EMPTY_KINDS)
    if not out["temp"]["active"]:
        out["occ"] = {"active": False, "kinds": kinds,
                      "why": "未启用：气温模型没启用时不启用占用锚点"}
    elif oi is None:
        out["occ"] = {"active": False, "kinds": kinds,
                      "why": "未启用：没有可对比的窗口"}
    elif force or oi >= MIN_IMPROVEMENT_PCT:
        out["occ"] = {"active": True, "kinds": kinds,
                      "why": f"已启用：长假移出锚点让日均误差再改善 {oi:.1f}%"
                             f"（{ev['temp']:.2f} → {ev['occ']:.2f} 元）"}
    else:
        out["occ"] = {"active": False, "kinds": kinds,
                      "why": (f"未启用：占用锚点只改善 {oi:.1f}%，"
                              f"不到 {MIN_IMPROVEMENT_PCT:.0f}% 的门槛")}

    # ---- 滚动偏差校正 ----
    # 近期残差均值（收缩）修正锚点系统偏差，门槛口径 = 在气温之上再改善多少。
    bi = ev.get("bias_over_temp_improve")
    if not out["temp"]["active"]:
        out["bias"] = {"active": False, "k": BIAS_K, "shrink": BIAS_SHRINK_K,
                       "why": "未启用：气温模型没启用时不启用偏差校正"}
    elif bi is None:
        out["bias"] = {"active": False, "k": BIAS_K, "shrink": BIAS_SHRINK_K,
                       "why": "未启用：没有可对比的窗口"}
    elif force or bi >= MIN_IMPROVEMENT_PCT:
        out["bias"] = {"active": True, "k": BIAS_K, "shrink": BIAS_SHRINK_K,
                       "why": f"已启用：滚动偏差校正让日均误差再改善 {bi:.1f}%"
                              f"（{ev['temp']:.2f} → {ev['bias']:.2f} 元）"}
    else:
        out["bias"] = {"active": False, "k": BIAS_K, "shrink": BIAS_SHRINK_K,
                       "why": (f"未启用：偏差校正只改善 {bi:.1f}%，"
                               f"不到 {MIN_IMPROVEMENT_PCT:.0f}% 的门槛")}

    out["active"] = (out["temp"]["active"] or out["weekday"]["active"]
                     or out["heater"]["active"] or out["occ"]["active"]
                     or out["bias"]["active"])
    outs = [v["why"] for v in (out["temp"], out["weekday"],
                               out["heater"], out["occ"], out["bias"])]
    out["why"] = " ／ ".join(outs)
    return out



# ---------------------------------------------------------------- 状态存取

STATE_KEY = "model_state"


def save_state(conn, dec: dict) -> dict:
    """把 decide() 的结论存进 meta，供 forecast 使用。

    只存真正会用到的部分 —— 系数和周几因子，不存整个评估明细。
    """
    wd = dec.get("weekday") or {}
    tp = dec.get("temp") or {}
    ht = dec.get("heater") or {}
    oc = dec.get("occ") or {}
    bs = dec.get("bias") or {}
    fit = dec.get("fit") or {}
    tm = None
    if tp.get("active") and fit.get("active"):
        keys = ("feature", "form", "base", "hot", "cold", "knee",
                "c_cool", "c_max", "c_heat", "tc", "th", "profile")
        tm = {k: fit[k] for k in keys if k in fit}
    # 热水器分解：只存启用了的那套参数（rate=λ̂·Ê、var=泊松方差、day=逐日烧水）
    heater_state = None
    if ht.get("active"):
        heater_state = {
            "rate": float(ht.get("rate", 0.0) or 0.0),
            "lambda": float(ht.get("lambda", 0.0) or 0.0),
            "E": float(ht.get("E", 0.0) or 0.0),
            "var": float(ht.get("var", 0.0) or 0.0),
            "events_per_day": float(ht.get("events_per_day", 0.0) or 0.0),
            "energy_per_event": float(ht.get("energy_per_event", 0.0) or 0.0),
            "day": ht.get("day") or {},
        }
    state = {
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "weekday_active": bool(wd.get("active")),
        "weekday_factors": ({str(k): v for k, v in (wd.get("factors") or {}).items()}
                            if wd.get("active") else {}),
        "temp_active": bool(tp.get("active") and tm),
        "temp_model": tm,
        "heater": heater_state,
        "occ_active": bool(oc.get("active")),
        "occ_kinds": list(oc.get("kinds") or []) if oc.get("active") else [],
        "bias_active": bool(bs.get("active")),
        "bias_k": int(bs.get("k") or BIAS_K) if bs.get("active") else 0,
        "bias_shrink": float(bs.get("shrink") or BIAS_SHRINK_K) if bs.get("active") else 0.0,
        "why": dec.get("why") or "",
    }
    # 统一走 db.set_meta（它内部会 JSON 编码），避免双重编码
    from . import db as _db
    _db.set_meta(conn, STATE_KEY, state)
    return state


def load_state(conn) -> dict:
    """读回模型状态。没有就是没启用。"""
    from . import db as _db

    st = _db.get_meta(conn, STATE_KEY, None)
    # 防守：meta 里可能存着空字符串或别的类型
    if not isinstance(st, dict):
        return {}
    try:
        st["weekday_factors"] = {int(k): float(v)
                                 for k, v in (st.get("weekday_factors") or {}).items()}
    except (ValueError, TypeError):
        st["weekday_factors"] = {}
    return st


def _model_ok(m) -> bool:
    if not isinstance(m, dict):
        return False
    if m.get("feature") == "deg":
        return all(k in m for k in ("base", "c_cool", "c_max", "c_heat"))
    return all(k in m for k in ("knee", "base", "hot", "cold"))


def overrides(conn) -> dict:
    """给 forecast 用的覆盖参数（没启用就是空 dict）。"""
    st = load_state(conn)
    out: dict = {}
    if st.get("weekday_active") and st.get("weekday_factors"):
        out["weekday_factors"] = st["weekday_factors"]
    tm = st.get("temp_model")
    if st.get("temp_active") and _model_ok(tm):
        out["weather_model"] = {k: (float(v) if isinstance(v, (int, float)) else v)
                                for k, v in tm.items()}
        # 关键：把每天的特征（含未来预报日）一起带上，用模型自己的冷却基准算。
        # 只给参数不给特征的话，forecast 里天气项永远进不去。
        try:
            from . import weather as _W
            hourly = _hourly_by_day(_W.load(conn))
            tc = float(tm.get("tc", 22.0))
            th = float(tm.get("th", HEAT_BASE))
            feats = {}
            for d in hourly:
                f = day_features(hourly, d, tc, th)
                if f:
                    feats[d] = f
            if feats:
                out["weather_feat"] = feats
            # 逐小时气温（供日内预测按"钟点画像 + 空调项"算权重）
            wh = {d: [t for _, t in sorted(hourly[d])] for d in hourly
                  if len(hourly[d]) >= 20}
            if wh:
                out["weather_hours"] = wh
        except Exception:  # noqa: BLE001 天气表不可用就退回无天气修正
            pass
    # 热水器分解：把期望率、泊松方差、逐日实际烧水量交给 forecast
    hs = st.get("heater")
    if isinstance(hs, dict) and hs.get("rate", 0) > 0:
        out["heater_rate"] = float(hs["rate"])
        out["heater_var"] = float(hs.get("var", 0.0) or 0.0)
        out["heater_day"] = {str(k): float(v) for k, v in (hs.get("day") or {}).items()}
    # 占用/假期锚点：把要剔的日类型 + 历史与未来的日类型表交给 forecast
    if st.get("occ_active"):
        out["occ_kinds"] = list(st.get("occ_kinds") or [])
        try:
            from . import daytype as _D
            km = {r["date"]: r["kind"] for r in _D.load(conn) if r.get("date")}
            if km:
                out["day_kinds"] = km
        except Exception:  # noqa: BLE001 表不可用就退回普通锚点
            out.pop("occ_kinds", None)
    # 滚动偏差校正：把窗口参数交给 forecast
    if st.get("bias_active"):
        out["bias_on"] = True
        out["bias_k"] = int(st.get("bias_k") or BIAS_K)
        out["bias_shrink"] = float(st.get("bias_shrink") or BIAS_SHRINK_K)
    return out


def summary_line(dec: dict) -> str:
    """给终端用的一行结论。"""
    return dec.get("why") or "模型未启用"


def clock_peak(prof: list) -> dict | None:
    """钟点画像里用电最高的钟点（16 点以后）。只给事实，不做判定。"""
    if not prof or len(prof) != 24:
        return None
    h = max(range(16, 24), key=lambda i: float(prof[i] or 0.0))
    v = float(prof[h] or 0.0)
    if v <= 0:
        return None
    return {"hour": h, "value": round(v, 3)}
