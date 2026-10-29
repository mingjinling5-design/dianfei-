#!/usr/bin/env python
"""yischool-power 命令行入口。

  python cli.py init                初始化数据库与房间配置
  python cli.py login               终端登录（手机号 + 短信验证码，无需抓包）
  python cli.py seed-demo [天数]    写入演示数据（无需凭据，先看效果）
  python cli.py check               自检登录态与数据源
  python cli.py collect             采集一次
  python cli.py loop                常驻整点采集（推荐部署用）
  python cli.py report [天数]       终端打印分析报告
  python cli.py quality             数据质量体检（覆盖率/缺口/卡住）
  python cli.py test-email          发一封测试邮件，验证 SMTP 配置
  python cli.py test-alert          跑一次告警规则（只看会报什么，不推送）
  python cli.py export       # 导出全部历史为 CSV
python cli.py backup       # 立即做一次数据库快照
python cli.py serve               启动 Web 仪表盘
"""
from __future__ import annotations

import csv
import io
import sys
import time
from datetime import date, datetime, timedelta

from ysp import analyze, db
from ysp.adapters import SourceError, build_adapter
from ysp.auth import (
    AuthClient,
    AuthError,
    load_session,
    resolve_phone,
    resolve_school,
    save_session,
)
from ysp.collect import backfill_from_nofish, collect_once, run_forever, seed_demo
from ysp.config import db_path, load_config
from ysp.notify import has_channel, test_email


def _client(cfg):
    src = cfg["source"]
    return AuthClient(str(src.get("base_url") or "https://tool.no-fish.cn"),
                      int(src.get("timeout", 20)), bool(src.get("strict_tls", False)))


def cmd_login(argv):
    """终端交互式登录：手机号 → 验证码 → 选学校 → 选宿舍。全部在本地完成。"""
    cfg = load_config()
    if str(cfg["source"].get("type", "demo")).lower() != "nofish":
        print(f"当前数据源是 {cfg['source'].get('type')}，无需登录。"
              "如需验证码登录，请把 config.yaml 的 source.type 改为 nofish。")
        return 1
    conn = db.connect(db_path(cfg))
    cli = _client(cfg)

    phone = input("手机号（11 位）：").strip()
    try:
        cli.send_code(phone)
        print("验证码已发送，请查看短信。")
    except AuthError as e:
        print(f"发送失败：{e}")
        return 1

    code = input("短信验证码：").strip()

    school_code, school_name = resolve_school(cfg, conn)
    if not school_code:
        print("\n搜索学校（直接回车列出全部）：")
        kw = input("关键词：").strip()
        try:
            schools = cli.schools(kw)
        except AuthError as e:
            print(f"学校列表获取失败：{e}")
            return 1
        if not schools:
            print("没搜到，换个关键词再试。")
            return 1
        for i, s in enumerate(schools[:40], 1):
            print(f"  [{i:>2}] {s['school_name']}  ({s['school_code']})")
        try:
            pick = int(input("选择序号：").strip())
            school_code = schools[pick - 1]["school_code"]
            school_name = schools[pick - 1]["school_name"]
        except (ValueError, IndexError):
            print("选择无效。")
            return 1
    else:
        print(f"沿用已配置学校：{school_name}（{school_code}）")

    try:
        cli.login(phone, code, school_code, school_name)
    except AuthError as e:
        print(f"登录失败：{e}")
        return 1

    save_session(conn, phone=phone, school_code=school_code, school_name=school_name,
                 need_relogin=False, logged_in_at=datetime.now().isoformat(timespec="seconds"))
    print(f"\n✓ 登录成功：{phone} · {school_name}")

    info = cli.user_info(phone)
    if info.get("need_location") or not info.get("is_bound"):
        print("\n还需绑定宿舍位置。")
        try:
            areas = cli.areas(phone, school_code)
            for i, a in enumerate(areas, 1):
                print(f"  [{i:>2}] {a['label']}")
            area = areas[int(input("校区序号：").strip()) - 1]

            builds = cli.buildings(phone, area["value"])
            for i, b in enumerate(builds, 1):
                print(f"  [{i:>2}] {b['label']}")
            bld = builds[int(input("楼栋序号：").strip()) - 1]

            floors = cli.floors(phone, area["value"], bld["value"])
            for i, f in enumerate(floors, 1):
                print(f"  [{i:>2}] {f['label']}")
            flr = floors[int(input("楼层序号：").strip()) - 1]

            rooms = cli.rooms(phone, area["value"], bld["value"], flr["value"])
            for i, r in enumerate(rooms, 1):
                print(f"  [{i:>2}] {r['label']}")
            rm = rooms[int(input("房间序号：").strip()) - 1]
        except (ValueError, IndexError):
            print("选择无效。可稍后在网页 http://127.0.0.1:8787/login 上绑定。")
            return 1
        except AuthError as e:
            print(f"位置接口出错：{e}")
            return 1

        cli.set_location(phone, {
            "area_id": area["value"], "area_name": area["label"],
            "building_code": bld["value"], "building_name": bld["label"],
            "floor_code": flr["value"], "floor_name": flr["label"],
            "room_code": rm["value"], "room_name": rm["label"],
        })
        info = cli.user_info(phone)
        save_session(conn, need_location=False)
        print("✓ 宿舍位置已绑定")

    # 登记房间
    from ysp.server import _register_room_from_info

    room = _register_room_from_info(info)
    print(f"✓ 已登记房间：{room['label']}")
    print("\n下一步：python cli.py collect   然后 python cli.py serve")
    conn.close()
    return 0


def cmd_init(argv):
    cfg = load_config()
    conn = db.connect(db_path(cfg))
    for room in cfg["rooms"]:
        db.upsert_room(conn, room)
    rooms = db.list_rooms(conn)
    print(f"配置来源：{cfg['_config_path']}")
    print(f"数据库：{db_path(cfg)}")
    print(f"已登记 {len(rooms)} 个房间：")
    for r in rooms:
        print(f"  - {r['room_key']:>16}  {r['label']}  单价 {r['unit_price']} 元/度"
              f"  阈值 {r['warn_kwh']}/{r['critical_kwh']} 度")
    conn.close()


