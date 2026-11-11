# Click 3019 公开复现场景

本目录仅包含从 [Click issue 3019](https://github.com/pallets/click/issues/3019) 的公开描述推导的行为检查，不包含参考补丁或隐藏验收。`scenarios.py` 不以 `test_` 开头，避免项目自身的默认测试收集把已知缺陷当成 DevFlow AI 回归；需要显式指定该文件运行。

当前候选基准为 Click 8.3.0 对应的提交 `00fadb8904387158ce6e9aa1573be770446895c1`。这是公开复现候选，不代表完整任务包已完成；固定环境、独立验收和任务包版本仍待核验。

2026-11-09 本机使用 Python 3.11.15、pytest 9.1.1 和 colorama 0.4.6 对上述提交运行公开检查，结果为 **2 failed、2 passed**：两个空后缀场景失败，两个空格后缀对照场景通过。基准仓库声明 BSD-3-Clause、Python >=3.10，并在 Windows 使用 colorama。问题报告使用 Python 3.13，本机复现环境与报告不完全相同。该结果证明公开场景在本机候选基准上出现了对应问题，不代表 Agent 已完成修复。

在 PowerShell 中，将下面的 `<CLICK_CHECKOUT>` 替换为该提交的隔离仓库绝对路径，从 DevFlow AI 根目录运行：

```powershell
$env:PYTHONPATH = '<CLICK_CHECKOUT>\src'
$env:DEVFLOW_CLICK_SOURCE = '<CLICK_CHECKOUT>\src'
uv run --no-sync --no-cache python -B -m pytest -q -p no:cacheprovider benchmarks/click_3019_public/scenarios.py
```

检查会确认实际导入的 Click 来自指定源码目录，避免误测本机另一个已安装版本。基准预期出现空后缀相关失败，空格后缀检查用于确认测试环境能运行。实际运行结果以测试输出为准，不能用这些公开检查替代独立验收。

Worker 在目标仓库根目录运行时，可通过可信配置将该目录下的 `src` 提供给测试子进程，并将 pytest 根目录固定为本公开测试文件所在目录，使 JUnit 报告保留稳定的测试名称。此方式不要求改动目标仓库或向其中复制测试文件。
