"""SQLite 存储层。纯 stdlib。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS rooms (
  room_key       TEXT PRIMARY KEY,
  label          TEXT NOT NULL,
  school_code    TEXT DEFAULT '',
  school_name    TEXT DEFAULT '',
  area_id        TEXT DEFAULT '',
  area_name      TEXT DEFAULT '',
  building_code  TEXT DEFAULT '',
  building_name  TEXT DEFAULT '',
  floor_code     TEXT DEFAULT '',
  floor_name     TEXT DEFAULT '',
  room_code      TEXT DEFAULT '',
  room_name      TEXT DEFAULT '',
  unit_price     REAL DEFAULT 0.538,
  warn_kwh       REAL DEFAULT 20,
  critical_kwh   REAL DEFAULT 5,
  enabled        INTEGER DEFAULT 1,
  created_at     TEXT
);

CREATE TABLE IF NOT EXISTS readings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  room_key    TEXT NOT NULL,
  ts          TEXT NOT NULL,
  balance_kwh REAL,
  balance_cny REAL,
  source      TEXT DEFAULT '',
  raw         TEXT DEFAULT '',
  UNIQUE(room_key, ts)
);
CREATE INDEX IF NOT EXISTS idx_readings_room_ts ON readings(room_key, ts);

CREATE TABLE IF NOT EXISTS events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  room_key TEXT NOT NULL,
  ts       TEXT NOT NULL,
  kind     TEXT NOT NULL,
  value    REAL DEFAULT 0,
  note     TEXT DEFAULT '',
  UNIQUE(room_key, ts, kind)
);
CREATE INDEX IF NOT EXISTS idx_events_room_ts ON events(room_key, ts);

CREATE TABLE IF NOT EXISTS alerts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  room_key TEXT NOT NULL,
  ts       TEXT NOT NULL,
  level    TEXT NOT NULL,
  message  TEXT NOT NULL,
  acked    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_room ON alerts(room_key, ts);

CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);

-- 逐小时气温（Open-Meteo）。过去用于训练，未来用于预测。
-- 目前只存不参与计算 —— 样本太少时拟合气温系数会过拟合。
CREATE TABLE IF NOT EXISTS weather (
  ts         TEXT PRIMARY KEY,          -- 本地时间，小时对齐
  temp       REAL,                      -- 气温 °C
  feels      REAL,                      -- 体感 °C
  humidity   REAL,                      -- 相对湿度 %
  precip     REAL,                      -- 降水量 mm
  kind       TEXT DEFAULT 'obs',        -- obs=已发生 / fcst=预报
  source     TEXT DEFAULT 'open-meteo',
  fetched_at TEXT,
  -- 以下为"零成本"字段：同一次请求就返回，先存着，有没有用等回测说话
  wcode      INTEGER,                   -- WMO 天气码（0=晴 3=阴 61=雨 …）
  sunshine   REAL,                      -- 这一小时的日照秒数
  is_day     INTEGER,                   -- 1=白天 0=夜里
  cloud      REAL,                      -- 云量 %
  wind       REAL,                      -- 10 米风速（km/h）
  gust       REAL                       -- 阵风（km/h）
);
CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather(ts);

-- 每天的类型标签：上课日/周末/短假/长假/考试周/寒暑假
-- 日历只提供「假设」，判断宿舍在不在人仍然看实测基线。
CREATE TABLE IF NOT EXISTS daytype (
  date       TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,          -- class/weekend/short_break/long_break/exam/vacation
  label      TEXT DEFAULT '',        -- 中文名
  name       TEXT DEFAULT '',        -- 具体名称（国庆节、春节…）
  is_holiday INTEGER DEFAULT 0,      -- 法定放假日
  is_makeup  INTEGER DEFAULT 0,      -- 调休补班日
  weekday    INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_daytype_kind ON daytype(kind);
"""


def migrate(conn: sqlite3.Connection) -> list[str]:
    """给已存在的库补上新增的列。

    CREATE TABLE IF NOT EXISTS 不会给老表加列，所以新字段要单独 ALTER。
    已经有的列会抛 OperationalError，直接忽略。
    """
    wanted = {
        "weather": [
            ("wcode", "INTEGER"), ("sunshine", "REAL"),
            ("is_day", "INTEGER"), ("cloud", "REAL"),
            ("wind", "REAL"), ("gust", "REAL"),
        ],
    }
    added: list[str] = []
    for table, cols in wanted.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue            # 表还不存在，建表语句会处理
        for name, typ in cols:
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                added.append(f"{table}.{name}")
            except sqlite3.OperationalError:
                pass
    if added:
        conn.commit()
    return added


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


