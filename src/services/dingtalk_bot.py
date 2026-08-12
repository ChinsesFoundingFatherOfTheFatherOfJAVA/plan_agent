# -*- coding: utf-8 -*-
"""
钉钉机器人服务（Stream 模式）

通过钉钉 Stream SDK 长连接接收用户消息，调用排产图执行排产，
将结果格式化为 Markdown 回复到钉钉。

支持指令：
    - 自然语言排产（LLM 解析意图）
    - "确认" / "确认计划" -- 确认最新草稿计划
    - "查看计划" -- 查看最新计划概要
    - "甘特图" -- 返回甘特图链接
    - "帮助" -- 显示帮助菜单

启动方式：
    python -m src.services.dingtalk_bot
"""
import json
import logging
from typing import Optional

from dingtalk_stream import (
    AckMessage,
    AsyncChatbotHandler,
    ChatbotMessage,
    Credential,
    DingTalkStreamClient,
)

from config.settings import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 帮助菜单
HELP_TEXT = """## 排产助手使用指南

### 快捷指令
- **确认** / **确认计划** -- 确认最新草稿计划
- **查看计划** -- 查看最新计划概要
- **甘特图** -- 获取甘特图链接
- **帮助** -- 显示本帮助

### 自然语言指令
- "安排下周的生产计划"
- "检查未来三天的物料缺口"
- "检查订单交付进度"
- "订单ORD-008紧急，帮我插单"

### 提示
排产计划生成后为草稿状态，发送"确认"可下发执行。

### 时间关键词
- 支持"下周"、"下下周"、"本周"、"未来N天"等时间描述

### 草稿管理
- **删除草稿** / **删除计划** -- 删除最新草稿计划"""


# 用户-计划绑定：{sender_id: plan_id}，排产后记录，确认时取回
_user_plan_cache: dict = {}

# 用户最近确认记录：{sender_id: {"plan_id": ..., "status": ..., "time": ...}}
_user_confirm_cache: dict = {}


