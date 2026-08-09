from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC_DIR = Path(__file__).resolve().parent
REQUIRED = {
    "AI_CONTEXT.md",
    "AI_HANDOFF.md",
    "评标工作台设计.md",
    "协作与文档规范.md",
}
HANDOFF_FIELDS = {
    "handoff_schema",
    "updated_at",
    "module",
    "status",
    "base_commit",
    "branch",
    "working_tree",
    "production_commit",
    "prompt_version",
    "database_change",
    "user_approval",
}
HANDOFF_STATUSES = {
    "local_changes",
    "tests_pending",
    "ready_for_commit",
    "repository_synced_cloud_unverified",
    "deployed_validation_pending",
    "blocked_user_decision",
    "clean_no_active_work",
}
LINE_BUDGETS = {
    "AI_CONTEXT.md": (40, 100),
    "AI_HANDOFF.md": (30, 100),
    "评标工作台设计.md": (280, 500),
    "协作与文档规范.md": (55, 120),
}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def read_utf8(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(errors, f"{path.name}: 不是可读 UTF-8 文件：{exc}")
        return ""


def parse_handoff_header(text: str, errors: list[str]) -> dict[str, str]:
    match = re.search(r"```yaml\s*(.*?)\s*```", text, re.DOTALL)
    if not match:
        fail(errors, "AI_HANDOFF.md: 缺少 YAML 元数据块")
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    missing = sorted(HANDOFF_FIELDS - values.keys())
    if missing:
        fail(errors, f"AI_HANDOFF.md: 缺少字段 {', '.join(missing)}")
    if values.get("status") not in HANDOFF_STATUSES:
        fail(errors, f"AI_HANDOFF.md: status 非法：{values.get('status', '')}")
    return values


def commit_exists(commit: str) -> bool:
    if not commit or commit == "unknown":
        return True
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    errors: list[str] = []
    missing_files = sorted(REQUIRED - {path.name for path in DOC_DIR.iterdir() if path.is_file()})
    if missing_files:
        fail(errors, f"缺少协作文档：{', '.join(missing_files)}")

    texts = {name: read_utf8(DOC_DIR / name, errors) for name in REQUIRED if (DOC_DIR / name).exists()}
    for name, (minimum, maximum) in LINE_BUDGETS.items():
        line_count = len(texts.get(name, "").splitlines())
        if line_count < minimum or line_count > maximum:
            fail(errors, f"{name}: {line_count} 行，不在 {minimum}–{maximum} 行范围内")

    design = texts.get("评标工作台设计.md", "")
    if "当前接力状态" in design or "## 11. 多 AI 接力规范" in design:
        fail(errors, "评标工作台设计.md: 仍混有动态接力或协作规范")

    context = texts.get("AI_CONTEXT.md", "")
    for name in ("AI_HANDOFF.md", "评标工作台设计.md", "协作与文档规范.md"):
        if name not in context:
            fail(errors, f"AI_CONTEXT.md: 未链接 {name}")

    handoff = texts.get("AI_HANDOFF.md", "")
    fields = parse_handoff_header(handoff, errors)
    for key in ("base_commit", "production_commit"):
        if not commit_exists(fields.get(key, "")):
            fail(errors, f"AI_HANDOFF.md: {key} 不是当前仓库可识别的提交")
    if len(re.findall(r"^### \d+\.", handoff, re.MULTILINE)) > 10:
        fail(errors, "AI_HANDOFF.md: 活跃记录超过 10 条")

    root_agents = read_utf8(ROOT / "AGENTS.md", errors)
    if "docs/ai协作/AI_CONTEXT.md" not in root_agents:
        fail(errors, "AGENTS.md: 缺少统一 AI 文档入口")

    secret_pattern = re.compile(
        r"(?i)(?:secret\s*key|api[_ -]?key|token|password)\s*[:=]\s*[A-Za-z0-9_./+=-]{16,}"
    )
    for name, text in texts.items():
        if secret_pattern.search(text):
            fail(errors, f"{name}: 疑似包含密钥，请人工检查")

    for path in (
        ROOT / "tests" / "test_evaluation_workbench.py",
        ROOT / "tests" / "test_evaluation_workbench_ai_gateway.py",
        ROOT / "dashboard" / "evaluation_workbench" / "prompt_templates.py",
    ):
        if not path.exists():
            fail(errors, f"文档引用路径不存在：{path.relative_to(ROOT)}")

    if errors:
        print("AI 协作文档校验失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print("AI 协作文档校验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
