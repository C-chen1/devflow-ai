# DevFlow AI

DevFlow AI 是一个面向软件研发 Issue 的证据驱动 Agent 项目。它将任务与执行过程持久化，由 Supervisor 协调 Explorer、Planner、Coder 和 Verifier，在受限工具、权限及预算下完成探索、修改和验证。

```text
Issue → Task 准入 → Run → Supervisor 路由
                            ↓
                 Explorer → Planner → Coder
                                      ↓
                             Verifier 公开检查
                                      ↓
                       可信侧独立评测 → 人工审核
```

这是概念流程，实际路由可能补充探索、重新规划、停止或升级人工。Coder 的补丁、公开测试通过、独立评测通过和人工审核通过是不同的结果。

## 当前进度

- 已实现任务与 Run 持久化、五类 Agent、受控工具、预算与部分失败恢复。
- Click #3019 已有一次真实任务正式通过并经人工审核；Click #3084 两次尝试未完成，尚不能据此判断多任务成功率或稳定性。
- 人工审核的状态转换已在隔离测试数据库验证；迁移文件已提供，但现有数据库不一定已升级。审核人身份目前是自由字符串，改判与历史回填尚未实现。
- 交付部署已把 API 与显式迁移入口接入 Compose；镜像构建、隔离库迁移、API 及重启后数据保留已实测。
  一次性 Worker 执行入口（`python -m devflow_ai.worker_run`）与常驻调度入口
  （`python -m devflow_ai.worker_scheduler`，只执行可信清单里登记的 Task）已实现并接入 Compose 的可选服务；
  它们只执行登记或指定的 Task，不是自动发现任务的调度器；容器内的正式验收仍受平台限制。
- 已实现面向可信操作者的记忆候选、审核、冲突与版本关系、停用及同仓库候选预览；命令入口为 `python -m devflow_ai.memory --help`。可信调用方可在开发演练中显式选择一条候选，经阶段驱动交给下一次 Explorer，内容仍未核实；通过该路径标记过记忆输入的 Task 不得提交独立评测结果。默认调度不会自动选择，仓库标签也不是身份认证；具体入口见 [部署说明](docs/DEPLOYMENT.md)。
- 已实现测试命令与修改范围检查 Skills，Worker 的补丁前基线、补丁后公开测试及符合条件的范围诊断复用这些 Skill；调用经过既有工具和权限边界，尚缺真实多任务复用证据。
- 多任务与重复评测仍在完善；受控 RSI 尚未达到启动门槛，见 [RSI 前置条件](docs/RSI_READINESS.md)。记忆相关迁移尚未应用于已有业务数据库。
- 受控 RSI 已有实验账目、预算通道与双进程模型桩演示；尚无真实候选改进或独立对照评测，自动循环和接受晋升仍禁用。可复现步骤见 [演示说明](docs/DEMO.md)，项目介绍及证据边界见 [简历项目草稿](docs/RESUME_PROJECT.md)。

最新进度以 [docs/PROGRESS.md](docs/PROGRESS.md) 顶部为准；架构和阶段目标见 [docs/SPEC.md](docs/SPEC.md)。

## 环境与启动

以下示例面向 Windows PowerShell，需要 Python 3.11、[uv](https://docs.astral.sh/uv/) 和运行中的 Docker。
`compose.yaml` 现在提供 `db`、`api`、显式的 `migrate` 入口，以及 `worker`（一次性）与 `scheduler`（按清单调度）
两个可选入口；两者只执行调用方指定或清单登记的 Task，不会自动发现任务，容器内的正式验收仍受平台限制。
缺口与容器内做不了的事写在 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

在项目根目录安装锁定的依赖：

```powershell
uv sync --locked
```

准备一个本地数据库配置。首次使用时可将 `.env.example` 复制为 `.env`，替换本地数据库密码；已有 `.env` 时不要覆盖。`.env`、模型密钥和隐藏评测材料不能提交到仓库。Compose 数据卷只在数据库首次初始化时使用这些账号配置，修改配置不会自动更改已有数据库用户或密码。

```powershell
docker compose --env-file .env up -d db
docker compose --env-file .env ps
```

使用前确认目标库及迁移版本。下面的 `upgrade` 会修改 `.env` 指向的数据库，只在确认可以升级的目标库上执行；不要直接对已有业务数据操作：

```powershell
uv run --env-file .env --locked alembic current
uv run --env-file .env --locked alembic heads
# 确认目标库及升级计划后，才执行：
uv run --env-file .env --locked alembic upgrade head
```

启动 API：

```powershell
uv run --env-file .env --locked uvicorn devflow_ai.api:app --host 127.0.0.1 --port 8000
```

在另一个 PowerShell 窗口检查进程和数据库连接：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

`live` 只说明 API 存活；`ready` 检查数据库连接，不证明所有迁移或真实任务已经通过。API 还提供 `/docs`、创建任务、查询任务及准入状态接口。

### 或用 Compose 启动 API 与迁移入口

镜像构建需要能拉取基础镜像与依赖：

```powershell
docker compose --env-file .env build
docker compose --env-file .env up -d
```

`api` 只发布到 `127.0.0.1:8000`，容器内用 `db:5432` 连接数据库，启动时不建表也不升级。迁移只能显式执行，
默认命令是只读的 `alembic current`：

```powershell
docker compose --env-file .env run --rm migrate            # 只读：当前 revision
docker compose --env-file .env run --rm migrate upgrade head
```

容器内可以运行 API，但受控 pytest 的 JUnit 报告采集只在 Windows 可用，因此 Linux 容器不能作为可信基线
或正式验收环境。细节见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

Worker 入口同样是可选的显式调用（默认只打印帮助，不改任何状态）：

```powershell
docker compose --env-file .env run --rm worker
```

它执行调用方指定的一个 Task 的 Run，先校验任务包、Task、commit、Policy 与目录，再领取；
`--attempt-kind formal` 在无法记录可信基线的平台上会被拒绝，演练用 `--attempt-kind rehearsal`。
同一 Task 的再次调用返回 `existing_run`，不会建第二个 Run；停止信号与截止时间在工作区初始化及每个阶段都可见。

调度入口按可信清单执行（同样默认只打印帮助）：

```powershell
docker compose --env-file .env run --rm scheduler
```

默认是有限批次：读一次清单、逐个领取驱动、全部结束即退出。加 `--watch` 才是常驻模式：按
`--poll-interval-seconds`（必须有限正数）重读清单，清单写坏时暂停领取且不沿用旧清单，修好后继续，
停止信号会唤醒等待。两种模式都不会自动重试、接管或重放孤儿节点。细节见
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) 第 3.1 节。

