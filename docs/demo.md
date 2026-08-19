# FastFix 安全演示与冻结证据复现

## 演示边界

本页默认路径只做四件事：

1. 读取已提交的 fixture、测试和冻结 result；
2. 重建 aggregate assessment；
3. 重建 post-freeze scripted mechanism assessment；
4. 用 scripted Agent 与记录型 validation backend 执行临时工作流测试。

默认路径不会调用真实 Provider，不会创建新 Baseline，不会重跑任何历史 `run-001`，不会 Apply 冻结 Candidate，也不会修改 canonical fixture/Gold patch。

| 路径 | 是否调用 Provider | 是否产生新评测 | 是否修改 source |
| --- | --- | --- | --- |
| 查看冻结 JSON/Markdown | 否 | 否 | 否 |
| 重建 aggregate assessment | 否 | 否；只从冻结 evidence 派生 | 只重写同内容的聚合文件 |
| 重建 post-freeze mechanism assessment | 否 | 是；非 formal、不可计入原指标 | 只重写独立的机制评测文件 |
| 工作流单元测试 | 否；scripted Agent | 否；临时目录 | 否 |
| `run_fastfix_secure.py inspect` | 否 | 否 | 否 |
| `run_fastfix_secure.py prepare` | 是 | 会创建新运行 | Candidate 阶段不改 source，但不属于本演示 |
| Baseline runner 的 run 路径 | 是 | 会消耗 attempt lease | 禁止用于冻结 `run-001` |

## 1. 环境准备

需要：

- Windows/Linux/macOS；
- Python 3.10+；
- Git；
- Docker 仅在演示受限容器或运行 `docker` 标记测试时需要。

确认当前仓库：

```powershell
git status --short --branch
git rev-parse HEAD
```

本轮 post-freeze enhancement 的冻结起点是分支 `experiment/ff015-current-baseline`、HEAD `6b404165c8d8d7a8808af1ae3133a927717ac432`。如果起点或工作树不同，先识别差异来源；不要为了演示 reset 用户变更。

## 2. 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

distribution name 暂时保留为 `mini-swe-agent`，但安装会同时包含 `minisweagent` 与 `fastfix` package。

## 3. 配置说明

- repair prompt：`src/fastfix/config/repair.yaml`
- diagnosis prompt：`src/fastfix/config/diagnosis.yaml`
- 默认验证路径：fixture 中的 `app` 与 `tests`
- 默认安全 runner runtime：`.fastfix-runtime`（已忽略，不应发布）
- Provider 模型名和凭据仅供真实运行；安全演示不需要设置

不要在命令、截图或文档中粘贴 API key、Bearer token 或真实私有 endpoint。若本机已有 mini-SWE-agent 全局 `.env`，本演示命令也不需要显示其内容。

## 4. 运行单元测试

先运行不需要 Provider/Docker 的 FastFix 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/fastfix -n 2 --basetemp .ptv
```

若只做 5 分钟面试演示，可运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv `
  tests/fastfix/workflows/test_secure_repair.py::test_approve_and_rollback_complete_isolated_workflow `
  tests/fastfix/workflows/test_secure_repair.py::test_reject_never_changes_source_and_sessions_are_independent `
  tests/fastfix/benchmark/test_aggregate_assessment.py
```

前两个测试使用 scripted tool calls 和记录型 validation backend：它们建立临时 source/Candidate，验证 diff、targeted/regression/Ruff、approval package、Approve/Reject 与 reverse-patch rollback，并断言 source 的最终状态。它们不会连接模型服务。

## 5. 查看冻结评测结果

人类可读版本：

```powershell
Get-Content benchmarks/results/aggregate-assessment.md
```

读取权威字段：

```powershell
$assessment = Get-Content benchmarks/results/aggregate-assessment.json -Raw | ConvertFrom-Json
$assessment.formal_benchmark
$assessment.metric_eligible
$assessment.primary_metric
$assessment.task_results |
  Select-Object task_id, classification, validated_candidate, changed_files
