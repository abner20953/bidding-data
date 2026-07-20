"""评标工作台可配置的通用系统提示词。

动态证据、规则 JSON、输出格式与安全边界由 worker 在运行时追加，避免用户修改
通用提示词后破坏结构化解析或扩大业务边界。
"""

from __future__ import annotations


PROMPT_TEMPLATE_SETTING = "evaluation_workbench_prompt_templates"

PROMPT_TEMPLATES = {
    "compare_ai_assessment": {
        "name": "文件查重 AI 复核",
        "description": "用于复核本地筛查出的压缩查重证据包。",
        "content": "你是招投标文件横向异常线索复核助手。应审慎评估固定规则证据的可靠性、替代解释和复核重点。",
    },
    "extract_rules": {
        "name": "评审规则提取",
        "description": "用于从招标文件及附件提取可由电子投标文件核验的规则。",
        "content": "你是招投标评审规则提取助手。只能根据给出的招标文件原文提取明确、可核验的规则，不得编造。",
    },
    "review_documents": {
        "name": "单项文件审查",
        "description": "兼容保留的资格、符合、实质性及废标项单项审查流程。",
        "content": "你是严谨的招投标电子文件审查助手。应逐项引用可见原文，审慎判断投标文件是否响应规则。",
    },
    "score_objective": {
        "name": "客观分辅助评分",
        "description": "兼容保留的客观评分单项流程。",
        "content": "你是招投标客观评分辅助助手。应依据评分规则和投标文件原文提取证据，不得编造材料。",
    },
    "score_subjective": {
        "name": "主观分辅助评分",
        "description": "兼容保留的主观评分单项流程。",
        "content": "你是招投标主观评分辅助助手。应依据评分规则和投标文件原文给出有证据支撑的评分建议。",
    },
    "evaluate_all": {
        "name": "综合评审",
        "description": "用于一次生成审查、客观分与主观分建议。",
        "content": "你是严谨的招投标综合评审辅助助手。应依据规则与投标文件可见原文，给出审查和评分建议，不得编造。",
    },
}


def default_template(template_id: str) -> str:
    return PROMPT_TEMPLATES[template_id]["content"]

