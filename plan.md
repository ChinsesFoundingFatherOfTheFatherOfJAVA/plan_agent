# 智能排产计划 Agent 技术实现方案

> 版本：v1.1 | 日期：2026-08-07 | 状态：方案定稿（已修复架构问题）

---

## 1. 系统概述

### 1.1 核心能力

| 能力 | 描述 | 优先级 |
|------|------|--------|
| 订单整合 | 汇总订单、物料、产能、交期约束，生成主生产计划 | P0 |
| 滚动排产 | 基于时间窗口自动滚动，动态调整排产计划 | P0 |
| 物料缺料预判 | 预测未来时段物料缺口，提前预警 | P1 |
| 插单扰动重排 | 新订单插入时，最小化扰动重排现有计划 | P1 |
| 产能负荷均衡 | 平衡各产线/工位负荷，避免瓶颈 | P2 |
| 交付达成预警 | 实时监控交付进度，偏差预警 | P1 |

### 1.2 技术栈定版

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| Agent框架 | LangGraph | 状态图驱动，支持复杂工作流编排 |
| LLM | 千问3（已部署） | 推理与决策引擎 |
| 向量数据库 | Milvus | RAG知识检索（开发+生产统一） |
| 关系数据库 | PostgreSQL | 业务数据存储 |
| 缓存 | Redis | 实时状态缓存 |
| 任务队列 | Celery + Redis | 异步任务处理（长耗时排产、事件驱动） |
| 前端交互 | 钉钉IM + Web（后续） | 第一期钉钉机器人为主 |

### 1.3 优化策略

| 优先级 | 目标 | 说明 |
|--------|------|------|
| 1 | 交期达成 | 客户满意度优先 |
| 2 | 成本控制 | 利润率次之 |
| 3 | 负荷均衡 | 设备寿命最后 |

### 1.4 LLM使用规范

**LLM的角色边界**：

| 可以做 | 不可以做 |
|--------|----------|
| 意图解析、参数提取 | 直接生成排产任务列表 |
| 策略建议、权重调整 | 替代约束求解器 |
| 结果解释、异常分析 | 生成未经验证的方案 |

**说明**：排产任务必须经过OR-Tools求解器验证可行性，LLM不直接生成任务列表。

---

## 2. 数据库表设计

> 以下为模拟表结构，后续根据实际业务系统补全。

### 2.1 订单相关

```sql
-- 销售订单主表
CREATE TABLE sales_order (
    order_id VARCHAR(32) PRIMARY KEY COMMENT '订单ID',
    customer_id VARCHAR(32) NOT NULL COMMENT '客户ID',
    order_date DATE NOT NULL COMMENT '下单日期',
    required_date DATE NOT NULL COMMENT '要求交期（订单级）',
    priority TINYINT DEFAULT 2 COMMENT '优先级(1-紧急 2-正常 3-低)',
    status VARCHAR(20) DEFAULT 'pending' COMMENT '状态(pending/confirmed/in_progress/completed)',
    total_amount DECIMAL(15,2) COMMENT '订单金额',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 订单明细表
CREATE TABLE sales_order_item (
    item_id VARCHAR(32) PRIMARY KEY COMMENT '明细ID',
    order_id VARCHAR(32) NOT NULL COMMENT '订单ID',
    product_id VARCHAR(32) NOT NULL COMMENT '产品ID',
    quantity DECIMAL(12,2) NOT NULL COMMENT '需求数量',
    unit_price DECIMAL(12,2) COMMENT '单价',
    due_date DATE COMMENT '明细交期（优先于订单交期）',
    FOREIGN KEY (order_id) REFERENCES sales_order(order_id)
);
```

**交期优先级规则**：
- 明细交期优先于订单交期（更精确）
- 明细交期不能超过订单交期（不能超过客户要求）
- 两者都为空时无法排产，报错

### 2.2 物料相关

```sql
-- 物料主数据
CREATE TABLE material (
    material_id VARCHAR(32) PRIMARY KEY COMMENT '物料ID',
    material_name VARCHAR(100) NOT NULL COMMENT '物料名称',
    material_type VARCHAR(20) COMMENT '类型(raw/semi/finished)',
    unit VARCHAR(10) COMMENT '单位',
    safety_stock DECIMAL(12,2) DEFAULT 0 COMMENT '安全库存',
    lead_time_days INT DEFAULT 0 COMMENT '采购提前期(天)',
    supplier_id VARCHAR(32) COMMENT '默认供应商'
);

-- 库存表
CREATE TABLE inventory (
    inventory_id VARCHAR(32) PRIMARY KEY,
    material_id VARCHAR(32) NOT NULL,
    warehouse_id VARCHAR(32) COMMENT '仓库ID',
    quantity_on_hand DECIMAL(12,2) DEFAULT 0 COMMENT '在库数量',
    quantity_reserved DECIMAL(12,2) DEFAULT 0 COMMENT '已预留数量',
    quantity_on_order DECIMAL(12,2) DEFAULT 0 COMMENT '在途数量',
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (material_id) REFERENCES material(material_id)
);

-- BOM 物料清单
CREATE TABLE bill_of_material (
    bom_id VARCHAR(32) PRIMARY KEY,
    product_id VARCHAR(32) NOT NULL COMMENT '产品ID',
    material_id VARCHAR(32) NOT NULL COMMENT '物料ID',
    quantity_required DECIMAL(12,4) NOT NULL COMMENT '单位用量',
    FOREIGN KEY (material_id) REFERENCES material(material_id)
);
```

### 2.3 产能相关

```sql
-- 产线/工作中心
CREATE TABLE work_center (
    center_id VARCHAR(32) PRIMARY KEY COMMENT '工作中心ID',
    center_name VARCHAR(100) NOT NULL,
    capacity_per_day DECIMAL(10,2) COMMENT '标准日产能基准值（单位：工时）',
    shift_pattern VARCHAR(20) DEFAULT 'single' COMMENT '班次(single/double/triple)',
    efficiency_rate DECIMAL(5,2) DEFAULT 1.0 COMMENT '效率系数',
    is_active BOOLEAN DEFAULT TRUE
);

-- 产能日历
CREATE TABLE capacity_calendar (
    calendar_id VARCHAR(32) PRIMARY KEY,
    center_id VARCHAR(32) NOT NULL,
    work_date DATE NOT NULL,
    available_hours DECIMAL(5,2) COMMENT '当日实际可用工时 = 基准值 × 效率系数 × 日历系数',
    planned_hours DECIMAL(5,2) DEFAULT 0 COMMENT '当日已计划工时',
    status VARCHAR(20) DEFAULT 'available' COMMENT 'available/maintenance/holiday',
    FOREIGN KEY (center_id) REFERENCES work_center(center_id),
    UNIQUE KEY (center_id, work_date)
);
```

**产能计算公式**：
```
available_hours = capacity_per_day × efficiency_rate × calendar_factor

calendar_factor:
- 正常工作日: 1.0
- 半天工作: 0.5
- 节假日/维护: 0.0
```

### 2.4 排产计划相关

