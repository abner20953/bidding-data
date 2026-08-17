# 当前 AI 接力状态

```yaml
handoff_schema: 1
updated_at: 2026-08-17
module: evaluation-workbench
status: deployed_validation_pending
base_commit: 6b6449a
branch: main
working_tree: clean_after_price_sheet_v2_deploy
remote_github: 6b6449a
remote_gitee: 6b6449a
production_commit: 6b6449a
prompt_version: compare-evidence-ai-v5（本轮未修改）
database_change: ew_price_entries 新增 adjustment_json 列（已部署；SQLite 兼容迁移）
user_approval: 已授权实施、提交、推送和部署
```

元数据不是部署事实的替代品。`production_commit` 只能来自云端 build-info、任务运行时版本或服务器核验；不知道时必须保持 `unknown`。

## 下一位先做

1. 云端已部署 `6b6449a` 并验证：sxyh2 force 刷新后 4 家报价全部 `missing`（2026 年份不再误识别，旧错误值已清空）；剩余动作是人工在弹窗中填写 sxyh2 4 家真实报价并验收批量保存/重算。
2. 部署后以一个含最低价比例公式的项目验收：自动报价、手工补录、移出/恢复和重新计算；确认不会新增任务、模型或 OCR 调用。
3. 综合评审、规则提取、OCR/图片、查重和既有评分结果仍保持冻结，本轮价格试算不得接入这些主链。

## 活跃记录（最多 10 条）

### 1. 文件中心独立价格工作表（本地完成，待提交部署）

- 已实施：文件清单下方新增“报价与价格分”；解析文字或既有 OCR 缓存只读提取唯一总报价，支持手工投标人、报价/计分价修正、移出/恢复和多价格规则试算。已上传文件不会从价格表删除，复杂公式可受满分约束手工填分；GET 纯读取，只有显式 POST 或检测到文件变化才刷新。
- 隔离边界：独立 `ew_price_entries`、新 API 和 `price_sheet.py`；不注册任务、不调用模型、不启动 OCR、不读写综合评审与评分结果，不修改 `worker.py`、提示词、任务指纹或缓存；项目任务排队或运行时不扫描解析文件。
- 验证：新增最低价比例重算、手工投标人、移出恢复、禁止删除上传投标人、复杂公式手工分、GET 零写入、报价数字误识别、运行时延迟扫描及零任务/零模型调用测试；完整工作台与 AI 网关 549 项通过，前端语法、文档与差异校验通过。
- 主要文件：`storage.py`、`price_sheet.py`、工作台 blueprint、文件中心 HTML/CSS/JS、测试与稳定设计。

### 2. 报价误识别修复与批量弹窗（已部署 6b6449a，云端已验证）

- 已实施：报价字段排除“报价表/报价栏/一览表”等标题；无单位金额最低五位且排除年份、日期和常见编号，避免把章节标题后的年份写入报价台账。
- 交互：文件中心收敛为“报价与价格分”入口；大弹窗中集中编辑、一次保存并重算，旧逐行 API 保留兼容。报价分规则以折叠明细展示，不占用表格空间。
- 人工调整：支持价格优惠扣除、剔除税率部分和直接填写计分价；所有政策资格、适用金额、比例和说明均由人工输入，后端用 `Decimal` 确定性换算，不调用任务、模型或 OCR。
- 验证：定向报价测试和完整工作台/AI 网关 550 项通过；前端语法、文档和差异校验通过。云端 build-info `version_consistent=true`；sxyh2 force 刷新后 4 家报价 `missing`（不再误识别 2026），待人工填写真实报价并验收弹窗批量保存。

### 3. 已有稳定核心与后续门槛

- 综合评审、OCR/图片路由、规则提取、查重和评分主链当前不在本轮范围内；受保护边界、黄金项目和 EvidencePack/缓存影子门槛以 `评标工作台设计.md` 为准。
