# 智能排产计划Agent

基于LangGraph和OR-Tools的智能排产系统，通过LLM进行意图解析和策略建议，使用约束求解器生成最优排产方案。

## 功能特性

- **订单整合**：汇总订单、物料、产能、交期约束，生成主生产计划
- **滚动排产**：基于时间窗口自动滚动，动态调整排产计划
- **物料缺料预判**：预测未来时段物料缺口，提前预警
- **插单扰动重排**：新订单插入时，最小化扰动重排现有计划
- **产能负荷均衡**：平衡各产线/工位负荷，避免瓶颈
- **交付达成预警**：实时监控交付进度，偏差预警

## 技术栈

| 组件 | 技术选型 | 说明 |
|------|----------|------|
| Agent框架 | LangGraph | 状态图驱动，支持复杂工作流编排 |
| LLM | DeepSeek (deepseek-v4-pro) | 推理与决策引擎，支持 langchain_openai 接口 |
| 向量数据库 | Milvus | RAG知识检索（开发+生产统一） |
| 关系数据库 | PostgreSQL | 业务数据存储 |
| 缓存 | Redis | 实时状态缓存 |
| 任务队列 | Celery + Redis | 异步任务处理 |
| 约束求解 | OR-Tools | 排产任务调度求解器 |

## 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone <repository-url>
cd plan_agent

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并修改配置：

```bash
cp .env.example .env
# 编辑 .env 文件，配置数据库连接等信息
```

### 3. 启动依赖服务

```bash
# 使用Docker Compose启动PostgreSQL、Redis、Milvus
docker-compose up -d
```

### 4. 初始化基础设施

一键脚本会自动建库、建表、导入测试数据、创建 Milvus 集合：

```bash
python scripts/init_infra.py check     # 仅连通性检查
python scripts/init_infra.py migrate   # 建库 + 建表
python scripts/init_infra.py seed      # 导入测试数据
python scripts/init_infra.py milvus    # 创建 Milvus 集合
python scripts/init_infra.py all       # 全部执行
```

> 注意：`database/init.sql` 为早期 MySQL 语法版本，已废弃。
> PostgreSQL 正式脚本为 `database/migrations/001_init_postgres.sql`。

### 5. 运行应用

```bash
# 启动Agent服务
python -m src.agent.main

# 或者启动Celery Worker（异步任务）
celery -A src.services.celery_app worker --loglevel=info
```

## 项目结构

```
plan_agent/
├── AGENTS.md              # AI助手工作指南
├── plan.md               # 详细技术实现方案
├── work.md               # 工作记录
├── requirements.txt      # Python依赖
├── docker-compose.yml    # Docker服务配置
├── .env                  # 环境变量配置
├── config/               # 配置模块
│   ├── __init__.py
│   └── settings.py       # 应用配置
├── src/                  # 源代码
│   ├── agent/           # LangGraph Agent实现
│   ├── tools/           # 工具函数
│   ├── models/          # 数据模型
│   ├── services/        # 业务服务
│   └── utils/           # 工具类
├── database/            # 数据库相关
│   ├── init.sql.legacy_mysql  # 早期 MySQL 语法版本（已废弃）
│   ├── migrations/      # 迁移脚本
│   └── seeds/           # 测试数据
├── templates/           # Excel知识模板（TODO: 待实现）
├── mcp_servers/         # MCP Server实现（TODO: 待实现）
│   ├── erp_server/       # 占位，当前无实现
│   ├── wms_server/        # 占位，当前无实现
│   └── mes_server/        # 占位，当前无实现
└── tests/               # 测试代码
```

## 开发指南

### 核心概念

1. **LLM角色边界**：LLM只负责意图解析、策略建议和结果解释，不直接生成排产任务列表
2. **约束求解**：排产任务必须经过OR-Tools求解器验证可行性
3. **状态管理**：使用LangGraph状态图驱动工作流
4. **异步处理**：长耗时任务通过Celery异步执行

### 排产时间模型（重要）

求解**不在自然时间轴上进行**，而是在每个工作中心的「工作分钟轴」上进行。
该轴由 `capacity_calendar` 构建，只包含可生产时间，夜间与周末在模型中不存在，
因此任务不可能被排到非工作时间。

