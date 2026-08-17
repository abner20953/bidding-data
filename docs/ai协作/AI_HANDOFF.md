# 当前 AI 接力状态

```yaml
handoff_schema: 1
updated_at: 2026-08-17
module: evaluation-workbench
status: local_changes
base_commit: 13b50e1
branch: main
working_tree: modified
remote_github: 13b50e1
remote_gitee: 13b50e1
production_commit: 13b50e1
prompt_version: compare-evidence-ai-v5（本轮未修改）
database_change: 新增 ew_price_entries（仅独立价格试算，待部署）
user_approval: 已授权实施、提交、推送和部署
```

元数据不是部署事实的替代品。`production_commit` 只能来自云端 build-info、任务运行时版本或服务器核验；不知道时必须保持 `unknown`。

## 下一位先做

1. 对本轮独立价格工作表做最终差异审查；如无问题，等待用户确认是否提交、推送和部署。
2. 部署后以一个含最低价比例公式的项目验收：自动报价、手工补录、移出/恢复和重新计算；确认不会新增任务、模型或 OCR 调用。
3. 综合评审、规则提取、OCR/图片、查重和既有评分结果仍保持冻结，本轮价格试算不得接入这些主链。

## 活跃记录（最多 10 条）

### 1. 文件中心独立价格工作表（本地完成，待提交部署）

- 已实施：文件清单下方新增“报价与价格分”；解析文字或既有 OCR 缓存只读提取唯一总报价，支持手工投标人、报价/计分价修正、移出/恢复和多价格规则试算。已上传文件不会从价格表删除，复杂公式可受满分约束手工填分；GET 纯读取，只有显式 POST 或检测到文件变化才刷新。
- 隔离边界：独立 `ew_price_entries`、新 API 和 `price_sheet.py`；不注册任务、不调用模型、不启动 OCR、不读写综合评审与评分结果，不修改 `worker.py`、提示词、任务指纹或缓存；项目任务排队或运行时不扫描解析文件。
- 验证：新增最低价比例重算、手工投标人、移出恢复、禁止删除上传投标人、复杂公式手工分、GET 零写入、报价数字误识别、运行时延迟扫描及零任务/零模型调用测试；完整工作台与 AI 网关 549 项通过，前端语法、文档与差异校验通过。
- 主要文件：`storage.py`、`price_sheet.py`、工作台 blueprint、文件中心 HTML/CSS/JS、测试与稳定设计。

### 2. 已有稳定核心与后续门槛

- 综合评审、OCR/图片路由、规则提取、查重和评分主链当前不在本轮范围内；受保护边界、黄金项目和 EvidencePack/缓存影子门槛以 `评标工作台设计.md` 为准。
