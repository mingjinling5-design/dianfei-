"""Open-Meteo 逐小时气温。

为什么用它
----------
* **免费、无需 key、无账号** —— 不引入任何账号体系或个人凭据
* 一个接口同时给「过去 92 天」和「未来 16 天」的逐小时气温
* 时区参数直接给 Asia/Shanghai，拿到的就是本地时间，不用换算

用途（分两阶段）
----------------
* **过去的气温** → 训练：学出「气温 → 用电」的关系
* **未来的气温** → 预测：降温了用电会降，升温了会升

⚠️ 现在只做「存」，**不参与任何计算**。
   可用样本只有几天，这时候拟合气温系数必然是过拟合
   （就是之前「150 块能用 24.8 天」那类错误）。等攒够 2~3 周再说。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

API = "https://api.open-meteo.com/v1/forecast"
CTX = ssl.create_default_context()
UA = {"User-Agent": "yischool-power/1.0 (+dorm electricity monitor)"}

# 前 4 个是主力字段；后 5 个是"零成本"字段 —— 同一次请求就返回，
# 先存着，有没有用等回测说话（总比事后想加却发现没历史数据强）。
FIELDS = [
    "temperature_2m", "apparent_temperature",
    "relative_humidity_2m", "precipitation",
    # 以下是"零成本"字段：同一次请求就返回，先存着
    "weather_code",        # WMO 天气码
    "sunshine_duration",   # 这一小时有多少秒是日照
    "is_day",              # 1=白天 0=夜里
    "cloud_cover",         # 云量 %
    "wind_speed_10m", "wind_gusts_10m",
]
# 注：daylight_duration 是「日级」变量，作为 hourly 传会 400。
#     它本身就是日期+纬度算出来的天文量，且 sunshine_duration 更细，不取。

# Open-Meteo 的 past_days 上限实测为 93；取 92 留一点余量
MAX_PAST_DAYS = 92
MAX_FORECAST_DAYS = 16


class WeatherError(RuntimeError):
    pass


def fetch(latitude: float, longitude: float, timezone: str = "Asia/Shanghai",
          past_days: int = 14, forecast_days: int = 16,
          timeout: int = 25) -> list[dict]:
    """拉一次气温。返回 [{ts, temp, feels, humidity, precip}, ...]（本地时间）。"""
    past_days = max(0, min(MAX_PAST_DAYS, int(past_days)))
    forecast_days = max(1, min(MAX_FORECAST_DAYS, int(forecast_days)))

    params = {
        "latitude": round(float(latitude), 2),
        "longitude": round(float(longitude), 2),
        "hourly": ",".join(FIELDS),
        "past_days": past_days,
        "forecast_days": forecast_days,
        "timezone": timezone,
    }
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise WeatherError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:120]}")
    except Exception as e:  # noqa: BLE001
        raise WeatherError(f"{type(e).__name__}: {e}")

    if data.get("error"):
        raise WeatherError(str(data.get("reason") or "接口返回错误"))

    h = data.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        raise WeatherError("接口没返回 hourly.time")

    def _at(key: str, i: int):
        v = h.get(key) or []
        return v[i] if i < len(v) else None

    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    out: list[dict] = []
    for i, ts in enumerate(times):
        # 判断这条是"已发生"还是"预报"
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # Open-Meteo 给的是 "2026-09-13T00:00"（分钟精度），
        # 补成秒精度 "…T00:00:00" 好和别处的时间戳统一比较。
        if len(ts) == 16:
            ts = ts + ":00"
        elif len(ts) == 13:
            ts = ts + ":00:00"
        out.append({
            "ts": ts,
            "temp": _at("temperature_2m", i),
            "feels": _at("apparent_temperature", i),
            "humidity": _at("relative_humidity_2m", i),
            "precip": _at("precipitation", i),
            "kind": "fcst" if t > now else "obs",
            "wcode": _at("weather_code", i),
            "sunshine": _at("sunshine_duration", i),
            "is_day": _at("is_day", i),
            "cloud": _at("cloud_cover", i),
            "wind": _at("wind_speed_10m", i),
            "gust": _at("wind_gusts_10m", i),
        })
    if not out:
        raise WeatherError("解析后没有任何有效数据")
    return out


def save(conn, rows: list[dict], source: str = "open-meteo") -> int:
    """写入/更新气温。同一小时重复拉取时覆盖（预报会更准，观测会补上）。"""
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    payload = [(r["ts"], r.get("temp"), r.get("feels"), r.get("humidity"),
                r.get("precip"), r.get("kind") or "obs", source, now,
                r.get("wcode"), r.get("sunshine"), r.get("is_day"),
                r.get("cloud"), r.get("wind"), r.get("gust")) for r in rows]
    conn.executemany(
        """INSERT INTO weather (ts,temp,feels,humidity,precip,kind,source,fetched_at,
                                  wcode,sunshine,is_day,cloud,wind,gust)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ts) DO UPDATE SET
             temp=excluded.temp, feels=excluded.feels, humidity=excluded.humidity,
             precip=excluded.precip, kind=excluded.kind,
             source=excluded.source, fetched_at=excluded.fetched_at,
             wcode=excluded.wcode, sunshine=excluded.sunshine,
             is_day=excluded.is_day, cloud=excluded.cloud,
             wind=excluded.wind, gust=excluded.gust""",
        payload)
    conn.commit()
    return len(payload)


def load(conn, since: str | None = None, until: str | None = None) -> list[dict]:
    sql = ("SELECT ts,temp,feels,humidity,precip,kind,"
           "wcode,sunshine,is_day,cloud,wind,gust FROM weather WHERE 1=1")
    args: list = []
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    if until:
        sql += " AND ts <= ?"
        args.append(until)
    sql += " ORDER BY ts ASC"
    return [dict(r) for r in conn.execute(sql, args)]


def stats(conn) -> dict:
    """覆盖率概览：存了多少、覆盖到什么时候、有没有缺口。"""
    row = conn.execute(
        "SELECT COUNT(*) rows, MIN(ts) a, MAX(ts) b, "
        "SUM(CASE WHEN temp IS NULL THEN 1 ELSE 0 END) nulls, "
        "MIN(temp) lo, MAX(temp) hi FROM weather").fetchone()
    if not row or not row["rows"]:
        return {"rows": 0}
    d = dict(row)

    # 过去 / 未来各有多少小时
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    past = conn.execute("SELECT COUNT(*) n FROM weather WHERE ts <= ?",
                        (now,)).fetchone()["n"]
    d["past_hours"] = past
    d["future_hours"] = d["rows"] - past
    d["past_days"] = round(past / 24, 1)
    d["future_days"] = round((d["rows"] - past) / 24, 1)

    # 缺口：相邻两小时超过 90 分钟
    ts_list = [r["ts"] for r in conn.execute("SELECT ts FROM weather ORDER BY ts")]
    gaps = 0
    for a, b in zip(ts_list, ts_list[1:]):
        try:
            if (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() > 5400:
                gaps += 1
        except ValueError:
            continue
    d["gaps"] = gaps
    return d
