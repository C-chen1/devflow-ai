# Click 3084 公开复现场景

本目录只包含从 [pallets/click#3084](https://github.com/pallets/click/issues/3084) 的公开描述推导的行为检查，
不含参考补丁、隐藏用例或任何私有材料。`scenarios.py` 不以 `test_` 开头，避免项目自身的默认测试收集把它当成 DevFlow AI 的回归测试；
必须显式指定该文件运行。

## 任务与基准

- 问题：声明 `is_flag=False, flag_value=<value>` 的选项，自 8.3.0 起不再接受"只给选项、不给值"的写法，
  而是以 `Error: Option --name requires an argument.`（`SystemExit(2)`）退出。
- 基准 commit：`7f7bbe4569ea68e8dabee232eade069ef3310aea`（`stable` 分支上修复合并前的 tip，
  即修复 PR #3152 的父提交）。**该 commit 与可信 checkout 尚未由 Codex 确认。**
- 参考修复：PR #3152，PR 分支 tip `955ca492a6b9e8de0660dae1c415568ca6f9c4b3`，合并提交
  `ebcd548d50662bc8fa4f8b9ec7fe13cb8310bfaa`（2025-11-20 合入 `stable`，随后随 8.3.2 发布）。
  这里只把它当作"公开测试能区分缺陷与修复"的对照，不作为隐藏材料。

## 已复现（Windows 正式候选环境，Codex 核对）

在 Windows + Python 3.11.15 + pytest 9.1.1 + colorama 0.4.6 上，用解包后的浅仓库跑本目录的公开检查：

- 基准 `7f7bbe4…`：**1 failed / 2 passed，退出码 1**，目标 JUnit 名称匹配；
- 参考修复 `955ca49…`：**3 passed**。

两个浅仓库的 HEAD、tree、144 个跟踪文件、`git fsck` 与工作区干净均由 Codex 核对（Windows 不保留 Linux 可执行位，
只在解包副本里设置 `core.filemode=false`）。**这些结论不改变下面那条来源限制**：仓库仍来自第三方代理。

## 已复现（Linux，非可信环境）

在本项目的 Linux 沙箱里，用该 commit 的 tarball 建立只读 checkout 后运行：

```bash
PYTHONPATH=<checkout>/src python -B -m pytest -q -p no:cacheprovider benchmarks/click_3084_public/scenarios.py
```

- 基准 `7f7bbe4…`：**1 failed / 2 passed，退出码 1**。失败节点正是目标
  `scenarios::test_flag_value_used_when_the_flag_is_given_alone`，另外两条是环境对照。
- 参考修复 `955ca492…`：**3 passed**。
- 环境：Linux、Python 3.11.17、pytest 9.2.0、checkout 由 upstream tarball 解出。

这次复现证明的是"这个公开检查能区分缺陷与修复"，**不**证明：可信 checkout 一致、Windows 固定解释器
（Python 3.11.15 / pytest 9.1.1）上同样如此、或缺陷在别的 Python 版本上表现一致。

## 仍需可信侧完成

1. 确认基准 commit `7f7bbe4…` 与可信 checkout 一致（本地 HEAD、干净工作区）；
2. 在固定的 Windows 解释器与依赖上重跑本目录的公开检查，记录精确节点与退出码；
3. 该任务**自己的**隐藏材料、参考版本与校准（不能复用 Click #3019 的私有套件）；
4. 外部连接核验结论（当前 `external_connections` 为 `unknown`）；
5. Policy 定稿（草案 `policy.draft.json`，版本 `benchmark-click3084-v1`，与 #3019 的快照版本无关）。

隐藏材料只由可信控制端保管并在运行时注入，不写入本目录、仓库、Agent 上下文或普通日志。

## 供 Windows 独立复核的 checkout 包

`click3084-public-checkouts.tar.gz`（同目录，sha256 `47a1d20915c77136075d785485429ec284ed7c4cd6bdb7c13b60dcc48be887fd`，
约 1.7 MB）包含两个**已 checkout 的浅克隆仓库**：

| 目录 | HEAD | tree | 跟踪文件 |
| --- | --- | --- | --- |
| `click3084-public/base/` | `7f7bbe4569ea68e8dabee232eade069ef3310aea` | `260e8630d5c43ff65dae1454b7caec1213d44862` | 144 |
| `click3084-public/fix/` | `955ca492a6b9e8de0660dae1c415568ca6f9c4b3` | `00e4ad8027ca662cd109a8573e93b8f29623ff00` | 144 |

为什么不是 git bundle：两个 commit 用 `--depth=1` 取得，父提交按设计缺失；从这种状态生成的 bundle 无法被
`git clone`（git 拒绝越过缺失的父提交）。因此交付的是"可以直接用的浅仓库"，同样能创建独立 checkout。
`git log` 只会显示一个提交、`git fsck` 会报缺少父提交，这是预期；不要在目录里再 fetch/deepen。

来源与完整性：对象经第三方 HTTPS 代理（ghproxy.net）从 upstream `pallets/click` 取得——沙箱到 github.com:443 不通。
git 按 SHA-1 存储对象，且每个 commit 的 tree 与 GitHub API 报告的 tree 一致，因此内容可与上游对齐复核；
**但"经代理取得"与"从可信 checkout 取得"不是同一件事**，正式运行仍需可信侧用自己的 checkout。

包内不含隐藏材料、凭据或本项目文件，只有公开上游代码；用完可以删除这个压缩包。

## 环境差异（分别记录，不混用）

| 环境 | 解释器 / pytest | 基准 | 参考修复 |
| --- | --- | --- | --- |
| Windows（**正式候选**） | Python 3.11.15 / pytest 9.1.1 / colorama 0.4.6 | 1 failed / 2 passed，退出 1 | 3 passed |
| Linux（编码侧） | Python 3.11.17 / pytest 9.2.0 | 1 failed / 2 passed，退出 1 | 3 passed |

任务清单的 `environment` 与 `baseline.environment_note` 记录的是 Windows 正式候选环境；Linux 那次只作为编码侧的独立记录。
正式环境定版后按实际版本重跑，两次结果不能拼接使用。

## 独立评测：公开适配器与材料格式（隐藏用例仍由可信侧保管）

本目录提供**公开适配器** `stdio_adapter.py`：容器内一次 JSON 请求进、一次 JSON 结果出，
隐藏用例只以 stdin/stdout 配对的形式由可信侧提供，不进入本仓库。

- 请求：`{"flag_value": str, "default": str, "argv": [str, ...]}`
- 结果：`{"exit_code": int, "output": str}`
- 候选源码由 `DEVFLOW_CANDIDATE_ROOT`（默认 `/candidate`）定位，适配器把 `<root>/src` 加入 `sys.path`。

评测容器的既有约束（见 `devflow_ai.independent_evaluation`）：`--network none`、`--read-only`、
只把候选工作区只读挂到 `/candidate`，其余目录不存在。因此适配器必须**打进镜像**——本目录已附公开 `Dockerfile`：

```
docker build -t click3084-eval:local benchmarks/click_3084_public
```

对应的材料 `command` 为 `["python", "-B", "/opt/devflow/click3084_adapter.py"]`。
镜像里只有公开适配器，不含任何用例、凭据或本项目文件。

材料 JSON（由可信侧保管并注入，格式与 #3019 相同）：

```json
{
  "suite_version": "<本任务自己的套件版本>",
  "image": "<已在本机的镜像>",
  "command": ["python", "-B", "/opt/devflow/click3084_adapter.py"],
  "cases": [{"stdin_base64": "...", "stdout_base64": "..."}],
  "calibration": {
    "baseline_workspace": "<基准 7f7bbe4… 的 checkout 绝对路径>",
    "corrected_workspace": "<参考修复 955ca49… 的 checkout 绝对路径>",
    "expected_commit": "955ca492a6b9e8de0660dae1c415568ca6f9c4b3"
  },
  "timeout_seconds": 30
}
```

校准由 `calibrate_stdio_suite` 在正式评测时实际执行：同一个适配器与同一批用例分别跑基准与参考版本，
**基准必须 failed、参考必须 passed** 才算 `verified`，否则材料不可用。校准结果按任务独立，**不能复用 Click #3019 的套件**，
也不要把用例内容写进本目录、仓库、Agent 上下文或普通日志。