```sql
-- 主生产计划
CREATE TABLE production_plan (
    plan_id VARCHAR(32) PRIMARY KEY,
    plan_date DATE NOT NULL COMMENT '计划日期',
    plan_horizon_days INT DEFAULT 30 COMMENT '计划周期(天)',
    status VARCHAR(20) DEFAULT 'draft' COMMENT 'draft/confirmed/executing/completed',
    version INT DEFAULT 1 COMMENT '版本号',
    parent_version INT COMMENT '上一版本',
    created_by VARCHAR(32) COMMENT '创建者(AI/人工)',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 排产任务
CREATE TABLE production_task (
    task_id VARCHAR(32) PRIMARY KEY,
    plan_id VARCHAR(32) NOT NULL,
    plan_version INT NOT NULL COMMENT '计划版本号',
    order_id VARCHAR(32) NOT NULL,
    item_id VARCHAR(32) NOT NULL,
    product_id VARCHAR(32) NOT NULL,
    center_id VARCHAR(32) NOT NULL COMMENT '分配工作中心',
    quantity DECIMAL(12,2) NOT NULL,
    start_time DATETIME NOT NULL COMMENT '计划开始',
    end_time DATETIME NOT NULL COMMENT '计划结束',
    actual_start DATETIME COMMENT '实际开始',
    actual_end DATETIME COMMENT '实际完成',
    status VARCHAR(20) DEFAULT 'planned' COMMENT 'planned/started/paused/completed',
    sequence_num INT COMMENT '加工顺序',
    FOREIGN KEY (plan_id) REFERENCES production_plan(plan_id),
    -- 唯一约束：同一工作中心同一时间只能有一个任务
    UNIQUE KEY uk_center_time (center_id, start_time, end_time),
    -- 检查约束：结束时间必须大于开始时间
    CHECK (end_time > start_time)
);

-- 版本更新策略：新版本全量重建任务，保留历史版本用于对比和回滚

-- 插单记录
CREATE TABLE insert_order_log (
    log_id VARCHAR(32) PRIMARY KEY,
    order_id VARCHAR(32) NOT NULL,
    original_plan_id VARCHAR(32) COMMENT '原计划ID',
    new_plan_id VARCHAR(32) COMMENT '新计划ID',
    disruption_score DECIMAL(5,2) COMMENT '扰动评分(0-100)',
    reschedule_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 插单影响任务关联表（替代JSON存储）
CREATE TABLE insert_order_affected_task (
    id VARCHAR(32) PRIMARY KEY,
    log_id VARCHAR(32) NOT NULL,
    task_id VARCHAR(32) NOT NULL,
    impact_type VARCHAR(20) COMMENT 'delayed/rescheduled/cancelled',
    delay_days INT COMMENT '延迟天数',
    FOREIGN KEY (log_id) REFERENCES insert_order_log(log_id),
    FOREIGN KEY (task_id) REFERENCES production_task(task_id)
);
```

**扰动评分计算公式**：

```python
def calculate_disruption_score(original_plan, new_order, affected_tasks):
    """
    扰动评分计算
    范围: 0-100，越高表示扰动越大
    """
    score = 0
    
    # 1. 受影响任务数量（权重30%）
    task_impact = min(len(affected_tasks) / 10, 1.0) * 30
    score += task_impact
    
    # 2. 延迟天数（权重40%）
    total_delay = sum(t.delay_days for t in affected_tasks)
    delay_impact = min(total_delay / 5, 1.0) * 40
    score += delay_impact
    
    # 3. 交期冲突（权重20%）
    if new_order.required_date < datetime.now() + timedelta(days=3):
        score += 20  # 紧急订单
    
    # 4. 资源冲突（权重10%）
    resource_conflicts = count_resource_conflicts(original_plan, new_order)
    resource_impact = min(resource_conflicts / 5, 1.0) * 10
    score += resource_impact
    
    return min(score, 100)

# 评分等级：
# 0-30: 低扰动，自动接受
# 30-60: 中扰动，人工确认
# 60-100: 高扰动，建议拒绝或大幅调整
```

### 2.5 预警相关

```sql
-- 物料缺料预警
CREATE TABLE shortage_alert (
    alert_id VARCHAR(32) PRIMARY KEY,
    material_id VARCHAR(32) NOT NULL,
    required_date DATE NOT NULL,
    required_quantity DECIMAL(12,2) NOT NULL,
    available_quantity DECIMAL(12,2) NOT NULL,
    shortage_quantity DECIMAL(12,2) NOT NULL,
    risk_level VARCHAR(10) COMMENT 'high/medium/low',
    status VARCHAR(20) DEFAULT 'active' COMMENT 'active/resolved/ignored',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 交付预警
CREATE TABLE delivery_alert (
    alert_id VARCHAR(32) PRIMARY KEY,
    order_id VARCHAR(32) NOT NULL,
    item_id VARCHAR(32) NOT NULL,
    expected_date DATE COMMENT '预期完成',
    required_date DATE NOT NULL COMMENT '要求交期',
    delay_days INT COMMENT '预计延迟天数',
    alert_type VARCHAR(20) COMMENT 'at_risk/delayed/critical',
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 3. LangGraph 图架构设计

### 3.1 整体图结构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Plan Agent State Graph                            │
│                                                                             │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐              │
│  │  input   │───▶│ data     │───▶│ analyze  │───▶│ plan     │              │
│  │  node    │    │  fetch   │    │  (子图)   │    │  (子图)   │              │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘                   │
│       │                                               │                      │
│       │               ┌──────────────────────────────┘                      │
│       ▼               ▼                                                      │
│  ┌──────────┐    ┌──────────┐                                               │
│  │  output  │◀───│ validate │                                               │
│  │  node    │    │  (子图)   │                                               │
│  └──────────┘    └──────────┘                                               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 节点实现策略

采用混合方案，根据节点复杂度选择实现方式：

| 节点 | 实现方式 | 说明 |
|------|----------|------|
| Input Node | 单节点 | LLM解析意图+路由，逻辑简单 |
| Data Fetch Node | 单节点 | 并行调用多个MCP Tools |
| Analyze Node | 子图 | 4个子模块顺序执行，含快速路径 |
| Plan Node | 子图 | 策略选择→OR-Tools求解→任务生成 |
| Validate Node | 子图 | 校验→评估→重试（最多3次） |
| Output Node | 单节点 | 格式化+保存+通知 |

### 3.3 状态定义

```python
from typing import TypedDict, List, Dict, Optional, Annotated
from langgraph.graph import add_messages
from datetime import datetime

class PlanAgentState(TypedDict):
    # 输入
    user_input: str                          # 用户输入/指令
    request_type: str                        # 请求类型: new_plan/insert_order/check_shortage/rebalance
    
    # 数据层
    orders: List[Dict]                       # 订单数据
    materials: List[Dict]                    # 物料数据
    inventory: List[Dict]                    # 库存数据
    capacity: List[Dict]                     # 产能数据
    boms: List[Dict]                         # BOM数据
    
    # 中间状态
    constraints: Dict                        # 约束条件
    conflicts: List[Dict]                    # 冲突列表
    shortage_predictions: List[Dict]         # 缺料预测
    disruption_analysis: Dict                # 扰动分析
    quick_path: bool                         # 是否走快速路径
    
    # 输出
    production_plan: Dict                    # 排产计划
    alerts: List[Dict]                       # 预警列表
    recommendations: List[str]               # 建议列表
    
    # 对话历史
    messages: Annotated[list, add_messages]  # 消息列表
