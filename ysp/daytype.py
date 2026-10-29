"""日类型识别：上课日 / 周末 / 短假 / 长假 / 考试周 / 寒暑假。

为什么要日类型
--------------
用电水平跟「今天是什么日子」强相关：

    热天 + 长假 + 宿舍有人  →  24 小时空调 + 几台游戏本通宵
    冷天 + 长假 + 宿舍有人  →  只开电脑
    热天 + 长假 + 宿舍没人  →  只剩余待机
    上课日                  →  下课才开始，晚上概率更高

⚠️ **日历只提供「假设」，结论必须由数据给出。**
   不能因为日历写着国庆，就断定宿舍没人 —— 有人留下打游戏是完全正常的。
   所以这里的输出只是标签，判断「在不在」仍然看实测基线。

数据来源
--------
1. **法定节假日**：timor.tech 免费接口，**拉一次缓存到本地**，运行时不联网
2. **校历**：中国计量大学 2026-2027 学年第一学期（用户提供）
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

API = "https://timor.tech/api/holiday/year/"
CTX = ssl.create_default_context()
UA = {"User-Agent": "yischool-power/1.0"}

# 日类型
CLASS = "class"          # 上课日
WEEKEND = "weekend"      # 周末
SHORT = "short_break"    # 短假（清明/端午/中秋，一般 3 天）
LONG = "long_break"      # 长假（五一/国庆，5 天以上）
EXAM = "exam"            # 期末考试周
VACATION = "vacation"    # 寒暑假

LABELS = {
    CLASS: "上课日", WEEKEND: "周末", SHORT: "短假",
    LONG: "长假", EXAM: "考试周", VACATION: "寒暑假",
}

# 长假起点：连休达到这个天数就算「长假」（学生可能离校）
LONG_BREAK_DAYS = 5


# ---------------------------------------------------------------- 校历
# 中国计量大学 2026-2027 学年第一学期（从校历图片 OCR 得到）
# ⚠️ 校历里有两个日期 OCR 没读出来（老生注册日、开课日），标注为待确认
TERMS = {
    "2026-2027-1": {
        "name": "2026-2027 学年第一学期",
        "school": "中国计量大学",
        # 老生（有宿舍、有历史数据的那批）
        "senior": {
            "register": ("2026-09-13", "2026-09-14"),   # 老生注册、学年鉴定
            "start": "2026-09-15",                       # 开始上课（OCR 未读到，按常规推）
            "exam": ("2027-01-11", "2027-01-20"),        # 老生期末考试
            "vacation_from": "2027-01-21",               # 老生寒假
        },
        "notes": [
            "9月11日 干部管理人员开始上班",
            "9月13日 新生报到、入学教育",
            "9月13-14日 老生注册、学年鉴定",
            "9月14日 教师开始上班",
            "9月15日 前后 新生、老生开始上课（校历此格 OCR 未读出）",
            "10月22-23日 校田径运动会",
            "1月4-10日 新生期末考试",
            "1月11-24日 新生军训",
            "1月23日起 教职工开始轮休",
            "1月25日起 新生寒假",
        ],
    },
}


# ---------------------------------------------------------------- 节假日
def _cache_path(cache_dir: Path, year: int) -> Path:
    return cache_dir / f"holidays-{year}.json"


def fetch_holidays(year: int, timeout: int = 20) -> dict:
    """从 timor.tech 拉某年的法定节假日安排。"""
    req = urllib.request.Request(API + str(year), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{type(e).__name__}: {e}")
    if d.get("code") != 0:
        raise RuntimeError(f"接口返回 code={d.get('code')}")
    return d.get("holiday") or {}


def load_holidays(cache_dir: Path, year: int, allow_fetch: bool = True) -> dict:
    """读缓存；没有且允许联网就拉一次并写缓存。

    缓存是刻意的：节假日安排一年只变一次，没必要每次运行都联网，
    也避免运行时依赖第三方（拉取失败不该影响主流程）。
    """
    p = _cache_path(cache_dir, year)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    if not allow_fetch:
        return {}
    try:
        data = fetch_holidays(year)
    except Exception:  # noqa: BLE001
        return {}
    if data:
        cache_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def holiday_spans(holidays: dict) -> list[dict]:
    """把逐日的节假日记录合并成连续的假期段。

    timor 的返回是按天给的（{"09-25": {...}, ...}），
    合并成段之后才能判断这是 3 天短假还是 7 天长假。
    """
    days: dict[str, dict] = {}
    for k, v in (holidays or {}).items():
        d = v.get("date")
        if d:
            days[d] = {"holiday": bool(v.get("holiday")), "name": v.get("name") or ""}
    if not days:
        return []

    # 只对「放假」日做连续合并
    off = sorted(d for d, v in days.items() if v["holiday"])
    spans: list[dict] = []
    cur: list[str] = []
    for ds in off:
        d = date.fromisoformat(ds)
        if cur and (d - date.fromisoformat(cur[-1])).days == 1:
            cur.append(ds)
        else:
            if cur:
                spans.append(_span(cur, days))
            cur = [ds]
    if cur:
        spans.append(_span(cur, days))
    return spans


def _span(seq: list[str], days: dict) -> dict:
    return {
        "from": seq[0], "to": seq[-1], "days": len(seq),
        "name": days[seq[0]]["name"],
        "long": len(seq) >= LONG_BREAK_DAYS,
    }


# ---------------------------------------------------------------- 分类
def classify(d: date, holidays: dict, term: dict | None = None) -> dict:
    """判断某天属于哪种日类型。"""
    ds = d.isoformat()
    info = (holidays or {}).get(ds[5:]) or {}
    is_off = bool(info.get("holiday"))
    is_makeup = bool(info) and not is_off

    # 校历：考试周 / 寒暑假 优先于普通工作日
    kind, name = None, ""
    if term:
        sen = term.get("senior") or {}
        ex = sen.get("exam")
        if ex and ex[0] <= ds <= ex[1]:
            kind, name = EXAM, "老生期末考试"
        elif sen.get("vacation_from") and ds >= sen["vacation_from"]:
            kind, name = VACATION, "寒假"

    if kind is None:
        if is_off:
            # 看这天的假期总共多长
            span = next((s for s in holiday_spans(holidays)
                         if s["from"] <= ds <= s["to"]), None)
            n = span["days"] if span else 1
            kind = LONG if n >= LONG_BREAK_DAYS else SHORT
            name = info.get("name") or ""
        elif is_makeup:
            # 调休补班日：学校多半也调课，按上课日处理但记下来
            kind, name = CLASS, info.get("name") or "调休补班"
        elif d.weekday() >= 5:
            kind, name = WEEKEND, ""
        else:
            kind, name = CLASS, ""

    return {
        "date": ds, "kind": kind, "label": LABELS.get(kind, kind),
        "name": name, "is_holiday": is_off, "is_makeup": is_makeup,
        "weekday": d.weekday(),
    }


def for_range(d0: date, d1: date, cache_dir: Path,
              term_name: str = "2026-2027-1") -> list[dict]:
    """给一个日期区间打标签（跨年时两年都读）。"""
    term = TERMS.get(term_name)
    out: list[dict] = []
    d = d0
    cache: dict[int, dict] = {}
    while d <= d1:
        y = d.year
        if y not in cache:
            cache[y] = load_holidays(cache_dir, y)
        out.append(classify(d, cache[y], term))
        d += timedelta(days=1)
    return out


def save(conn, rows: list[dict]) -> int:
    """写入 daytype 表。"""
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    conn.executemany(
        """INSERT INTO daytype (date,kind,label,name,is_holiday,is_makeup,weekday,updated_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(date) DO UPDATE SET
             kind=excluded.kind, label=excluded.label, name=excluded.name,
             is_holiday=excluded.is_holiday, is_makeup=excluded.is_makeup,
             weekday=excluded.weekday, updated_at=excluded.updated_at""",
        [(r["date"], r["kind"], r["label"], r["name"],
          int(r["is_holiday"]), int(r["is_makeup"]), r["weekday"], now) for r in rows])
    conn.commit()
    return len(rows)


def load(conn, since: str | None = None, until: str | None = None) -> list[dict]:
    sql = "SELECT * FROM daytype WHERE 1=1"
    args: list = []
    if since:
        sql += " AND date >= ?"
        args.append(since)
    if until:
        sql += " AND date <= ?"
        args.append(until)
    sql += " ORDER BY date ASC"
    return [dict(r) for r in conn.execute(sql, args)]


def upcoming_breaks(cache_dir: Path, days: int = 60,
                    today: date | None = None) -> list[dict]:
    """未来 N 天内的假期段（用于改进空置判定：长假窗口重点观察）。"""
    today = today or date.today()
    out = []
    for y in {today.year, (today + timedelta(days=days)).year}:
        for s in holiday_spans(load_holidays(cache_dir, y)):
            if date.fromisoformat(s["to"]) < today:
                continue
            if date.fromisoformat(s["from"]) > today + timedelta(days=days):
                continue
            out.append(s)
    return sorted(out, key=lambda s: s["from"])