class SchedulingChatbotHandler(AsyncChatbotHandler):
    """
    排产机器人消息处理器。

    收到用户消息后：
        1. 快捷指令检测（确认/查看/帮助/甘特图）
        2. 关键词意图检测（排产/缺料/交付/插单）
        3. LLM 兜底解析
        4. 调用排产图，格式化结果回复
    """

    def process(self, callback_message):
        """处理钉钉消息（同步，在 ThreadPool 中执行）。"""
        try:
            incoming_message = ChatbotMessage.from_dict(callback_message.data)
            text_list = incoming_message.text.content if incoming_message.text else ""
            text = text_list.strip()
            if not text:
                self.reply_text('请输入指令，发送帮助查看用法', incoming_message)
                return AckMessage.STATUS_OK, "ok"

            sender = incoming_message.sender_nick or "用户"
            logger.info(f"钉钉消息来自 {sender}: {text[:80]}")

            # ---- 快捷指令 ----
            cmd = self._match_command(text)
            if cmd:
                self._handle_command(cmd, text, incoming_message)
                return AckMessage.STATUS_OK, "ok"

            # ---- 排产/检查类指令 ----
            request_type = self._detect_request_type(text)

            # 先回复进度提示
            progress_msg = self._progress_message(request_type, text)
            self.reply_text(progress_msg, incoming_message)

            # 记录当前用户 ID，供排产后绑定计划
            self._current_sender_id = incoming_message.sender_id or incoming_message.sender_staff_id or "default"

            # 调用排产图
            result = self._run_scheduling(text, request_type)

            if result:
                md = self._format_result(result, text)
                self.reply_markdown("排产结果", md, incoming_message)
            else:
                self.reply_text("处理失败，请稍后重试或检查日志", incoming_message)

        except Exception as e:
            logger.exception("钉钉消息处理失败")
            try:
                self.reply_text(f"处理出错: {type(e).__name__}: {str(e)[:100]}", incoming_message)
            except Exception:
                pass

        return AckMessage.STATUS_OK, "ok"

    # ==================== 指令匹配 ====================

    def _match_command(self, text: str) -> Optional[str]:
        """匹配快捷指令，返回指令名或 None"""
        text_lower = text.lower().strip()
        if text_lower in ("确认", "确认计划", "confirm", "确认下发"):
            return "confirm"
        if text_lower in ("查看计划", "最新计划", "查看", "view"):
            return "view_plan"
        if text_lower in ("甘特图", "gantt", "查看甘特图"):
            return "gantt"
        if text_lower in ("帮助", "help", "?", "？", "菜单"):
            return "help"
        if text_lower in ("删除草稿", "删除计划", "删除", "delete", "清理草稿"):
            return "delete_draft"
        return None

    def _handle_command(self, cmd: str, text: str, incoming_message):
        """处理快捷指令"""
        if cmd == "help":
            self.reply_markdown("帮助", HELP_TEXT, incoming_message)

        elif cmd == "confirm":
            self.reply_text("正在确认最新计划...", incoming_message)
            sender_id = incoming_message.sender_id or incoming_message.sender_staff_id or "default"
            plan_id = self._confirm_latest_plan(sender_id)
            if plan_id:
                self.reply_text(f"计划 {plan_id} 已确认，任务状态已转为 scheduled。", incoming_message)
            else:
                self.reply_text("确认失败：未找到草稿状态的计划，或确认时出错。", incoming_message)

        elif cmd == "view_plan":
            plan_info = self._get_latest_plan_summary()
            if plan_info:
                self.reply_markdown("计划概要", plan_info, incoming_message)
            else:
                self.reply_text('当前无排产计划。发送安排下周生产计划生成新计划。', incoming_message)

        elif cmd == "gantt":
            host = "http://localhost:8000"
            self.reply_markdown(
                "甘特图",
                f"## 甘特图可视化\n\n[点击查看排产甘特图]({host}/gantt)\n\n支持缩放、拖拽调整任务时间、计划对比。",
                incoming_message,
            )

        elif cmd == "delete_draft":
            self.reply_text("正在删除最新草稿计划...", incoming_message)
            sender_id = incoming_message.sender_id or incoming_message.sender_staff_id or "default"
            result = self._delete_latest_draft(sender_id)
            if result:
                self.reply_text(f"已删除草稿计划 {result}。", incoming_message)
            else:
                self.reply_text("删除失败：未找到草稿状态的计划，或删除时出错。", incoming_message)

    def _confirm_latest_plan(self, sender_id: str = "") -> Optional[str]:
        """确认最新草稿计划

        优先级：
            1. 如果 sender_id 在 _user_plan_cache 中，确认该用户最近生成的计划
            2. 否则确认 DB 中最新的 draft 计划（兜底）

        TODO: 多用户并发场景下方案2有风险，后续应改为钉钉会话级状态管理
        """
        try:
            from src.services.db import pg_query
            from src.services import plan_writer as writer

            plan_id = None

            # 方案1：从用户缓存取 plan_id
            if sender_id and sender_id in _user_plan_cache:
                plan_id = _user_plan_cache[sender_id]
                logger.info(f"从用户缓存取计划: sender={sender_id} plan={plan_id}")
                # 校验该计划仍为 draft
                rows = pg_query(
                    "SELECT status FROM production_plan WHERE plan_id=%s",
                    (plan_id,),
                )
                if not rows or rows[0]["status"] != "draft":
                    logger.info(f"缓存计划 {plan_id} 状态已变更，尝试 DB 兜底")
                    plan_id = None

            # 方案2：DB 兜底
            if not plan_id:
                rows = pg_query(
                    "SELECT plan_id FROM production_plan WHERE status='draft' ORDER BY created_at DESC LIMIT 1"
                )
                if not rows:
                    return None
                plan_id = rows[0]["plan_id"]
                logger.info(f"DB 兜底取计划: {plan_id}")

            writer.confirm_plan(plan_id)

            # 确认成功后清理缓存
            if sender_id and sender_id in _user_plan_cache:
                del _user_plan_cache[sender_id]

            return plan_id
        except Exception as e:
            logger.error(f"确认计划失败: {e}")
            return None

    def _delete_latest_draft(self, sender_id: str = "") -> Optional[str]:
        """删除最新草稿计划（与 _confirm_latest_plan 逻辑对称）"""
        try:
            from src.services.db import pg_query
            from src.services import plan_writer as writer

            plan_id = None

            if sender_id and sender_id in _user_plan_cache:
                plan_id = _user_plan_cache[sender_id]
                rows = pg_query(
                    "SELECT status FROM production_plan WHERE plan_id=%s",
                    (plan_id,),
                )
                if not rows or rows[0]["status"] != "draft":
                    plan_id = None

            if not plan_id:
                rows = pg_query(
                    "SELECT plan_id FROM production_plan WHERE status='draft' ORDER BY created_at DESC LIMIT 1"
                )
                if not rows:
                    return None
                plan_id = rows[0]["plan_id"]

            writer.delete_plan(plan_id)

            if sender_id and sender_id in _user_plan_cache:
                del _user_plan_cache[sender_id]

            return plan_id
        except Exception as e:
            logger.error(f"删除草稿失败: {e}")
            return None

    def _get_latest_plan_summary(self) -> Optional[str]:
        """获取最新计划概要"""
        try:
            from src.services.db import pg_query

            plans = pg_query(
                """SELECT plan_id, plan_name, status, plan_date, horizon_start, horizon_end
                   FROM production_plan ORDER BY created_at DESC LIMIT 1"""
            )
            if not plans:
                return None
            p = plans[0]

            tasks = pg_query(
                """SELECT task_id, product_id, center_id, quantity, duration_hours,
                          start_time, end_time, status, due_date, setup_time_minutes
                   FROM production_task WHERE plan_id=%s
                   ORDER BY start_time""",
                (p["plan_id"],),
            )

            lines = [
                f"## 计划概要",
                f"",
                f"**计划编号**: {p['plan_id']}",
                f"**状态**: {p['status']}",
                f"**日期范围**: {p['horizon_start']} ~ {p['horizon_end']}",
                f"**任务数**: {len(tasks)} 个",
                f"",
            ]

            if tasks:
                lines.append("### 任务列表")
                for t in tasks[:10]:
                    setup = t.get("setup_time_minutes", 0)
                    setup_str = f" 换型{setup}min" if setup else ""
                    st = str(t["start_time"])[:16]
                    en = str(t["end_time"])[:16]
                    lines.append(
                        f"- **{t['task_id']}** {t['product_id']} x{t['quantity']} "
                        f"@ {t['center_id']} {float(t['duration_hours']):.1f}h{setup_str}\n"
                        f"  {st} ~ {en}"
                    )

            if p["status"] == "draft":
                lines.append(f"\n> 发送确认可下发执行")

            return "\n".join(lines)
        except Exception as e:
            logger.error(f"获取计划概要失败: {e}")
            return None

    # ==================== 排产执行 ====================

    def _detect_request_type(self, text: str) -> Optional[str]:
        """关键词检测请求类型"""
        text_lower = text.lower()
        if any(kw in text for kw in ["插单", "insert"]):
            return "insert_order"
        if any(kw in text for kw in ["缺料", "物料", "shortage"]):
            return "check_shortage"
        if any(kw in text for kw in ["交付", "交期", "delivery"]):
            return "check_delivery"
        if any(kw in text for kw in ["平衡", "rebalance"]):
            return "rebalance"
        if any(kw in text for kw in ["计划", "排产", "安排", "plan", "生产"]):
            return "new_plan"
        return None

    def _progress_message(self, request_type: Optional[str], text: str) -> str:
        """生成进度提示消息"""
        if request_type == "new_plan":
            return "正在生成排产计划..."
        elif request_type == "check_shortage":
            return "正在检查物料缺口..."
        elif request_type == "check_delivery":
            return "正在检查交付进度..."
        elif request_type == "insert_order":
            return "正在处理插单..."
        else:
            return "正在通过 AI 理解您的需求，请稍候..."

    @staticmethod
    def _parse_time_window(text: str) -> Optional[dict]:
        """从用户输入中解析时间窗口关键词，返回 date_range 或 None"""
        import re
        from datetime import datetime, timedelta

        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())

        if "下下周" in text:
            start = monday + timedelta(days=14)
            end = start + timedelta(days=6)
            return {"start": start.isoformat(), "end": end.isoformat()}
        if "下周" in text:
            start = monday + timedelta(days=7)
            end = start + timedelta(days=6)
            return {"start": start.isoformat(), "end": end.isoformat()}
        if "本周" in text or "这周" in text:
            start = monday
            end = start + timedelta(days=6)
            return {"start": start.isoformat(), "end": end.isoformat()}
        m = re.search(r"未来(\d+)天", text)
        if m:
            n = int(m.group(1))
            return {"start": today.isoformat(), "end": (today + timedelta(days=n)).isoformat()}
        m = re.search(r"未来(\d+)周", text)
        if m:
            n = int(m.group(1))
            return {"start": today.isoformat(), "end": (today + timedelta(days=n*7)).isoformat()}
        return None

    def _run_scheduling(self, user_input: str, request_type: Optional[str] = None) -> Optional[dict]:
        """调用排产图执行排产"""
        try:
            from src.agent.graphs.plan_graph import build_plan_graph
            from src.models.state import create_initial_state
            from src.services.celery_app import _get_llm_or_fallback, _MockLLM

            if request_type:
                llm = _MockLLM(request_type)
            else:
                llm = _get_llm_or_fallback()

            graph = build_plan_graph(llm)
            state = create_initial_state(user_input)
            if request_type:
                state["request_type"] = request_type

            # 解析时间窗口，注入 request_params
            time_window = self._parse_time_window(user_input)
            if time_window:
                state["request_params"] = {"date_range": time_window}
                logger.info(f"解析到时间窗口: {time_window}")

            try:
                result = graph.invoke(state)
            except Exception as invoke_err:
                logger.error(f"排产图执行失败: {type(invoke_err).__name__}: {invoke_err}")
                if not request_type:
                    logger.info("LLM 路径失败，降级为关键词检测重试...")
                    fallback_type = self._detect_request_type(text) or "new_plan"
                    llm = _MockLLM(fallback_type)
                    state["request_type"] = fallback_type
                    state["request_params"] = {}
                    graph = build_plan_graph(llm)
                    result = graph.invoke(state)
                else:
                    raise

            # 排产成功后，将 plan_id 与用户绑定
            plan_id = result.get("plan_id")
            if plan_id and plan_id != "N/A":
                # 从 incoming_message 的 sender 信息获取 ID
                # 注意：process() 中调用时传入 sender_id
                if hasattr(self, "_current_sender_id"):
                    _user_plan_cache[self._current_sender_id] = plan_id
                    logger.info(f"用户 {self._current_sender_id} 生成计划 {plan_id}")

            return result

        except Exception as e:
            logger.error(f"排产执行失败: {type(e).__name__}: {e}")
            return None

    # ==================== 结果格式化 ====================

    def _format_result(self, result: dict, user_input: str) -> str:
        """将排产结果格式化为钉钉 Markdown"""
        request_type = result.get("request_type", "unknown")

        if request_type == "check_shortage":
            return self._format_shortage(result)
        elif request_type == "check_delivery":
            return self._format_delivery(result)
        else:
            return self._format_plan(result)

    def _format_plan(self, result: dict) -> str:
        """格式化排产计划结果"""
        plan_id = result.get("plan_id", "N/A")
        strategy = result.get("strategy", "N/A")
        tasks = result.get("production_tasks") or []
        unscheduled = result.get("unscheduled_tasks") or []
        schedule = result.get("schedule") or {}
        alerts = result.get("alerts") or []
        recs = result.get("recommendations") or []
        solver_status = schedule.get("solver_status", "N/A")
        total_hours = schedule.get("total_hours", 0)

        lines = [
            f"## 排产计划已生成",
            f"",
            f"**计划编号**: {plan_id}",
            f"**策略**: {strategy}",
            f"**求解状态**: {solver_status}",
            f"**任务数**: {len(tasks)} 个",
            f"**总工时**: {total_hours:.1f}h",
        ]

        if unscheduled:
            lines.append(f"**未排产**: {len(unscheduled)} 个")

        if alerts:
            lines.append(f"**预警**: {len(alerts)} 条")

        if tasks:
            lines.append("")
            lines.append("### 任务明细")
            for t in tasks[:8]:
                task_id = t.get("task_id", "?")
                product = t.get("product_id", "?")
                qty = t.get("quantity", 0)
                center = t.get("center_id", "?")
                dur = t.get("duration_hours", 0)
                setup = t.get("setup_time_minutes", 0)
                due = str(t.get("due_date", ""))[:10]
                segs = t.get("segments") or []
                setup_str = f" 换型{setup}min" if setup else ""
                lines.append(f"- **{task_id}** {product} x{qty} @ {center} {dur:.1f}h{setup_str} 交期{due}")

                for seg in segs[:3]:
                    st = str(seg.get("start_time", ""))[:16]
                    en = str(seg.get("end_time", ""))[:16]
                    sd = seg.get("duration_hours", 0)
                    lines.append(f"  - {st} ~ {en} ({sd:.1f}h)")
                if len(segs) > 3:
                    lines.append(f"  - ... 共 {len(segs)} 段")

        if recs:
            lines.append("")
            lines.append("### 建议")
            for rec in recs[:5]:
                lines.append(f"- {rec[:100]}")

        if result.get("plan_persisted"):
            lines.append("")
            lines.append(f"> 计划 {plan_id} 为草稿状态")
            lines.append(f"> 发送确认可下发执行")
            lines.append(f"> [查看甘特图](http://localhost:8000/gantt)")

        return "\n".join(lines)

    def _format_shortage(self, result: dict) -> str:
        """格式化缺料检查结果"""
        shortages = result.get("shortages") or []
        if not shortages:
            return "## 缺料检查完成\n\n当前无物料缺口"

        lines = [
            "## 缺料检查完成",
            f"发现 **{len(shortages)}** 项物料缺口：",
            "",
        ]
        for s in shortages:
            mid = s.get("material_id", "?")
            mname = s.get("material_name", "")
            shortage = s.get("shortage_quantity", 0)
            risk = s.get("risk_level", "?")
            req_date = str(s.get("required_date", ""))[:10]
            emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(risk, "⚪")
            lines.append(f"- {emoji} **{mid}** ({mname}) 缺口 {shortage} 需求日期 {req_date}")

        return "\n".join(lines)

    def _format_delivery(self, result: dict) -> str:
        """格式化交付检查结果"""
        risks = result.get("delivery_risks") or []
        if not risks:
            return "## 交付检查完成\n\n当前无交付延误风险"

        lines = [
            "## 交付检查完成",
            f"发现 **{len(risks)}** 项交付风险：",
            "",
        ]
        for r in risks:
            oid = r.get("order_id", "?")
            risk = r.get("risk_level", "?")
            due = str(r.get("due_date", ""))[:10]
            reason = r.get("reason", "")[:60]
            emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(risk, "⚪")
            lines.append(f"- {emoji} **{oid}** 交期 {due} - {reason}")

        return "\n".join(lines)


