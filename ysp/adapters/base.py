"""数据源适配器：把不同上游的电费接口归一化成 Reading。"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# 某些校园网/代理会做 TLS 中间人，或上游证书过期。
# 遇到 CERTIFICATE_VERIFY_FAILED 时自动降级并打印一次告警，避免整个采集流程直接挂掉。
_INSECURE_CTX: ssl.SSLContext | None = None
_TLS_WARNED = False


def _ssl_context(insecure: bool) -> ssl.SSLContext | None:
    global _INSECURE_CTX
    if not insecure:
        return None
    if _INSECURE_CTX is None:
        _INSECURE_CTX = ssl.create_default_context()
        _INSECURE_CTX.check_hostname = False
        _INSECURE_CTX.verify_mode = ssl.CERT_NONE
    return _INSECURE_CTX


@dataclass
class Reading:
    room_key: str
    balance_kwh: float | None
    balance_cny: float | None = None
    ts: str | None = None
    raw: Any = None
    source: str = ""
    unit: str = ""          # 上游原生单位："cny"（元）或 "kwh"（度）


class SourceError(RuntimeError):
    pass


def http_json(url: str, method: str = "GET", body: dict | None = None,
              headers: dict | None = None, timeout: int = 20,
              insecure_tls: bool = False) -> Any:
    global _TLS_WARNED
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "yischool-power/1.0"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)

    def _do(ctx: ssl.SSLContext | None) -> str:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", "replace")

    try:
        try:
            text = _do(_ssl_context(insecure_tls))
        except urllib.error.URLError as e:
            if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                raise
            if not _TLS_WARNED:
                _TLS_WARNED = True
                print("[!] TLS 证书校验失败（上游证书过期或代理中间人），已降级为不校验。"
                      "如需强制校验，请在 config.yaml 设 source.strict_tls: true",
                      file=sys.stderr)
            text = _do(_ssl_context(True))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise SourceError(f"HTTP {e.code} {url} :: {detail}") from None
    except urllib.error.URLError as e:
        raise SourceError(f"网络不可达 {url} :: {e.reason}") from None
    try:
        return json.loads(text)
    except ValueError:
        raise SourceError(f"响应不是 JSON（{url}）:: {text[:200]}") from None


# 上游字段名五花八门，这里做一次宽松的“找数”归一化。
#
# 重要：nofish 上游返回的 surplus_amount 单位是**元**（剩余金额），不是度。
#      前端渲染为 `<span>{value}</span><span>元</span>`，告警线也是按元设的。
#      所以它必须出现在 _CNY_KEYS 里，不能出现在 _KWH_KEYS 里。
_KWH_KEYS = (
    "surplusKwh", "surplus_kwh", "surplusElectric", "surplus_degree",
    "balanceKwh", "balance_kwh", "balanceElectric",
    "remainKwh", "remain_kwh", "remaining_kwh",
    "electric", "electricity", "electricBalance", "quantity",
    "roomSurplus", "syElectric", "剩余电量", "电量", "度数",
)
_CNY_KEYS = (
    "surplus_amount", "surplusAmount", "surplus_money", "surplusMoney",
    "balanceAmount", "balance_amount", "balanceCny", "balance_cny",
    "remain_amount", "remainingAmount", "remainAmount",
    "amount", "money", "fee", "cny", "余额", "剩余金额", "金额",
)


def dig_kwh(payload: Any) -> float | None:
    """在任意嵌套结构里找出第一个像“剩余电量(度)”的数值。"""
    return _dig(payload, _KWH_KEYS)


def dig_cny(payload: Any) -> float | None:
    return _dig(payload, _CNY_KEYS)


def _dig(node: Any, keys: tuple[str, ...], depth: int = 0) -> float | None:
    if depth > 6:
        return None
    if isinstance(node, dict):
        low = {str(k).lower(): v for k, v in node.items()}
        for key in keys:
            v = low.get(key.lower())
            f = _as_float(v)
            if f is not None:
                return f
        for v in node.values():
            f = _dig(v, keys, depth + 1)
            if f is not None:
                return f
    elif isinstance(node, list):
        for v in node:
            f = _dig(v, keys, depth + 1)
            if f is not None:
                return f
    return None


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- adapters

class BaseAdapter:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def fetch(self, room: dict) -> Reading:
        raise NotImplementedError

    def check(self) -> str:
        """连通性/凭据自检，返回人类可读结论。"""
        return "未实现自检"


class NoFishAdapter(BaseAdapter):
    """与 tool.no-fish.cn/yischool 同构的后端接口。

    该站前端为 Vue SPA，真实数据全部来自 `/api/yischool/*`：
      POST /api/yischool/user/login/send-code   <- 发短信验证码
      POST /api/yischool/user/login             <- 手机号 + 验证码登录
      GET  /api/yischool/electric/schools?school_name=
      GET  /api/yischool/electric/areas?phone=&school_code=
      GET  /api/yischool/electric/buildings?phone=&area_id=
      GET  /api/yischool/electric/floors?phone=&area_id=&building_code=
      GET  /api/yischool/electric/rooms?phone=&area_id=&building_code=&floor_code=
      GET  /api/yischool/electric/surplus?phone=            <- 当前余额
      GET  /api/yischool/electric/records/{phone}?days=N    <- 历史（默认只给 7 天）

    登录由本项目的 `ysp.auth` 负责（自己的网页输验证码，无需抓包），
    这里只负责用会话里的手机号取数。
    """

    name = "nofish"

    def __init__(self, cfg: dict, phone: str | None = None):
        super().__init__(cfg)
        src = cfg["source"]
        self.base = str(src.get("base_url") or "https://tool.no-fish.cn").rstrip("/")
        self.phone = str(phone or src.get("phone") or "")
        self.timeout = int(src.get("timeout", 20))
        self.strict_tls = bool(src.get("strict_tls", False))

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
            if qs:
                url += "?" + qs
        return http_json(url, timeout=self.timeout, insecure_tls=not self.strict_tls)

    def check(self) -> str:
        if not self.phone:
            raise SourceError(
                "尚未登录。请运行 `python cli.py login`，"
                "或打开 http://127.0.0.1:8787/login 用手机验证码登录。"
            )
        info = self._get(f"/api/yischool/user/{self.phone}")
        if info.get("need_relogin"):
            raise SourceError("登录已过期，请重新登录（网页点「重新登录」或运行 python cli.py login）")
        bound = info.get("is_bound")
        return (f"用户 {self.phone} 可用；已绑定宿舍={bound}；"
                f"校区={info.get('area_name','')} 楼栋={info.get('building_name','')} "
                f"房间={info.get('room_name','')}")

    def fetch(self, room: dict) -> Reading:
        if not self.phone:
            raise SourceError("未配置 source.phone")
        payload = self._get("/api/yischool/electric/surplus", {"phone": self.phone})
        cny = dig_cny(payload)
        kwh = dig_kwh(payload)
        if kwh is None and cny is None:
            raise SourceError(f"无法从响应中解析余额：{json.dumps(payload, ensure_ascii=False)[:300]}")

        if cny is not None:
            # 上游给的是「元」（剩余金额）。不折算成度 ——
            # 校园电价常为阶梯电价，用固定单价反推度数会得到错误结果。
            return Reading(room["room_key"], None, cny, raw=payload,
                           source=self.name, unit="cny")

        price = float(room.get("unit_price") or 0.538)
        return Reading(room["room_key"], kwh, round(kwh * price, 2), raw=payload,
                       source=self.name, unit="kwh")

    def history(self, days: int = 7) -> Any:
        return self._get(f"/api/yischool/electric/records/{self.phone}", {"days": str(days)})

    def list_schools(self, name: str = "") -> Any:
        return self._get("/api/yischool/electric/schools", {"school_name": name})


class XiaofubaoAdapter(BaseAdapter):
    """直连易校园上游（小富宝/正元智慧）。

    抓包路径：`https://application.xiaofubao.com/app/electric/queryISIMSRoomSurplus`
    需要两个凭据：
      * shiroJID  —— 位于请求 Cookie 中，形如 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
      * ymId      —— 位于请求参数/表单中，一串数字
    抓法：手机装「易校园」+ HttpCanary/Stream，登录后点一次「电费查询」，
    在抓到的请求里复制 Cookie 与参数即可。
    """

    name = "xiaofubao"

    def __init__(self, cfg: dict, shiro_jid: str | None = None, ym_id: str | None = None):
        super().__init__(cfg)
        src = cfg["source"]
        self.endpoint = str(
            src.get("endpoint")
            or "https://application.xiaofubao.com/app/electric/queryISIMSRoomSurplus"
        )
        self.shiro = str(shiro_jid or src.get("shiro_jid") or "")
        self.ym_id = str(ym_id or src.get("ym_id") or "")
        self.timeout = int(src.get("timeout", 20))
        self.strict_tls = bool(src.get("strict_tls", False))

    def check(self) -> str:
        if not self.shiro or not self.ym_id:
            raise SourceError("未配置 source.shiro_jid / source.ym_id")
        r = self.fetch({"room_key": "probe", "unit_price": 0.538})
        return f"上游可达，探针余额={r.balance_kwh}"

    def fetch(self, room: dict) -> Reading:
        if not self.shiro or not self.ym_id:
            raise SourceError("未配置 source.shiro_jid / source.ym_id")
        body = {
            "ymId": self.ym_id,
            "areaNo": str(room.get("area_id") or ""),
            "buildNo": str(room.get("building_code") or ""),
            "floorNo": str(room.get("floor_code") or ""),
            "roomNo": str(room.get("room_code") or ""),
        }
        headers = {"Cookie": f"shiroJID={self.shiro}"}
        payload = http_json(self.endpoint, method="POST", body=body,
                            headers=headers, timeout=self.timeout,
                            insecure_tls=not self.strict_tls)
        kwh = dig_kwh(payload)
        cny = dig_cny(payload)
        if kwh is None and cny is None:
            raise SourceError(f"上游响应无法解析：{json.dumps(payload, ensure_ascii=False)[:300]}")
        price = float(room.get("unit_price") or 0.538)
        if kwh is None and cny is not None:
            kwh = cny / price if price else None
        if cny is None and kwh is not None:
            cny = kwh * price
        return Reading(room["room_key"], kwh, cny, raw=payload, source=self.name)


def build_adapter(cfg: dict, session: dict | None = None) -> BaseAdapter:
    """session 来自登录态（ysp.auth.load_session），优先级高于 config。"""
    s = session or {}
    # 会话里显式指定了数据源就用它（例如粘贴 Cookie 导入的 xiaofubao 会话）
    kind = str(s.get("source_type") or cfg["source"].get("type", "demo")).lower()
    if cfg['source'].get('type') == 'eschool':
        from .eschool import EschoolAdapter
        return EschoolAdapter(cfg)
    if kind == "nofish":
        return NoFishAdapter(cfg, phone=s.get("phone"))
    if kind == "xiaofubao":
        return XiaofubaoAdapter(cfg, shiro_jid=s.get("shiro_jid") or s.get("cookie"),
                                ym_id=s.get("ym_id"))
    from .demo import DemoAdapter

    return DemoAdapter(cfg)