```

### 3.4 节点详细设计

#### Input Node（输入处理）

**职责**：解析用户意图，提取结构化参数，决定路由

| 请求类型 | 触发条件示例 | 提取参数 |
|----------|--------------|----------|
| new_plan | "安排下周计划"、"生成排产方案" | 日期范围、产品范围 |
| insert_order | "插单"、"紧急订单ORD-008" | 订单ID、优先级 |
| check_shortage | "检查物料缺口"、"下周缺料情况" | 日期范围、物料类型 |
| rebalance | "负荷均衡"、"调整产线分配" | 工作中心范围 |
| check_delivery | "交付预警"、"哪些订单会延迟" | 订单范围、时间窗口 |
| query | "当前计划是什么"、"查看产线A排产" | 查询条件 |

#### Data Fetch Node（数据获取）

**职责**：根据请求类型，调用MCP Tools获取业务数据

| 请求类型 | 需要获取的数据 |
|----------|----------------|
| new_plan | 订单 + 物料 + 库存 + 产能 + BOM |
| insert_order | 原计划 + 新订单 + 影响范围数据 |
| check_shortage | BOM + 库存 + 在途 + 订单需求 |
| rebalance | 产能日历 + 当前计划 + 工作中心 |
| check_delivery | 订单 + 当前进度 + 产能 |

#### Analyze Node（分析子图）

**子模块**：

| 子模块 | 输入 | 输出 | 说明 |
|--------|------|------|------|
| 约束提取 | 订单+产能+物料 | constraints | 交期约束、产能上限、物料可用量 |
| 冲突检测 | 订单+产能+约束 | conflicts | 订单间资源竞争、交期重叠 |
| 缺料预判 | BOM+库存+订单需求 | shortage_predictions | 未来时段物料缺口 |
| 负荷分析 | 产能计划+工作中心 | capacity_load | 各中心负荷率 |

**快速路径设计**：

```python
def analyze_node(state):
    # 快速路径判断
    conflicts = detect_conflicts(state)
    shortages = detect_shortages(state)
    
    if not conflicts and not shortages:
        # 无冲突无缺料，跳过详细分析
        state["quick_path"] = True
        state["analysis_summary"] = "无约束冲突，可直接排产"
        return state
    
    # 正常路径：完整分析
    state["quick_path"] = False
    state["constraints"] = extract_constraints(state)
    state["conflicts"] = conflicts
    state["shortage_predictions"] = shortages
    state["capacity_load"] = analyze_capacity(state)
    
    return state
```

```
┌─────────────────────────────────────────────────────────────┐
│                    Analyze Subgraph                          │
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │   extract    │───▶│   detect     │───▶│   predict    │   │
│  │ constraints  │    │  conflicts   │    │   shortage   │   │
│  └──────────────┘    └──────────────┘    └──────────────┘   │
│                                                  │           │
│                                                  ▼           │
│                                          ┌──────────────┐   │
│                                          │  analyze     │   │
│                                          │  capacity    │   │
│                                          └──────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

#### Plan Node（排产子图）

**排产算法分层架构**：

```
┌─────────────────────────────────────────────────────────────┐
│                     Plan Node 分层架构                        │
│                                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Layer 3: LLM 决策层（可选）                            │  │
│  │  - 复杂场景推理（多目标权衡、异常处理策略）              │  │
│  │  - 不直接生成任务，只输出策略参数                        │  │
│  └───────────────────────────────────────────────────────┘  │
│                          ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Layer 2: 约束求解层（核心）- OR-Tools CP-SAT          │  │
│  │  - 保证方案可行性（硬约束不可违反）                     │  │
│  │  - 输出：任务分配、时间安排                             │  │
│  └───────────────────────────────────────────────────────┘  │
│                          ▼                                   │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Layer 1: 规则引擎层（基础）                            │  │
│  │  - 业务规则预处理、优先级排序、约束过滤                 │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**子模块**：

| 子模块 | 功能 | 说明 |
|--------|------|------|
| 策略选择 | 根据场景选择排产策略 | 常规/插单/快速路径 |
| OR-Tools求解 | 约束满足问题求解 | 保证方案可行性 |
| 任务生成 | 生成生产任务列表 | 计算计划指标 |

**快速路径处理**：

```python
def plan_node(state):
    if state.get("quick_path"):
        # 快速路径：使用简化排产逻辑（无冲突场景）
        return quick_schedule(state)
    
    # 正常路径：OR-Tools求解
    return full_schedule_with_ortools(state)
```

#### Validate Node（校验子图）

**校验项**：

| 校验项 | 规则 | 严重程度 |
|--------|------|----------|
| 物料可行性 | 所需物料 <= 可用量 + 在途 | 高 |
| 产能可行性 | 计划工时 <= 可用工时 | 高 |
| 交期满足度 | 计划完成时间 <= 要求交期 | 高 |
| 负荷均衡度 | 各中心负荷偏差 < 阈值 | 中 |
| 时间冲突 | 同一工作中心无时间重叠 | 高 |

**回退策略与重试机制**：

```python
MAX_AUTO_RETRY = 3  # 最大自动重试次数

AUTO_FIX_THRESHOLD = {
    "load_deviation": 0.1,      # 负荷偏差<10%可自动调整
    "material_shortage": 100,   # 物料缺口<100可自动调整
}

def validate_with_retry(state):
    for attempt in range(MAX_AUTO_RETRY):
        result = validate_plan(state)
        
        if result.is_valid:
            return "pass"
        
        if attempt < MAX_AUTO_RETRY - 1 and can_auto_fix(result.conflicts):
            state = auto_adjust(state, result.conflicts)
        else:
            return "alert"  # 转人工
    
    # 兜底：超过最大重试次数
    return "alert"
```

| 冲突级别 | 判定条件 | 处理方式 |
|----------|----------|----------|
| 轻微 | 负荷偏差<10%、物料缺口<100 | 系统自动调整（最多3次） |
| 重大 | 交期延迟>2天、关键物料严重短缺 | 暂停并人工确认 |

#### Output Node（输出节点）

**职责**：格式化结果，保存计划，触发通知

| 输出类型 | 内容 | 渠道 |
|----------|------|------|
| 计划摘要 | 计划概览、关键指标 | 钉钉消息 |
| 预警信息 | 缺料预警、交期预警 | 钉钉消息 |
| 详细计划 | 甘特图数据、任务列表 | Excel导出 |
| 建议操作 | 采购建议、产能调整建议 | 钉钉消息 |

### 3.5 路由设计

```python
def route_by_request_type(state: PlanAgentState) -> str:
    """主路由：根据请求类型分发"""
    request_type = state.get("request_type")
    
    if request_type in ["new_plan", "insert_order", "check_shortage", 
                         "rebalance", "check_delivery"]:
        return "data_fetch"
    elif request_type == "query":
        return "output"  # 查询直接输出
    else:
        return "input"  # 重新解析

def check_validation_result(state: PlanAgentState) -> str:
    """校验后路由"""
    conflicts = state.get("conflicts", [])
    
    if not conflicts:
        return "pass"
    
    high_severity = [c for c in conflicts if c["severity"] == "high"]
    
    if high_severity:
        return "alert"  # 重大冲突，人工确认
    else:
        return "pass"   # 轻微问题，忽略
