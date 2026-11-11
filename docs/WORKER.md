# Worker 单次领取入口

## 单次自动准入扫描

`uv run --env-file .env.test python -m devflow_ai.admission_dispatch --trusted-repo 仓库地址` 扫描一页并最多处理一个 Task。只考虑明确列出的可信仓库；该命令可能产生真实模型费用，须先配置模型环境变量与 Task 的准入模型预算，并确认目标库已执行迁移。它不启动 Run，也不是持续运行的服务。

输出的 `next_cursor` 包含 `created_at` 和 `task_id`。有游标时，调用方可以把这两个值分别传给 `--after-created-at` 和 `--after-task-id` 继续扫描；游标为空表示本轮到达末尾。单页没有领取不代表没有后续任务。调用方负责保存游标、安排下一轮及处理错误；不要将仓库凭据写入命令参数。

## 单任务准入入口

内部准入现有显式命令：在迁移已更新的目标数据库中，使用已创建的 PENDING Task ID，并以 `--trusted-repo` 提供与该 Task 的 repo 完全一致、由操作者确认可信的仓库。命令只对该 Task 执行一次准入，不自动轮询或领取 Run。默认启动策略未配置准入模型额度，Task 创建时须使用可信内部策略工厂显式配置预算；既有 Task 的快照不会自动更改。

```powershell
uv run --env-file .env.test python -m devflow_ai.admission --task-id <Task-UUID> --trusted-repo <可信仓库地址>
```

替换两个尖括号占位符，PowerShell 不要输入尖括号。环境中须有 `DEEPSEEK_API_KEY` 和 `DEVFLOW_DEEPSEEK_MODEL`。此命令会产生真实模型调用费用；`BLOCKED` 保持 Task 的 PENDING 状态，不能视为准入通过。不要在仓库地址中放凭据。现有数据库是否升级须事先核对；本轮只在隔离 schema 中验证，未升级实际开发库。

当前阶段用于验证独立进程到 PostgreSQL 的领取链路：原子选择一个 ELIGIBLE Task、创建 Run1、保存执行归属及租约、将 Task 改为 RUNNING，成功提交后输出回执并退出。

## 运行

在项目根目录的终端执行，沿用已有 `.env.test` 测试库配置：

```powershell
uv run --locked --env-file .env.test python -m devflow_ai.worker --once
```

可指定调试用 Worker 标识：

```powershell
uv run --locked --env-file .env.test python -m devflow_ai.worker --once --worker-id local-worker
```

帮助：

```powershell
uv run --locked python -m devflow_ai.worker --help
```

CLI 自身不自动读取 .env，数据库仍由当前环境的 DEVFLOW_DB_* 决定；通过 uv --env-file 显式加载，运行前确认选用预期数据库及已应用迁移。命令没有数据库口令参数，不输出连接字符串。

`--once` 必须显式提供。未给 `--worker-id` 时生成新的 UUID 标识。租约默认10秒心跳间隔、30秒窗口，可通过 `--heartbeat-interval-seconds` / `--lease-duration-seconds` 覆写，必须是正整数且租约严格大于间隔。当前心跳间隔只参与配置校验，尚未发送周期心跳。

## 结果

- `worker_run_claimed`：成功提交；回执包含 task_id、run_id、attempt_number、status、worker_id、creation_idempotency_key、lease_expires_at，退出码0。
- `worker_idle`：本轮没有可领取的 ELIGIBLE 且未锁候选，退出码0；PENDING、RETRYING或全部候选已锁也可能出现此结果，不表示所有任务完成。
- `worker_configuration_error`：Worker标识或租约参数错误，stderr JSON、退出码2，在创建数据库资源之前拒绝。字段提示排除原始输入。
- `worker_claim_failed`：领取发生错误，stderr JSON、退出码1，不当作空闲；只输出安全错误类型，不打印SQL参数、连接字符串或原始异常。

成功和空闲写 stdout，错误写 stderr；参数解析错误也可由 argparse 显示用法并退出2。配置缺失或未迁移等数据库问题会得到非零错误，先核对配置和数据库状态。

## 当前边界

本命令领取后即退出，不执行Agent、不维护心跳、不标记PASSED，也不启动连续轮询。领取到的Run会保留RUNNING、租约随时间到期；实际执行/心跳/到期恢复尚未接入，因此它是调试入口，不是长期运行Worker。

2026-11-01：协调层 RunCoordinator.renew_lease 已实现并通过真实 PostgreSQL 测试，返回提交后的独立 LeaseRenewal 回执；但本 CLI 尚未调用周期续约，不因协调方法完成而自动变成持续 Worker。工具前与结果提交时的运行时执行权检查仍待接入。

每次调用是取下一项新工作。需要查询/重放一次已知创建请求时，使用明确Task ID和保存的创建标识调用RunCoordinator定向接口；重复执行本CLI不保证返回上一Run。

Worker在Session关闭前把结果复制到独立回执，回执不是实时执行权检查；后续执行仍须以数据库中的状态、归属和有效租约为准。此领取命令不负责 PENDING 任务准入；准入的单次扫描与单任务入口见本文开头，尚未接成持续服务。

## 已有验证

Worker专项14项测试通过，包含CLI参数/退出码/资源清理、Session关闭后的回执读取，以及独立Python进程在隔离测试schema中实际创建Run、再次空闲和错误回滚。子进程先验证数据库和搜索路径，不操作public中的用户任务。它们不计真实Issue修复或心跳恢复成果。

2026-11-01 最近完整测试263 passed、1条既有依赖警告 in27.91s，包含既有Worker、26项续约及新增31项心跳配置/生命周期/真实PG测试；没有真实Issue修复或崩溃恢复证据。


## 独立单 Run 心跳组件

`src/devflow_ai/heartbeat.py::RunHeartbeat` 已实现，当前CLI尚未使用它。调用方提供Session工厂、run_id、worker_id、LeaseConfig及显式HeartbeatConfig；后者的statement_timeout_seconds和shutdown_timeout_seconds必须自行配置为严格正整数，暂无生产默认值。现有10/30秒租约参数不变。

start启动单次后台循环；每轮通过工厂创建独立Session，事务局部SQL超时，调用协调层续约后关闭。正常收尾调用stop，或使用with让退出逻辑负责stop；finally/上下文负责覆盖主流程异常路径。heartbeat_stop_event用于请求心跳退出，execution_stop_event表示主流程需停止继续执行；failure保留后台原异常，last_renewal是独立结果快照，raise_if_failed重新抛出已保存的失败，不是实时数据库授权检查。

stop有界等待线程实际退出；仍存活则抛HeartbeatShutdownError，调用方应终止本轮处理/调度，不能捕获后当清理成功继续领取下一Run。daemon线程只为进程退出兜底，不强制结束数据库操作，也不代表资源清理完成。单条SQL超时包含锁等待，不替代网络/连接池的所有超时；Engine既有建连超时保持不变。宿主仍负责在启动工具前、正式结果提交事务中检查数据库执行权，并由Harness停止已启动进程；这些接入尚未实现。

组件的25项单测和6项隔离PG测试覆盖两个信号、正常/异常收尾、保留原错误且日志脱敏、后台多轮独立Session、主流程事务不被心跳提交、事务局部SQL超时恢复、数据库锁等待取消、join超时确有活线程以及释放锁后最终结束。不创建正式public Run，不将测试处理当专业Agent执行。