def cmd_seed(argv):
    days = int(argv[0]) if argv else 45
    r = seed_demo(days=days)
    print(f"已写入演示数据 {r['inserted']} 条（{days} 天）。运行 python cli.py serve 查看。")


def cmd_check(argv):
    cfg = load_config()
    # ⚠️ 必须带上会话：登录态存在库里，不读的话 nofish 适配器拿不到手机号，
    #    会误报"尚未登录"（明明已经登录过了）。
    conn = db.connect(db_path(cfg))
    session = load_session(conn)
    ad = build_adapter(cfg, session)
    port = cfg["app"].get("port", 8787)
    print(f"数据源：{ad.name}（{cfg['_config_path']}）")
    if ad.name == "nofish":
        print(f"登录手机号：{resolve_phone(cfg, conn) or '（无）'}")
    conn.close()
    try:
        print(ad.check())
    except SourceError as e:
        print(f"自检失败：{e}")
        if "尚未登录" in str(e):
            print(f"→ 打开 http://127.0.0.1:{port}/login 用手机验证码登录，或运行 python cli.py login")
        return 1
    return 0


def cmd_collect(argv):
    r = collect_once()
    if not r.get("ok"):
        print(f"失败：{r.get('error')}")
        return 1
    ok = sum(1 for x in r["results"] if x.get("ok"))
    new = sum(1 for x in r["results"] if x.get("inserted"))
    print(f"\n完成：成功 {ok}/{len(r['results'])}，新增 {new} 条，告警 {len(r['alerts'])} 条")


def cmd_loop(argv):
    """常驻整点采集（部署到服务器就用这个）。"""
    return run_forever(load_config(), verbose=True)


def cmd_quality(argv):
    """数据质量体检。"""
    days = int(argv[0]) if argv else 30
    cfg = load_config()
    conn = db.connect(db_path(cfg))
    native = str(db.get_meta(conn, "native_unit", "kwh"))
    unit = "元" if native == "cny" else "度"
    interval = int(cfg["collect"].get("interval_minutes", 60))
    for room in db.list_rooms(conn, enabled_only=True):
        readings = db.fetch_readings(conn, room["room_key"],
                                     since=(datetime.now() - __import__("datetime").timedelta(days=days)).isoformat(timespec="seconds"))
        analyze.native_view(readings, native)
        q = analyze.data_quality(readings, expect_minutes=interval)
        bar = "=" * 62
        print(f"\n{bar}\n  {room['label']} · 数据质量体检（近 {days} 天）\n{bar}")
        print(f"  质量评分      {q['score']} / 100   等级：{q['level']}")
        print(f"  读数条数      {q['readings']} 条 / 期望 {q['expected']} 条")
        print(f"  采样覆盖率    {q['coverage_pct']}%   （采集间隔设定 {interval} 分钟）")
        print(f"  数据新鲜度    {q['freshness_minutes']} 分钟前  （最后采集 {db.get_meta(conn,'last_collect') or '—'}）")
        print(f"  连续失败      {db.get_meta(conn,'fail_streak',0)} 次")
        if q["stale_segments"]:
            print(f"  数据卡住      {len(q['stale_segments'])} 段，合计 {q['stale_hours']} 小时")
            for sgg in q["stale_segments"][-3:]:
                tag = "（仍在卡住）" if sgg["ongoing"] else ""
                print(f"                  {sgg['start']} → {sgg['end']}  "
                      f"{sgg['hours']}h  值 {sgg['value']} {unit}{tag}")
        if q["gaps"]:
            print(f"  采集缺口      {len(q['gaps'])} 处，漏采约 {q['missing']} 个点")
            for g in q["gaps"][-3:]:
                print(f"                  {g['start']} → {g['end']}  ({g['minutes']} 分钟)")
        print("  结论：")
        for n in q["notes"]:
            print(f"    · {n}")
    conn.close()


def cmd_test_email(argv):
    cfg = load_config()
    ec = (cfg.get("alerts") or {}).get("email") or {}
    if not ec.get("enabled"):
        print("邮件未启用。请在 config.yaml 里设 alerts.email.enabled: true 并填好 SMTP 信息。")
        print("提示：QQ/163 邮箱的 password 要填「授权码」，不是登录密码。")
        return 1
    print(f"正在通过 {ec.get('smtp_host')}:{ec.get('smtp_port')} 发送测试邮件…")
    r = test_email(cfg)
    print(("✓ " if r.get("ok") else "✗ ") + str(r.get("detail")))
    return 0 if r.get("ok") else 1


def cmd_test_alert(argv):
    """跑一遍告警规则，只看会报什么，不推送、不落库。"""
    from ysp import alerts as alert_engine

    cfg = load_config()
    conn = db.connect(db_path(cfg))
    native = str(db.get_meta(conn, "native_unit", "kwh"))
    unit = "元" if native == "cny" else "度"
    total = 0
    for room in db.list_rooms(conn, enabled_only=True):
        readings = db.fetch_readings(conn, room["room_key"])
        if len(readings) < 2:
            print(f"  {room['label']}：数据太少（{len(readings)} 条），无法评估")
            continue
        analyze.native_view(readings, native)
        last = db.last_reading(conn, room["room_key"])
        bal = (last["balance_cny"] if native == "cny" else last["balance_kwh"]) if last else None
        fired = alert_engine.evaluate(conn, cfg, room, readings, native, bal)
        if not fired:
            print(f"  {room['label']}：没有需要告警的项")
        for a in fired:
            total += 1
            print(f"  [{a['level']}] {a['title']}")
            for k, v in a["rows"]:
                print(f"        {k}：{v}")
    print()
    print(f"共 {total} 条告警" + ("" if has_channel(cfg) else
          "（未配置邮件/webhook，实际运行时不会推送）"))
    conn.close()


