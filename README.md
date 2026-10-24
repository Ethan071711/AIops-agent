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
- 自动线上变更默认关闭；明确启用后，只允许指定上下文/命名空间内 `inventory`、`worker` 的重启。其他操作生成持久审核请求，不自动执行。
- 修复只能改配置允许的样例源文件；不能改测试、依赖、工作流或执行任意 shell。源码补丁检查导入、反射和 I/O，构建与测试均通过才能提交分支和 PR；不会自动合并。
- Milvus 保存每次已形成的诊断，MCP 使用官方 Python SDK 的内存传输和 ClientSession，非普通函数冒充协议调用。
- 本地 `Qwen/Qwen3-4B-Instruct-2507` 实际选择工具与生成诊断/补丁；BGE 用于历史记录检索。没有付费 API 调用。

这些代码按功能要求独立实现；没有导入提供材料中受限的第三方 AIOps 源码、脚本、手册或历史结果。

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

`demo/fault-injected` 分支包含一个故意取消请求缓存上限的样例，用于真实的“失败测试 → 修改源码 → 通过测试 → 提 PR”验证；主分支的样例已经有上限。未开启发布时只留本地验证提交。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests examples/tests
# 13 个新场景、共 23 次运行；需要 git、curl 和 kubectl
$env:AIOPS_KUBECTL = '你的 kubectl.exe 路径'
.\.venv\Scripts\python.exe scripts/benchmark.py
```

`eval/cases.json` 是本次新建的合成观测回归集，包含服务定位、低风险重启、高风险拦截、证据不足和日志指令注入等情况。评测预期只由评测器读取，不交给 Agent。结果按实际模型输出生成，不设目标分数，不恢复或替换原历史业务成绩。

观测/Kubernetes API 测试服务是显式的 fixture；真实 curl、git、kubectl 客户端、MCP/Milvus 与构建测试会执行，但它不是生产 Kubernetes 集群。回归运行时长不等于生产故障恢复时长，未做人工 30–60 分钟对照实验。

代码入口：`aiops/workflow.py`；两个 Agent：`aiops/agents.py`；安全门禁：`aiops/policy.py`；记忆/MCP：`aiops/memory.py`。本项目保留必要的命令行输出、事件记录和回归数据，没有附加通知平台或网页看板。

依赖参考：[Milvus Lite](https://github.com/milvus-io/milvus-lite)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。MIT 仅覆盖本仓库新代码；依赖与模型保留各自许可。