```

预期主字段：

- `assessment_kind=development_frozen_aggregate`
- `formal_benchmark=false`
- `metric_eligible=false`
- `development_validated_candidate_rate=11/13`
- `display_percentage=84.6%`

不要从 trajectory、token 数或历史 summary 另算“成功率”。

## 6. 重建 aggregate assessment

```powershell
.\.venv\Scripts\python.exe benchmarks/scripts/build_aggregate_assessment.py
.\.venv\Scripts\python.exe -m pytest -q tests/fastfix/benchmark/test_aggregate_assessment.py
git diff -- benchmarks/results/aggregate-assessment.json benchmarks/results/aggregate-assessment.md
```

脚本读取冻结单题 evidence、FF-008 metrics erratum 和 FF-014 assessment amendment，校验 SHA-256 source manifest，然后重建 JSON/Markdown。正常情况下最后一条命令没有 diff。

这是“重建聚合视图”，不是“重跑 Benchmark”：不会启动 Agent、Provider、Docker 或 fixture 测试，也不会修改单题 evidence。

## 7. 重建 post-freeze scripted mechanism assessment

```powershell
.\.venv\Scripts\python.exe benchmarks/scripts/build_post_freeze_mechanism_assessment.py
.\.venv\Scripts\python.exe -m pytest -q `
  tests/fastfix/benchmark/test_post_freeze_mechanism_assessment.py
git diff -- `
  benchmarks/results/post-freeze-mechanism-assessment.json `
  benchmarks/results/post-freeze-mechanism-assessment.md
```

这条路径实际运行 DeterministicToolcallModel、scripted Agent、RecordingValidationBackend 和临时 Git 仓库；对应 pytest 一致性测试也使用隔离临时目录。它覆盖 ready 后 `replace_text`/`apply_patch` 拒绝、Diff/validation 不变、show Diff/submit、reopen 后重新编辑、validation failure retention 和大输出 Context Projection。它不读取 Provider 凭据、不连接模型服务、不运行 Docker、不重跑 FF-003—FF-015，也不修改任何 `run-001`。

三个机制是 post-freeze enhancement：来源是 FF-008 中“正确 Candidate 已完成验证但 Agent 继续编辑”的 closure failure。Claude Code/Hermes 只提供了完整轨迹、活动上下文和确定性环境约束分离的设计启发；FastFix 实现的是 revision-aware repair 的垂直控制，不复制其通用 Agent 功能，也不声称优于或替代它们。

产物必须保持 `evaluation_role=post_freeze_scripted_mechanism_evaluation`、`formal_benchmark=false`、`metric_eligible=false`、`provider_calls=0`、`frozen_run_001_replayed=false`。它与冻结 aggregate 分开，原始 development validated Candidate rate 仍是 `11/13`。

报告中的 raw/projected characters 是各轮 model-visible JSON 字符数的累计值，并另列逐轮最大值、平均值、配置上限与超限次数。累计 reduction ratio 不是单次 prompt、Token 或 Provider 成本指标；必要证据超过上限时保留证据并记录超限。

## 8. Inspect 历史运行

### 8.1 只查看仓库内冻结 evidence

以 FF-007 为例：

```powershell
$run = "benchmarks/results/ff-007-current-baseline/run-001"
Get-Content "$run/summary.json" -Raw | ConvertFrom-Json |
  Select-Object status, failure_stage, last_provider_status_code,
    targeted_tests_passed, regression_tests_passed, ruff_passed
Get-Content "$run/assessment.json"
```

这只读取已提交 evidence。不要执行 `run_task_baseline.py` 的 run 路径；`run-001` 是单次冻结 attempt。

### 8.2 查看本机 runtime session

如果本机仍保留对应 `.fastfix-runtime` session，可用只读 inspect：

```powershell
.\.venv\Scripts\python.exe benchmarks/scripts/run_fastfix_secure.py inspect `
  --runtime-root <LOCAL_RUNTIME_ROOT> `
  --session-id <SESSION_ID>
```

`<LOCAL_RUNTIME_ROOT>` 与 `<SESSION_ID>` 必须来自本机已有 session，不要从 README 猜测。inspect 不调用 Provider、不修改 session。runtime 目录不是冻结 evidence 的替代品，也不应提交。

## 9. 演示 Candidate、Diff、validation 与 approval package

安全方式是运行临时工作流测试并对照断言：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv `
  tests/fastfix/workflows/test_secure_repair.py::test_approve_and_rollback_complete_isolated_workflow
```

该测试的可演示顺序：

