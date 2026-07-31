# FastFix 安全演示

这个演示只使用公开副本内的源码、合成 fixture、精简聚合结果和 scripted validation backend。它不调用付费 Provider，不创建新 Baseline，不运行 Docker，也不重放内部原始 trajectory。

## 1. 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 2. 查看聚合结果

```powershell
Get-Content benchmarks/results/aggregate-assessment.md
```

主结果是开发阶段冻结合成任务上的
`development_validated_candidate_rate = 11/13 ≈ 84.6%`。它满足
`formal_benchmark=false` 和 `metric_eligible=false`，不是 SWE-bench 或生产修复率。

## 3. 运行 Provider-free 工作流测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/fastfix/workflows/test_secure_repair.py
```

该测试使用确定性 scripted model 和 validation backend，用于展示 Candidate 隔离、revision-aware validation、approval package 以及 Approve/Reject/rollback 边界。

## 4. 可选的静态检查

```powershell
.\.venv\Scripts\python.exe -m ruff check --no-cache src/fastfix benchmarks/scripts tests/fastfix
.\.venv\Scripts\python.exe -m ruff format --check src/fastfix benchmarks/scripts tests/fastfix
```

Docker 集成测试和真实 Provider 运行不属于默认公开演示。若需执行，操作者必须自行提供受控环境、凭据和成本授权，且不得将凭据写入仓库。
