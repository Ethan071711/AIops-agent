# AIOps 故障诊断与自愈 Agent

告警进入诊断 Agent，调用 curl、git、kubectl 和进程内 MCP 历史检索，形成带证据的根因与 `remediation_type`；低风险的指定无状态服务重启可放行，高风险操作进入人工审核。代码缺陷交修复 Agent：克隆隔离分支、复现失败、生成受限源码补丁、构建与测试，通过后才能提 PR。

```text
告警 → 诊断 Agent → 根因 + 证据 + remediation_type
                     ├─ code_fix → 修复 Agent → clone → 失败复现 → 补丁 → build/test → PR
                     ├─ online_op → 白名单范围内重启 / 高风险转人工
                     └─ info_only 或低置信度 → 报告 / 人工审核
诊断记录 → BGE 向量 → Milvus → 进程内 MCP 历史检索
```

## 范围

- 双 Agent 职责及工具白名单分开，`PreToolUse` 在命令、文件修改和 PR 操作前校验。
- 诊断只允许 get/describe/top/logs 形式的 kubectl、固定观测 URL 的 curl GET 和只读 git。
- 自动线上变更默认关闭；明确启用后，还须本次结构化指标明确提供该服务的 `stateful: false`，才允许指定上下文/命名空间内 `inventory`、`worker` 的重启。未知状态、有状态服务及其他操作生成持久审核请求。
- 根因服务要与当前指标/调用链对应，历史工单不能替换当前服务；自动重启目标必须等于该根因服务。当前修复仓库只绑定 `catalog`，其他服务的代码修复转人工，避免改错仓库。
- 修复只能改配置允许的样例源文件；不能改测试、依赖、工作流或执行任意 shell。源码补丁检查导入、反射和 I/O，构建与测试均通过才能提交分支和 PR；不会自动合并。
- Milvus 保存每次已形成的诊断，包括后续修复失败的情况；重启时重新加载集合。MCP 使用官方 Python SDK 的内存传输和 ClientSession 执行工具调用。
- 本地 `Qwen/Qwen3-4B-Instruct-2507` 实际选择工具与生成诊断/补丁；BGE 用于历史记录检索。没有付费 API 调用。

实现、样例、回归观测和实测记录均在本仓库，可按下列命令复现。

## 安装与运行

已验证的环境为 Windows / Python 3.13。首次安装需要网络及模型下载空间；本地模型建议 CUDA GPU。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 终端一：启动明确标为合成数据的回归观测服务
.\.venv\Scripts\python.exe -m examples.observations
# 终端二：从一个告警开始
.\.venv\Scripts\python.exe run.py --alert alerts/memory.json
```

Milvus Lite 3.2.2 使用官方纯 Python 实现，Windows 可以本地运行；设置 `MILVUS_URI` 可切换到已配置的 Milvus 服务。运行数据写入忽略提交的 `.storage/`。模型缓存遵循 Hugging Face 环境变量。

`--publish-pr` 明确启用后，修复才会推送和创建 PR，目标限为配置的认证账号自有仓库。GitHub 凭据从 `GITHUB_TOKEN` 或现有 Git Credential Manager 读取，留在内存中，不写进 URL、结果或日志。

线上重启需要明确设置 `AIOPS_ALLOW_RESTARTS=1`，并用 `AIOPS_KUBECONFIG` / `AIOPS_KUBE_CONTEXT` / `AIOPS_KUBE_NAMESPACE` 指向已授权的专用环境。命令成功表示该重启请求已接受，不能据此断言业务已经恢复。其他高风险动作仍进入人工审核。配置项见 `.env.example`。

`demo/fault-injected` 分支包含一个故意取消请求缓存上限的样例，用于真实的“失败测试 → 修改源码 → 通过测试 → 提 PR”验证；主分支的样例已经有上限。未开启发布时只留本地验证提交。

## 验证

首次克隆后，先建立故障复现分支与工作树；已经有该工作树时跳过这一步。

```powershell
git worktree add --track -b demo/fault-injected .storage/fault-branch origin/demo/fault-injected
```

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests examples/tests
# 13 个新场景、共 23 次运行；需要 git、curl 和 kubectl
$env:AIOPS_KUBECTL = '你的 kubectl.exe 路径'
.\.venv\Scripts\python.exe scripts/benchmark.py
# 单独验证真实样例故障、构建测试与自动提 PR
.\.venv\Scripts\python.exe scripts/capstone.py
```

`eval/cases.json` 是本次新建的合成观测回归集，包含服务定位、低风险重启、高风险拦截、证据不足和日志指令注入等情况。评测预期只由评测器读取，不交给 Agent。结果按实际模型输出生成，不设目标分数，不恢复或替换原历史业务成绩。

2026-10-24 实测记录：

| 验证 | 结果与原始记录 |
|---|---|
| 单元与集成测试 | 25 项通过，含真实 Milvus 持久化重开、官方 MCP 会话、权限校验及隔离克隆中的失败/通过测试门禁：[XML](results/unit_20261024.xml) |
| 真实样例代码修复 | 连续处理 1,000 个请求，缓存由错误的 1,000 条恢复为最多 64 条；失败测试复现后，补丁编译与 2 项样例测试通过，自动创建 [PR #1](https://github.com/Ethan071711/AIops-agent/pull/1)，保持待审未合并：[结果](results/capstone_20261024.json)、[原始 Trace](results/capstone_trace_20261024.jsonl) |
| 13 场景 / 23 次本地模型回归 | 服务定位 20/23（86.96%），处理类型分类 13/23（56.52%），最终路由 20/23（86.96%）；平均运行 16.345 秒，属于本地功能回归耗时：[逐次结果](results/regression_20261024.json) |

该次 PR 运行保留了历史查询失败的降级事件；集合启动加载问题随后已修正，持久化重开查询由上述集成测试单独验证。

模型仍有处理类型误判和过度转人工的情况，不能将上述单元测试通过理解为模型判断全部正确。本次回归发出的 7 次 Kubernetes PATCH 均对应明确无状态的白名单场景，Kafka 偏移异常等未知风险进入人工审核。

观测/Kubernetes API 测试服务是显式的 fixture；真实 curl、git、kubectl 客户端、MCP/Milvus 与构建测试会执行，但它不是生产 Kubernetes 集群。回归运行时长不等于生产故障恢复时长，未做人工 30–60 分钟对照实验。

代码入口：`aiops/workflow.py`；两个 Agent：`aiops/agents.py`；安全门禁：`aiops/policy.py`；记忆/MCP：`aiops/memory.py`。本项目保留必要的命令行输出、事件记录和回归数据，没有附加通知平台或网页看板。

依赖参考：[Milvus Lite](https://github.com/milvus-io/milvus-lite)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。MIT 仅覆盖本仓库新代码；依赖与模型保留各自许可。