def cmd_report(argv):
    days = int(argv[0]) if argv else 30
    cfg = load_config()
    conn = db.connect(db_path(cfg))
    native = str(db.get_meta(conn, "native_unit", "kwh"))
    unit = "元" if native == "cny" else "度"
    for room in db.list_rooms(conn, enabled_only=True):
        readings = db.fetch_readings(conn, room["room_key"])
        analyze.native_view(readings, native)
        price = analyze.native_price(room, native)
        dd = analyze._fill_missing_eod(analyze.daily(readings, price))
        last = db.last_reading(conn, room["room_key"])
        bal = None
        if last:
            bal = last["balance_cny"] if native == "cny" else last["balance_kwh"]
        s = analyze.summarize(dd, readings, room, bal, native_unit=native)
        dv = s.get("derived") or {}
        has_deg = bool(dv.get("available")) and native == "cny"

        def both(v_cny, v_kwh, dec=2):
            """同时给出元和度。"""
            if not has_deg:
                return f"{round(v_cny, dec) if v_cny is not None else '—'} {unit}"
            return (f"{round(v_cny, dec) if v_cny is not None else '—'} 元"
                    f"  ({round(v_kwh, dec) if v_kwh is not None else '—'} 度)")

        bar = "=" * 66
        head = (f"  电价 {s['unit_price']} 元/度（元为上游原生值，度为折算值）"
                if has_deg else f"  单价 {price} 元/度")
        print(f"\n{bar}\n  {s['label']}\n{head}\n{bar}")
        print(f"  当前余额      {both(s['balance'], dv.get('balance'))}")
        print(f"  今日用电      {both(s['today'], dv.get('today'))}")
        print(f"  近 7 日均耗   {both(s['avg7'], dv.get('avg7'))}/天"
              + (f"   周同比 {s['wow_pct']:+.1f}%" if s["wow_pct"] is not None else ""))
        print(f"  近 7 日累计   {both(s['week'], dv.get('week'))}")
        print(f"  近 30 日累计  {both(s['month'], dv.get('month'))}")
        if s.get("days_left") is not None:
            print(f"  预计耗尽      {s['empty_date']}（还有 {s['days_left']} 天）")
        elif s.get("forecast", {}).get("reason"):
            print(f"  预计耗尽      {s['forecast']['reason']}")
        rb = s.get("rate_basis") or {}
        if rb:
            print(f"  统计口径      {rb.get('note','')}")
            if rb.get("dates"):
                print(f"                纳入的日子：{'、'.join(rb['dates'])}")
        ex = s.get("excluded") or {}
        if ex.get("stale_list"):
            print(f"                已排除卡住日：{'、'.join(ex['stale_list'])}")
        print(f"  充值建议      {both(s['recharge_suggest'], dv.get('recharge_suggest'), 1)} 可撑 30 天")
        # 分摊预估
        people = int(cfg["app"].get("people", 4) or 4)
        rate = s.get("rate_per_day") or s.get("avg7") or 0
        if rate > 0:
            monthly = rate * 30
            print(f"  分摊预估      {people} 人每人每月 {monthly/people:.0f} {unit}"
                  f"  （{people} 人合计 {monthly:.0f} {unit}/月）")
            if s.get("recharge_suggest", 0) > 1:
                print(f"                下次充 {s['recharge_suggest']:.0f} {unit} "
                      f"时每人 {s['recharge_suggest']/people:.0f} {unit}")
        if has_deg:
            print(f"  消耗速率      {s['rate_per_day'] or '—'} 元/天"
                  f"  ({dv.get('rate_per_day', '—')} 度/天)")

        prof = analyze.hourly_profile(readings, price / (price if native == "cny" else 1))
        top = sorted(prof, key=lambda x: -x["kwh"])[:3]
        print("  高峰时段      " + "  ".join(f"{t['hour']:02d}:00({t['share']}%)" for t in top))
        wk = analyze.weekday_profile(dd)
        print("  周内分布      " + "  ".join(f"{w['label']}{w['avg_kwh']}" for w in wk if w["samples"]))

        an = analyze.detect_anomalies(dd)
        if an:
            print(f"  异常日        {len(an)} 天：", end="")
            print("  ".join(f"{a['date']}({a['kwh']}{unit})" for a in an[-5:]))
        ad = analyze.adjust_events(readings)
        if ad:
            print(f"  数据修正      {len(ad)} 次（抄表/换表，不计入用电）："
                  + "  ".join(f"{a['ts']} 跳变 {a['kwh']}{unit}" for a in ad[-3:]))
        drain = analyze.detect_continuous_drain(readings)
        if drain:
            d = drain[-1]
            print(f"  连续高耗      {d['start']} 起持续 {d['hours']}h，耗 {d['kwh']} {unit}")
        rc = analyze.recharge_events(readings)
        if rc:
            tot = sum(x["kwh"] for x in rc)
            extra = f" ({round(tot / s['unit_price'], 2)} 度)" if has_deg else ""
            print(f"  充值记录      {len(rc)} 次，共 {round(tot,1)} {unit}{extra}")
        print(f"  数据覆盖      {s['coverage']['readings']} 条 / {s['coverage']['days']} 天"
              f"  ({s['coverage']['first']} → {s['coverage']['last']})")

        # 功率换算
        real_price = float(room.get("unit_price") or 0.538) or 0.538
        pdays = [d for d in analyze.available_days(readings) if d != datetime.now().date().isoformat()]
        pday = pdays[0] if pdays else None
        if pday:
            prows = analyze.power_profile(readings, real_price, native, pday)
            ps = analyze.power_summary(prows, real_price, native)
            if ps.get("available"):
                print("\n  功率换算（%s）" % pday)
                print(f"    平均功率      {ps['avg_watts']:.0f} W")
                print(f"    最高功率      {ps['peak_watts']:.0f} W  ({ps['peak_hour']} 点左右"
                      f"{' · ' + ps['peak_estimate'] if ps.get('peak_estimate') else ''})")
                if ps.get("baseline_watts"):
                    m = ps["baseline_watts"] / 1000 * 24 * 30 * real_price
                    print(f"    夜间基础      {ps['baseline_watts']:.0f} W"
                          f"  → 一个月约 {m:.0f} 元（可能有电器常开）")
                print("    档位分布      " + "  ".join(
                    f"{b['level']}{b['hours']}h" for b in ps["bands"]))
                hi = sorted([p for p in prows if p["watts"]], key=lambda x: -x["watts"])[:4]
                print("    最费电的小时  " + "  ".join(
                    f"{p['hour']:02d}:00={p['watts']:.0f}W" for p in hi))

        # 环比 / 同比
        cp = analyze.period_compare(dd, readings)
        rows = [("今天", cp.get("today")), ("最近一周", cp.get("week")),
                ("最近一月", cp.get("month")), ("去年同期", cp.get("yoy"))]
        print("\n  花费对比")
        for name, b in rows:
            if not b:
                continue
            if b.get("available"):
                pct = b.get("pct")
                tail = f"  {pct:+.1f}%" if pct is not None else ""
                lbl = b["name"] if b.get("shortened") else name
                print(f"    {lbl:<8} {b['previous']:>8.2f} → {b['current']:<8.2f}{unit}{tail}")
            else:
                print(f"    {name:<8} {b.get('reason','')}")

        # 分析建议
        ins = analyze.insights(dd, readings, room, bal, native)
        if ins:
            print("\n  分析建议")
            for it in ins:
                print(f"    {it['icon']} {it['title']}")
                print(f"       {it['text']}")
    conn.close()