1. `CandidateWorkspaceManager` 从临时 clean source HEAD 创建 detached Candidate。
2. scripted Agent 读取 `app/main.py`，用 `replace_text` 修改 Candidate。
3. `show_git_diff` 证明只有允许的 source 文件变化。
4. recording backend 依次接收 targeted pytest、完整 regression pytest 和 Ruff。
5. `submit_repair` 绑定当前 revision，`ApprovalPackageManager` 生成并验证 package manifest。
6. 测试执行 Approve，确认 source 获得预期 patch。
7. 测试执行 rollback，确认 source 内容与原始 HEAD 恢复一致。

这条演示会在 pytest 的临时目录中创建和清理数据，不接触仓库 fixture 或冻结 results。

如需演示 Docker 安全参数而不调用 Provider：

```powershell
.\.venv\Scripts\python.exe -m pytest -vv `
  tests/fastfix/sandbox/test_docker.py::test_create_arguments_have_only_the_restricted_mount_and_security_options
```

该测试核对命令构造，不要求启动 Docker daemon。真实 Docker integration 只有在镜像已由操作者可信构建时才应运行。

## 10. 安全演示 Reject 与 rollback

### Reject

```powershell
.\.venv\Scripts\python.exe -m pytest -vv `
  tests/fastfix/workflows/test_secure_repair.py::test_reject_never_changes_source_and_sessions_are_independent
```

它证明 Reject 记录决定并清理 Candidate，但 source HEAD、branch 和文件保持不变。

### Rollback

```powershell
.\.venv\Scripts\python.exe -m pytest -vv `
  tests/fastfix/workflows/test_secure_repair.py::test_approve_and_rollback_complete_isolated_workflow
```

不要为了展示 rollback 而对当前仓库运行 `approve` 或 `rollback` CLI。真实 rollback 只适用于已有 application record 且 source 仍匹配记录状态的 session；不满足条件时系统应阻断，而不是覆盖后续人工修改。

## 11. Windows 常见问题

### PowerShell 与 UTF-8

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

这些变量只需设置在当前测试进程。若直接 `Get-Content` 显示乱码，可使用：

```powershell
[IO.File]::ReadAllText(
  "benchmarks/results/aggregate-assessment.md",
  [Text.Encoding]::UTF8
)
```

### pytest 临时目录权限或长路径

优先使用仓库内短路径：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/fastfix -n 2 --basetemp .ptv
```

结束后仅在确认 `.ptv` 是本轮测试目录时删除：

```powershell
Remove-Item -LiteralPath .ptv -Recurse -Force
```

### Docker mount

- Docker Desktop 必须能访问仓库所在盘符。
- Candidate 路径含空格受支持；路径含逗号会被安全 backend 拒绝，因为 Docker mount 参数无法可靠表达。
- 验证镜像必须存在且 image ID 可解析。
- validation 使用只读 mount；测试写临时文件必须落在容器 tmpfs。
- `network=none` 是预期行为，不应通过放宽网络来“修复”测试。

### Windows result ACL

原子目录替换后，FastFix 会使用 `icacls ... /inheritance:e /t /c` 恢复继承，并重新枚举、读取 JSON、核对 SHA-256。ACL 或可读性失败会留下 publication state/隔离目录，应人工检查；不要把不完整目录当作成功 result。

## 12. 清理

完成测试后：

```powershell
git status --short
```

若仅有本轮 `.ptv`：

```powershell
Remove-Item -LiteralPath .ptv -Recurse -Force
```

不要删除或重写：

- `benchmarks/results/*/run-001`
- 单题 `assessment.json`
- `metrics-erratum.json`
- `assessment-amendment.json`
- canonical fixture 或 Gold patch
- 不明来源的 `.fastfix-runtime` session/lease

## 13. 五类路径的最终区分

- **安全演示**：读取冻结结果、重建聚合、运行 scripted/recording backend 测试。
- **Post-freeze mechanism evaluation**：只评估完成态锁、状态卡和 Context Projection，不进入原始 `11/13`。
- **真实 Provider 运行**：`run_fastfix_secure.py prepare`；需要模型配置和外部调用，不属于默认演示。
- **冻结 Benchmark evidence**：已提交的单次开发运行与聚合视图，只读解释。
- **历史 `run-001`**：attempt lease 已消耗，不允许用失败、演示或发布准备作为重跑理由。