```

### 3.6 异常处理

| 异常类型 | 处理策略 |
|----------|----------|
| 数据获取失败 | 重试3次 → 报错终止 |
| OR-Tools求解超时 | 降级为规则引擎排产 + 告警 |
| 校验循环（3次重试仍失败） | 转人工确认 |
| MCP服务不可用 | 使用缓存数据 + 告警 |
| LLM调用失败 | 重试 → 使用规则引擎兜底 |

---

## 4. 钉钉交互设计

### 4.1 交互模式

| 模式 | 定位 | 说明 |
|------|------|------|
| 自然语言 | **主交互** | 用户直接描述需求，LLM解析意图 |
| 卡片交互 | **确认与操作** | 推荐操作按钮、预警确认、方案选择 |
| 推荐命令 | **引导输入** | 输入框提示示例，降低使用门槛 |

**输入框提示示例**：
```
试试这样说：
• 安排下周的生产计划
• 检查未来三天的物料缺口
• 订单ORD-008紧急，帮我插单
```

### 4.2 消息格式设计

#### 排产结果推送

```
📋 排产计划生成完成

📅 计划周期: 2026-08-08 ~ 2026-08-14
📦 订单数量: 25个
🔧 生产任务: 42个
✅ 预计完成率: 92%

⚠️ 风险提醒:
- 订单ORD-008预计延迟2天
- 物料M-023库存不足

[查看详细] [导出Excel] [确认执行]
```

#### 预警消息

```
🚨 物料缺料预警

物料: M-023 (螺丝M6x20)
需求日期: 2026-08-10
需求数量: 5000
当前库存: 1200
缺口数量: 3800

建议: 请联系供应商加急采购

[创建采购单] [忽略] [稍后处理]
```

#### 交互卡片（插单确认）

```
📥 新订单插入请求

订单: ORD-008
客户: ABC公司
产品: 产品A
数量: 1000
交期: 2026-08-12

📊 影响分析:
- 受影响任务: 3个
- 预计扰动评分: 65分（高扰动）
- 其他订单延迟风险: 低

请选择处理方式:
[按方案A执行] [按方案B执行] [人工调整] [拒绝插单]
```

### 4.3 交互流程示例

```
用户: 安排下周计划

Bot: 🔄 正在生成排产计划...
     ├─ 已获取订单: 25个
     ├─ 已获取物料数据
     └─ 已获取产能数据

Bot: ⚠️ 检测到以下约束:
     ├─ 物料缺口: 3种
     ├─ 产能紧张: WC-001
     └─ 交期压力: 5个订单

Bot: ✅ 计划生成完成 [卡片消息]

用户: [点击"确认执行"]

Bot: 📢 计划已确认，已推送至MES系统
```

### 4.4 甘特图展示方案

| 阶段 | 方案 | 说明 |
|------|------|------|
| 第一期 | 图片 + Excel | 钉钉推送简化甘特图，支持导出Excel |
| 后期 | Web交互组件 | ECharts/Gantt实现缩放、筛选、拖拽 |

---

## 5. RAG 知识库设计

### 5.1 知识库内容分类

| 分类 | 内容示例 | 来源 | 更新频率 |
|------|----------|------|----------|
| 排产规则 | 优先级规则、约束规则、分配策略 | manual | 低（规则变更时） |
| 历史计划 | 成功排产方案、优化案例 | auto | 每日自动 |
| 物料知识 | 替代物料、供应商交期、采购周期 | system | 中（供应商变更时） |
| 异常案例 | 插单处理、设备故障、缺料应对 | manual | 中（发生时记录） |
| 最佳实践 | 负荷均衡策略、交期谈判技巧 | manual | 低 |

**来源说明**：

| 来源 | 含义 | 入库方式 | 维护责任人 |
|------|------|----------|------------|
| manual | 人工编写 | Excel导入或对话录入 | 业务专家、系统管理员 |
| auto | 自动生成 | 排产完成后自动触发 | 系统自动 |
| system | 系统同步 | 定时任务或事件触发 | 系统自动 |

### 5.2 Milvus Collection 设计

```python
# Collection: production_knowledge
{
    "collection_name": "production_knowledge",
    "fields": [
        {"name": "id", "type": "VARCHAR", "is_primary": True},
        {"name": "content", "type": "VARCHAR"},           # 文本内容
        {"name": "embedding", "type": "FLOAT_VECTOR", "dim": 1536},  # 向量
        {"name": "category", "type": "VARCHAR"},           # 分类
        {"name": "metadata", "type": "JSON"},              # 元数据
        {"name": "created_at", "type": "INT64"},           # 创建时间戳
        {"name": "source", "type": "VARCHAR"}              # 来源
    ]
}
```

### 5.3 检索策略

```python
def retrieve_knowledge(query: str, category: str = None, top_k: int = 5) -> List[Dict]:
    """检索相关知识"""
    filter_expr = f'category == "{category}"' if category else None
    
    results = collection.search(
        data=[embedding],
        anns_field="embedding",
        param={"metric_type": "COSINE", "params": {"nprobe": 16}},
        limit=top_k,
        expr=filter_expr,
        output_fields=["content", "category", "metadata", "source"]
    )
    
    return results
