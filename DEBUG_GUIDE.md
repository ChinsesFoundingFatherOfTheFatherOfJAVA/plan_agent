# 本地调试指南

## 方案一：VSCode launch.json（推荐）

在项目根目录创建 `.vscode/launch.json`，按 `F5` 即可断点调试。

```json
{
    "version": "0.2.0",
    "configurations": [
        {
            "name": "调试 API 服务",
            "type": "debugpy",
            "request": "launch",
            "module": "uvicorn",
            "args": ["src.api.app:app", "--host", "0.0.0.0", "--port", "8000"],
            "cwd": "${workspaceFolder}",
            "python": "${workspaceFolder}/venv/Scripts/python.exe",
            "justMyCode": false
        },
        {
            "name": "调试钉钉机器人",
            "type": "debugpy",
            "request": "launch",
            "module": "src.services.dingtalk_bot",
            "cwd": "${workspaceFolder}",
            "python": "${workspaceFolder}/venv/Scripts/python.exe",
            "justMyCode": false
        },
        {
            "name": "调试排产图（单次执行）",
            "type": "debugpy",
            "request": "launch",
            "program": "${workspaceFolder}/debug_graph.py",
            "cwd": "${workspaceFolder}",
            "python": "${workspaceFolder}/venv/Scripts/python.exe",
            "justMyCode": false
        }
    ]
}
```

### 使用方法

1. 在任意 Python 文件行号左侧点击设置断点（红点）
2. 按 `F5` 启动调试，选择对应配置
3. 在钉钉中发送消息触发断点
4. 在 VSCode 调试面板查看变量、调用栈

---

## 方案二：pdb 断点（无需 IDE）

在代码中插入断点：

```python
# 在 dingtalk_bot.py 的 process() 方法中：
def process(self, callback_message):
    import pdb; pdb.set_trace()  # 执行到这里会暂停
    ...
```

或者使用更现代的 breakpoint()（Python 3.7+）：

```python
def _confirm_latest_plan(self, sender_id=""):
    breakpoint()  # 执行到这里暂停，进入交互式调试
    ...
```

### 调试命令

```
n          # 执行下一行
s          # 步入函数
c          # 继续执行
p var_name # 打印变量
l          # 查看当前代码位置
q          # 退出调试
```

---

## 方案三：日志调试（最简单）

代码中已集成 loguru 日志，可直接加 logger.debug()：

```python
# dingtalk_bot.py
logger.debug(f"确认计划: sender={sender_id}, cached_plan={_user_plan_cache}")

# plan_writer.py
logger.debug(f"归档旧计划: archived={archived}, target={plan_id}")
```

查看实时日志：

```powershell
# API 服务日志
Get-Content api_stdout.log -Wait -Tail 20

# 错误日志
Get-Content api_stderr.log -Wait -Tail 20
```

---

## 方案四：debug_graph.py 脚本

创建一个测试脚本直接调用排产图，不需要启动整个服务：

```python
# debug_graph.py
from src.agent.graphs.plan_graph import build_plan_graph
from src.models.state import create_initial_state
from src.services.celery_app import _MockLLM

llm = _MockLLM("new_plan")
graph = build_plan_graph(llm)
state = create_initial_state("安排下周生产计划")
state["request_type"] = "new_plan"

result = graph.invoke(state)
print(f"Plan ID: {result.get('plan_id')}")
print(f"Tasks: {len(result.get('production_tasks', []))}")
```

运行：`.\venv\Scripts\python.exe debug_graph.py`

---

## 常见调试场景

### 1. 调试钉钉确认流程

在 `dingtalk_bot.py` 的 `_confirm_latest_plan` 方法中设断点：
- 检查 `sender_id` 是否正确传入
- 检查 `_user_plan_cache` 是否有绑定记录
- 检查 DB 查询返回的 plan_id

### 2. 调试排产图执行

在 `plan_graph.py` 的各节点函数中设断点：
- `input_node`: 检查意图解析
- `data_fetch_node`: 检查数据获取
- `plan_node`: 检查求解器结果

### 3. 调试数据库操作

在 `plan_writer.py` 的 `confirm_plan` 中设断点：
- 检查旧计划归档数量
- 检查事务是否正确提交
- 检查唯一约束是否冲突