def cmd_export(argv):
    """导出全部历史读数为 CSV。

    和网页上的导出不同：网页那个受所选时间范围限制，
    这个默认导出**数据库里的全部历史**（本地库是唯一的长历史来源）。
    """
    cfg = load_config()
    conn = db.connect(db_path(cfg))

    out = "power-export.csv"
    since = until = None
    room_key = None
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
        elif a == "--from" and i + 1 < len(argv):
            since = argv[i + 1]
        elif a == "--to" and i + 1 < len(argv):
            until = argv[i + 1] + "T23:59:59"
        elif a == "--room" and i + 1 < len(argv):
            room_key = argv[i + 1]

    rooms = db.list_rooms(conn)
    if not rooms:
        print("还没有房间，先跑 python cli.py login")
        return 1
    targets = [r for r in rooms if not room_key or r["room_key"] == room_key]
    if not targets:
        print(f"没有匹配的房间：{room_key}")
        return 1

    rows: list[list] = []
    for room in targets:
        price = float(room.get("unit_price") or 0.538) or 0.538
        # fetch_readings 只返回 ts 和余额两列（它按房间过滤，不带 room_key / source），
        # 导出要带上来源，所以这里直接查。
        sql = "SELECT ts, balance_cny, balance_kwh, source FROM readings WHERE room_key=?"
        args: list = [room["room_key"]]
        if since:
            sql += " AND ts >= ?"
            args.append(since)
        if until:
            sql += " AND ts <= ?"
            args.append(until)
        sql += " ORDER BY ts ASC"
        for r in conn.execute(sql, args):
            cny = r["balance_cny"]
            deg = (cny / price) if cny is not None else r["balance_kwh"]
            rows.append([
                r["ts"], room["label"], room["room_key"],
                f"{cny:.2f}" if cny is not None else "",
                f"{deg:.3f}" if deg is not None else "",
                r["source"] or "",
            ])

    rows.sort(key=lambda x: (x[0], x[2]))

    with io.open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "房间", "房间标识", "余额(元)", "余额(度)", "来源"])
        w.writerows(rows)

    span = f"{rows[0][0][:16]} → {rows[-1][0][:16]}" if rows else "（无数据）"
    print(f"已导出 {len(rows)} 条到 {out}")
    print(f"  房间   {len(targets)} 个")
    print(f"  范围   {span}")
    if since or until:
        print(f"  筛选   from={since or '不限'}  to={until or '不限'}")
    print()
    print("  说明：上游只保留 7 天历史，本地库是唯一的长历史来源，建议定期备份。")
    print(f"        python cli.py backup   （或已由 serve 每日自动备份到 data/backup/）")
    return 0


def cmd_backup(argv):
    """手动做一次数据库快照。"""
    cfg = load_config()
    bcfg = cfg["app"].get("backup") or {}
    # 项目根 = 数据库文件的祖父目录（data/power.db → 项目根）
    root = db_path(cfg).parent.parent
    d = root / str(bcfg.get("dir") or "data/backup")
    keep = int(bcfg.get("keep", 30) or 30)
    r = db.backup_db(db_path(cfg), d, keep=keep)
    if not r.get("ok"):
        print(f"备份失败：{r.get('error')}")
        return 1
    if r.get("skipped"):
        print(f"今天已经备过了：{r['path']}（{r['size'] // 1024} KB）")
        return 0
    print(f"已生成快照：{r['path']}")
    print(f"  大小   {r['size'] // 1024} KB")
    print(f"  保留   {r.get('kept')} 份")
    if r.get("removed"):
        print(f"  清理   {', '.join(r['removed'])}")
    for f in sorted(d.glob("power-*.db")):
        print(f"    {f.name}  {f.stat().st_size // 1024} KB")
    return 0


def cmd_weather(argv):
    """查看 / 更新气温数据。

    python cli.py weather            看现状
    python cli.py weather fetch      立刻拉一次
    """
    from ysp import weather as W

    cfg = load_config()
    wcfg = cfg["app"].get("weather") or {}
    conn = db.connect(db_path(cfg))

    if argv and argv[0] == "fetch":
        if not wcfg.get("enabled", True):
            print("气温功能已在配置里关闭（app.weather.enabled=false）")
            return 1
        try:
            rows = W.fetch(
                float(wcfg.get("latitude", 30.27)),
                float(wcfg.get("longitude", 120.16)),
                str(wcfg.get("timezone") or "Asia/Shanghai"),
                int(wcfg.get("past_days", 14) or 14),
                int(wcfg.get("forecast_days", 16) or 16),
            )
        except W.WeatherError as e:
            print(f"拉取失败：{e}")
            return 1
        W.save(conn, rows)
        db.set_meta(conn, "weather_fetched_at", datetime.now().isoformat(timespec="seconds"))
        past = sum(1 for r in rows if r["kind"] == "obs")
        print(f"已更新 {len(rows)} 小时（已发生 {past} / 预报 {len(rows) - past}）")

    st = W.stats(conn)
    if not st.get("rows"):
        print("还没有气温数据。运行 python cli.py weather fetch")
        return 0

    print(f"数据源      Open-Meteo（免费，无需 key）")
    print(f"坐标        {wcfg.get('latitude')}, {wcfg.get('longitude')}"
          f"  {wcfg.get('timezone')}")
    print(f"记录条数    {st['rows']} 小时")
    print(f"覆盖范围    {st['a'][:16]} → {st['b'][:16]}")
    print(f"  已发生    {st['past_hours']} 小时（{st['past_days']} 天）")
    print(f"  预报      {st['future_hours']} 小时（{st['future_days']} 天）")
    print(f"温度范围    {st['lo']:.1f} ~ {st['hi']:.1f} °C")
    print(f"缺口        {st['gaps']} 处")
    print(f"空值        {st['nulls']} 条")
    last = db.get_meta(conn, "weather_fetched_at")
    print(f"最近更新    {last or '（还没自动拉过）'}")
    print()
    print("  说明：气温目前只做存储，不参与任何计算。")
    print("        可用样本还太少，现在拟合气温系数会过拟合。")
    return 0


