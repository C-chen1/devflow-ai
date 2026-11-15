# 真实成功案例：Click #3019

本文演示如何**只读查看既有结果**，不重新运行模型或隐藏测试。历史记录保存在本机仓库外；换机器时需要可信侧提供对应记录和数据库访问。本文件不包含隐藏用例、参考答案或凭据。

## 案例事实

2026-11-12 的一次正式尝试（此前同任务还有两次未完成）：

| 项目 | 已核对结果 |
| --- | --- |
| Task | `e40e9c8d-207c-4a71-85b1-fe45e1cfc6ef` |
| Run | `80a7ba3b-460d-42fb-bc99-f9defcfda84b`，数据库状态 `PASSED` |
| Verification | `29e07087-9214-4adc-8f9a-01714dc9b77a`，`trusted_v1` / `passed` |
| 公开检查与独立评测 | 修正记录分别为 `passed`；独立评测记录已校准 |
| 人工审核 | 用户已审核通过，审核结论保存在审核副本及修正副本 |
| Task 当前状态 | 数据库仍为 `AWAITING_REVIEW`；审核结论**尚未回填数据库** |
| 模型用量 | 12 次请求，共 51,620 token，无未结算请求 |

修正副本用于从正式证据重建展示摘要；它没有覆盖原记录或人工审核副本，也没有改变正式结论。历史记录 schema 为 V1，修正副本本身未包含 Task/Run ID；因此核对数据库时以本页记录的 ID 为准，不从缺失字段猜测。

## 本机只读演示

以下路径仅适用于本次 Windows 工作环境。首先用当前 `benchmark_record` 汇总已核验的修正副本：

```powershell
$demoRecord = 'D:\python\.devflow-test-temp\benchmark-third-c59fb2821245474abb9bac281d589eae\record.corrected.json'
uv run --locked python -m devflow_ai.benchmark_record summarize $demoRecord
```

期望看到 `formal_pass: 1`、`solved: 1`。该命令只汇总传入的**这一份**记录，输出 `1/1` 仅代表选中的一个样本，不是三次或所有真实任务的成功率。命令读取记录中的声明；正式 Verification 和回执仍需可信侧核验，不能仅凭字符串宣称证据有效。

数据库中查询模型消耗（先在当前进程配置指向该次运行的隔离库；不要输出密码）：

```powershell
uv run --env-file <数据库配置文件> --locked python -m devflow_ai.usage_report --run 80a7ba3b-460d-42fb-bc99-f9defcfda84b
```

期望总计 51,620 token、12 次请求。该命令只读取已提交的用量，不读取或展示提示词和模型回复。不同数据库没有该 Run 时不能复现此结果。

## 讲解时的边界

1. 展示路径是“公开检查 → 独立评测 → 正式 Verification → 人工审核”，而不是只展示一个通过标签。
2. 人工审核保存在文件中，不代表历史 Task 已经由新审核服务更新；不要在演示时自动回填或迁移数据库。
3. 独立套件版本或材料摘要能够标识所用材料，不单独证明它的质量、来源或隔离。不要打开、复制或展示隐藏材料。
4. 该案例只能说明一次成功；Click #3084 尚未完成，多任务稳定性需要独立样本评估。
