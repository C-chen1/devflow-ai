"""一次性的跨任务召回演练准备：**一个**开发仓库、**一个**基线 commit、**两个**实质不同的开发问题。

召回要求候选的仓库范围等于目标 Task 的 `repo` 字符串，并且候选的适用 commit 等于目标的
`base_commit`（`memory.record_use`）。因此两个 Task 必须落在同一个仓库、同一个基线上；它们要解决的
是**两个不同的缺陷**（价格计算 / slug 生成），所以"关于仓库结构与公开检查怎么跑"的经验可以复用到
第二个问题，而第一个问题的答案不行。

它只把可审查的材料摆出来，不调用模型、不跑项目评测、不联网、不创建 Run、不摄入记忆：

    <root>/repo/dev-rehearsal/          开发仓库：src/pricing.py（问题 A）、src/slug.py（问题 B）
    <root>/packs/dev-source/            来源 Task 的 task.json + scenarios.py（问题 A 的公开检查）
    <root>/packs/dev-target/            目标 Task 的 task.json + scenarios.py（问题 B 的公开检查）
    <root>/material/dev-source-material.json   可信侧单独保管的**构造**评测材料
    <root>/adapter/                     评测适配器与本地镜像配方（Dockerfile）
    <root>/calibration/baseline/        基线工作区（两个缺陷都在，commit C0）
    <root>/calibration/corrected/       纠正工作区（只修 A，commit C1 ≠ C0）
    <root>/plan.json                    以上路径、commit、镜像与摘要

**`--confirm` 目前被禁用**：写库前必须先让 Codex 审阅"如何证明连到的是隔离库"以及任务准入方案
（见 README「隔离数据库身份与准入方案（待审）」）。脚本本身不创建 Task、不创建 Run。

校准要真的在容器里跑一遍（`calibrate_stdio_suite`）。本脚本提供适配器与镜像配方，并可用
`--calibrate` 尝试校准；没有 Docker 或没有本地镜像时结果会是 `unavailable`，脚本如实报告，
不以"文件能解析"或"commit 不同"冒充校准成功。

顺序上有一个必然的两步：镜像只能从 `<root>/adapter` 构建，而该目录要等第一次准备之后才存在。因此
`--calibrate` 作用在**已准备**的根目录上时不会重写任何材料，只核对该根目录记录的适配器/材料摘要与三个
commit 仍然一致，然后重跑校准并把结果写回 `plan.json`。摘要或 commit 有任何变化即拒绝——那样校准的
就不是材料所声明的那个东西。
"""
from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

SUITE_VERSION = "dev-rehearsal-suite-v1"
ADAPTER_IMAGE = "devflow-rehearsal-adapter:v1"
ADMISSION_REASON = "开发演练：非 benchmark 的小型开发仓库，用于跨任务召回与 Skill 演练"

REPOSITORY_NAME = "dev-rehearsal"

BUGGY_PRICING = '''"""Pricing helpers used by the development rehearsal."""


def average_price(prices):
    """Return the average price; an empty cart costs nothing."""
    return sum(prices) / len(prices)
'''

FIXED_PRICING = '''"""Pricing helpers used by the development rehearsal."""


def average_price(prices):
    """Return the average price; an empty cart costs nothing."""
    if not prices:
        return 0
    return sum(prices) / len(prices)
'''

BUGGY_SLUG = '''"""Slug helpers used by the development rehearsal."""


def slugify(text):
    """Turn a title into a lowercase slug."""
    return text.lower().replace(" ", "-")
'''

SOURCE_SCENARIOS = """from src.pricing import average_price


def test_empty_cart_costs_nothing():
    assert average_price([]) == 0


def test_non_empty_cart_still_averages():
    assert average_price([2, 4]) == 3
"""

TARGET_SCENARIOS = """from src.slug import slugify


def test_messy_title_becomes_a_slug():
    assert slugify("Hello  World!") == "hello-world"
"""

