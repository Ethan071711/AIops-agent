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
- 处理类型由诊断 Agent 单独复核，只读取当前观测及绑定服务的代码证据；历史处理方式不进入这一步。资源压力不能直接推断为源码缺陷，外部依赖调查也不等于本服务需要重启。缺失服务证据时生成可保存的 `unknown` 诊断并转人工。
- 修复只能改配置允许的样例源文件；不能改测试、依赖、工作流或执行任意 shell。补丁限定为基本函数、分支、循环和容器操作，禁止导入、反射、危险内置函数引用、函数别名调用和装饰器。构建与测试均通过才能提交分支和 PR；不会自动合并。该检查服务于受控样例修复，不是运行任意不可信 Python 仓库的操作系统沙箱。
- Milvus 保存每次已形成的诊断，包括后续修复失败的情况；重启时重新加载集合。MCP 使用官方 Python SDK 的内存传输和 ClientSession 执行工具调用。
- 本地 `Qwen/Qwen3-4B-Instruct-2507` 实际选择工具与生成诊断/补丁；BGE 用于历史记录检索。没有付费 API 调用。

实现、样例、回归观测和实测记录均在本仓库，可按下列命令复现。

## 安装与运行

已验证的环境为 Windows / Python 3.13。首次安装需要网络及模型下载空间；本地模型建议 CUDA GPU。

`accelerate` 已列入依赖。默认包源安装 CPU 版 PyTorch；本机完整 Qwen 实测使用另行安装的 `torch 2.11.0+cu128`。两个环境的用途与结果分别记录，不把 CPU 安装检查当作 GPU 性能测试。

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

