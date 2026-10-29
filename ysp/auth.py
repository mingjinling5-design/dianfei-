"""短信验证码登录：把「手机号 + 验证码」流程做进本项目自己的后端。

设计目标：**单一学校、不抓包**。
你在自己的网页上输入手机号和验证码，后端替你完成登录，之后所有查询都用这个会话。

登录态存放位置：
  * SQLite 的 meta 表（`session` 键）—— 运行期真源
  * config.yaml 的 source.phone / school_code —— 落盘备份，重启不丢

上游会话失效时，`user_info()` 会带出 `need_relogin=True`，
前端据此弹出「重新登录」向导，重新发一次验证码即可，无需抓包。
"""
from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import datetime
from typing import Any

from .adapters.base import SourceError, http_json

SESSION_KEY = "session"


class AuthError(SourceError):
    """登录相关的可预期错误（验证码错、手机号错、需要重登…）。"""


class AuthClient:
    """与 nofish 同构的账号 / 电费接口客户端。"""

    def __init__(self, base_url: str = "https://tool.no-fish.cn",
                 timeout: int = 20, strict_tls: bool = False):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.strict_tls = strict_tls

    # ---------------------------------------------------------- 底层
    def _call(self, path: str, method: str = "GET",
              params: dict | None = None, body: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
            if qs:
                url += "?" + qs
        payload = http_json(url, method=method, body=body, timeout=self.timeout,
                            insecure_tls=not self.strict_tls)
        return self._unwrap(payload, path)

    @staticmethod
    def _unwrap(payload: Any, what: str = "请求") -> Any:
        """校验上游响应信封 —— 关键！上游失败时也可能回 HTTP 200。

        成功：{"status":"ok","message":"验证码已发送"}
              {"code":0,"data":{...}} / {"code":200,...}
        失败：{"detail":"短信发送超限，请一分钟后再试"}
              {"code":500,"msg":"Server error","data":null}
              {"status":"error","message":"..."}
        """
        if not isinstance(payload, dict):
            return payload

        d = payload.get("detail")
        if isinstance(d, str) and d.strip():
            raise AuthError(d.strip())
        if isinstance(d, list):  # FastAPI 422
            fields = "、".join(str(x.get("loc", ["?"])[-1]) for x in d if isinstance(x, dict))
            raise AuthError(f"缺少必填参数：{fields}")

        code = payload.get("code")
        if code is not None and str(code) not in ("0", "200", "None", "true", "True"):
            msg = payload.get("msg") or payload.get("message") or f"{what} 失败（code={code}）"
            raise AuthError(str(msg))

        status = payload.get("status")
        if isinstance(status, bool) and not status:
            raise AuthError(str(payload.get("message") or payload.get("msg") or f"{what} 失败"))
        if isinstance(status, str) and status.strip().lower() not in ("ok", "success", "true", "1"):
            raise AuthError(str(payload.get("message") or payload.get("msg") or f"{what} 失败"))

        # 还有一层：{"status":"ok","data":{"code":500,...}}
        data = payload.get("data")
        if isinstance(data, dict):
            inner = data.get("code")
            if inner is not None and str(inner) not in ("0", "200", "None"):
                raise AuthError(str(data.get("msg") or data.get("message") or f"{what} 失败"))
        return payload

    @staticmethod
    def _detail(exc_msg: str) -> str:
        """把上游异常串转成人话，并保留上游自己的提示。"""
        text = exc_msg
        if "::" in exc_msg:
            text = exc_msg.split("::", 1)[-1].strip()
        try:
            payload = json.loads(text)
        except ValueError:
            # 不是 JSON：多半是 HTTP 状态行 + 文本
            if "HTTP 500" in exc_msg:
                return "上游服务器出错（HTTP 500）。可能是短信号码未在该校注册，或发送过于频繁，请稍后重试。"
            return exc_msg
        if isinstance(payload, dict):
            d = payload.get("detail")
            if isinstance(d, str) and d.strip():
                return d.strip()
            if isinstance(d, list):
                fields = "、".join(str(x.get("loc", ["?"])[-1]) for x in d if isinstance(x, dict))
                return f"缺少必填参数：{fields}"
            msg = payload.get("msg") or payload.get("message")
            if isinstance(msg, str) and msg.strip() and msg.strip().lower() != "server error":
                return msg.strip()
            if str(payload.get("code")) == "500":
                return "上游服务器出错（HTTP 500）。可能是短信号码未在该校注册，或发送过于频繁，请稍后重试。"
        return exc_msg

    # ---------------------------------------------------------- 学校
    def schools(self, keyword: str = "") -> list[dict]:
        data = self._call("/api/yischool/electric/schools", params={"school_name": keyword})
        if not isinstance(data, list):
            raise AuthError(f"学校列表返回异常：{str(data)[:200]}")
        return [
            {"school_code": str(x.get("schoolCode", "")),
             "school_name": str(x.get("schoolName", "")),
             # signType / cardExistFlag 是上游给出的学校能力标记，用于排查登录方式
             "sign_type": x.get("signType"),
             "card_exist": x.get("cardExistFlag")}
            for x in data
        ]

    # ---------------------------------------------------------- 登录
    def send_code(self, phone: str) -> dict:
        phone = (phone or "").strip()
        if not (len(phone) == 11 and phone.isdigit()):
            raise AuthError("请输入 11 位中国大陆手机号")
        self._cooldown_guard("send:" + phone, 60, "验证码已发送，请等待倒计时结束后再试")
        try:
            r = self._call("/api/yischool/user/login/send-code",
                           method="POST", body={"phone": phone})
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None
        self._mark("send:" + phone)
        return r

    # ---------------------------------------------------------- 冷却
    # 上游按手机号限流（提示「短信发送超限，请一分钟后再试」），
    # 本地先拦一道，避免把配额烧光。
    _last: dict[str, float] = {}

    @classmethod
    def _cooldown_guard(cls, key: str, seconds: int, msg: str) -> None:
        import time

        last = cls._last.get(key, 0.0)
        left = seconds - (time.time() - last)
        if left > 0:
            raise AuthError(f"{msg}（还需 {int(left) + 1} 秒）")

    @classmethod
    def _mark(cls, key: str) -> None:
        import time

        cls._last[key] = time.time()

    def login(self, phone: str, sms_code: str,
              school_code: str = "", school_name: str = "") -> dict:
        phone = (phone or "").strip()
        sms_code = (sms_code or "").strip()
        if not phone or not sms_code:
            raise AuthError("手机号和验证码都不能为空")
        try:
            return self._call("/api/yischool/user/login", method="POST", body={
                "phone": phone, "sms_code": sms_code,
                "school_code": school_code or "", "school_name": school_name or "",
            })
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None

    def relogin_send_code(self, phone: str) -> dict:
        self._cooldown_guard("relogin:" + phone, 60, "验证码已发送，请等待倒计时结束后再试")
        try:
            r = self._call(f"/api/yischool/user/{phone}/relogin/send-code", method="POST", body={})
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None
        self._mark("relogin:" + phone)
        return r

    def relogin(self, phone: str, sms_code: str) -> dict:
        try:
            return self._call(f"/api/yischool/user/{phone}/relogin",
                              method="POST", body={"sms_code": sms_code})
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None

    def user_info(self, phone: str) -> dict:
        try:
            return self._call(f"/api/yischool/user/{phone}")
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None

    # ---------------------------------------------------------- 宿舍位置
    def areas(self, phone: str, school_code: str = "") -> list[dict]:
        d = self._call("/api/yischool/electric/areas",
                       params={"phone": phone, "school_code": school_code})
        return _pairs(d, "id", "areaName")

    def buildings(self, phone: str, area_id: Any) -> list[dict]:
        d = self._call("/api/yischool/electric/buildings",
                       params={"phone": phone, "area_id": area_id})
        return _pairs(d, "buildingCode", "buildingName")

    def floors(self, phone: str, area_id: Any, building_code: str) -> list[dict]:
        d = self._call("/api/yischool/electric/floors",
                       params={"phone": phone, "area_id": area_id, "building_code": building_code})
        return _pairs(d, "floorCode", "floorName")

    def rooms(self, phone: str, area_id: Any, building_code: str, floor_code: str) -> list[dict]:
        d = self._call("/api/yischool/electric/rooms",
                       params={"phone": phone, "area_id": area_id,
                               "building_code": building_code, "floor_code": floor_code})
        return _pairs(d, "roomCode", "roomName")

    def set_location(self, phone: str, payload: dict) -> dict:
        try:
            return self._call(f"/api/yischool/user/{phone}/location", method="POST", body=payload)
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None

    # ---------------------------------------------------------- 电费
    def surplus(self, phone: str) -> Any:
        try:
            return self._call("/api/yischool/electric/surplus", params={"phone": phone})
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None

    def records(self, phone: str, days: int = 7) -> Any:
        try:
            return self._call(f"/api/yischool/electric/records/{phone}", params={"days": str(days)})
        except SourceError as e:
            raise AuthError(self._detail(str(e))) from None


def _pairs(data: Any, key_field: str, name_field: str) -> list[dict]:
    if not isinstance(data, list):
        return []
    out = []
    for x in data:
        if not isinstance(x, dict):
            continue
        k = x.get(key_field)
        n = x.get(name_field)
        if k is None:
            continue
        out.append({"value": k, "label": str(n or k)})
    return out


# ------------------------------------------------------------------ 会话

def load_session(conn: sqlite3.Connection) -> dict:
    raw = _meta(conn, SESSION_KEY)
    return raw if isinstance(raw, dict) else {}


def save_session(conn: sqlite3.Connection, **kw) -> dict:
    s = load_session(conn)
    s.update(kw)
    s["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _set_meta(conn, SESSION_KEY, s)
    return s


def clear_session(conn: sqlite3.Connection) -> None:
    _set_meta(conn, SESSION_KEY, {})


def _meta(conn: sqlite3.Connection, k: str) -> Any:
    if getattr(conn,'tenant_store',False):return conn.get_meta(k)
    row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["v"])
    except (TypeError, ValueError):
        return None


def _set_meta(conn: sqlite3.Connection, k: str, v: Any) -> None:
    if getattr(conn,'tenant_store',False):
        conn.set_meta(k,v)
        return
    conn.execute(
        "INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (k, json.dumps(v, ensure_ascii=False)),
    )
    conn.commit()


def resolve_phone(cfg: dict, conn: sqlite3.Connection | None = None) -> str:
    """会话里的手机号优先（必须真正登录过），其次 config。"""
    if conn is not None:
        s = load_session(conn)
        if s.get("phone") and s.get("logged_in_at"):
            return str(s["phone"])
    return str(cfg.get("source", {}).get("phone") or "")


def is_logged_in(conn: sqlite3.Connection) -> bool:
    s = load_session(conn)
    return bool(s.get("phone")) and bool(s.get("logged_in_at")) and not s.get("need_relogin")


def resolve_school(cfg: dict, conn: sqlite3.Connection | None = None) -> tuple[str, str]:
    """返回 (school_code, school_name)；单一学校，登录后固定。"""
    if conn is not None:
        s = load_session(conn)
        if s.get("school_code"):
            return str(s["school_code"]), str(s.get("school_name") or "")
    src = cfg.get("source", {})
    return str(src.get("school_code") or ""), str(src.get("school_name") or "")
