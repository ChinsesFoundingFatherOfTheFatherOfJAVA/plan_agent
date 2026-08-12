"""
工作日历服务（Work Calendar）

解决的核心问题：
    OR-Tools 的时间轴是连续的自然时间，而工厂只在班次内生产。
    若直接把「200 件 x 0.5h = 100 小时」当成一段连续区间，任务会跨越夜间与周末，
    结果不可执行。

本模块的做法：
    为每个工作中心用 capacity_calendar 构建一条「工作分钟轴」（work-minute axis）：
        自然时间   08:00-23:12(15.2h)  |  夜间/周末(0h)  |  次日 08:00-23:12
        工作分钟轴 0 ------------ 912  |     (不存在)     |  912 ---------- 1824
    求解器只在工作分钟轴上排任务，天然不会排到非工作时间；
    求解完成后再用 split() 把区间还原为若干段真实时间（跨天任务自动拆段）。

设计取舍（TODO）：
    - 班次假设为「每天从 SHIFT_START 开始连续生产 available_hours 小时」，
      未考虑多班制、午休、跨零点夜班。真实场景应引入 shift 表（班次定义），
      届时只需改造 _build_slots，上层无感。
    - 未考虑设备换型时间（setup time），需工艺路线数据支撑。
"""
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 班次默认起始时间：每天 08:00 开工
# TODO: 缺少班次主数据时的默认假设，接入 shift 表后替换
SHIFT_START = time(8, 0)


class WorkSlot:
    """一个连续可生产时段（通常对应产能日历的一天）"""

    __slots__ = ("work_date", "start_dt", "minutes", "offset")

    def __init__(self, work_date: date, start_dt: datetime, minutes: int, offset: int):
        self.work_date = work_date      # 所属日期
        self.start_dt = start_dt        # 真实开始时间
        self.minutes = minutes          # 时长（分钟）
        self.offset = offset            # 该时段起点在工作分钟轴上的坐标

    @property
    def end_dt(self) -> datetime:
        return self.start_dt + timedelta(minutes=self.minutes)

    @property
    def end_offset(self) -> int:
        return self.offset + self.minutes

    def __repr__(self) -> str:
        return f"<WorkSlot {self.work_date} {self.start_dt:%H:%M}+{self.minutes}m @{self.offset}>"