def cmd_daytype(argv):
    """查看 / 更新日类型标签。

    python cli.py daytype            看最近和未来
    python cli.py daytype fetch      立刻刷新标签和节假日缓存
    """
    from ysp import daytype as D

    cfg = load_config()
    dcfg = cfg["app"].get("daytype") or {}
    root = db_path(cfg).parent.parent
    cache = root / str(dcfg.get("cache_dir") or "data/holidays")
    conn = db.connect(db_path(cfg))

    if argv and argv[0] == "fetch":
        today = date.today()
        d0 = today - timedelta(days=int(dcfg.get("lookback_days", 30) or 30))
        d1 = today + timedelta(days=int(dcfg.get("lookahead_days", 120) or 120))
        for y in {d0.year, d1.year}:
            h = D.load_holidays(cache, y)
            print(f"  {y} 年节假日：{len(h)} 条"
                  + ("（还没公布）" if not h else ""))
        rows = D.for_range(d0, d1, cache, str(dcfg.get("term") or "2026-2027-1"))
        D.save(conn, rows)
        print(f"  已更新 {len(rows)} 天标签（{d0} → {d1}）")
        print()

    term = D.TERMS.get(str(dcfg.get("term") or "2026-2027-1")) or {}
    if term:
        sen = term.get("senior") or {}
        print(f"校历        {term.get('school')}  {term.get('name')}")
        print(f"  开学      {sen.get('start')}")
        print(f"  期末考    {sen.get('exam', ('?', '?'))[0]} → {sen.get('exam', ('?', '?'))[1]}"
              "   （除了考试那几个小时，基本全天在宿舍）")
        print(f"  寒假起    {sen.get('vacation_from')}")

    print()
    print("节假日段（2026）:")
    for s in D.holiday_spans(D.load_holidays(cache, 2026, allow_fetch=False)):
        mark = "长假" if s["long"] else "短假"
        print(f"  {mark}  {s['from']} → {s['to']}  {s['days']:>2} 天  {s['name']}")
    for s in D.holiday_spans(D.load_holidays(cache, 2027, allow_fetch=False)):
        mark = "长假" if s["long"] else "短假"
        print(f"  {mark}  {s['from']} → {s['to']}  {s['days']:>2} 天  {s['name']}")

    print()
    print("未来 20 天:")
    rows = D.for_range(date.today() - timedelta(days=3),
                       date.today() + timedelta(days=20), cache)
    for r in rows:
        wd = "一二三四五六日"[r["weekday"]]
        extra = f"  {r['name']}" if r["name"] else ""
        if r["is_makeup"]:
            extra += "  [调休补班]"
        print(f"  {r['date']}  {wd}  {r['label']:<6}{extra}")

    print()
    print("  说明：日历只提供「假设」，判断宿舍在不在人仍然看实测基线。")
    print("        「长假」只表示学生可能离校，不代表宿舍一定没人。")
    return 0


