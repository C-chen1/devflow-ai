# DevFlow AI 演示：创建与查询任务

本演示验证 API 接收任务、持久化、幂等提交和查询；**不会启动 Agent 或调用模型**，也不代表真实修复或正式验收通过。示例会在 API 当前连接的数据库中创建一条 Task，请使用已迁移至项目 head 的专用演示数据库，不要使用业务数据。

## 四项能力的隔离测试演示

在项目根目录、确认 `.env.test` 指向专用 `devflow_test` 且 PostgreSQL 可用后，可运行以下测试，查看四项能力的当前闭环：

```powershell
uv run --env-file .env.test --locked --no-sync python -m pytest `
  tests/test_memory.py::test_operator_can_preview_review_prepare_and_trace_recall `
  tests/test_memory.py::test_explicit_recall_reaches_explorer_but_disables_independent_evaluation `
  tests/test_worker_integration.py::test_registered_skill_reuses_same_version_across_isolated_tasks `
  tests/test_experiment.py::test_a_registered_record_is_traced_to_its_registration `
  tests/test_experiment.py::test_a_promotable_pass_without_a_registered_record_cannot_be_promoted `
  -q -p no:cacheprovider
```

这五项分别覆盖记忆准备与追溯、显式召回及独立评测限制、Skill 在隔离任务中的复用与权限、实验记录登记及未登记的拒绝条件。它们使用构造数据、隔离 schema 和模型桩，不调用真实模型，不代表真实任务成功、召回有效或 RSI 已运行。数据库测试需要具备创建和删除自身隔离 schema 的权限；不要指向案例数据库。

如需展示实验账目与阶段驱动的完整构造流程，可另行运行：

```powershell
uv run --env-file .env.test --locked --no-sync python -m pytest tests/test_rsi_demo_flow.py -s
```

