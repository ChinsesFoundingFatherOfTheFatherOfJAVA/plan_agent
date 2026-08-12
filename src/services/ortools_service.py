"""
智能排产计划Agent - OR-Tools CP-SAT求解器（日历感知版）

与初版的关键差异（重要设计变更，已同步 plan.md 附录C）：

1. 时间轴改为「工作分钟轴」
   初版把任务当成自然时间上的连续区间，导致 100 小时的任务跨越夜间和周末，
   排出的计划不可执行。现在每个工作中心依据 capacity_calendar 建立工作分钟轴，
   求解只在班次内进行，跨天任务在还原阶段自动拆段（task["segments"]）。

2. 交期从硬约束改为软约束（惩罚项）
   初版交期违反直接导致 INFEASIBLE，无法告知「哪个订单排不下」。
   现在超期体现为 tardiness 惩罚，validate 节点据此产出可解释的冲突信息。

3. 引入「未排产」出口
   当总需求超过窗口内总产能时，允许任务不排产并施加极高惩罚，
   求解器因此总能返回可行解，并明确指出哪些任务放不下（result.unscheduled_tasks）。

4. 每个（任务, 工作中心）使用可选区间（OptionalInterval）
   配合 AddNoOverlap 实现「同一中心同一时刻仅一个任务」，
   比初版两两布尔配对的写法更简洁且求解效率更高。

约束条件：
    交期约束（软）、产能约束（由工作分钟轴隐式保证）、
    资源独占约束（NoOverlap）、顺序约束（前置任务）、已排产任务占用（固定区间）

优化目标：
    最小化 未排产惩罚 >> 加权超期 >> 完工时间 / 负荷不均衡
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import logging

from ortools.sat.python import cp_model

from src.services.calendar_service import WorkCalendar, build_calendars

logger = logging.getLogger(__name__)

# 目标函数权重量级：未排产惩罚必须压倒一切其他项
PENALTY_UNSCHEDULED = 10 ** 7
PENALTY_TARDINESS_BASE = 100


class SolverStrategy(str, Enum):
    """求解策略"""
    PRIORITY_FIRST = "priority_first"      # 优先级优先
    SHORTEST_JOB = "shortest_job"          # 最短作业优先
    LOAD_BALANCE = "load_balance"          # 负荷均衡
    CUSTOM = "custom"                      # 自定义权重


@dataclass
class TaskInput:
    """任务输入数据"""
    task_id: str
    order_id: str
    item_id: str
    product_id: str
    quantity: float
    duration_hours: float                       # 标准工时（小时，兜底用）
    per_center_hours: Optional[Dict[str, float]] = None  # 按工作中心区分的工时 {center_id: hours}
    setup_time_minutes: int = 0                # 固定换型时间（分钟，兜底用）
    per_center_setup: Optional[Dict[str, int]] = None   # 按工作中心区分的换型时间 {center_id: minutes}
    due_date: Optional[datetime] = None         # 交期
    priority: int = 2                           # 优先级（1紧急/2正常/3低）
    predecessors: List[str] = field(default_factory=list)
    allowed_centers: List[str] = field(default_factory=list)


@dataclass
class WorkCenterInput:
    """
    工作中心输入数据

    注意：calendar 中的 available_hours 已包含效率系数（如 16h x 0.95 = 15.2h），
    因此 efficiency_rate 不再二次折算工时，仅作为展示与策略参考。
    """
    center_id: str
    center_name: str
    capacity_per_day: float                     # 日产能（小时）
    efficiency_rate: float = 1.0
    operating_hours_per_day: float = 8.0
    calendar: Optional[WorkCalendar] = None     # 工作日历，缺失时按日产能兜底生成


@dataclass
class SolverResult:
    """求解结果"""
    success: bool
    status: str
    tasks: List[Dict[str, Any]]                       # 已排产任务（含 segments）
    unscheduled_tasks: List[Dict[str, Any]] = field(default_factory=list)
    objective_value: Optional[float] = None
    solve_time_seconds: float = 0.0
    statistics: Dict[str, Any] = field(default_factory=dict)


class ORToolsScheduler:
    """
    OR-Tools CP-SAT 排产求解器（日历感知）

    用法：
        scheduler = ORToolsScheduler(strategy=SolverStrategy.PRIORITY_FIRST)
        result = scheduler.solve(task_inputs, wc_inputs)
    """

    def __init__(
        self,
        strategy: SolverStrategy = SolverStrategy.PRIORITY_FIRST,
        max_time_seconds: int = 30,
        num_workers: int = 4,
        strategy_params: Dict[str, float] = None,
        fixed_intervals: Dict[str, List[Tuple[datetime, datetime, str]]] = None,
    ):
        """
        Args:
            strategy: 求解策略
            max_time_seconds: 最大求解时间（秒）
            num_workers: 并行线程数
            strategy_params: 策略权重参数
            fixed_intervals: 已排产任务占用 {center_id: [(start, end, task_id), ...]}，
                             插单场景用于保护既有计划
        """
        self.strategy = strategy
        self.max_time_seconds = max_time_seconds
        self.num_workers = num_workers
        self.strategy_params = strategy_params or {}
        self.fixed_intervals = fixed_intervals or {}

    # ==================== 主入口 ====================

    def solve(
        self,
        tasks: List[TaskInput],
        work_centers: List[WorkCenterInput],
        start_date: datetime = None,
    ) -> SolverResult:
        """
        求解排产问题

        Returns:
            SolverResult；tasks 中每项含 segments（真实时间分段），
            放不下的任务进入 unscheduled_tasks
        """
        if not tasks:
            return SolverResult(True, "OPTIMAL", [], statistics={"total_tasks": 0})
        if not work_centers:
            return SolverResult(False, "INFEASIBLE", [],
                                statistics={"error": "没有可用的工作中心"})

        start_date = start_date or datetime.now()

        # 确保每个工作中心都有日历（缺失则按日产能兜底）
        calendars = self._ensure_calendars(work_centers, start_date)
        usable = [wc for wc in work_centers if calendars.get(wc.center_id)]
        if not usable:
            return SolverResult(False, "INFEASIBLE", [],
                                statistics={"error": "所有工作中心在排产窗口内均无可用产能"})

        logger.info(f"开始求解: {len(tasks)}个任务, {len(usable)}个工作中心, 策略={self.strategy.value}")

        model = cp_model.CpModel()
        v = self._build_model(model, tasks, usable, calendars, start_date)
        self._set_objective(model, tasks, usable, v)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_time_seconds
        solver.parameters.num_workers = self.num_workers

        t0 = datetime.now()
        status = solver.Solve(model)
        solve_time = (datetime.now() - t0).total_seconds()

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return SolverResult(
                False, self._status_name(status), [],
                solve_time_seconds=solve_time,
                statistics={
                    "total_tasks": len(tasks),
                    "total_work_centers": len(usable),
                    "error": "求解失败，约束无法满足",
                },
            )

        scheduled, unscheduled = self._extract(solver, tasks, usable, calendars, v)

        return SolverResult(
            success=True,
            status=self._status_name(status),
            tasks=scheduled,
            unscheduled_tasks=unscheduled,
            objective_value=solver.ObjectiveValue(),
            solve_time_seconds=solve_time,
            statistics={
                "total_tasks": len(tasks),
                "scheduled_tasks": len(scheduled),
                "unscheduled_tasks": len(unscheduled),
                "total_work_centers": len(usable),
                "total_segments": sum(len(t["segments"]) for t in scheduled),
                "tardy_tasks": sum(1 for t in scheduled if t.get("tardiness_hours", 0) > 0),
                "solver_status": self._status_name(status),
                "horizon_hours": {
                    cid: round(cal.total_minutes / 60, 1) for cid, cal in calendars.items()
                },
            },
        )

    # ==================== 建模 ====================

    def _ensure_calendars(
        self, work_centers: List[WorkCenterInput], start_date: datetime
    ) -> Dict[str, WorkCalendar]:
        """取出或兜底生成各中心日历"""
        calendars: Dict[str, WorkCalendar] = {}
        for wc in work_centers:
            cal = wc.calendar
            if cal is None:
                # TODO: 无产能日历时按日产能连续 30 天兜底，生产环境应视为数据异常
                cal = WorkCalendar.continuous(
                    wc.center_id, wc.capacity_per_day or 8.0,
                    days=30, start_date=start_date.date(),
                )
            if cal.slots:
                calendars[wc.center_id] = cal
        return calendars

    def _build_model(
        self,
        model: cp_model.CpModel,
        tasks: List[TaskInput],
        work_centers: List[WorkCenterInput],
        calendars: Dict[str, WorkCalendar],
        start_date: datetime,
    ) -> Dict[str, Any]:
        """
        构建决策变量与约束

        变量结构：
            presence[(task, center)] : 该任务是否排在该中心
            start/end[(task, center)]: 该中心工作分钟轴上的起止坐标
            unscheduled[task]        : 该任务是否未能排产
            tardiness[task]          : 超期分钟数
        """
        presence: Dict[Tuple[str, str], Any] = {}
        starts: Dict[Tuple[str, str], Any] = {}
        ends: Dict[Tuple[str, str], Any] = {}
        unscheduled: Dict[str, Any] = {}
        tardiness: Dict[str, Any] = {}
        intervals_by_center: Dict[str, List[Any]] = {wc.center_id: [] for wc in work_centers}
        setup_intervals_by_center: Dict[str, List[Dict[str, Any]]] = {}

        center_ids = [wc.center_id for wc in work_centers]

        for task in tasks:
            # 每个中心的工时可能不同（来自 routing 表），按中心分别计算
            center_durations: Dict[str, int] = {}
            allowed = [c for c in (task.allowed_centers or center_ids) if c in calendars] or list(calendars)

            lits = []
            for cid in allowed:
                cal = calendars[cid]
                # 优先用 per_center_hours，缺失时回退到 duration_hours
                hrs = (task.per_center_hours or {}).get(cid, task.duration_hours)
                dur = max(1, int(round(hrs * 60)))
                center_durations[cid] = dur

                horizon = cal.total_minutes
                if dur > horizon:
                    # 该中心窗口内总产能装不下这个任务，直接排除该选项
                    continue

                lit = model.NewBoolVar(f"x_{task.task_id}_{cid}")
                s = model.NewIntVar(0, horizon - dur, f"s_{task.task_id}_{cid}")
                e = model.NewIntVar(dur, horizon, f"e_{task.task_id}_{cid}")
                model.Add(e == s + dur)

                iv = model.NewOptionalIntervalVar(s, dur, e, lit, f"iv_{task.task_id}_{cid}")
                intervals_by_center[cid].append(iv)

                presence[(task.task_id, cid)] = lit
                starts[(task.task_id, cid)] = s
                ends[(task.task_id, cid)] = e
                lits.append(lit)

                # ---- 换型时间 interval ----
                # 换型时间紧接在任务开始之前，占用工作中心但不产出
                setup_min = (task.per_center_setup or {}).get(cid, task.setup_time_minutes or 0)
                if setup_min > 0:
                    setup_s = model.NewIntVar(0, horizon, f"ss_{task.task_id}_{cid}")
                    setup_e = model.NewIntVar(0, horizon, f"se_{task.task_id}_{cid}")
                    model.Add(setup_e == setup_s + setup_min)
                    model.Add(setup_e <= s)  # 换型必须在任务开始前完成
                    # 换型 interval 仅在该任务被选到此中心时存在
                    setup_iv = model.NewOptionalIntervalVar(
                        setup_s, setup_min, setup_e, lit, f"siv_{task.task_id}_{cid}"
                    )
                    setup_intervals_by_center.setdefault(cid, []).append({
                        "setup_iv": setup_iv,
                        "task_id": task.task_id,
                        "center_id": cid,
                    })

            # 未排产出口：允许放不下，但代价极高
            u = model.NewBoolVar(f"u_{task.task_id}")
            unscheduled[task.task_id] = u
            if lits:
                model.AddExactlyOne(lits + [u])
            else:
                model.Add(u == 1)

            # 超期分钟数（软交期约束）
            tardiness[task.task_id] = model.NewIntVar(0, 10 ** 7, f"tard_{task.task_id}")

        # ---- 资源独占：同一中心的任务区间（含换型时间）互不重叠 ----
        # 每个任务在该中心额外占用一个换型时间 interval，紧接在任务 interval 之前。
        # 换型 interval 与任务 interval 一起参与 NoOverlap，保证换型期间工作中心被独占。
        # 换型 interval 与任务 interval 通过 start >= setup_end 绑定顺序。
        for cid, ivs in intervals_by_center.items():
            setup_ivs = []
            for iv_data in setup_intervals_by_center.get(cid, []):
                setup_ivs.append(iv_data["setup_iv"])
            fixed = self._fixed_intervals_for(cid, calendars.get(cid), model)
            all_ivs = ivs + setup_ivs + fixed
            if len(all_ivs) > 1:
                model.AddNoOverlap(all_ivs)

        # ---- 交期软约束 ----
        for task in tasks:
            if not task.due_date:
                model.Add(tardiness[task.task_id] == 0)
                continue
            for cid in calendars:
                key = (task.task_id, cid)
                if key not in ends:
                    continue
                # 交期换算到该中心的工作分钟轴
                deadline = calendars[cid].to_work_minutes(task.due_date)
                model.Add(
                    tardiness[task.task_id] >= ends[key] - deadline
                ).OnlyEnforceIf(presence[key])

        # ---- 前置任务约束 ----
        self._add_precedence(model, tasks, calendars, presence, starts, ends, unscheduled, start_date)

        return {
            "presence": presence,
            "starts": starts,
            "ends": ends,
            "unscheduled": unscheduled,
            "tardiness": tardiness,
            "center_ids": center_ids,
        }

    def _fixed_intervals_for(
        self, center_id: str, calendar: Optional[WorkCalendar], model: cp_model.CpModel
    ) -> List[Any]:
        """把已排产任务转成固定区间，保护既有计划不被覆盖（插单场景）"""
        out = []
        if calendar is None:
            return out
        for idx, (s_dt, e_dt, task_id) in enumerate(self.fixed_intervals.get(center_id, [])):
            s_wm = calendar.to_work_minutes(s_dt, mode="ceil")
            e_wm = calendar.to_work_minutes(e_dt)
            if e_wm <= s_wm:
                continue
            out.append(model.NewIntervalVar(
                s_wm, e_wm - s_wm, e_wm, f"fixed_{center_id}_{idx}_{task_id}"
            ))
        return out

    def _add_precedence(
        self, model, tasks, calendars, presence, starts, ends, unscheduled, start_date
    ) -> None:
        """
        添加前置任务约束

        同轴情形（前后任务都只能在同一个中心）可直接比较工作分钟坐标，精确无误。
        跨中心情形因各中心工作分钟轴不同源，需换算到统一的真实时间轴，
        此处通过「所在时段 + 累计非工作间隔」做分段线性映射实现。
        """
        task_map = {t.task_id: t for t in tasks}
        real_end: Dict[str, Any] = {}
        real_start: Dict[str, Any] = {}

        def centers_of(tid: str) -> List[str]:
            return [c for (t, c) in presence if t == tid]

        for task in tasks:
            for pred_id in task.predecessors or []:
                if pred_id not in task_map:
                    continue
                cs_succ, cs_pred = centers_of(task.task_id), centers_of(pred_id)
                if not cs_succ or not cs_pred:
                    continue

                # 同一个中心且都只有这一个选项 -> 直接在工作分钟轴上比较
                if cs_succ == cs_pred and len(cs_succ) == 1:
                    cid = cs_succ[0]
                    model.Add(
                        starts[(task.task_id, cid)] >= ends[(pred_id, cid)]
                    ).OnlyEnforceIf([
                        presence[(task.task_id, cid)], presence[(pred_id, cid)]
                    ])
                    continue

                # 跨中心 -> 换算到真实时间轴
                if pred_id not in real_end:
                    real_end[pred_id] = self._link_real_time(
                        model, pred_id, ends, presence, calendars, is_end=True
                    )
                if task.task_id not in real_start:
                    real_start[task.task_id] = self._link_real_time(
                        model, task.task_id, starts, presence, calendars, is_end=False
                    )
                model.Add(real_start[task.task_id] >= real_end[pred_id]).OnlyEnforceIf([
                    unscheduled[task.task_id].Not(), unscheduled[pred_id].Not()
                ])

    def _link_real_time(self, model, task_id, wm_vars, presence, calendars, is_end: bool):
        """
        建立「工作分钟坐标 -> 真实分钟坐标」的分段线性映射变量。

        原理：真实分钟 = 工作分钟 + 该时段之前累计的非工作间隔（gap）。
        gap 在每个时段内是常量，故按时段引入指示变量即可线性化。
        """
        horizon_real = 366 * 24 * 60
        rv = model.NewIntVar(0, horizon_real, f"real_{'e' if is_end else 's'}_{task_id}")

        for (tid, cid), lit in list(presence.items()):
            if tid != task_id:
                continue
            cal = calendars[cid]
            wm = wm_vars[(tid, cid)]
            base = cal.slots[0].start_dt
            slot_lits = []
            for k, slot in enumerate(cal.slots):
                b = model.NewBoolVar(f"in_{task_id}_{cid}_{k}_{'e' if is_end else 's'}")
                lo, hi = slot.offset, slot.end_offset
                # 结束坐标可落在时段右端点上，故用 wm-1 判定所属时段
                if is_end:
                    model.Add(wm >= lo + 1).OnlyEnforceIf(b)
                    model.Add(wm <= hi).OnlyEnforceIf(b)
                else:
                    model.Add(wm >= lo).OnlyEnforceIf(b)
                    model.Add(wm <= hi - 1).OnlyEnforceIf(b)
                gap = int((slot.start_dt - base).total_seconds() // 60) - slot.offset
                model.Add(rv == wm + gap).OnlyEnforceIf(b)
                slot_lits.append(b)
            model.Add(sum(slot_lits) == 1).OnlyEnforceIf(lit)
            model.Add(sum(slot_lits) == 0).OnlyEnforceIf(lit.Not())
        return rv

    # ==================== 目标函数 ====================

    def _set_objective(self, model, tasks, work_centers, v) -> None:
        """
        设置目标函数

        统一形式：未排产惩罚 + 加权超期 + 策略相关项
        """
        terms = []

        # 1. 未排产惩罚（压倒性权重，确保尽量都排上）
        for task in tasks:
            terms.append(PENALTY_UNSCHEDULED * v["unscheduled"][task.task_id])

        # 2. 加权超期惩罚（优先级越高，超期代价越大）
        for task in tasks:
            w = PENALTY_TARDINESS_BASE * self._priority_weight(task.priority)
            terms.append(w * v["tardiness"][task.task_id])

        # 3. 策略相关项
        if self.strategy == SolverStrategy.LOAD_BALANCE:
            terms.append(self._load_balance_term(model, tasks, work_centers, v))
        else:
            terms.extend(self._completion_terms(model, tasks, v))

        model.Minimize(sum(terms))

    def _completion_terms(self, model, tasks, v) -> List[Any]:
        """
        完工时间项

        priority_first：按优先级加权完工时间，紧急订单尽早完成
        shortest_job  ：按工时倒数加权，短作业优先（近似 SPT 规则）
        """
        terms = []
        for task in tasks:
            comp = self._completion_var(model, task, v)
            if self.strategy == SolverStrategy.SHORTEST_JOB:
                duration_min = max(1, int(round(task.duration_hours * 60)))
                w = max(1, 6000 // duration_min)
            else:
                w = self._priority_weight(task.priority)
            terms.append(w * comp)
        return terms

    def _completion_var(self, model, task, v):
        """
        任务完工坐标变量

        说明：不同中心的工作分钟轴不同源，此处直接取所选中心的坐标作为
        目标函数中的完工度量。由于各中心轴均以「可生产分钟」为单位、
        起点同为排产窗口开始，量级可比，作为优化方向足够；
        精确时间比较一律在 _extract 后用真实时间进行。
        """
        keys = [(t, c) for (t, c) in v["ends"] if t == task.task_id]
        if not keys:
            return model.NewConstant(0)
        comp = model.NewIntVar(0, 10 ** 7, f"comp_{task.task_id}")
        for key in keys:
            model.Add(comp == v["ends"][key]).OnlyEnforceIf(v["presence"][key])
        model.Add(comp == 0).OnlyEnforceIf(v["unscheduled"][task.task_id])
        return comp

    def _load_balance_term(self, model, tasks, work_centers, v):
        """负荷均衡项：最小化各中心已分配工时的最大值"""
        loads = []
        for wc in work_centers:
            terms = []
            for task in tasks:
                key = (task.task_id, wc.center_id)
                if key in v["presence"]:
                    duration = max(1, int(round(task.duration_hours * 60)))
                    terms.append(duration * v["presence"][key])
            load = model.NewIntVar(0, 10 ** 7, f"load_{wc.center_id}")
            if terms:
                model.Add(load == sum(terms))
            else:
                model.Add(load == 0)
            loads.append(load)

        max_load = model.NewIntVar(0, 10 ** 7, "max_load")
        model.AddMaxEquality(max_load, loads)
        return 10 * max_load

    @staticmethod
    def _priority_weight(priority: int) -> int:
        """优先级权重：1紧急=10, 2正常=3, 3低=1"""
        return {1: 10, 2: 3, 3: 1}.get(priority, 1)

    # ==================== 结果提取 ====================

    def _extract(
        self, solver, tasks, work_centers, calendars, v
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """把求解结果还原为真实时间，跨天任务拆成 segments"""
        wc_map = {wc.center_id: wc for wc in work_centers}
        scheduled: List[Dict[str, Any]] = []
        unscheduled: List[Dict[str, Any]] = []

        for task in tasks:
            if solver.Value(v["unscheduled"][task.task_id]):
                unscheduled.append({
                    "task_id": task.task_id,
                    "order_id": task.order_id,
                    "item_id": task.item_id,
                    "product_id": task.product_id,
                    "quantity": task.quantity,
                    "duration_hours": task.duration_hours,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "priority": task.priority,
                    "reason": "排产窗口内产能不足，无法安排",
                })
                continue

            cid = next(
                c for (t, c) in v["presence"]
                if t == task.task_id and solver.Value(v["presence"][(t, c)])
            )
            cal = calendars[cid]
            s_wm = solver.Value(v["starts"][(task.task_id, cid)])
            e_wm = solver.Value(v["ends"][(task.task_id, cid)])
            segments = cal.split(s_wm, e_wm)

            # 实际工时按分配的工作中心计算
            actual_hours = (task.per_center_hours or {}).get(cid, task.duration_hours)
            setup_min = (task.per_center_setup or {}).get(cid, task.setup_time_minutes or 0)

            tard_min = solver.Value(v["tardiness"][task.task_id])
            scheduled.append({
                "task_id": task.task_id,
                "order_id": task.order_id,
                "item_id": task.item_id,
                "product_id": task.product_id,
                "center_id": cid,
                "center_name": wc_map[cid].center_name,
                "quantity": task.quantity,
                "start_time": segments[0][0].isoformat() if segments else None,
                "end_time": segments[-1][1].isoformat() if segments else None,
                "duration_hours": actual_hours,
                "setup_time_minutes": setup_min,
                "priority": task.priority,
                "status": "planned",
                "due_date": task.due_date.isoformat() if task.due_date else None,
                "tardiness_hours": round(tard_min / 60.0, 2),
                # 真实时间分段：落库时每段一行，规避 production_task 的时间重叠排他约束
                "segments": [
                    {"segment_index": i, "start_time": a.isoformat(), "end_time": b.isoformat(),
                     "duration_hours": round((b - a).total_seconds() / 3600, 4)}
                    for i, (a, b) in enumerate(segments)
                ],
            })

        scheduled.sort(key=lambda t: (t["start_time"] or "", t["task_id"]))
        return scheduled, unscheduled

    @staticmethod
    def _status_name(status: int) -> str:
        """求解状态名"""
        return {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "UNKNOWN",
        }.get(status, "UNKNOWN")


# ==================== 辅助函数 ====================

def create_scheduler_from_state(state: Dict[str, Any]) -> ORToolsScheduler:
    """从图状态创建求解器实例"""
    from config.settings import settings
    return ORToolsScheduler(
        strategy=SolverStrategy(state.get("strategy", "priority_first")),
        max_time_seconds=settings.ortools_max_time_seconds,
        num_workers=settings.ortools_num_workers,
        strategy_params=state.get("strategy_params", {}),
    )


def prepare_solver_inputs(
    orders: List[Dict],
    work_centers: List[Dict],
    capacity_calendar: List[Dict],
    start_date: datetime = None,
    existing_tasks: List[Dict] = None,
) -> Tuple[List[TaskInput], List[WorkCenterInput]]:
    """
    准备求解器输入

    关键点：
        - 工时直接复用 data_fetch 预置的 item["duration_hours"]，不再重复估算，
          避免两处口径不一致
        - 为每个工作中心装配 WorkCalendar
    """
    start_date = start_date or datetime.now()
    calendars = build_calendars(work_centers, capacity_calendar, now=start_date)

    wc_inputs: List[WorkCenterInput] = []
    for wc in work_centers:
        if wc.get("status") != "active":
            continue
        cal = calendars.get(wc["center_id"])
        if cal is None:
            logger.warning(f"工作中心 {wc['center_id']} 在窗口内无可用产能，已跳过")
            continue
        wc_inputs.append(WorkCenterInput(
            center_id=wc["center_id"],
            center_name=wc["center_name"],
            capacity_per_day=float(wc.get("capacity_per_day") or 8.0),
            efficiency_rate=float(wc.get("efficiency_rate") or 1.0),
            calendar=cal,
        ))

    all_wc_ids = [wc.center_id for wc in wc_inputs]
    task_inputs: List[TaskInput] = []
    counter = 1

    for order in orders or []:
        for item in order.get("items", []):
            qty = float(item["quantity"])

            # 从 state 中的 routing 或直接查库获取工艺路线
            routing = item.get("_routing") or {}
            if not routing:
                # data_fetch 未预置时，尝试从 state 传入的 routing_map 获取
                # prepare_solver_inputs 的签名不变，routing 通过 item 上的
                # _routing 字段传入（由 plan_subgraph 在调用前注入）
                routing = {}

            if routing:
                # 有工艺路线：按中心设置差异化工时和换型时间
                per_center = {}
                per_center_setup = {}
                for cid, info in routing.items():
                    if cid not in all_wc_ids:
                        continue
                    if isinstance(info, dict):
                        rate = info.get("hours_per_unit", 0.5)
                        setup = int(info.get("setup_time_minutes", 0))
                    else:
                        # 兼容旧格式 {center_id: hours_per_unit}
                        rate = info
                        setup = 0
                    per_center[cid] = round(rate * qty, 4)
                    if setup > 0:
                        per_center_setup[cid] = setup
                allowed = list(per_center.keys())
                duration = min(per_center.values()) if per_center else qty * 0.5
                setup_time = min(per_center_setup.values()) if per_center_setup else 0
            else:
                # 无工艺路线：所有中心统一工时（兜底）
                per_center = None
                per_center_setup = None
                allowed = list(all_wc_ids)
                duration = item.get("duration_hours") or qty * 0.5
                setup_time = 0
                if not item.get("duration_hours"):
                    logger.warning(
                        f"产品 {item['product_id']} 无工艺路线且无预置工时，使用兜底 0.5h/件"
                    )

            due_raw = item.get("due_date") or order.get("required_date")
            due_date = None
            if due_raw:
                due_date = datetime.fromisoformat(str(due_raw))
                if due_date.hour == 0 and due_date.minute == 0:
                    due_date = due_date.replace(hour=23, minute=59)

            task_inputs.append(TaskInput(
                task_id=f"TASK-{counter:03d}",
                order_id=order["order_id"],
                item_id=item["item_id"],
                product_id=item["product_id"],
                quantity=qty,
                duration_hours=float(duration),
                per_center_hours=per_center,
                per_center_setup=per_center_setup,
                setup_time_minutes=setup_time,
                due_date=due_date,
                priority=int(order.get("priority", 2)),
                allowed_centers=allowed,
            ))
            counter += 1

    return task_inputs, wc_inputs


def build_fixed_intervals(existing_tasks: List[Dict]) -> Dict[str, List[Tuple[datetime, datetime, str]]]:
    """把库中已排产任务整理为 {center_id: [(start, end, task_id)]}，用于插单保护"""
    out: Dict[str, List[Tuple[datetime, datetime, str]]] = {}
    for t in existing_tasks or []:
        cid = t.get("center_id")
        if not cid or not t.get("start_time") or not t.get("end_time"):
            continue
        out.setdefault(cid, []).append((
            datetime.fromisoformat(str(t["start_time"])),
            datetime.fromisoformat(str(t["end_time"])),
            t.get("task_id", ""),
        ))
    return out