"""工作日历服务测试：验证真实时间 <-> 工作分钟的双向映射与区间拆段"""
import sys, os
from datetime import datetime, date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.calendar_service import WorkCalendar, build_calendars, SHIFT_START


def rows(start, days, hours, skip_weekend=True):
    """构造产能日历行，周末为 0"""
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        h = 0 if (skip_weekend and d.weekday() >= 5) else hours
        out.append({"center_id": "WC-001", "work_date": d.isoformat(),
                    "available_hours": h, "planned_hours": 0})
    return out


def test_skip_nonwork():
    """非工作时间不应被排入"""
    print("测试1: 跳过周末与夜间...")
    base = date(2026, 8, 10)  # 周一
    cal = WorkCalendar.from_calendar_rows("WC-001", rows(base, 14, 15.2),
                                         now=datetime(2026, 8, 10, 0, 0))
    # 14 天里 10 个工作日
    assert len(cal.slots) == 10, cal.slots
    assert cal.total_minutes == 10 * 912, cal.total_minutes
    # 周六周日不在 slots 中
    for s in cal.slots:
        assert s.work_date.weekday() < 5, s
    print(f"   OK slots={len(cal.slots)} total={cal.total_minutes/60:.1f}h")


def test_roundtrip():
    """to_datetime 与 to_work_minutes 应互逆"""
    print("测试2: 双向映射互逆...")
    base = date(2026, 8, 10)
    cal = WorkCalendar.from_calendar_rows("WC-001", rows(base, 14, 15.2),
                                         now=datetime(2026, 8, 10, 0, 0))
    for wm in [0, 1, 500, 911, 912, 913, 1824, 4000, 9119]:
        dt = cal.to_datetime(wm)
        back = cal.to_work_minutes(dt)
        assert back == wm, f"wm={wm} dt={dt} back={back}"
    print("   OK 9 个采样点全部互逆")


def test_split_crossday():
    """跨天任务应被拆成多段，且每段都落在班次内"""
    print("测试3: 跨天任务拆段...")
    base = date(2026, 8, 10)
    cal = WorkCalendar.from_calendar_rows("WC-001", rows(base, 14, 15.2),
                                         now=datetime(2026, 8, 10, 0, 0))
    # 100 小时 = 6000 分钟，需跨约 6.6 个工作日
    segs = cal.split(0, 6000)
    assert len(segs) == 7, f"期望7段, 实际{len(segs)}: {segs}"
    total = sum((b - a).total_seconds() / 60 for a, b in segs)
    assert abs(total - 6000) < 1, total
    # 每段必须在 08:00 之后、且不跨越当天班次结束
    for a, b in segs:
        assert a.time() >= SHIFT_START, a
        assert a.date() == b.date(), (a, b)
        assert a.weekday() < 5, a
    # 段与段之间必须有间隔（夜间/周末）
    for i in range(len(segs) - 1):
        assert segs[i][1] < segs[i + 1][0], segs
    print(f"   OK 6000工作分钟 -> {len(segs)}段, 首段{segs[0][0]:%m-%d %H:%M}, 末段{segs[-1][1]:%m-%d %H:%M}")


def test_today_partial():
    """当天已过去的班次时间不可用"""
    print("测试4: 当天剩余产能...")
    base = date(2026, 8, 10)
    # 当前 14:00，08:00 开工 -> 已消耗 6h，当天剩 9.2h
    cal = WorkCalendar.from_calendar_rows("WC-001", rows(base, 3, 15.2),
                                         now=datetime(2026, 8, 10, 14, 0))
    first = cal.slots[0]
    assert first.start_dt == datetime(2026, 8, 10, 14, 0), first.start_dt
    assert first.minutes == 912 - 360, first.minutes
    print(f"   OK 当天剩余 {first.minutes/60:.1f}h, 起点 {first.start_dt:%H:%M}")


def test_deadline_mapping():
    """交期换算：周末交期应映射到周五班次结束"""
    print("测试5: 交期映射...")
    base = date(2026, 8, 10)
    cal = WorkCalendar.from_calendar_rows("WC-001", rows(base, 14, 15.2),
                                         now=datetime(2026, 8, 10, 0, 0))
    # 8/15 周六 18:00 交期 -> 应等于周一至周五 5 天的总工时
    wm = cal.to_work_minutes(datetime(2026, 8, 15, 18, 0))
    assert wm == 5 * 912, wm
    assert cal.capacity_before(datetime(2026, 8, 15, 18, 0)) == 76.0
    print(f"   OK 周六交期 -> {wm}工作分钟 ({wm/60:.1f}h)")


def test_maintenance_center_excluded():
    """全零产能的工作中心不应产生日历"""
    print("测试6: 停机产线排除...")
    wcs = [{"center_id": "WC-001", "capacity_per_day": 16, "status": "active"},
           {"center_id": "WC-003", "capacity_per_day": 8, "status": "maintenance"}]
    cal_rows = rows(date(2026, 8, 10), 5, 15.2)
    cal_rows += [{"center_id": "WC-003", "work_date": (date(2026, 8, 10) + timedelta(days=i)).isoformat(),
                  "available_hours": 0, "planned_hours": 0} for i in range(5)]
    cals = build_calendars(wcs, cal_rows, now=datetime(2026, 8, 10, 0, 0))
    assert "WC-001" in cals
    assert "WC-003" not in cals, "停机产线不应有可用日历"
    print("   OK WC-003 已排除")


if __name__ == "__main__":
    tests = [test_skip_nonwork, test_roundtrip, test_split_crossday,
             test_today_partial, test_deadline_mapping, test_maintenance_center_excluded]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"   FAIL {t.__name__}: {e}")
        except Exception as e:
            print(f"   ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n结果: {passed}/{len(tests)} 通过")
    sys.exit(0 if passed == len(tests) else 1)