# ==================== 全局管理 ====================

_dingtalk_client: Optional[DingTalkStreamClient] = None


def start_dingtalk_bot():
    """启动钉钉 Stream 机器人"""
    global _dingtalk_client

    if not settings.dingtalk_client_id or not settings.dingtalk_client_secret:
        logger.warning("钉钉 Client ID/Secret 未配置，跳过启动")
        return False

    credential = Credential(
        client_id=settings.dingtalk_client_id,
        client_secret=settings.dingtalk_client_secret,
    )

    _dingtalk_client = DingTalkStreamClient(credential)
    handler = SchedulingChatbotHandler()
    _dingtalk_client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    logger.info("钉钉机器人启动中 (Stream 模式)...")
    try:
        _dingtalk_client.start_forever()
    except KeyboardInterrupt:
        logger.info("钉钉机器人已停止")
    except Exception as e:
        logger.error(f"钉钉机器人异常: {type(e).__name__}: {e}")
        _dingtalk_client = None
        return False

    return True


def stop_dingtalk_bot():
    """停止钉钉机器人"""
    global _dingtalk_client
    _dingtalk_client = None
    logger.info("钉钉机器人已停止")


def is_running() -> bool:
    """检查机器人是否在运行"""
    return _dingtalk_client is not None


if __name__ == "__main__":
    start_dingtalk_bot()