```

### 5.4 RAG 集成到节点

| 节点 | RAG用途 | 检索时机 |
|------|---------|----------|
| Analyze Node | 检索相关约束规则 | 分析前 |
| Plan Node | 检索历史排产方案、排产规则 | 排产决策时 |
| Validate Node | 检索异常案例 | 校验失败时 |
| Output Node | 检索最佳实践生成建议 | 生成建议时 |

### 5.5 知识库管理方案

**第一期功能**：

| 功能 | 方式 | 优先级 |
|------|------|--------|
| 批量初始化 | Excel模板导入（通过钉钉上传） | P0 |
| 零散补充 | 对话录入 | P1 |
| 查看/搜索 | 对话查询 | P1 |
| 编辑/删除 | 对话指令 | P2 |
| Web管理界面 | 后续扩展 | P3 |

**Excel模板设计**：

| Sheet | 字段 |
|-------|------|
| 排产规则 | 规则名称、规则内容、适用产品、优先级 |
| 物料知识 | 物料ID、知识类型、内容、来源 |
| 产线约束 | 产线ID、约束类型、约束内容、备注 |

**知识库更新频率**：定时任务（每日凌晨）+ 手动触发

---

## 6. MCP Server 设计

### 6.1 外部系统对接方案

- 对接方式: MCP协议
- MCP Server: 在Agent项目中封装，包装现有系统API为MCP Tools
- 架构: Agent ←MCP→ MCP Server ←API→ ERP/MES/WMS等
- 优势: 解耦、可复用、易维护

### 6.2 外部系统优先级

| 系统 | 提供能力 | 优先级 |
|------|----------|--------|
| ERP | 订单数据、客户数据 | P0 |
| WMS | 库存数据、在途物料 | P0 |
| MES | 生产进度、实际工时 | P1 |
| 采购系统 | 采购订单、供应商交期 | P1 |
| 人力资源 | 人员排班、可用工时 | P2 |

### 6.3 MCP Tools 清单

#### 订单相关

| Tool名称 | 功能 | 参数 | 返回值 |
|----------|------|------|--------|
| fetch_orders | 获取订单列表 | status, date_range, priority | 订单列表 |
| fetch_order_detail | 获取订单详情 | order_id | 订单+明细 |
| update_order_status | 更新订单状态 | order_id, status | 操作结果 |

#### 物料相关

| Tool名称 | 功能 | 参数 | 返回值 |
|----------|------|------|--------|
| fetch_materials | 获取物料主数据 | material_type, keyword | 物料列表 |
| fetch_inventory | 查询库存 | material_id, warehouse_id | 库存信息 |
| fetch_bom | 获取BOM | product_id | BOM树 |
| check_material_availability | 检查物料可用性 | material_id, quantity, date | 可用/缺口数量 |

#### 产能相关

| Tool名称 | 功能 | 参数 | 返回值 |
|----------|------|------|--------|
| fetch_work_centers | 获取工作中心列表 | is_active | 工作中心列表 |
| fetch_capacity_calendar | 查询产能日历 | center_id, date_range | 可用工时/已计划工时 |
| update_capacity_plan | 更新产能计划 | center_id, date, planned_hours | 操作结果 |

#### 计划相关

| Tool名称 | 功能 | 参数 | 返回值 |
|----------|------|------|--------|
| save_production_plan | 保存排产计划 | plan_data | plan_id |
| fetch_production_plan | 获取计划详情 | plan_id, version | 计划数据 |
| create_production_task | 创建生产任务 | task_data | task_id |
| fetch_tasks_by_plan | 获取计划下任务 | plan_id | 任务列表 |

#### 预警相关

| Tool名称 | 功能 | 参数 | 返回值 |
|----------|------|------|--------|
| create_alert | 创建预警 | alert_type, alert_data | alert_id |
| fetch_active_alerts | 获取活跃预警 | alert_type, severity | 预警列表 |
| resolve_alert | 关闭预警 | alert_id, resolution | 操作结果 |

### 6.4 MCP Server 结构

```
mcp_servers/
├── erp_server/              # ERP系统
│   ├── server.py
│   ├── tools/
│   │   ├── orders.py
│   │   └── customers.py
│   └── config.yaml
├── wms_server/              # WMS系统
│   ├── server.py
│   ├── tools/
│   │   ├── inventory.py
│   │   └── materials.py
│   └── config.yaml
├── mes_server/              # MES系统
│   ├── server.py
│   ├── tools/
│   │   ├── production.py
│   │   └── capacity.py
│   └── config.yaml
└── shared/                  # 共享组件
    ├── auth.py
    └── utils.py
```

---

## 7. 持久化设计

### 7.1 状态持久化

```python
from langgraph.checkpoint.postgres import PostgresSaver

# PostgreSQL 作为Checkpoint存储（开发+生产统一）
checkpointer = PostgresSaver.from_conn_string(
    "postgresql://user:pass@localhost:5432/plan_agent"
)