## 测试

创建并配置专门的 `devflow_test` 测试数据库及 `.env.test`，不要指向正式数据。许多数据库集成测试会检查库名，并在测试库中使用临时隔离 schema。启动数据库后，可运行定向回归：

```powershell
uv run --env-file .env.test --locked pytest -p no:cacheprovider tests/test_health.py tests/test_task_review.py tests/test_benchmark_record.py -q
```

测试结果以实际执行输出为准。部分公开测试的 JUnit 报告捕获依赖 Windows；这些用例现在显式标注为仅在 Windows 运行，
在 Linux 上会被跳过而不是失败——**跳过不等于通过**。Linux + 隔离 PostgreSQL 的全量实跑结果为
1171 collected、1116 passed、55 skipped、0 failed（2026-11-16，含 `tests/test_worker_run.py` 24 项、
`tests/test_worker_scheduler.py` 32 项、`tests/test_worker_finalize_chain.py` 31 项与
`tests/test_worker_scheduler_evaluation_chain.py` 3 项；跳过项含 1 条 Windows-only UNC 用例和 2 条需要
Windows + Docker 的贯通用例；随后增加默认阶段驱动的成功与独立评测失败用例，完整文件在 Windows + Docker 隔离环境实跑 5 passed，使用模型桩和构造材料），
其中 17 项跳过正是这些 Windows-only 用例。
Docker 独立评测测试可能需要单独启用，跳过同样不计为通过。

仓库提供 GitHub Actions 基础 CI（`.github/workflows/ci.yml`），使用独立 PostgreSQL 服务运行所列的定向测试；它不是完整测试集，也不运行真实模型或独立隐藏评测。同一测试集合在 Linux + 隔离 PostgreSQL 上实跑 224 passed（2026-11-13），但 GitHub 上的 workflow 尚未实际触发，Runner 侧结果仍需确认。

## 真实任务与审核

无需模型额度的任务创建与查询演示见 [docs/DEMO.md](docs/DEMO.md)；真实运行的用量、证据链与限制见 [真实评测报告](docs/EVALUATION_REPORT.md)。

真实模型运行会消耗额度，并要求任务包、固定 checkout、隔离数据库、Docker、模型环境变量和由可信侧保管的独立评测材料。先完成 preflight，再由明确的调用者启动；隐藏用例和参考材料不进入 Agent 工作区、仓库或普通日志。运行材料及审核操作的完整流程见：

- [Benchmark 运行手册](docs/BENCHMARK_RUNBOOK.md)
- [Click #3084 任务计划](docs/CLICK3084_RUN_PLAN.md)
- [多任务验证方案与统计口径](docs/MULTI_TASK_PLAN.md)
- [部署：Compose 与显式迁移](docs/DEPLOYMENT.md)

相关入口可用 `--help` 查看参数：

```powershell
uv run --locked python -m devflow_ai.preflight --help
uv run --locked python -m devflow_ai.benchmark_task --help
uv run --locked python -m devflow_ai.benchmark_run --help
uv run --locked python -m devflow_ai.worker_run --help
uv run --locked python -m devflow_ai.worker_scheduler --help
uv run --locked python -m devflow_ai.benchmark_record --help
uv run --locked python -m devflow_ai.task_review --help
```

人工审核入库需要目标数据库包含 `task_reviews` 迁移；它不会自动把旧的人工审核回填。正式验收材料由可信控制端保管，摘要一致不等于材料本身已经过独立校准。