class WorkCalendar:
    """
    单个工作中心的工作日历，提供「真实时间 <-> 工作分钟」双向映射。

    典型用法：
        cal = WorkCalendar("WC-001", slots)
        deadline = cal.to_work_minutes(due_datetime)       # 交期换算为轴坐标
        segments = cal.split(start_wm, end_wm)             # 求解结果还原为真实时间段
    """

    def __init__(self, center_id: str, slots: List[WorkSlot]):
        self.center_id = center_id
        self.slots = slots
        self.total_minutes = slots[-1].end_offset if slots else 0

    # ---------- 构建 ----------

    @classmethod
    def from_calendar_rows(
        cls,
        center_id: str,
        rows: List[Dict[str, Any]],
        now: Optional[datetime] = None,
        use_remaining: bool = False,
    ) -> "WorkCalendar":
        """
        由 capacity_calendar 行构建日历。

        Args:
            center_id: 工作中心
            rows: [{work_date, available_hours, planned_hours, ...}]
            now: 当前时间，早于 now 的产能不可用（今天只取剩余部分）
            use_remaining: True 则用 available_hours - planned_hours（扣除已排产），
                           False 则用全量 available_hours（已排任务另以固定区间参与 NoOverlap）
        """
        now = now or datetime.now()
        slots: List[WorkSlot] = []
        offset = 0

        for row in sorted(rows, key=lambda r: str(r.get("work_date"))):
            d = _as_date(row.get("work_date"))
            if d is None or d < now.date():
                continue

            hours = float(row.get("available_hours") or 0)
            if use_remaining:
                hours -= float(row.get("planned_hours") or 0)
            if hours <= 0:
                continue

            start_dt = datetime.combine(d, SHIFT_START)
            minutes = int(round(hours * 60))

            # 今天：已经过去的班次时间不可再用
            if d == now.date() and now > start_dt:
                consumed = int((now - start_dt).total_seconds() // 60)
                minutes -= consumed
                start_dt = now.replace(second=0, microsecond=0)
                if minutes <= 0:
                    continue

            slots.append(WorkSlot(d, start_dt, minutes, offset))
            offset += minutes

        return cls(center_id, slots)

    @classmethod
    def continuous(
        cls,
        center_id: str,
        hours_per_day: float,
        days: int = 30,
        start_date: Optional[date] = None,
    ) -> "WorkCalendar":
        """
        构建「连续 N 天、每天固定工时」的兜底日历。

        用途：单元测试或产能日历缺失时的降级方案。
        TODO: 生产环境不应走此分支，缺日历应视为数据异常并告警。
        """
        start_date = start_date or datetime.now().date()
        rows = [
            {"work_date": start_date + timedelta(days=i), "available_hours": hours_per_day}
            for i in range(days)
        ]
        return cls.from_calendar_rows(center_id, rows)

    # ---------- 映射 ----------

    def to_work_minutes(self, dt: datetime, mode: str = "floor") -> int:
        """
        真实时间 -> 工作分钟坐标。

        mode='floor'：落在非工作时段时取该时段之前的坐标（用于截止时间，偏保守）
        mode='ceil' ：落在非工作时段时取下一个时段起点（用于最早开工时间）
        """
        if not self.slots:
            return 0
        if dt <= self.slots[0].start_dt:
            return 0
        if dt >= self.slots[-1].end_dt:
            return self.total_minutes

        for slot in self.slots:
            if dt < slot.start_dt:
                # 落在两个时段之间的空隙
                return slot.offset if mode == "ceil" else slot.offset
            if dt < slot.end_dt:
                return slot.offset + int((dt - slot.start_dt).total_seconds() // 60)
        return self.total_minutes

    def to_datetime(self, work_minute: int) -> datetime:
        """工作分钟坐标 -> 真实时间"""
        if not self.slots:
            raise ValueError(f"工作中心 {self.center_id} 无可用产能日历")
        if work_minute <= 0:
            return self.slots[0].start_dt
        if work_minute >= self.total_minutes:
            return self.slots[-1].end_dt

        for slot in self.slots:
            if work_minute < slot.end_offset:
                return slot.start_dt + timedelta(minutes=work_minute - slot.offset)
        return self.slots[-1].end_dt

    def split(self, start_wm: int, end_wm: int) -> List[Tuple[datetime, datetime]]:
        """
        把工作分钟区间 [start_wm, end_wm) 还原为真实时间段列表。

        跨天任务会被拆成多段；相邻且首尾相接的段自动合并。

        Returns:
            [(段开始时间, 段结束时间), ...]
        """
        if end_wm <= start_wm or not self.slots:
            return []

        segments: List[Tuple[datetime, datetime]] = []
        for slot in self.slots:
            if slot.end_offset <= start_wm:
                continue
            if slot.offset >= end_wm:
                break
            a = max(start_wm, slot.offset)
            b = min(end_wm, slot.end_offset)
            seg_start = slot.start_dt + timedelta(minutes=a - slot.offset)
            seg_end = slot.start_dt + timedelta(minutes=b - slot.offset)

            # 与上一段首尾相接则合并（例如产能日历把一天拆成了多行）
            if segments and segments[-1][1] == seg_start:
                segments[-1] = (segments[-1][0], seg_end)
            else:
                segments.append((seg_start, seg_end))

        return segments

    def capacity_before(self, deadline: datetime) -> float:
        """deadline 之前的累计可用工时（小时），供交付风险分析使用"""
        return round(self.to_work_minutes(deadline) / 60.0, 2)

    def __repr__(self) -> str:
        return (f"<WorkCalendar {self.center_id} slots={len(self.slots)} "
                f"total={self.total_minutes / 60:.1f}h>")


def _as_date(value: Any) -> Optional[date]:
    """把 ISO 字符串 / datetime / date 统一为 date"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value).date()
    return None


def build_calendars(
    work_centers: List[Dict[str, Any]],
    capacity_calendar: List[Dict[str, Any]],
    now: Optional[datetime] = None,
    fallback_days: int = 30,
) -> Dict[str, WorkCalendar]:
    """
    为所有工作中心批量构建日历。

    Args:
        work_centers: [{center_id, capacity_per_day, status, ...}]
        capacity_calendar: [{center_id, work_date, available_hours, planned_hours}]
        now: 当前时间
        fallback_days: 无日历数据时兜底生成的天数

    Returns:
        {center_id: WorkCalendar}，无任何可用产能的中心不会出现在结果中
    """
    now = now or datetime.now()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in capacity_calendar or []:
        grouped.setdefault(row["center_id"], []).append(row)

    calendars: Dict[str, WorkCalendar] = {}
    for wc in work_centers or []:
        cid = wc["center_id"]
        rows = grouped.get(cid)
        if rows:
            cal = WorkCalendar.from_calendar_rows(cid, rows, now=now)
        else:
            # 没有日历数据，用日产能兜底
            cal = WorkCalendar.continuous(
                cid, float(wc.get("capacity_per_day") or 8.0),
                days=fallback_days,
                start_date=now.date() if hasattr(now, 'date') else now,
            )
        if cal.slots:
            calendars[cid] = cal
    return calendars