# 编译图时绑定
app = graph.compile(checkpointer=checkpointer)
```

### 7.2 持久化内容

| 内容 | 存储位置 | 保留策略 | 说明 |
|------|----------|----------|------|
| 对话历史 | PostgreSQL（Checkpoint） | 保留90天，超过归档 | 支持断点续传、回溯 |
| 排产计划 | PostgreSQL（业务表） | 长期保留，版本管理 | 核心业务数据 |
| 生产任务 | PostgreSQL（业务表） | 长期保留 | 计划执行记录 |
| 预警记录 | PostgreSQL（业务表） | 90天 | 可归档 |
| 知识库向量 | Milvus | 持久化 | RAG检索用 |
| 实时状态 | Redis | 临时（会话期间） | 缓存热点数据 |
| 异步任务 | Redis（Celery Broker） | 任务完成后清理 | 任务队列 |

### 7.3 数据清理策略

| 数据类型 | 清理规则 | 触发方式 |
|----------|----------|----------|
| 对话历史 | 保留90天，超过归档 | 定时任务（每周） |
| 预警记录 | 保留90天，超过归档 | 定时任务（每月） |
| Redis缓存 | 会话结束24小时后过期 | 自动过期 |
| Celery任务 | 任务完成24小时后清理 | 自动过期 |
| 日志文件 | 保留180天 | 定时任务（每月） |

> 注：以上时间周期为初始设定，后期可根据实际使用情况调整。

### 7.4 版本管理

- 每次调整生成新版本，保留完整历史记录
- 保留规则：最近30天保留所有版本，30天后仅保留每日最后一个版本
- 支持手动标记"重要版本"永久保留
- 支持版本对比和快速回滚
- 新版本全量重建任务，保留历史版本用于对比

---

## 8. 项目目录结构

```
plan_agent/
├── plan.md                          # 技术实现方案（本文档）
│
├── src/
│   ├── __init__.py
│   ├── main.py                      # 入口，启动Agent服务
│   │
│   ├── graph/                       # LangGraph图定义
│   │   ├── __init__.py
│   │   ├── builder.py               # 主图构建
│   │   ├── state.py                 # 状态定义
│   │   └── nodes/                   # 节点实现
│   │       ├── __init__.py
│   │       ├── input_node.py        # 单节点
│   │       ├── data_fetch_node.py   # 单节点
│   │       ├── analyze/             # 子图
│   │       │   ├── __init__.py
│   │       │   ├── graph.py
│   │       │   ├── constraints.py
│   │       │   ├── conflicts.py
│   │       │   ├── shortage.py
│   │       │   └── capacity.py
│   │       ├── plan/                # 子图
│   │       │   ├── __init__.py
│   │       │   ├── graph.py
│   │       │   ├── strategy.py
│   │       │   ├── ortools_solver.py  # OR-Tools求解器
│   │       │   └── generate.py
│   │       ├── validate/            # 子图
│   │       │   ├── __init__.py
│   │       │   ├── graph.py
│   │       │   ├── checker.py
│   │       │   └── retry.py
│   │       └── output_node.py       # 单节点
│   │
│   ├── tools/                       # MCP Tools
│   │   ├── __init__.py
│   │   ├── mcp_tools.py
│   │   └── db_tools.py
│   │
│   ├── rag/                         # RAG模块
│   │   ├── __init__.py
│   │   ├── retriever.py             # Milvus检索器
│   │   ├── embedder.py              # 向量化
│   │   └── knowledge_manager.py     # 知识管理
│   │
│   ├── models/                      # 数据模型
│   │   ├── __init__.py
│   │   ├── schemas.py               # Pydantic模型
│   │   └── database.py              # ORM模型
│   │
│   ├── integrations/                # 外部集成
│   │   ├── __init__.py
│   │   ├── dingtalk.py              # 钉钉机器人
│   │   └── excel.py                 # Excel导入导出
│   │
│   ├── tasks/                       # Celery异步任务
│   │   ├── __init__.py
│   │   ├── celery_app.py            # Celery配置
│   │   └── scheduling_tasks.py      # 排产相关任务
│   │
│   └── config/
│       ├── __init__.py
│       └── settings.py
│
├── mcp_servers/                     # MCP Server实现
│   ├── erp_server/
│   │   ├── server.py
│   │   ├── tools/
│   │   │   ├── orders.py
│   │   │   └── customers.py
│   │   └── config.yaml
│   ├── wms_server/
│   │   ├── server.py
│   │   ├── tools/
│   │   │   ├── inventory.py
│   │   │   └── materials.py
│   │   └── config.yaml
│   └── mes_server/
│       ├── server.py
│       ├── tools/
│       │   ├── production.py
│       │   └── capacity.py
│       └── config.yaml
│
├── database/
│   ├── migrations/                  # 数据库迁移脚本
│   └── seeds/                       # 测试数据
│
├── templates/                       # Excel知识模板
│   ├── 排产规则模板.xlsx
│   ├── 物料知识模板.xlsx
│   └── 产线约束模板.xlsx
│
├── tests/
│   ├── test_graph.py
│   ├── test_nodes.py
│   ├── test_tools.py
│   ├── test_rag.py
│   └── test_ortools.py              # OR-Tools求解器测试
│
├── requirements.txt
├── docker-compose.yml               # 依赖服务（PG、Redis、Milvus）
└── README.md
```

---

## 9. 待定事项

| 事项 | 说明 | 计划时间 |
|------|------|----------|
| 权限控制 | 后续对接现有系统或自建权限网关 | 第二期 |
| Web管理界面 | 后期根据需求扩展 | 第二期 |
| 甘特图交互组件 | ECharts/Gantt实现 | 第二期 |

---

## 10. 下一步计划

1. **环境搭建** — Docker Compose部署PG、Redis、Milvus
2. **数据库初始化** — 执行建表脚本，导入测试数据
3. **核心图实现** — 先跑通最小可用流程（new_plan场景）
4. **OR-Tools求解器** — 实现约束求解核心逻辑
5. **MCP Server开发** — 优先实现ERP、WMS的MCP Tools
6. **钉钉机器人** — 实现基础交互和消息推送
7. **RAG知识库** — 构建排产知识库，实现Excel导入
8. **Celery任务** — 实现异步任务处理
9. **测试优化** — 边界测试、性能优化

---

## 附录A：问题修复记录

| 问题 | 修复方案 | 版本 |
|------|----------|------|
| 排产算法内核未定义 | 明确使用OR-Tools CP-SAT求解器，LLM只做策略建议 | v1.1 |
| production_task缺少唯一约束 | 添加uk_center_time唯一约束（仅防完全重复） | v1.1 |
| production_task未关联version | 添加plan_version字段 | v1.1 |
| capacity_calendar语义重叠 | 明确计算公式和字段含义 | v1.1 |
| 交期优先级未定义 | 明确明细交期优先于订单交期 | v1.1 |
| affected_tasks用JSON存储 | 改为关联表insert_order_affected_task | v1.1 |
| MQ与MCP混淆 | 引入Celery处理异步任务 | v1.1 |
| 扰动评分无计算方法 | 定义评分公式和等级 | v1.1 |
| Validate重试次数未定义 | 定义MAX_AUTO_RETRY=3 | v1.1 |
| 无快速路径设计 | 添加quick_path判断逻辑 | v1.1 |

## 附录B：技术债务清单

| 编号 | 问题描述 | 当前状态 | 风险等级 | 建议解决方案 | 计划版本 |
|------|----------|----------|----------|--------------|----------|
| TD-001 | DB层无法防止时间段重叠冲突 | **已关闭**（v1.2 落地 EXCLUDE 约束，v1.3 migration 003 收窄至计划内、004 删除遗留 uk_center_time） | 已解决 | 已采用方案B: PG EXCLUDE USING gist + tsrange | v1.3 |
| TD-002 | 单位工时硬编码 0.5h/件，无工艺路线与换型时间 | **已关闭**（v1.4 引入 routing 表，按产品×中心差异化工时） | 已解决 | 已引入 routing 表 | v1.4 |
| TD-003 | 班次模型简化为每天 08:00 连续生产 | 阻塞中，等待班次主数据 | 中 | 引入 shift 表，改造 calendar_service._build_slots | 待定 |
| TD-004 | 产能回写为覆盖式，多计划并存时会互相覆盖 | 单计划场景可接受 | 中 | 引入 capacity_allocation 明细表按 plan_id 汇总 | 待定 |

### TD-001 详细说明

**问题**：

uk_center_time (center_id, start_time, end_time) 唯一约束只能防止完全相同的时间段重复分配，无法防止时间段重叠。

`
示例：
任务A: center_id=WC-001, start_time=09:00, end_time=11:00  ✓
任务B: center_id=WC-001, start_time=09:30, end_time=10:30  ✓ (约束不拦截，但实际冲突)
`

**当前防线**：
- OR-Tools 求解器的 NoOverlap 约束（核心能力，正常流程不会产生冲突）
- 正常链路：求解器生成 → 写入DB，不会有问题

**风险场景**：
- 人工调整任务
- 脚本补数据
- 绕过求解器直接写库的路径

**建议解决方案**：

`sql
-- 方案B: PostgreSQL EXCLUDE 约束（推荐，DB层兜底）
-- 需要安装 btree_gist 扩展
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE production_task ADD CONSTRAINT EXCLUDE_center_time_overlap
    EXCLUDE USING gist (
        center_id WITH =,
        tsrange(start_time, end_time) WITH &&
    );