SOURCE_TASK = {
    "role": "source",
    "pack_id": "dev-rehearsal-source",
    "title": "开发演练（来源）：空购物车的平均值应为 0",
    "description": (
        "The pricing helper divides by the number of prices, so an empty cart raises "
        "ZeroDivisionError instead of returning 0. Fix the pricing helper."
    ),
    "scenarios": SOURCE_SCENARIOS,
    "test_name": "scenarios::test_empty_cart_costs_nothing",
    "problem": "空输入的 average_price 抛出 ZeroDivisionError。",
    "error": "ZeroDivisionError: division by zero（来源公开检查失败）",
    "expected": "空列表返回 0，非空列表仍返回算术平均值。",
}

TARGET_TASK = {
    "role": "target",
    "pack_id": "dev-rehearsal-target",
    "title": "开发演练（目标）：slugify 应折叠重复空白并去掉标点",
    "description": (
        "On the same repository and baseline, the slug helper only replaces single spaces, so "
        "repeated spaces and punctuation end up in the slug. This is a different defect from "
        "the pricing issue."
    ),
    "scenarios": TARGET_SCENARIOS,
    "test_name": "scenarios::test_messy_title_becomes_a_slug",
    "problem": "slugify 只替换单个空格，重复空白与标点会留在结果里。",
    "error": 'AssertionError：实际得到 "hello--world!"，期望 "hello-world"（目标公开检查失败）',
    "expected": "连续空白折叠为一个连字符，标点被去掉。",
}

