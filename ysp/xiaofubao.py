"""易校园上游（小富宝 / 正元智慧）直连客户端。

用途：**不依赖任何第三方站点**地查询电费。

凭据说明（重点）：
  * `shiroJID` —— 登录成功后服务器下发的会话票据（就是个 Cookie）。
                  **它本身就是"登录"的产物**，不是绕过登录。
                  获取方式二选一：
                    A. 电脑浏览器打开 https://application.xiaofubao.com/ 登录，
                       F12 → Application → Cookies → 复制 shiroJID（不用抓包工具）
                    B. 手机易校园 App + HttpCanary/Stream 抓包
  * `ymId` —— 用户在一卡通系统里的编号，串数字。
                  本模块会尝试用 shiroJID 自动查出来（`discover()`），
                  查不到时再让用户手填。

上游响应信封：
    成功 {"statusCode":200,"success":true,"data":{...}}
    失效 {"statusCode":204,"message":"请重新登录","success":false}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

BASE = "https://application.xiaofubao.com"

# 可能携带用户信息（含 ymId）的端点，按可能性排序
USER_ENDPOINTS = (
    "/app/kayroad/getUserInfo",
    "/app/kayroad/queryCardInfo",
    "/app/electric/getCoutomConfig",
    "/app/login/getUser4Authorize",
)

# ymId 在这些字段名下出现过
YM_KEYS = ("ymId", "ymid", "ym_id", "YMID", "userId", "userNo", "custId", "customerId",
           "personId", "cardNo", "account", "accNum")

ROOM_KEYS = {
    "area_id": ("areaNo", "areaId", "areaCode", "campusNo"),
    "building_code": ("buildNo", "buildId", "buildingNo", "buildCode"),
    "floor_code": ("floorNo", "floorId", "floorCode"),
    "room_code": ("roomNo", "roomId", "roomCode"),
    "room_name": ("roomName", "roomFullName", "name"),
    "building_name": ("buildName", "buildingName"),
    "area_name": ("areaName", "campusName"),
}


class UpstreamError(RuntimeError):
    """上游明确返回的业务错误（会话失效、参数错等）。"""


@dataclass
class UpstreamResult:
    ok: bool
    raw: Any = None
    message: str = ""
    data: Any = None
    found: dict = field(default_factory=dict)


def _http(url: str, cookie: str, body: dict | None = None,
          timeout: int = 20, insecure: bool = True) -> Any:
    import ssl

    data = None
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Cookie": cookie,
        "Origin": BASE,
        "Referer": BASE + "/",
    }
    if body is not None:
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if body is not None else "GET")
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise UpstreamError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise UpstreamError(f"网络不可达：{e.reason}") from None
    if not text.strip():
        raise UpstreamError("上游返回空响应（该路径可能不存在）")
    try:
        return json.loads(text)
    except ValueError:
        raise UpstreamError(f"上游返回非 JSON：{text[:200]}") from None


def normalize_cookie(raw: str) -> str:
    """接受三种输入：纯 shiroJID 值 / `shiroJID=xxx` / 整段 Cookie 字符串。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if "=" not in s:
        return f"shiroJID={s}"
    # 整段 Cookie 里挑出 shiroJID
    parts = [p.strip() for p in s.split(";") if p.strip()]
    for p in parts:
        k, _, v = p.partition("=")
        if k.strip().lower() == "shirojid" and v.strip():
            return f"shiroJID={v.strip()}"
    # 没找到 shiroJID 就原样带上（可能上游还需要别的 cookie）
    return s


def unwrap(payload: Any, what: str = "请求") -> UpstreamResult:
    """解析易校园响应信封。"""
    if not isinstance(payload, dict):
        return UpstreamResult(ok=True, raw=payload, data=payload)

    code = payload.get("statusCode")
    success = payload.get("success")
    msg = str(payload.get("message") or payload.get("msg") or "")

    if success is False or (code is not None and str(code) not in ("200", "0")):
        if str(code) == "204" or "重新登录" in msg or "登录" in msg and "失效" in msg:
            raise UpstreamError("会话已失效，请重新获取 shiroJID")
        raise UpstreamError(msg or f"{what} 失败（statusCode={code}）")

    return UpstreamResult(ok=True, raw=payload, message=msg, data=payload.get("data"))


def _dig_first(node: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 6 or node is None:
        return None
    if isinstance(node, dict):
        low = {str(k).lower(): v for k, v in node.items()}
        for k in keys:
            if k.lower() in low and low[k.lower()] not in (None, "", []):
                return low[k.lower()]
        for v in node.values():
            r = _dig_first(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _dig_first(v, keys, depth + 1)
            if r is not None:
                return r
    return None


def discover(cookie: str, timeout: int = 20) -> dict:
    """用 shiroJID 探出 ymId 与房间信息。探不到就返回空字段，不报错。"""
    cookie = normalize_cookie(cookie)
    found: dict[str, Any] = {"ym_id": None, "probe": [], "user": None}
    for path in USER_ENDPOINTS:
        try:
            payload = _http(BASE + path, cookie, body={}, timeout=timeout)
        except UpstreamError as e:
            found["probe"].append({"path": path, "ok": False, "detail": str(e)})
            continue
        try:
            res = unwrap(payload, path)
        except UpstreamError as e:
            found["probe"].append({"path": path, "ok": False, "detail": str(e)})
            if "会话已失效" in str(e):
                raise
            continue
        data = res.data if res.data is not None else res.raw
        found["probe"].append({"path": path, "ok": True,
                               "keys": sorted(data.keys()) if isinstance(data, dict) else type(data).__name__})
        if found["user"] is None:
            found["user"] = data
        ym = _dig_first(data, YM_KEYS)
        if ym is not None and found["ym_id"] is None:
            found["ym_id"] = str(ym)
        for field_name, keys in ROOM_KEYS.items():
            if not found.get(field_name):
                v = _dig_first(data, keys)
                if v is not None:
                    found[field_name] = str(v)
    return found


def verify(cookie: str, timeout: int = 20) -> UpstreamResult:
    """只验证会话是否有效。"""
    cookie = normalize_cookie(cookie)
    last_err: Exception | None = None
    for path in USER_ENDPOINTS:
        try:
            payload = _http(BASE + path, cookie, body={}, timeout=timeout)
            res = unwrap(payload, path)
            res.found = {"path": path}
            return res
        except UpstreamError as e:
            last_err = e
            if "会话已失效" in str(e):
                raise
            continue
    raise UpstreamError(f"无法用该凭据访问易校园：{last_err}")


def query_surplus(cookie: str, ym_id: str, room: dict | None = None,
                  timeout: int = 20) -> Any:
    """查剩余电量。"""
    room = room or {}
    body = {
        "ymId": str(ym_id),
        "areaNo": str(room.get("area_id") or ""),
        "buildNo": str(room.get("building_code") or ""),
        "floorNo": str(room.get("floor_code") or ""),
        "roomNo": str(room.get("room_code") or ""),
    }
    return _http(BASE + "/app/electric/queryISIMSRoomSurplus",
                 normalize_cookie(cookie), body=body, timeout=timeout)