```
自然时间    08:00-23:12(15.2h) | 夜间/周末 | 次日 08:00-23:12
工作分钟轴  0 ------------ 912 |  (不存在) | 912 --------- 1824
```

求解完成后还原为真实时间，**跨天任务自动拆成多段**，落库时一段一行：

```
TASK-002 (100h)  ->  7 段
  段0: 08-12 10:05 ~ 23:12  (13.1h)
  段1: 08-13 08:00 ~ 23:12  (15.2h)
  ...  自动跳过 08-15、08-16 周末
```

相关实现：`src/services/calendar_service.py`（时间映射）、
`src/services/ortools_service.py`（求解）、`src/services/plan_writer.py`（落库）。

三个关键行为约定：

- **交期是软约束**：排不下不返回 INFEASIBLE，而是给出 `tardiness_hours` 超期量
- **产能不足显式报告**：放不下的任务进入 `unscheduled_tasks` 并附原因，不静默丢弃
- **计划默认 draft 态**：需调用 `plan_writer.confirm_plan()` 确认后方可执行

### 添加新工具

1. 在 `src/tools/` 目录创建新工具文件
2. 实现工具函数并添加类型注解
3. 在 `src/tools/__init__.py` 中导出
4. 在Agent中注册工具

### 添加新节点

1. 在 `src/agent/nodes/` 目录创建新节点
2. 实现节点逻辑
3. 在状态图中添加节点和边

## 测试

当前测试为独立可执行脚本（非 pytest 用例），需真实 PostgreSQL 连接。

```bash
# 时间映射与分段还原（纯逻辑，无需数据库）
python tests/test_calendar.py

# 求解器：非工作时间、资源独占、未排产、超期、固定占用
python tests/test_ortools.py

# 落库：分段写入、产能回写、幂等、多计划并存、重叠拦截（需 PG）
python tests/test_plan_writer.py

# 图结构与状态
python tests/test_graph.py

# 端到端：真实数据走完整图，校验输出与库内一致（需 PG）
python tests/test_integration.py

# 运行带覆盖率的测试
pytest --cov=src tests/
```

## 部署

### 生产环境配置

1. 修改 `.env` 文件，配置生产环境参数
2. 使用 `docker-compose -f docker-compose.prod.yml up -d` 启动服务
3. 配置Nginx反向代理（可选）
4. 配置监控和日志收集

### 监控指标

- Agent响应时间
- OR-Tools求解时间
- 数据库连接池状态
- Celery任务队列长度
- Redis缓存命中率

## 常见问题

### Q: OR-Tools求解超时怎么办？

A: 调整 `settings.py` 中的 `ortools_max_time_seconds` 参数，或优化约束条件。

### Q: 如何扩展新的排产约束？

A: 在 `src/services/ortools_service.py` 中添加新的约束条件。

### Q: 如何集成其他ERP系统？

A: 参考 `mcp_servers/erp_server/` 的实现，添加新的MCP Server。

## 技术债务

| 编号 | 问题描述 | 风险等级 | 计划版本 |
|------|----------|----------|----------|
| ~~TD-001~~ | ~~DB层无法防止时间段重叠冲突~~ | ~~中~~ | **已关闭** (migration 004 排他约束已实现) |

## 版本历史

- **v1.5.0** - 钉钉机器人交互设计落地，确认并发安全修复，策略 fallback，调试工具
- **v1.4.0** - LLM 策略建议增强（规则+LLM+RAG三层），多计划产能回写
- **v1.3.0** - 前端甘特图拖拽调整，产能负荷/缺料预警/插单管理页面
- **v1.2.0** - RAG 检索集成，DeepSeek LLM 接入
- **v1.1** - 修复架构问题，明确技术方案
- **v1.0** - 初始版本

## 贡献指南

1. Fork 项目
2. 创建功能分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 创建 Pull Request

## 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情

## 联系方式

如有问题或建议，请通过以下方式联系：
- 提交Issue
- 发送邮件至：[your-email@example.com]

## 致谢

感谢以下开源项目的支持：
- LangGraph
- OR-Tools
- PostgreSQL
- Redis
- Milvus