该演示使用独立临时 PostgreSQL（需要 pgserver，或已运行 Docker 且本地已有 `postgres:17.11` 镜像）、两个测试角色和模型桩；不调用真实模型。它包含单进程与双进程构造场景，版本摘要和工作区由测试构造，测试直接结束构造 Run；不是实际部署回退、正式验收或真实 RSI 改进实验。详情见 [RSI 演示说明](RSI_READINESS.md#手动触发的构造实验演示2026-11-19)。

如需准备真实开发演练，见 [显式记忆开发演练](DEPLOYMENT.md#显式记忆开发演练)；该操作需要先确定任务、预算和证据范围。受控 RSI 的 `accepted` 和自动循环仍未开放，见 [启动核对](RSI_READINESS.md)。

## 准备

依照 [README](../README.md) 启动数据库，检查数据库指向和迁移版本，再启动 API。下面的请求假定 API 地址是 `http://127.0.0.1:8000`。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

两者返回 `status: ok` 后继续。`ready` 只检查连接，并不验证数据库迁移完整性。

## 创建并查询

在同一个 PowerShell 窗口运行：

```powershell
$demoKey = [guid]::NewGuid().ToString()
$demoHeaders = @{ 'Idempotency-Key' = $demoKey }
$demoBody = @{
    title = 'Demo task'
    description = 'Demonstrate task intake'
    repo = 'https://example.com/demo.git'
} | ConvertTo-Json

$demoTask = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/tasks' -Headers $demoHeaders -ContentType 'application/json' -Body $demoBody
$demoTask
Invoke-RestMethod "http://127.0.0.1:8000/tasks/$($demoTask.id)"
Invoke-RestMethod "http://127.0.0.1:8000/tasks/$($demoTask.id)/admission"
```

期望创建结果包含任务 ID 和 `PENDING` 状态，查询返回相同 ID。示例仓库地址只是占位数据；创建任务不会克隆仓库，也不表示任务已经准入或运行。

再次提交同样的请求和同一个 key：

```powershell
$demoReplay = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/tasks' -Headers $demoHeaders -ContentType 'application/json' -Body $demoBody
$demoReplay.id -eq $demoTask.id
```

期望返回 `True`。同一个 key 配不同任务内容会产生冲突；新任务应使用新的 key。演示 Task 会留在所选演示数据库中。

## 真实任务和历史结果

这份演示不需要模型密钥或隐藏材料。需要演示真实修复时，先阅读 [Benchmark 运行手册](BENCHMARK_RUNBOOK.md)，准备可信材料、隔离库和预算。历史 Click #3019 已有一次正式通过且经人工审核；Click #3084 的两次尝试为未完成，不能展示成通过。历史记录不等于本次重跑结果。

无需重新调用模型的历史成功案例，见 [Click #3019 只读演示](SUCCESS_CASE_DEMO.md)。

## 未完成任务的诊断演示

Click #3084 的两次正式尝试均为 `incomplete`：首次多次读取大文件头后预算不足；第二次已用范围读取覆盖关键定义，但 Planner 没有获得模型预算。两次都没有形成可供正式验收的补丁。`incomplete` 表示本次没有得到完成裁决，不等于补丁已被判失败。

在保留两份记录的本机，可只读汇总（路径在其他机器需要调整）：

```powershell
$demoFirst = 'D:\python\.devflow-test-temp\benchmark-3084-ad7156d7b28647cfa5e578854e68b834\record.json'
$demoSecond = 'D:\python\.devflow-test-temp\benchmark-3084-attempt2-8fab4a1b4f9d4d438d3e5cad25c045eb\record.json'
uv run --locked python -m devflow_ai.benchmark_record summarize $demoFirst $demoSecond
```

期望看到 `sample_size: 2`、`incomplete: 2`、`solved: 0`。这是这两次所选尝试的结果，不是所有任务的整体统计。用量来自各自的数据库记录：首次 72,527 token，第二次 72,979 token；仅凭数字不能断言改动提高或降低了系统整体效率。

可在配置为历史运行所在数据库的进程中运行：

```powershell
uv run --locked python -m devflow_ai.usage_report --run 417a771f-19cb-42a0-9a3f-6841d000d0dd --compare ba503550-98cf-4e1e-857d-b518f1b18ef3
```

该命令只报告已有用量，不会恢复或重跑任务。两条历史 Run 在当时数据库中的状态仍为 `RUNNING`；这与记录的 `incomplete` 并不矛盾，演示不修改它们，也不将此案例称为已经演示自动恢复或人工升级。

## 隔离测试：过期运行的恢复与人工升级

这是基于测试夹具的可复现演示，使用 `.env.test` 指定的测试数据库和临时隔离 schema，不调用真实模型，也不操作历史 Run。需要测试数据库和运行中的 PostgreSQL：

```powershell
uv run --env-file .env.test --locked pytest -p no:cacheprovider tests/test_rediagnosis_recovery_integration.py::test_expired_run_derives_escalation_when_the_rediagnosis_quota_is_exhausted tests/test_rediagnosis_recovery_integration.py::test_expired_run_close_escalation_blocks_recovery tests/test_rediagnosis_recovery_integration.py::test_control_cli_closes_expired_runs_without_starting_recovery tests/test_rediagnosis_recovery_integration.py::test_expired_close_and_recover_starts_the_rediagnosis_run -v
```

四项通过说明测试场景中：重新诊断额度耗尽时任务升级人工并拒绝继续恢复；控制端关闭过期 Run 后将任务置为重试状态，但只关闭、不创建恢复 Run；额度允许时可创建一次重新诊断 Run，重复调用不会重复创建。最后一个用例的 Supervisor 模型是桩，返回阻塞结果，并未实际修复任务。

这些是隔离夹具中的执行证据，不是历史任务已经自动恢复的证据。节点运行中崩溃后的完整重放仍未实现；不要对历史 Run 使用批量关闭命令来复现此演示。
