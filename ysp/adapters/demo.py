"""演示数据源：无需任何凭据，生成一段带真实感的用电曲线。

设计要点：**余额是绝对时间的纯函数**。
以固定锚点 ANCHOR 为起点推演，随机抖动由时间戳派生（而非序列位置），
因此任意时刻查询同一个时刻得到的值都一致，历史与实时采集不会打架。

生成特征：
  * 基线日耗 ~7 度，周末偏高
  * 早晚双峰（7-9 点、19-24 点），凌晨低谷
  * 偶发大功率事件（烧水/吹风）
  * 余额低于 60 度时自动充值 100 度，形成锯齿状充值曲线
  * 每 20 天有一段异常高耗（模拟忘关空调）
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta

from .base import BaseAdapter, Reading

# 推演锚点：固定不变，保证任意窗口查询结果一致
ANCHOR = datetime(2026, 1, 1, 0, 0)
START_BALANCE = 130.0
TOPUP_BELOW = 60.0        # 余额低于此值触发充值
TOPUP_AMOUNT = 100.0      # 每次充值度数
SPIKE_PERIOD = 20         # 每 20 天一次异常高耗
RECENT_DAYS = 45          # 默认生成多少天历史


class DemoAdapter(BaseAdapter):
    name = "demo"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.interval = int(cfg["collect"].get("interval_minutes", 30))

    def check(self) -> str:
        return "演示数据源就绪（无需凭据）"

    def fetch(self, room: dict) -> Reading:
        now = datetime.now().replace(second=0, microsecond=0)
        kwh = self.balance_at(room, now)
        price = float(room.get("unit_price") or 0.538)
        return Reading(room["room_key"], round(kwh, 3), round(kwh * price, 2),
                       raw={"demo": True}, source=self.name)

    # ------------------------------------------------------------ 确定性随机
    @staticmethod
    def _hash01(*parts: str) -> float:
        """由字符串派生 [0,1) 的确定性伪随机数（不用内置 hash，避免进程间漂移）。"""
        key = "|".join(parts).encode("utf-8")
        h = int(hashlib.md5(key).hexdigest()[:12], 16)
        return h / float(16 ** 12)

    def _rate_at(self, room: dict, dt: datetime) -> float:
        """该时刻的瞬时功率（kW），确定性。"""
        h = dt.hour + dt.minute / 60.0
        morning = math.exp(-((h - 8.0) ** 2) / 3.0)
        evening = math.exp(-((h - 21.5) ** 2) / 8.0)
        base = 0.16 + 0.04 * math.sin(dt.timetuple().tm_yday / 58.0)
        weekend = 1.35 if dt.weekday() >= 5 else 1.0
        k = (base + 0.5 * morning + 0.7 * evening) * weekend

        stamp = dt.isoformat()
        rk = room["room_key"]
        k *= 1.0 + (self._hash01(rk, "jit", stamp) - 0.5) * 0.44   # ±22% 抖动

        day_i = (dt.date() - ANCHOR.date()).days
        if day_i % SPIKE_PERIOD == 5 and 18 <= dt.hour:
            k += 0.85                                              # 忘关空调（当晚）
        if self._hash01(rk, "evt", stamp) < 0.004:
            k += 1.2 + self._hash01(rk, "evt2", stamp) * 1.2       # 偶发大功率
        return k

    # ------------------------------------------------------------ 推演
    def _series(self, room: dict, start: datetime, end: datetime,
                interval_min: int | None = None) -> list[tuple[datetime, float]]:
        """从固定锚点推演到 end，只返回 start 之后的点。"""
        step_min = interval_min or self.interval
        step_h = step_min / 60.0
        t = ANCHOR
        balance = START_BALANCE
        out: list[tuple[datetime, float]] = []
        while t <= end:
            balance = max(0.0, balance - self._rate_at(room, t) * step_h)
            if balance < TOPUP_BELOW and t.hour == 10 and t.minute < step_min:
                balance += TOPUP_AMOUNT
            if t >= start:
                out.append((t, round(balance, 3)))
            t += timedelta(minutes=step_min)
        return out

    def balance_at(self, room: dict, when: datetime) -> float:
        end = when.replace(second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(minute=0)
        series = self._series(room, start, end)
        return series[-1][1] if series else START_BALANCE

    # ------------------------------------------------------------ 批量灌库
    def seed_history(self, room: dict, days: int = RECENT_DAYS) -> list[tuple]:
        """批量生成历史读数，返回可直接 bulk_insert 的元组列表。"""
        price = float(room.get("unit_price") or 0.538)
        end = datetime.now().replace(minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=days)).replace(minute=0)
        series = self._series(room, start, end)
        return [
            (room["room_key"], t.isoformat(timespec="seconds"), bal,
             round(bal * price, 2), self.name, '{"demo":true}')
            for t, bal in series
        ]