def cmd_model(argv):
    """回测预测误差，显示各特征能否启用。

    python cli.py model           看回测和模型状态
    python cli.py model force     无视门槛强制启用（仅调试用）
    """
    from ysp import model, weather as W

    cfg = load_config()
    conn = db.connect(db_path(cfg))
    rooms = db.list_rooms(conn, enabled_only=True)
    if not rooms:
        print("还没有房间，先跑 python cli.py login")
        return 1
    room = rooms[0]
    r = db.fetch_readings(conn, room["room_key"])
    native = "cny" if (cfg["source"].get("type") == "nofish") else "kwh"
    analyze.native_view(r, native)
    real_price = float(room.get("unit_price") or 0.538) or 0.538
    dd = analyze._fill_missing_eod(analyze.daily(r, 1.0))
    model.attach_kinds(dd, conn)
    wx = W.load(conn)
    use = [d for d in dd if d.get("usable")]

    print(f"房间        {room['label']}")
    print(f"可用日      {len(use)} 天")
    print(f"气温覆盖    {len([d for d in use if d['date'] in model._temp_by_day(r, wx)])} 天")
    print()

    print("=" * 60)
    print("  一、滚动验证（每一步只用历史拟合，不偷看答案）")
    print("=" * 60)
    ev = model.evaluate(dd, r, wx, unit_price=real_price)
    if not ev.get("available"):
        print(f"  {ev.get('reason')}")
        print()
        print("  回测跑不起来通常是：可用日太少，或中途有充值/停更打断。")
    else:
        print(f"  样本        {ev['samples']} 个窗口")
        print(f"  基线 MAE    {ev['base']} 元")
        print(f"  +周几       {ev['weekday']} 元   改善 {ev['weekday_improve']}%")
        print(f"  +气温       {ev['temp']} 元   改善 {ev['temp_improve']}%")
        print(f"  +两者       {ev['both']} 元   改善 {ev['both_improve']}%")
        wot = ev.get("weekday_over_temp_improve")
        if wot is not None:
            print(f"  周几(在气温之上) 再改善 {wot}%")
        hoi = ev.get("heater_over_temp_improve")
        if hoi is not None:
            print(f"  +拆烧水     {ev.get('decomp')} 元   在气温之上再改善 {hoi}%")
        oci = ev.get("occ_over_temp_improve")
        if oci is not None:
            print(f"  +占用锚点   {ev.get('occ')} 元   在气温之上再改善 {oci}%")
        bii = ev.get("bias_over_temp_improve")
        if bii is not None:
            print(f"  +偏差校正   {ev.get('bias')} 元   在气温之上再改善 {bii}%")
        print()
        print(f"  启用门槛    {model.MIN_IMPROVEMENT_PCT:.0f}%")

    print()
    print("=" * 60)
    print("  二、各星期几的规律")
    print("=" * 60)
    wf = model.weekday_factors(dd)
    for row in model.weekday_report(dd, wf):
        avg = f"{row['avg']:.2f} 元" if row["avg"] else "—"
        fac = f"{row['factor']:.3f}" if row["factor"] else "—"
        need = max(0, model.MIN_SAMPLES_PER_WEEKDAY - row["samples"])
        flag = f"  （还差 {need} 次）" if need else ""
        print(f"  {row['label']}   出现 {row['samples']} 次   均值 {avg:>9}   系数 {fac}{flag}")

    print()
    print("=" * 60)
    print("  三、结论")
    print("=" * 60)
    force = bool(argv and argv[0] == "force")
    dec = model.decide(dd, r, wx, force=force, unit_price=real_price)
    fit = dec.get("fit") or {}
    if fit.get("active"):
        if fit.get("feature") == "deg":
            print("  天气模型   度小时（用逐小时气温积分，反映下午峰值）")
            print(f"    冷却基准  {fit['tc']:.1f}°C（空调大概从这启动，各房间独立）")
            print(f"    基载      {fit['base']:.2f} 元/天（不开空调的地板）")
            print(f"    CDH 系数  {fit['c_cool']:.4f} 元/度·小时")
            print(f"    最高温系数 {fit['c_max']:.3f} 元/°C")
            print(f"    HDH 系数  {fit['c_heat']:.4f} 元/度·小时（冷天热水器）")
            print(f"    拟合      r²={fit['r2']:.2f}  RMSE={fit.get('rmse')}")
        elif fit.get("form") == "hinge":
            print("  天气模型   折线（铰链，度小时不够好时的退路）")
            print(f"    拐点      {fit['knee']:.1f}°C   基载 {fit['base']:.2f} 元/天")
            print(f"    热段      +{fit['hot']:.3f} 元/°C   冷段 {fit['cold']:+.3f} 元/°C")
            print(f"    拟合      r²={fit['r2']:.2f}  RMSE={fit.get('rmse')}")
        else:
            print("  天气模型   单一直线（数据还不够定拐点）")
            print(f"    斜率      +{fit['hot']:.3f} 元/°C    r²={fit['r2']:.2f}")
    else:
        print(f"  天气模型   未拟合：{fit.get('note') or fit.get('reason')}")
    # 钟点画像：去掉空调项后的"几点钟在干什么"
    prof = fit.get("profile") if fit.get("active") else None
    if prof:
        print()
        print("  钟点画像（已扣除空调，每小时平均用电）")
        mx = max(prof) or 1.0
        for h in range(24):
            bar = "█" * int(round(prof[h] / mx * 30))
            print(f"    {h:02d}点 {prof[h]:6.3f} {bar}")
    print(f"  天气修正    {dec['temp']['why']}")
    print(f"  周几修正    {dec['weekday']['why']}")
    heat = dec.get("heater") or {}
    print(f"  热水器分解  {heat.get('why')}")
    if heat.get("active"):
        print(f"              烧水 {heat.get('events_per_day')} 次/天 × 单次 "
              f"{heat.get('energy_per_event')} 元 ≈ {heat.get('rate')} 元/天"
              f"（泊松方差 {heat.get('var')}）")
    occ = dec.get("occ") or {}
    print(f"  占用锚点    {occ.get('why')}")
    bias = dec.get("bias") or {}
    print(f"  偏差校正    {bias.get('why')}")
    print()
    print("  说明：模型自己判断 —— 数据够 + 回测证明更准 才启用。")
    print("        实测纯噪声数据下启用率 6.7%，强规律 100%，不会乱开。")
    return 0


def cmd_serve(argv):
    from ysp.server import main as serve_main

    serve_main()