`

**优先级**：POC阶段不处理，正式排期时实现。

---

*文档版本: v1.1*
*最后更新: 2026-08-07*
*修复了v1.0中的架构级和数据模型问题*
---

## 附录C：部署实施记录（v1.2）

### C.1 基础设施实际部署

| 组件 | 地址 | 版本 | 状态 |
|------|------|------|------|
| PostgreSQL | 10.100.26.200:5432 | 18.4 | 已部署并初始化 |
| Redis | 10.100.26.200:6379 | 7.2.10 | 已验证 |
| Milvus | 10.100.26.200:19530 | standalone | 已验证 |

说明：开发环境改为独立虚拟机集中部署，不再用 docker-compose 在本机拉起依赖。
docker-compose.yml 保留作为备用方案，用于新环境快速搭建。

### C.2 与原方案的偏差修正

| 编号 | 原方案 | 实际实施 | 原因 |
|------|--------|----------|------|
| D-001 | database/init.sql 单文件建表 | 拆分为 database/migrations/001_init_postgres.sql | 原文件是 MySQL 语法（TINYINT、内联 COMMENT、ON UPDATE CURRENT_TIMESTAMP），PostgreSQL 无法执行；原文件已重命名为 init.sql.legacy_mysql 留档 |
| D-002 | TD-001 延后到 v2.0 | 本次已落地 EXCLUDE 约束 | PG 环境已就绪，btree_gist 开箱可用，实现成本极低，无需承担该技术债 |
| D-003 | insert_order_log.affected_tasks 用 JSON 字段 | 改为关联表 insert_order_affected_task | 与附录A 定稿保持一致（原 init.sql 未同步该修复） |
| D-004 | Milvus 集合名 plan_agent_vectors | plan_agent_knowledge | 与 rag_service.py 中的默认集合名统一 |
| D-005 | 未定义向量维度 | 1024（对齐千问 text-embedding-v3） | 需与实际 embedding 模型一致，换模型需重建集合 |

### C.3 表结构增强（相对第2章）

在 PG 脚本中额外补充了第2章未明确的工程约束：

- CHECK 约束：priority BETWEEN 1 AND 3、status 枚举校验、数量非负、end_time > start_time
- EXCLUDE 约束：excl_center_time_overlap 防止同一工作中心时间段重叠（TD-001 兜底）
- updated_at 触发器：trg_set_updated_at() 替代 MySQL 的 ON UPDATE CURRENT_TIMESTAMP
- 唯一约束：uk_inventory_mat_wh、uk_bom_product_material、uk_capacity_center_date
- 级联删除：订单明细、计划任务、插单影响任务随主表级联删除

实际建成 13 张表，30 个 CHECK 约束，2 个 updated_at 触发器。

### C.4 TD-001 状态更新：已关闭

TD-001 已通过 PostgreSQL EXCLUDE 约束关闭（附录B 中风险等级由「中」降为「已解决」）：

    CREATE EXTENSION IF NOT EXISTS btree_gist;
    ALTER TABLE production_task ADD CONSTRAINT excl_center_time_overlap
        EXCLUDE USING gist (center_id WITH =, tsrange(start_time, end_time) WITH &&);

实测验证 5 个场景全部符合预期：

| 场景 | 预期 | 实测结果 |
|------|------|----------|
| WC-001 09:30-10:30 插入 09:00-11:00 之中（部分重叠） | 拦截 | ExclusionViolation |
| 完全相同时间段 | 拦截 | UniqueViolation |
| 11:00-12:00 相邻不重叠 | 允许 | 正常插入 |
| 不同工作中心、同一时间 | 允许 | 正常插入 |
| end_time < start_time | 拦截 | CheckViolation |

现在 DB 层可拦截人工调整、脚本补数、绕过求解器直接写库等场景产生的时间段重叠。

### C.5 初始化入口

统一入口 scripts/init_infra.py：

| 命令 | 作用 |
|------|------|
| check | 连通性检查（PG / Redis / Milvus，Milvus 含 3 次重试） |
| migrate | CREATE DATABASE + 执行 migrations 下全部脚本 |
| seed | 执行 seeds 下全部脚本 |
| milvus | 创建知识库集合与 HNSW 索引 |
| all | 全部执行并校验 |
| verify | 仅输出表清单与行数 |

全部脚本均为幂等设计，可重复执行。

### C.6 Milvus 集合结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(64) | 主键，文档ID（内容 MD5 前16位） |
| vector | FLOAT_VECTOR | 维度 1024 |
| content | VARCHAR(8192) | 原文内容 |
| doc_type | VARCHAR(64) | 文档类型（scheduling_rule / material_rule / capacity_rule 等） |
| source | VARCHAR(512) | 来源（文件名等） |

索引：vector 字段上 HNSW（M=16, efConstruction=200），度量方式 COSINE。

### C.7 已知环境问题

- 虚拟机网络存在偶发抖动，TCP 端口探测会出现假失败（实测 pymilvus 连接成功率 5/5，
  但 Test-NetConnection 6 次中有 2 次返回 False）。init_infra.py 已加入重试规避。
- Milvus 中存在 fruit / love 两个历史测试集合，与本项目无关。
- .env 含明文数据库密码，已在 .gitignore 中忽略；生产环境须改用密钥管理服务。

*文档版本: v1.2*

---

## 附录D：排产内核重构记录（v1.3）

> 本附录记录 v1.3 对排产内核的重构。核心是修正一个会让计划**不可执行**的建模缺陷，
> 并完成排产结果落库闭环。

### D.1 根本问题：时间轴模型错误

初版 `ortools_service` 把任务建模为**自然时间**上的连续区间：

```
200 件 x 0.5h/件 = 100 小时  ->  一段连续 100 小时的区间
```

自然时间连续意味着这 100 小时跨越夜间与周末。求解器给出的 `start_time ~ end_time`
横跨 4 个自然日不间断生产，而工厂每天只有 15.2 小时可用产能（16h x 0.95 效率）。
**排出的计划在物理上无法执行**，且 `capacity_per_day` 实际上从未真正约束任何任务。

同时存在三个衍生问题：

| 问题 | 后果 |
|------|------|
| 交期为硬约束 | 排不下时返回 `INFEASIBLE`，无法告知「哪个订单排不下、差多少」 |
| 工时重复估算 | `data_fetch_node` 与 `prepare_solver_inputs` 各算一次 `quantity * 0.5`，口径易漂移 |
| 纯日期交期 | `2026-08-20` 被解析为 `00:00`，等于把交期提前了一整天 |

### D.2 解决方案：工作分钟轴（Work-Minute Axis）

新增 `src/services/calendar_service.py`，为每个工作中心依据 `capacity_calendar`
建立一条只包含可生产时间的坐标轴：

```
自然时间    08:00-23:12(15.2h) | 夜间/周末(0h) | 次日 08:00-23:12
工作分钟轴  0 ------------ 912 |    (不存在)   | 912 --------- 1824
```

求解只在该轴上进行，**非工作时间在模型中根本不存在**，因此不可能被排入。
求解完成后用 `WorkCalendar.split()` 把区间还原为若干段真实时间，跨天任务自动分段。

实测：100 小时任务 -> 7 个生产时段，`08-10 08:00 ~ 08-18 16:48`，
自动跳过 8/15、8/16 周末，每段均落在 08:00 之后且不跨天。

### D.3 求解器其余变更

| 变更 | 说明 |
|------|------|
| 交期改软约束 | 超期体现为 `tardiness_hours` 惩罚，可解释而非 INFEASIBLE |
| 新增未排产出口 | 产能绝对不足时任务进入 `unscheduled_tasks` 并附原因，求解器总能返回可行解 |
| 可选区间 + NoOverlap | 替代初版两两布尔配对，模型更简洁、求解更快 |
| 固定占用区间 | 插单/重平衡时把库中既有任务作为不可侵占区间，保护已确认计划 |
| 工时口径统一 | 复用 `item["duration_hours"]`，仅在缺失时兜底估算 |
| 交期归一 | 纯日期交期按当日 23:59 处理 |

目标函数量级：`未排产惩罚(1e7) >> 加权超期(100 x 优先级) >> 完工时间/负荷均衡`。

### D.4 数据库变更

**migration 002**：`production_task` 增加分段字段

跨天任务一段一行（`task_id` 形如 `TASK-001-S0-9F3A`），原因是原表存在排他约束
`EXCLUDE (center_id WITH =, tsrange(start_time,end_time) WITH &&)`。
若把跨天任务写成 `[首段开始, 末段结束]` 单行区间，该区间会覆盖夜间与周末，
与同中心其他任务**必然误判重叠**。一段一行使排他约束天然成立，甘特图也能精确渲染。

新增 `plan_run_log` 表记录每次求解的策略、状态、规模与耗时，用于追溯决策依据。

**migration 003**：排他约束范围收窄至计划内

原全局约束不含 `plan_id`，导致两个计划无法共存。这与 schema 自身矛盾——
`insert_order_log` 同时保留 `original_plan_id` 与 `new_plan_id`，本就要求两版并存。
现改为 `(plan_id, plan_version, center_id, tsrange)` 排他：
计划内不得重叠，跨计划允许（属不同候选方案）。
「同一时刻仅一版生效」由 `production_plan.uk_plan_active` 部分唯一索引保证。

**migration 004**：删除遗留索引 `uk_center_time`，**TD-001 正式关闭**

该唯一索引是 001 中的临时兜底（其注释已自述「无法防止重叠」），
现被 003 的排他约束完全覆盖；且因全局唯一而阻碍多方案并存，故删除。
001 中对应位置已加注释说明，防止重建库时被引回。

### D.5 落库闭环

新增 `src/services/plan_writer.py`，单事务写入计划头、任务段、产能回写：

- `planned_hours` 按段所属日期累加回写，实测从不超过当日 `available_hours`
- 段主键由 `plan_id + version` 派生**确定性**短标签（blake2s），
  既保证跨计划唯一，又使同计划重复保存幂等（不用随机值即为此）
- `ExclusionViolation` / `ForeignKeyViolation` 转为可读业务错误
- 计划默认写入 `draft` 态，需人工确认后转 `confirmed`

### D.6 一个隐蔽缺陷及修复

`plan_run_log.plan_id` 有外键约束。当**全部任务都未排产**时计划头不会写入，
但 `plan_id` 仍被带入审计日志 -> `ForeignKeyViolation` -> **审计静默丢失**。
而「为什么这次排不出来」恰恰是最需要留痕的信息。

修复：计划未落库时将审计的 `plan_id` 置空。已加回归测试
`test_run_log_survives_unpersisted_plan` 覆盖。

### D.7 测试覆盖

| 测试文件 | 项数 | 覆盖重点 |
|----------|------|----------|
| tests/test_calendar.py | 6 | 双向映射互逆、跨天拆段、当天剩余产能、停机产线排除 |
| tests/test_ortools.py | 8 | 不排入非工作时间、资源独占、未排产可解释、超期软约束、固定占用 |
| tests/test_plan_writer.py | 9 | 分段落库、产能回写、幂等、多计划并存、计划内重叠拦截 |
| tests/test_graph.py | 3 | 状态与图结构 |
| tests/test_integration.py | 7 | 真实 PG 全链路、输出与库内一致、审计留痕 |

合计 33 项全部通过。

### D.8 仍未解决（需业务数据支撑）

| 事项 | 现状 | 阻塞点 |
|------|------|--------|
| 单位工时 0.5h/件 | 硬编码假设 | 缺工艺路线表（routing），需每产品每工序标准工时 |
| 换型时间 setup time | 未建模 | 同上 |
| 班次模型 | 假设每天 08:00 连续生产 | 缺班次主数据，未支持多班制、午休、跨零点夜班 |
| 多计划并存的产能回写 | 覆盖式写入，仅适用单计划 | 需 capacity_allocation 明细表按 plan_id 汇总 |
| 策略选择 | 规则式 | 未接 LLM 给出策略与权重建议 |

*文档版本: v1.3*

### D.9 工艺路线接入（v1.4）

**migration 005** 新建 `routing` 表：

```sql
CREATE TABLE routing (
    routing_id         VARCHAR(32) PRIMARY KEY,
    product_id         VARCHAR(32) NOT NULL,
    center_id          VARCHAR(32) NOT NULL REFERENCES work_center(center_id),
    hours_per_unit     NUMERIC(10,6) NOT NULL CHECK (hours_per_unit > 0),
    setup_time_minutes INTEGER NOT NULL DEFAULT 0,
    status             VARCHAR(20) NOT NULL DEFAULT 'active',
    CONSTRAINT uk_routing_product_center UNIQUE (product_id, center_id)
);
```

种子数据（模拟值）：

| 产品 | 中心 | 工时/件 |
|------|------|---------|
| PROD-001 | WC-001 | 0.1h |
| PROD-001 | WC-002 | 0.2h |
| PROD-002 | WC-001 | 0.3h |
| PROD-002 | WC-002 | 0.4h |

代码变更：
- `TaskInput` 新增 `per_center_hours` 字段，求解器为每个（任务,中心）组合用各自工时建 interval
- `data_fetch_node` 从 routing 表加载工时，替换硬编码
- `prepare_solver_inputs` 按 routing 设置 `allowed_centers` 和 `per_center_hours`
- 无 routing 数据时降级到 0.5h/件 并打 warning

效果：总工时从 195h 降至 66h，求解器把 PROD-001 全部分配到更快的 WC-001。

测试：新增 `tests/test_routing.py`（5/5），全套 38/38 通过。

*文档版本: v1.4*

### D.10 前端甘特图可视化（v1.5）

**新增文件**：`static/gantt.html` - 单文件甘特图页面

**后端变更**：
- `GET /api/work-centers`：返回工作中心列表（ID、中文名、产能、效率、状态）
- `GET /gantt`：返回甘特图 HTML 页面（FastAPI 托管静态文件）
- `_plan_with_tasks` SQL 补充 `setup_time_minutes` 字段

**前端功能**：
- 按工作中心分行，时间轴横向展开
- 任务条按产品着色，换型时间 badge 标注
- 交期红色竖线、当前时间蓝色竖线、周末灰色背景
- 鼠标悬停 tooltip 显示完整段信息
- 生成新计划（调 `/api/scheduling/sync`）、刷新计划、确认下发
- 顶部状态卡片：计划编号、任务数、总工时、求解状态

**技术决策**：
- 纯 HTML+JS+CSS 单文件，零框架依赖
- 不用第三方甘特图库，自绘方案
- 由 FastAPI 直接托管，无需独立前端服务

*文档版本: v1.5*


### D.11 前端多页面 + 甘特图增强（v1.6）

**甘特图增强**：
- 计划下拉选择器（切换不同计划对比）
- 缩放控制（按钮 + Ctrl+滚轮）
- 鼠标拖拽滚动时间轴
- 图例栏

**新增页面**：
| 页面 | 路径 | 说明 |
|------|------|------|
| 导航首页 | `/` | 系统概览 + 6 个功能入口 |
| 产能负荷 | `/capacity` | 负荷率热力图 + 明细表 |
| 预警中心 | `/alerts` | 缺料 + 交付延误 Tab 切换 |
| 插单管理 | `/insert-orders` | 插单历史 + 受影响任务展开 |
| 物料库存 | `/materials` | 库存水位 + 安全库存预警 |
| 审计日志 | `/run-logs` | 排产运行记录 |

**新增后端 API**：
- `GET /api/capacity/load` - 产能负荷（含负荷率计算）
- `GET /api/alerts/shortage` - 缺料预警列表
- `GET /api/alerts/delivery` - 交付延误列表
- `GET /api/insert-orders` - 插单历史（含受影响任务）
- `GET /api/plans` - 计划列表
- `GET /api/materials` - 物料库存概览
- `GET /api/run-logs` - 排产审计日志

*文档版本: v1.6*


### D.12 多计划产能回写 + LLM 策略增强（v1.7）

**多计划产能回写**：
- 新建 `capacity_allocation` 表（migration 008）
- `_update_planned_hours` 改为增量写入：先删该 plan_id 的旧 allocation，再写新 allocation，最后从全表汇总重算 `capacity_calendar.planned_hours`
- 多计划并存时互不覆盖

**LLM 策略增强**：
- `select_strategy` 改为三层：规则式基础 -> LLM 增强（可调策略和权重） -> RAG 知识补充
- LLM 可确认或推翻规则式选择，可调整权重参数
- MockLLM（内部任务）跳过 LLM 策略建议

*文档版本: v1.7*