线上重启需要明确设置 `AIOPS_ALLOW_RESTARTS=1`，并用 `AIOPS_KUBECONFIG` / `AIOPS_KUBE_CONTEXT` / `AIOPS_KUBE_NAMESPACE` 指向已授权的专用环境。告警还须提供由操作者配置的回环 `health_url`；缺失时不执行重启。主流程在默认 90 秒内同时核验最新部署代次、副本就绪及健康端点 HTTP 200，满足后才记录 `auto_remediated`；重启失败、健康检查失败或超时均进入持久人工审核。其他高风险动作仍被拦截。配置项见 `.env.example`。

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
# 新增分类检查；用例用于开发反馈，不作为独立泛化测试
.\.venv\Scripts\python.exe scripts/evaluate_classification.py
# 单独验证真实样例故障、构建测试与自动提 PR
.\.venv\Scripts\python.exe scripts/capstone.py
```

`eval/cases.json` 是本次新建的合成观测回归集，包含服务定位、低风险重启、高风险拦截、证据不足和日志指令注入等情况。评测预期只由评测器读取，不交给 Agent。结果按实际模型输出生成，不设目标分数，不恢复或替换原历史业务成绩。

2026-10-24 实测记录：

| 验证 | 结果与原始记录 |
|---|---|
| 单元与集成测试 | 42 项通过，含真实 Milvus/MCP、历史信息隔离、补丁别名绕过拦截、隔离克隆的失败/通过测试门禁，以及重启目标校验、就绪/健康/超时/人工审核：[XML](results/unit_20261024.xml) |
| 独立安装 | 新建、不共享原项目包的 Python 3.13 虚拟环境，安装完整 requirements，依赖检查、微型 Qwen3 保存/加载/前向计算及 42 项测试通过：[安装记录](results/installation_20261024.json) |
| 真实样例代码修复 | 连续处理 1,000 个请求，缓存由错误的 1,000 条恢复为最多 64 条；失败测试复现后，补丁编译与 2 项样例测试通过，自动创建 [PR #1](https://github.com/Ethan071711/AIops-agent/pull/1)，保持待审未合并：[结果](results/capstone_20261024.json)、[原始 Trace](results/capstone_trace_20261024.jsonl) |
| 13 场景 / 23 次本地模型回归 | 修正后服务定位、处理类型分类、最终路由均为 23/23；原 13 场景及预期标签保持原样。平均运行 9.907 秒，属于本地功能回归耗时：[逐次结果](results/regression_20261024.json) |
| 分类开发检查 | 13/13，覆盖其他服务名、表述及累计计数与当前处理进度的区别；仅检查分类，服务身份预先给定。这些检查参与过开发反馈，不是未使用过的留出集：[用例](eval/classification_checks.jsonl)、[逐项结果](results/classification_checks_20261024.json) |
| 真实 K3s / containerd 联调 | `v1.37.1+k3s1` 单节点实际运行样例 Pod。Agent 主流程确认重启后的部署就绪及 HTTP 200，再由验证脚本独立确认 Pod UID 更换和处理进度恢复；本次观测恢复耗时 24.668 秒。有状态服务进入待审，Pod 保持不变；受限账号不能修改该服务或删除命名空间：[结果](results/kubernetes_live_20261024.json)、[放行 Trace](results/kubernetes_low_risk_trace_20261024.jsonl)、[拦截 Trace](results/kubernetes_high_risk_trace_20261024.jsonl) |
| 真实重启后仍故障 | 同一集群将受控工作进程设为每次启动均暂停；重启请求成功、Pod 更换、部署就绪，但 HTTP 503 且处理进度为零。主流程在 30 秒验收期限后转人工，未记录为已恢复：[结果](results/kubernetes_failed_recovery_20261024.json)、[Trace](results/kubernetes_failed_recovery_trace_20261024.jsonl) |

该次 PR 运行保留了历史查询失败的降级事件；集合启动加载问题随后已修正，持久化重开查询由上述集成测试单独验证。

最终分类器还重放了上述真实集群测试保存的三组观测，处理类型与重启目标均通过检查：[重放记录](results/kubernetes_observation_replay_20261024.json)。这一步只复核分类，使用已知服务名，没有再次执行集群操作。

初版分类为 13/23（56.52%），原记录保留在 [之前的提交](https://github.com/Ethan071711/AIops-agent/blob/622e3a5f2a1e05ebc27cf6352b5e49ed9c22d013/results/regression_20261024.json)。本次修正了历史记录对分类的干扰及缺失证据时直接报错的问题。小型合成回归与新增检查通过，不能推断模型在任意真实告警上都判断正确。13 场景回归中的 7 次 Kubernetes PATCH 均对应明确无状态的白名单场景，Kafka 偏移异常等未知风险进入人工审核。

13 场景回归的观测/Kubernetes API 服务是显式的 fixture；真实 curl、git、kubectl 客户端、MCP/Milvus 与构建测试会执行。另一次 K3s 联调使用实际 Linux 虚拟机、containerd 与 Kubernetes 控制器，故障由样例工作进程的受控暂停产生。两者均为测试，未验证生产高可用或业务负载；运行与恢复耗时也不替代历史生产 MTTR，未做人工 30–60 分钟对照实验。

## 复现真实集群验证

在专用测试集群应用 [样例清单](examples/kubernetes.yaml)。它创建 `aiops-demo` 命名空间内的受控工作进程、Service 和受限 ServiceAccount；`inventory` 首次启动暂停工作进程，`payments` 标记为有状态。hostPath 仅保存测试标记，应在可丢弃的测试节点上使用。

管理员负责应用清单，签发清单中 ServiceAccount 的短期 kubeconfig，并将两个 Service 映射到本机回环地址。Agent 的 kubeconfig context 必须为 `aiops-demo`，令牌和文件放在 `.storage/`。本次使用虚拟机 NodePort 转发；已有测试集群也可在两个终端分别转发：

```powershell
kubectl --context YOUR_TEST_ADMIN -n aiops-demo port-forward service/inventory 18088:8080
kubectl --context YOUR_TEST_ADMIN -n aiops-demo port-forward service/payments 18090:8080
```

然后运行：

```powershell
$env:AIOPS_KUBECONFIG = '.storage/k3s/kubeconfig.json'
$env:AIOPS_KUBECTL = '你的 kubectl.exe 路径'
.\.venv\Scripts\python.exe scripts/verify_kubernetes.py
```

脚本先核验部署的 `verification-only: true` 标签和 RBAC，再运行实际 Agent，等待替换 Pod 与工作进程恢复，并确认高风险操作没有执行。它要求初始工作进程停滞。重复验证前，可由测试管理员重新暂停 `inventory` 的样例工作进程：

```powershell
kubectl --context YOUR_TEST_ADMIN -n aiops-demo exec deployment/inventory -- sh -c 'kill -STOP "$(cat /tmp/worker.pid)"'
```

还可由测试管理员将现有样例的 `STALL_MODE` 改为 `always`，验证“重启被接受、Pod 更换，但工作进程仍停滞”的情况。该验证要求主流程超时转人工，不能报恢复成功；结束后恢复 `once`：

```powershell
kubectl --context YOUR_TEST_ADMIN -n aiops-demo set env deployment/inventory STALL_MODE=always
kubectl --context YOUR_TEST_ADMIN -n aiops-demo rollout status deployment/inventory --timeout=60s
.\.venv\Scripts\python.exe scripts/verify_kubernetes.py --expect-unrecovered
kubectl --context YOUR_TEST_ADMIN -n aiops-demo set env deployment/inventory STALL_MODE=once
```

代码入口：`aiops/workflow.py`；两个 Agent：`aiops/agents.py`；安全门禁：`aiops/policy.py`；记忆/MCP：`aiops/memory.py`。本项目保留必要的命令行输出、事件记录和回归数据，没有附加通知平台或网页看板。

依赖参考：[Milvus Lite](https://github.com/milvus-io/milvus-lite)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)。MIT 仅覆盖本仓库新代码；依赖与模型保留各自许可。
