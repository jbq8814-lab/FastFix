# FastFix 公开发布检查清单

## 公开快照

- 脱敏基线：`af83c58f`
- 发布目标：`local/feat/ff-recovery-control-interface` 的最新提交
- 评测边界：保留冻结指标、assessment、amendment 与 erratum；不重跑 benchmark

## 已完成的脱敏

- 原始 `pytest.log`、`ruff.log` 已从 Git 跟踪移除，并由 `*.log` 阻止再次提交。
- `.env`、`.env.*`、runtime、cache 与 `.fastfix-runtime` 已纳入忽略规则；`.env.example` 可作为无凭据模板提交。
- 冻结轨迹中的本机用户名和工作区绝对路径已替换为 `<home>`、`<repo>`；仅修改路径元数据和 traceback 展示文本，不修改指标、任务结果或评测结论。
- 配置测试只使用名称明确的 placeholder，不包含形似真实 Provider credential 的 fake key。
- `LICENSE.md` 与 `NOTICE.md` 保留 MIT 许可语义及上游归属。

## AgentGuard Recovery Control Interface

- `rerun-validation`：对当前 Candidate revision 重新执行受控验证。
- `reopen-repair`：清除当前 validation，回到受控修复态。
- `rollback`：回退 Candidate 改动或按 application record 逆向回退已应用补丁。

这些动作不绕过 revision 一致性、targeted/regression/Ruff gate、approval package 或人工审批。

## Push 前检查

- [ ] `git grep` 不包含个人 Windows 用户目录、原工作区盘符或真实 credential。
- [ ] `git ls-files "*.log"` 无输出。
- [ ] Ruff 与相关配置测试通过。
- [ ] 冻结主结果仍为 `11/13 ≈ 84.6%`，且没有把 post-hoc `11/12` 改成主结果。
- [ ] `git status --short` 在提交后无输出。
- [ ] 最终提交已推送到 `local/feat/ff-recovery-control-interface`。

## 可重复的只读检查

```powershell
git ls-files "*.log"
git grep -n -I -E '[A-Z]:\\Users\\|[A-Z]:\\[^[:space:]]*internship'
git grep -n -I -E 'Bearer[[:space:]]+|api[_-]?key|authorization|token'
git status --short --branch
```

凭据关键词扫描会命中环境变量名和 placeholder，需要人工确认值；不应将仅有 `API_KEY` 变量名的测试误报为真实 credential。
