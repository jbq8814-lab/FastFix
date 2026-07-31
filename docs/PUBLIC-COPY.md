# FastFix 公开副本

本仓库是从 FastFix 内部仓库的最终提交
`71839cbe5c35b38e15e156700eaf817d252dac99` 构建的独立安全展示副本。新仓库不继承内部 Git 历史。

## 保留范围

- FastFix 与上游 mini-SWE-agent 源码；
- FastFix 测试；
- 合成 benchmark 任务、fixture、安全 runner 与 Docker 定义；
- 聚合 assessment 和 Provider-free 机制评估；
- 架构、演示、许可证和上游归属文档。

## 明确排除

- 内部 `.git` 历史；
- 原始 Provider `trajectory.json` 和 `tool-calls.json`；
- pytest/Ruff 运行日志和其他原始运行包；
- 依赖内部原始运行包或原仓库 commit object 的证据一致性测试；
- `.env`、`.venv`、`.fastfix-runtime`、Python/test cache；
- API key、私有 endpoint 和本机绝对路径。

`benchmarks/results/` 中仅保留精简聚合材料。它们是展示产物，不是内部冻结 evidence 的逐字镜像，也不应被用来宣称可从本公开副本独立重建原始 Provider 运行。