def cmd_selftest(argv):
    """用合成数据跑一遍模型的关键行为，锁住回归。

    覆盖：纯噪声不误启用、强气温信号要启用、同均温不同峰值要能分开、
    空置/卡住时段不进钟点画像、forecast 输出健全。
    """
    import math
    import random
    from datetime import date, timedelta
    from ysp import model as M
    from ysp import analyze as A

    ok = True

    def synth(gen, seed=7, span=42, flat=None):
        """gen(temp, rnd) -> 该小时用量；flat 是"没人住"的日期集合（用量≈0）。"""
        random.seed(seed)
        b = date(2026, 4, 1)
        days, wx, rd = [], [], []
        bal = 400.0
        for i in range(span):
            ds = (b + timedelta(days=i)).isoformat()
            mean = random.uniform(16, 30)
            amp = random.uniform(1, 6)
            temps = [mean + amp * math.sin((h - 9) / 24 * 2 * math.pi) for h in range(24)]
            dayk = 0.0
            for h in range(24):
                u = 0.01 if (flat and ds in flat) else gen(temps[h], random)
                dayk += u
                bal -= u
                ts = f"{ds}T{h:02d}:00:00"
                rd.append({"ts": ts, "balance_kwh": round(bal, 4)})
                wx.append({"ts": ts, "temp": round(temps[h], 2), "kind": "obs"})
            days.append({"date": ds, "kwh": round(dayk, 4), "usable": True})
        return days, wx, rd

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))

    deve = lambda t, rnd: 1.2 + 0.03 * max(0.0, t - 22.0) + rnd.uniform(-0.1, 0.1)

    # 1) 纯噪声：不该启用气温
    d, w, r = synth(lambda t, rnd: rnd.uniform(2, 6))
    check("纯噪声不启用气温", not M.decide(d, r, w)["temp"]["active"])

    # 2) 强冷却度小时信号：应启用，且用度小时模型
    d, w, r = synth(deve)
    dec = M.decide(d, r, w)
    check("强气温信号启用且用度小时", dec["temp"]["active"] and dec["fit"].get("feature") == "deg")

    # 3) 同均温不同峰值：模型要能建出来（空调看峰值）
    random.seed(3)
    b = date(2026, 6, 1)
    d3, w3, r3 = [], [], []
    bal = 400.0
    for i in range(42):
        ds = (b + timedelta(days=i)).isoformat()
        mean = 20.0 + 3.0 * math.sin(i / 5)
        amp = random.uniform(1, 7)
        temps = [mean + amp * math.sin((h - 9) / 24 * 2 * math.pi) for h in range(24)]
        cdh = sum(max(0.0, t - 22) for t in temps)
        dayk = 0.0
        for h in range(24):
            u = max(0.01, (1.2 + 0.02 * cdh) / 24)
            dayk += u
            bal -= u
            ts = f"{ds}T{h:02d}:00:00"
            r3.append({"ts": ts, "balance_kwh": round(bal, 4)})
            w3.append({"ts": ts, "temp": round(temps[h], 2), "kind": "obs"})
        d3.append({"date": ds, "kwh": round(dayk, 4), "usable": True})
    dec3 = M.decide(d3, r3, w3)
    check("同均温不同峰值能建模", dec3["temp"]["active"] and dec3["fit"].get("feature") == "deg")

    # 4) good_dates 过滤：样本只含指定日期
    d, w, r = synth(deve)
    tk = {x["ts"]: x["temp"] for x in w}
    good = {x["date"] for x in d[:10]}
    hs = M._hourly_samples(r, tk, good_dates=good)
    check("good_dates 只保留指定日期", bool(hs) and all(s[0] in good for s in hs))

    # 5) 空置周要被标出、且不进画像
    vac = {(date(2026, 4, 1) + timedelta(days=i)).isoformat() for i in range(12, 18)}
    d, w, r = synth(lambda t, rnd: 2.0 + 0.04 * max(0.0, t - 22.0), flat=vac)
    gd = M._good_dates(d) or set()
    check("空置周被剔除出可用日", not (vac & gd))

    # 6) forecast 输出健全（有限、非负）
    d, w, r = synth(deve)
    f6 = M.decide(d, r, w)["fit"]
    hourly = M._hourly_by_day(w)
    tc = float(f6.get("tc", 22.0))
    feast = {}
    for dd_ in hourly:
        ff = M.day_features(hourly, dd_, tc)
        if ff:
            feast[dd_] = ff
    wm6 = {k: f6[k] for k in ("feature", "base", "c_cool", "c_max", "c_heat", "tc", "th")
           if k in f6}
    fc = A.forecast(d, 80.0, readings=[], unit_price=1.0,
                    weather_model=wm6, weather_feat=feast)
    proj = fc.get("projection") or []
    sane = fc.get("available") and proj and all(
        p["kwh"] == p["kwh"] and 0.0 <= p["kwh"] < 1e6 for p in proj)
    check("forecast 速率有限且非负", sane)

    # 7) 热水器检测：~2000W 的持续段应被记为一次事件（元制读数）
    price = 0.538
    hr = []
    bal = 300.0
    for day_i in range(3):
        ds = (date(2026, 7, 1) + timedelta(days=day_i)).isoformat()
        for h in range(24):
            u = 0.05                       # 基载 ≈93W
            if h in (20, 21):              # 20~22 点烧水 2 小时，≈2137W
                u = 1.15
            bal -= u
            hr.append({"ts": f"{ds}T{h:02d}:00:00", "balance_kwh": round(bal, 4)})
    pd, runs = M._heater_per_day(hr, price)
    evs = [pd[d]["count"] for d in pd]
    check("热水器检测到 3 次 ~2000W 事件",
          len(runs) == 3 and all(c == 1 for c in evs))

    # 8) 期望率 / 单次能量估计合理
    days7 = [{"date": (date(2026, 7, 1) + timedelta(days=i)).isoformat(),
              "kwh": 4.0, "usable": True} for i in range(3)]
    st = M._heater_stats(pd, runs, days7)
    check("热水器期望率/单次能量合理",
          st.get("active") and abs(st["lambda"] - 1.0) < 0.5
          and 2.0 < st["E"] < 2.6 and st["rate"] > 0 and st["var"] > 0)

    # 9) forecast 叠加热水器：used_heater=True，速率=余项+期望率，有限非负
    d, w, r = synth(deve)
    f9 = M.decide(d, r, w)["fit"]
    hourly = M._hourly_by_day(w)
    tc9 = float(f9.get("tc", 22.0))
    feast9 = {dd_: M.day_features(hourly, dd_, tc9) for dd_ in hourly}
    feast9 = {k: v for k, v in feast9.items() if v}
    wm9 = {k: f9[k] for k in ("feature", "base", "c_cool", "c_max", "c_heat", "tc", "th")
           if k in f9}
    hday9 = {dd_: 1.2 for dd_ in [x["date"] for x in d]}
    fc9 = A.forecast(d, 80.0, readings=[], unit_price=1.0,
                     weather_model=wm9, weather_feat=feast9,
                     heater_rate=1.2, heater_var=1.44, heater_day=hday9)
    p9 = fc9.get("projection") or []
    check("forecast 叠加热水器：used_heater 且速率有限非负",
          fc9.get("available") and fc9.get("used_heater") and p9 and all(
              p["kwh"] == p["kwh"] and 0.0 <= p["kwh"] < 1e6 for p in p9))

    # 10) 占用锚点：长假低用量不该拖累返校后的预测（occ 门槛要先赢过气温）
    occ_gen = lambda t, rnd: 0.5 + 0.25 * max(0.0, t - 20.0) + rnd.uniform(-0.05, 0.05)
    d10, w10, _ = synth(occ_gen, seed=21, span=40)
    brk10 = {d10[i]["date"] for i in range(16, 24)}
    for x in d10:
        x["kind"] = "long_break" if x["date"] in brk10 else "class"
        if x["date"] in brk10:
            x["kwh"] = 8.0
    r10 = []
    bal10 = 500.0
    for x in d10:
        for h in range(24):
            bal10 -= x["kwh"] / 24.0
            r10.append({"ts": f"{x['date']}T{h:02d}:00:00",
                        "balance_kwh": round(bal10, 4)})
    ev10 = M.evaluate(d10, r10, w10)
    dec10 = M.decide(d10, r10, w10)
    oi10 = ev10.get("occ_over_temp_improve")
    check("长假低用量不拖累返校预测（占用锚点启用）",
          bool(ev10.get("available")) and oi10 is not None
          and oi10 >= M.MIN_IMPROVEMENT_PCT
          and dec10["occ"]["active"] and dec10["temp"]["active"])

    # 11) forecast 占用锚点：近 7 天长假被剔出锚点（用户确认的长假离校先验）
    d11, w11 = [], []
    b11 = date(2026, 9, 1)
    for i in range(29):
        ds = (b11 + timedelta(days=i)).isoformat()
        temps = [24.0 + 3.0 * math.sin((h - 9) / 24 * 2 * math.pi) for h in range(24)]
        k = 0.5 if i >= 22 else 5.0
        for h in range(24):
            w11.append({"ts": f"{ds}T{h:02d}:00:00",
                        "temp": round(temps[h], 2), "kind": "obs"})
        d11.append({"date": ds, "kwh": k, "usable": True,
                    "kind": ("long_break" if i >= 22 else "class")})
    hourly11 = M._hourly_by_day(w11)
    feast11 = {x: M.day_features(hourly11, x, 22.0) for x in hourly11}
    feast11 = {k2: v for k2, v in feast11.items() if v}
    wm11 = {"feature": "deg", "base": 5.0, "c_cool": 0.0, "c_max": 0.0,
            "c_heat": 0.0, "tc": 22.0, "th": 18.0}
    fc11a = A.forecast(d11, 100.0, readings=[], unit_price=1.0,
                       weather_model=wm11, weather_feat=feast11)
    fc11b = A.forecast(d11, 100.0, readings=[], unit_price=1.0,
                       weather_model=wm11, weather_feat=feast11,
                       occ_kinds=["long_break", "vacation"],
                       day_kinds={x["date"]: x["kind"] for x in d11})
    check("forecast 占用锚点剔除长假",
          fc11a.get("available") and fc11b.get("available")
          and not fc11a.get("used_occ") and fc11b.get("used_occ")
          and (fc11a.get("rate_kwh_per_day") or 0) < 2.0
          and (fc11b.get("rate_kwh_per_day") or 0) > 4.0)

    # 12) 偏差校正：近期水平整体漂移（如降温）应被滚动残差拉回
    d12, w12, _ = synth(deve, seed=23, span=36)
    for x in d12[-6:]:
        x["kwh"] = x["kwh"] * 0.6
    r12 = []
    bal12 = 500.0
    for x in d12:
        for h in range(24):
            bal12 -= x["kwh"] / 24.0
            r12.append({"ts": f"{x['date']}T{h:02d}:00:00",
                        "balance_kwh": round(bal12, 4)})
    ev12 = M.evaluate(d12, r12, w12)
    dec12 = M.decide(d12, r12, w12)
    bi12 = ev12.get("bias_over_temp_improve")
    check("近期漂移被偏差校正拉回（偏差校正启用）",
          bool(ev12.get("available")) and bi12 is not None
          and bi12 >= M.MIN_IMPROVEMENT_PCT
          and dec12["bias"]["active"] and dec12["temp"]["active"])

    # 13) forecast 偏差校正：used_bias 且速率被拉回，有限非负
    b11 = date(2026, 9, 1)
    d13, w13 = [], []
    for i in range(30):
        ds = (b11 + timedelta(days=i)).isoformat()
        temps = [24.0 + 3.0 * math.sin((h - 9) / 24 * 2 * math.pi) for h in range(24)]
        k = 3.0 if i >= 24 else 5.0        # 后 6 天用量掉到 3
        for h in range(24):
            w13.append({"ts": f"{ds}T{h:02d}:00:00",
                        "temp": round(temps[h], 2), "kind": "obs"})
        d13.append({"date": ds, "kwh": k, "usable": True})
    hourly13 = M._hourly_by_day(w13)
    feast13 = {x: M.day_features(hourly13, x, 22.0) for x in hourly13}
    feast13 = {k2: v for k2, v in feast13.items() if v}
    wm13 = {"feature": "deg", "base": 5.0, "c_cool": 0.0, "c_max": 0.0,
            "c_heat": 0.0, "tc": 22.0, "th": 18.0}
    fc13a = A.forecast(d13, 100.0, readings=[], unit_price=1.0,
                       weather_model=wm13, weather_feat=feast13)
    fc13b = A.forecast(d13, 100.0, readings=[], unit_price=1.0,
                       weather_model=wm13, weather_feat=feast13,
                       bias_on=True, bias_k=3, bias_shrink=4.0)
    check("forecast 偏差校正生效",
          fc13a.get("available") and fc13b.get("available")
          and fc13b.get("used_bias")
          and (fc13b.get("rate_kwh_per_day") or 0) < (fc13a.get("rate_kwh_per_day") or 0)
          and (fc13b.get("rate_kwh_per_day") or 0) > 0)

    print()
    print("  结果:", "全部通过" if ok else "有失败")
    return 0 if ok else 1


COMMANDS = {
    "init": cmd_init,
    "login": cmd_login,
    "seed-demo": cmd_seed,
    "check": cmd_check,
    "collect": cmd_collect,
    "loop": cmd_loop,
    "report": cmd_report,
    "quality": cmd_quality,
    "test-email": cmd_test_email,
    "test-alert": cmd_test_alert,
    "serve": cmd_serve,
    "export": cmd_export,
    "backup": cmd_backup,
    "weather": cmd_weather,
    "daytype": cmd_daytype,
    "model": cmd_model,
    "selftest": cmd_selftest,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    fn = COMMANDS.get(sys.argv[1])
    if not fn:
        print(f"未知命令：{sys.argv[1]}\n")
        print(__doc__)
        return 2
    return fn(sys.argv[2:]) or 0


if __name__ == "__main__":
    sys.exit(main())