# The adapter runs inside the image: the candidate code is mounted at /candidate, the cases
# arrive on stdin, and the expected output never leaves the trusted side.
ADAPTER_SOURCE = '''"""Trusted-side adapter: one case on stdin, one line on stdout, candidate code mounted."""
import json
import os
import sys

CANDIDATE = os.environ.get("DEVFLOW_CANDIDATE_DIR", "/candidate")


def main():
    request = json.loads(sys.stdin.buffer.read() or b"{}")
    sys.path.insert(0, CANDIDATE)
    try:
        from src.pricing import average_price
        value = average_price(request["prices"])
    except Exception as error:  # a wrong implementation must be a wrong answer, not a crash
        print("error:" + type(error).__name__)
        return 0
    print(json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

ADAPTER_DOCKERFILE = """# Local image for the rehearsal's constructed evaluation material.
# Build it once on the rehearsal machine:  docker build -t devflow-rehearsal-adapter:v1 .
# The evaluation runs it with --network none, --read-only and the candidate mounted read-only.
FROM python:3.11-slim
COPY adapter.py /suite/adapter.py
WORKDIR /candidate
ENTRYPOINT ["python", "/suite/adapter.py"]
"""

ADAPTER_README = """# 演练评测适配器

- `adapter.py` 只在镜像内运行：候选代码挂载在 `/candidate`（只读），测试案例由可信侧通过 stdin 传入，
  期望输出不会出现在 Agent 工作区或镜像里。
- 构建：在当前目录执行 `docker build -t devflow-rehearsal-adapter:v1 .`
  （`docker compose` 环境同理；镜像必须本地存在，评测使用 `--pull never`，不联网）。
- 本地自检（不需要 Docker）：
  `DEVFLOW_CANDIDATE_DIR=<workspace> python adapter.py < <case-stdin>`
  基线应打印 `error:ZeroDivisionError`，纠正版本应打印 `0`。
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
        check=True, timeout=60,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=Rehearsal Fixture", "-c", "user.email=rehearsal@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", message)
    commit = _git(repo, "rev-parse", "HEAD")
    if len(commit) != 40:
        raise SystemExit("git did not produce a 40-character commit id")
    return commit


def _write_repository(root: Path) -> dict:
    """One repository with two independent open defects, committed as the baseline."""
    repo = root / "repo" / REPOSITORY_NAME
    if repo.exists():
        raise SystemExit(f"Refusing to reuse an existing repository: {repo}")
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "pricing.py").write_text(BUGGY_PRICING, encoding="utf-8", newline="\n")
    (repo / "src" / "slug.py").write_text(BUGGY_SLUG, encoding="utf-8", newline="\n")
    (repo / "README.md").write_text(
        "# dev-rehearsal\n\nSmall development repository with two open issues used by the "
        f"cross-task recall rehearsal.\n\n- {SOURCE_TASK['problem']}\n- {TARGET_TASK['problem']}\n",
        encoding="utf-8")
    _git(repo, "init", "--quiet")
    return {"repo": str(repo), "base_commit": _commit(repo, "initial development state")}


def _write_calibration(root: Path, repo: Path) -> dict:
    """Baseline (both defects) and corrected (source defect fixed) workspaces.

    The corrected copy is a **separate** repository: the prepared working repository keeps its
    baseline commit, so neither Task's checkout is silently changed. Only the source problem is
    fixed here, because that is the problem the source material's cases describe.
    """
    calibration = root / "calibration"
    baseline = calibration / "baseline"
    corrected = calibration / "corrected"
    for target in (baseline, corrected):
        if target.exists():
            raise SystemExit(f"Refusing to reuse an existing calibration directory: {target}")
        shutil.copytree(repo, target)
    baseline_commit = _git(baseline, "rev-parse", "HEAD")
    (corrected / "src" / "pricing.py").write_text(FIXED_PRICING, encoding="utf-8", newline="\n")
    corrected_commit = _commit(corrected, "fix the empty-cart average")
    if corrected_commit == baseline_commit:
        raise SystemExit("The corrected workspace must differ from the baseline commit")
    return {"baseline_workspace": str(baseline), "corrected_workspace": str(corrected),
            "baseline_commit": baseline_commit, "corrected_commit": corrected_commit}


def _write_adapter(root: Path) -> dict:
    adapter = root / "adapter"
    if adapter.exists():
        raise SystemExit(f"Refusing to reuse an existing adapter directory: {adapter}")
    adapter.mkdir(parents=True)
    (adapter / "adapter.py").write_text(ADAPTER_SOURCE, encoding="utf-8", newline="\n")
    (adapter / "Dockerfile").write_text(ADAPTER_DOCKERFILE, encoding="utf-8", newline="\n")
    (adapter / "README.md").write_text(ADAPTER_README, encoding="utf-8")
    # The image's own identity only comes back from a real calibration; these digests record what
    # the image was supposed to be built from, so a later image id can be tied to this source.
    return {"directory": str(adapter), "adapter": str(adapter / "adapter.py"),
            "dockerfile": str(adapter / "Dockerfile"), "image": ADAPTER_IMAGE,
            "adapter_sha256": sha256(ADAPTER_SOURCE.encode("utf-8")).hexdigest(),
            "dockerfile_sha256": sha256(ADAPTER_DOCKERFILE.encode("utf-8")).hexdigest(),
            "build": f"docker build -t {ADAPTER_IMAGE} {adapter}"}


def _case_payload() -> tuple[bytes, bytes]:
    """One controller-held case: the stdin it sends and the single line it expects back."""
    return b'{"prices": []}\n', b"0\n"


def _write_material(root: Path, calibration: dict) -> dict:
    """Construct the material; calibration runs separately and may not be available."""
    material_dir = root / "material"
    material_dir.mkdir(parents=True, exist_ok=True)
    stdin, stdout = _case_payload()
    payload = {
        "suite_version": SUITE_VERSION,
        "image": ADAPTER_IMAGE,
        "command": ["python", "/suite/adapter.py"],
        "cases": [{"stdin_base64": base64.b64encode(stdin).decode("ascii"),
                   "stdout_base64": base64.b64encode(stdout).decode("ascii")}],
        "calibration": {
            "baseline_workspace": calibration["baseline_workspace"],
            "corrected_workspace": calibration["corrected_workspace"],
            "expected_commit": calibration["corrected_commit"],
        },
        "timeout_seconds": 15,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    path = material_dir / "dev-source-material.json"
    path.write_bytes(raw)
    return {"path": str(path), "sha256": sha256(raw).hexdigest(),
            "suite_version": SUITE_VERSION, "image": ADAPTER_IMAGE, "timeout_seconds": 15,
            "case_stdin_sha256": sha256(stdin).hexdigest(),
            "case_stdout_sha256": sha256(stdout).hexdigest(),
            "calibration": calibration}


def _pack_manifest(task: dict, commit: str, material: dict | None) -> dict:
    manifest = {
        "pack_id": task["pack_id"],
        "schema_version": 1,
        "title": task["title"],
        "source": {
            "issue_url": "https://example.invalid/issues/dev-rehearsal",
            "repository_url": "https://example.invalid/dev-rehearsal",
            "license": "CC0-1.0",
            "issue_reference": f"dev-rehearsal#{task['role']}",
        },
        "base_commit": commit,
        "environment": {
            "python_requires": ">=3.11,<3.12",
            "dependencies": ["pytest"],
            "platform_notes": "开发演练；解释器由入口的 --python-executable 指定",
        },
        "issue": {
            "sanitized_problem": task["problem"],
            "observable_error": task["error"],
            "expected_behavior": task["expected"],
        },
        "scope": {"allowed_paths": ["src/"], "forbidden_paths": ["benchmarks/"]},
        "public_checks": {
            "command": ["python", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "scenarios.py"],
            "command_name": "pytest",
            "target_tests": [task["test_name"]],
            "pythonpath_relative": ".",
            "notes": "命令里的 python 由入口替换为 --python-executable 指定的绝对路径。",
        },
        "baseline": {
            "status": "not_reverified",
            "environment_note": "本脚本不运行公开检查；失败状态需由准备者在演练机上确认。",
        },
        "external_connections": "none",
        "policy": None,
        "independent_suite": None,
        "unverified": [
            {"field": "policy",
             "reason": "开发演练不固定 benchmark Policy 文件，实际快照以 Task 行为准。"},
            {"field": "baseline",
             "reason": "本脚本不运行公开检查；失败状态需由准备者在演练机上确认。"},
        ],
    }
    if material is None:
        manifest["unverified"].append(
            {"field": "independent_suite",
             "reason": "目标 Task 只做 rehearsal，不注入可信材料。"})
    else:
        manifest["independent_suite"] = {
            "suite_version": material["suite_version"],
            "materials_sha256": material["sha256"],
            "digest_scope": "material file bytes as written by the preparation script",
            "digest_method": "sha256 over those bytes",
            "provided_by": "trusted_control_plane",
            "declaration": "构造材料；校准结果见 plan.json 的 calibration_result。",
        }
    return manifest


def _write_pack(root: Path, task: dict, commit: str, material: dict | None) -> dict:
    pack = root / "packs" / task["role"]
    if pack.exists():
        raise SystemExit(f"Refusing to reuse an existing pack: {pack}")
    pack.mkdir(parents=True)
    manifest = _pack_manifest(task, commit, material)
    raw = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    (pack / "task.json").write_bytes(raw)
    (pack / "scenarios.py").write_text(task["scenarios"], encoding="utf-8", newline="\n")
    return {"pack": str(pack), "manifest": manifest, "manifest_sha256": sha256(raw).hexdigest()}


def _image_probe(image: str) -> tuple[str, str | None]:
    """Ask Docker whether the adapter image exists, without pulling or running anything.

    ``evaluate_stdio_candidate`` already inspects the image, but it only reports ``unavailable``
    for every cause. This probe exists so an operator can tell "no Docker here" from "the image
    was never built" — the two very different reasons a calibration cannot run.
    """
    if shutil.which("docker") is None:
        return "docker_missing", None
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, encoding="utf-8", timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    if probe.returncode != 0:
        return "image_missing", None
    identity = (probe.stdout or "").strip()
    return "present", identity or None


def calibrate(plan: dict) -> dict:
    """Attempt the real calibration with the existing entry; report what actually happened.

    ``calibrate_stdio_suite`` runs the adapter in the image against both workspaces. Without
    Docker, or without the locally built image, it returns ``unavailable`` — and so does this
    function. Nothing here turns "the file parses" or "the commits differ" into a pass.
    """
    from devflow_ai.independent_evaluation import PrivateCase, calibrate_stdio_suite

    calibration = plan["material"]["calibration"]
    stdin, stdout = _case_payload()
    prerequisite, probed_id = _image_probe(plan["material"]["image"])
    result = calibrate_stdio_suite(
        baseline_workspace=Path(calibration["baseline_workspace"]),
        corrected_workspace=Path(calibration["corrected_workspace"]),
        image=plan["material"]["image"], command=("python", "/suite/adapter.py"),
        cases=(PrivateCase(stdin=stdin, expected_stdout=stdout),),
        timeout_seconds=float(plan["material"].get("timeout_seconds", 15)))
    if result.outcome == "verified" and probed_id is not None and result.image_id != probed_id:
        raise SystemExit("The calibrated image identity changed during the run")
    return {"outcome": result.outcome, "baseline_outcome": result.baseline_outcome,
            "corrected_outcome": result.corrected_outcome,
            "image_id": result.image_id,
            "image": plan["material"]["image"],
            "prerequisite": prerequisite,
            "probed_image_id": probed_id,
            "attempted": True}


def verify_prepared_root(root: Path) -> dict:
    """Re-read a prepared root and check the recorded files and Git workspaces.

    Calibration is the one step that has to happen *after* the root exists (the image is built
    from ``<root>/adapter``), so it must be re-runnable without rewriting the root. Re-running is
    only allowed while every recorded digest and commit still matches; otherwise the materials
    would be calibrated against something other than what they say they are.
    """
    root = Path(root).expanduser().resolve()
    plan_path = root / "plan.json"
    if not plan_path.is_file():
        raise SystemExit(f"No prepared plan at {plan_path}; prepare into a missing or empty root")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    def unchanged(label: str, path: str, expected: str) -> None:
        try:
            actual = sha256(Path(path).read_bytes()).hexdigest()
        except OSError as error:
            raise SystemExit(f"{label} is missing from the prepared root: {path}") from error
        if actual != expected:
            raise SystemExit(f"{label} changed since it was prepared: {path}")

    unchanged("adapter", plan["adapter"]["adapter"], plan["adapter"]["adapter_sha256"])
    unchanged("Dockerfile", plan["adapter"]["dockerfile"], plan["adapter"]["dockerfile_sha256"])
    unchanged("material", plan["material"]["path"], plan["material"]["sha256"])

    calibration = plan["material"]["calibration"]
    heads = {
        "repository": (plan["repository"]["repo"], plan["repository"]["base_commit"]),
        "baseline workspace": (calibration["baseline_workspace"], calibration["baseline_commit"]),
        "corrected workspace": (calibration["corrected_workspace"], calibration["corrected_commit"]),
    }
    for label, (path, expected) in heads.items():
        try:
            actual = _git(Path(path), "rev-parse", "HEAD")
        except (OSError, subprocess.SubprocessError) as error:
            raise SystemExit(f"{label} cannot be read back: {path}") from error
        if actual != expected:
            raise SystemExit(f"{label} no longer sits on the recorded commit: {path}")
        if _git(Path(path), "status", "--porcelain", "--untracked-files=all"):
            raise SystemExit(f"{label} has uncommitted or untracked changes: {path}")
    return plan


def calibrate_prepared(root: Path) -> dict:
    """Re-run calibration on an existing prepared root and record the result next to it."""
    root = Path(root).expanduser().resolve()
    plan = verify_prepared_root(root)
    result = calibrate(plan)
    plan["calibration_result"] = result
    (root / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {"plan": plan, "calibration": result}


def prepare(root: Path) -> dict:
    """Build the repository, both packs, the adapter, the calibration copies and plan.json."""
    root = Path(root).expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise SystemExit(f"Refusing to write into a non-empty rehearsal root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    repository = _write_repository(root)
    calibration = _write_calibration(root, Path(repository["repo"]))
    adapter = _write_adapter(root)
    material = _write_material(root, calibration)
    source_pack = _write_pack(root, SOURCE_TASK, repository["base_commit"], material)
    target_pack = _write_pack(root, TARGET_TASK, repository["base_commit"], None)
    tasks = {}
    for task, pack in ((SOURCE_TASK, source_pack), (TARGET_TASK, target_pack)):
        tasks[task["role"]] = {
            "role": task["role"],
            "title": task["title"],
            "description": task["description"],
            "repo": repository["repo"],
            "base_commit": repository["base_commit"],
            "pack": pack["pack"],
            "pack_manifest_sha256": pack["manifest_sha256"],
            "issue_url": pack["manifest"]["source"]["issue_url"],
        }
    plan = {
        "root": str(root),
        "suite_version": SUITE_VERSION,
        "repository": repository,
        "adapter": adapter,
        "material": material,
        "tasks": tasks,
        "calibration_result": None,
        "pending_review": [
            "isolated database identity: README「隔离数据库身份与准入方案（待审）」",
            "task admission: disabled until that review finishes; --confirm exits 2",
            "suite calibration: run `--calibrate` on a machine with Docker and the adapter "
            "image built; the material is not calibrated until that reports verified",
        ],
    }
    (root / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return plan


def admit_tasks(session_factory, plan: dict, *, operator: str,
                reason: str = ADMISSION_REASON) -> dict:
    """Admit both development Tasks through the existing TaskService path.

    Kept for isolated-database tests: the CLI does not call it while ``--confirm`` is disabled.
    Evidence is ``dev-task:<operator>:<reason>`` — no ``pack:`` marker — and both Tasks share the
    same ``repo`` string and ``base_commit`` so recall can match them.
    """
    from devflow_ai.enums import TaskStatus
    from devflow_ai.services import AdmissionRecord, TaskService

    admitted = {}
    for role, entry in plan["tasks"].items():
        key = uuid5(NAMESPACE_URL, f"devflow-rehearsal:{role}:v1")
        with session_factory() as session:
            task, created = TaskService(session).create_task(
                title=entry["title"], description=entry["description"], repo=entry["repo"],
                idempotency_key=key)
            task_id, status = task.id, task.status
        if created:
            with session_factory() as session:
                TaskService(session).apply_admission_decision(
                    task_id, TaskStatus.ELIGIBLE, base_commit=entry["base_commit"],
                    admission_record=AdmissionRecord(
                        reason=reason, evidence=[f"dev-task:{operator}:{reason}"]))
            outcome = "admitted"
        else:
            outcome = f"existing:{status.value}"
        admitted[role] = {"task_id": str(task_id), "created": created, "admission": outcome}
    return admitted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare the two development Tasks for the cross-task recall rehearsal.")
    parser.add_argument("--root", required=True,
                        help="Rehearsal root (must be missing or empty)")
    parser.add_argument("--operator",
                        help="Trusted operator for the (currently disabled) admission step")
    parser.add_argument("--confirm", action="store_true",
                        help="Disabled: writing Tasks waits for the database-isolation review")
    parser.add_argument("--database",
                        help="Only used with --confirm, which is currently disabled")
    parser.add_argument("--calibrate", action="store_true",
                        help="Attempt the real calibration with the adapter image "
                             "(needs Docker and a locally built image)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.confirm:
        print("Task creation is disabled: the isolated-database identity check and the task "
              "admission plan have to be reviewed first (README 隔离数据库身份与准入方案).",
              file=sys.stderr)
        return 2
    root = Path(args.root).expanduser().resolve()
    if args.calibrate and (root / "plan.json").is_file():
        # The image is built from <root>/adapter, which only exists after a first preparation.
        # Re-running calibration on that same, unchanged root is the reproducible order; a new
        # root would need the image to be built again for the identical adapter source.
        outcome = calibrate_prepared(root)
        print(json.dumps(outcome["calibration"], ensure_ascii=False, indent=2), file=sys.stderr)
        print(json.dumps({"plan": outcome["plan"], "admitted": None, "reused_root": str(root)},
                         ensure_ascii=False, indent=2))
        return 0
    plan = prepare(root)
    if args.calibrate:
        result = calibrate(plan)
        plan["calibration_result"] = result
        (root / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({"calibration": result}, ensure_ascii=False, indent=2), file=sys.stderr)
    print(json.dumps({"plan": plan, "admitted": None}, ensure_ascii=False, indent=2))
    print("Prepared the repository, both packs, the adapter and the constructed material; no "
          "database was touched.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