# ---------------------------------------------------------------- rooms

def upsert_room(conn: sqlite3.Connection, room: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO rooms (room_key,label,school_code,school_name,area_id,area_name,
                           building_code,building_name,floor_code,floor_name,
                           room_code,room_name,unit_price,warn_kwh,critical_kwh,enabled,created_at)
        VALUES (:room_key,:label,:school_code,:school_name,:area_id,:area_name,
                :building_code,:building_name,:floor_code,:floor_name,
                :room_code,:room_name,:unit_price,:warn_kwh,:critical_kwh,:enabled,:created_at)
        ON CONFLICT(room_key) DO UPDATE SET
          label=excluded.label,
          school_code=excluded.school_code, school_name=excluded.school_name,
          area_id=excluded.area_id, area_name=excluded.area_name,
          building_code=excluded.building_code, building_name=excluded.building_name,
          floor_code=excluded.floor_code, floor_name=excluded.floor_name,
          room_code=excluded.room_code, room_name=excluded.room_name,
          unit_price=excluded.unit_price,
          warn_kwh=excluded.warn_kwh, critical_kwh=excluded.critical_kwh,
          enabled=excluded.enabled
        """,
        {
            "room_key": room["room_key"],
            "label": room.get("label") or room["room_key"],
            "school_code": room.get("school_code", ""),
            "school_name": room.get("school_name", ""),
            "area_id": str(room.get("area_id", "")),
            "area_name": room.get("area_name", ""),
            "building_code": str(room.get("building_code", "")),
            "building_name": room.get("building_name", ""),
            "floor_code": str(room.get("floor_code", "")),
            "floor_name": room.get("floor_name", ""),
            "room_code": str(room.get("room_code", "")),
            "room_name": room.get("room_name", ""),
            "unit_price": float(room.get("unit_price", 0.538)),
            "warn_kwh": float(room.get("warn_kwh", 20)),
            "critical_kwh": float(room.get("critical_kwh", 5)),
            "enabled": 1 if room.get("enabled", True) else 0,
            "created_at": now,
        },
    )
    conn.commit()


def list_rooms(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM rooms"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY room_key"
    return [dict(r) for r in conn.execute(sql)]


def get_room(conn: sqlite3.Connection, room_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM rooms WHERE room_key=?", (room_key,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- readings

def insert_reading(
    conn: sqlite3.Connection,
    room_key: str,
    ts: str,
    balance_kwh: float | None,
    balance_cny: float | None = None,
    source: str = "",
    raw: Any = None,
) -> bool:
    """写入一条读数；同 (room_key, ts) 已存在则忽略。返回是否新插入。"""
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO readings (room_key,ts,balance_kwh,balance_cny,source,raw)"
            " VALUES (?,?,?,?,?,?)",
            (
                room_key,
                ts,
                balance_kwh,
                balance_cny,
                source,
                json.dumps(raw, ensure_ascii=False)[:4000] if raw is not None else "",
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error:
        return False


def fetch_readings(
    conn: sqlite3.Connection,
    room_key: str,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    sql = "SELECT ts, balance_kwh, balance_cny FROM readings WHERE room_key=?"
    args: list[Any] = [room_key]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    if until:
        sql += " AND ts <= ?"
        args.append(until)
    sql += " ORDER BY ts ASC"
    return [dict(r) for r in conn.execute(sql, args)]


def last_reading(conn: sqlite3.Connection, room_key: str) -> dict | None:
    row = conn.execute(
        "SELECT ts, balance_kwh, balance_cny FROM readings WHERE room_key=?"
        " ORDER BY ts DESC LIMIT 1",
        (room_key,),
    ).fetchone()
    return dict(row) if row else None


def count_readings(conn: sqlite3.Connection, room_key: str | None = None) -> int:
    if room_key:
        return conn.execute(
            "SELECT COUNT(*) c FROM readings WHERE room_key=?", (room_key,)
        ).fetchone()["c"]
    return conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]


def coverage(conn: sqlite3.Connection, room_key: str) -> dict:
    row = conn.execute(
        "SELECT MIN(ts) a, MAX(ts) b, COUNT(*) c FROM readings WHERE room_key=?",
        (room_key,),
    ).fetchone()
    return {"first": row["a"], "last": row["b"], "count": row["c"] or 0}


# ---------------------------------------------------------------- events

def add_event(conn: sqlite3.Connection, room_key: str, ts: str, kind: str,
              value: float = 0.0, note: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO events (room_key,ts,kind,value,note) VALUES (?,?,?,?,?)",
        (room_key, ts, kind, value, note),
    )
    conn.commit()


def fetch_events(conn: sqlite3.Connection, room_key: str, since: str | None = None) -> list[dict]:
    sql = "SELECT ts, kind, value, note FROM events WHERE room_key=?"
    args: list[Any] = [room_key]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    sql += " ORDER BY ts ASC"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- alerts

def add_alert(conn: sqlite3.Connection, room_key: str, level: str, message: str) -> int:
    cur = conn.execute(
        "INSERT INTO alerts (room_key,ts,level,message,acked) VALUES (?,?,?,?,0)",
        (room_key, datetime.now().isoformat(timespec="seconds"), level, message),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def recent_alert(conn: sqlite3.Connection, room_key: str, level: str,
                 within_hours: int) -> dict | None:
    since = (datetime.now() - timedelta(hours=within_hours)).isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT * FROM alerts WHERE room_key=? AND level=? AND ts>=? ORDER BY ts DESC LIMIT 1",
        (room_key, level, since),
    ).fetchone()
    return dict(row) if row else None


def list_alerts(conn: sqlite3.Connection, room_key: str | None = None,
                limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM alerts"
    args: list[Any] = []
    if room_key:
        sql += " WHERE room_key=?"
        args.append(room_key)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def ack_alert(conn: sqlite3.Connection, alert_id: int) -> None:
    conn.execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))
    conn.commit()


# ---------------------------------------------------------------- meta

def set_meta(conn: sqlite3.Connection, k: str, v: Any) -> None:
    conn.execute(
        "INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, json.dumps(v, ensure_ascii=False)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, k: str, default: Any = None) -> Any:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except (TypeError, ValueError):
        return default


def bulk_insert(conn: sqlite3.Connection, rows: Iterable[tuple]) -> int:
    """rows: (room_key, ts, balance_kwh, balance_cny, source, raw)"""
    cur = conn.executemany(
        "INSERT OR IGNORE INTO readings (room_key,ts,balance_kwh,balance_cny,source,raw)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return cur.rowcount

def backup_db(src: str | Path, dest_dir: str | Path, keep: int = 30,
              stamp: str | None = None) -> dict:
    """给数据库做一次一致性快照。

    为什么用 VACUUM INTO 而不是直接拷文件：库跑在 WAL 模式下，
    直接 copy 可能拿到「主库已更新、WAL 还没合并」的半截状态。
    VACUUM INTO 由 SQLite 自己保证快照一致性。

    每天只做一份（按日期命名），已经存在就跳过。保留最近 keep 份。
    """
    src = Path(src)
    dest_dir = Path(dest_dir)
    if not src.exists():
        return {"ok": False, "error": f"源库不存在：{src}"}

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y-%m-%d")
    dest = dest_dir / f"power-{stamp}.db"

    if dest.exists():
        return {"ok": True, "skipped": True, "path": str(dest),
                "size": dest.stat().st_size, "removed": []}

    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    except sqlite3.OperationalError:
        # 老版本 SQLite 不支持参数绑定，退回字符串拼接（路径来自本程序，无注入风险）
        con.execute(f"VACUUM INTO '{dest.as_posix()}'")
    finally:
        con.close()

    # 轮转：只留最近 keep 份
    files = sorted(dest_dir.glob("power-*.db"))
    removed = []
    for old in files[:-keep] if keep > 0 else []:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass

    return {"ok": True, "skipped": False, "path": str(dest),
            "size": dest.stat().st_size, "removed": removed,
            "kept": min(len(files), keep) if keep > 0 else len(files)}
