# 当前 AI 接力状态

```yaml
handoff_schema: 1
updated_at: 2026-08-11
module: evaluation-workbench
status: deployed_validation_pending
base_commit: 6d74e4d
branch: main
working_tree: clean
remote_github: 6d74e4d
remote_gitee: 6d74e4d
production_commit: 6d74e4d
prompt_version: vision-evidence-contract-v58
database_change: evidence_layers JSON 增补可选事实元数据，无迁移
user_approval: submitted_pushed_deployed_6d74e4d
```

元数据不是部署事实的替代品。`production_commit` 只能来自云端 build-info、任务运行时版本或服务器核验；不知道时必须保持 `unknown`。

## 下一位先做

1. 云端已部署 `6d74e4d`：build-info `commit=6d74e4d`、`runtime_release_commit=6d74e4d`、`version_consistent=true`、`prompt_version=vision-evidence-contract-v58`，镜像 `.build-commit` 与宿主机 `.deploy-commit` 一致（2026-08-11 23:31 核验），`/pingbiao` 与关键 API 正常。本轮未跑验收：需强制重跑三原县，比较"企业关联关系承诺书"原页 P146 直接反证是否以高风险重点结论出现，同时比较此前满意结论、耗时、调用数、拆分数和 Token，任一关键结论下降即回退。
2. 已停用接口（`compare-signals` PATCH、`review-results/{id}` PATCH、`score-results/{id}` PATCH、`confirm-auto` ×2）云端验证返回 410；待确认 `rokid_glasses_app` 未调用后再彻底删除路由。
3. 内容策略拒答单元隔离、范围候选台账和明确否决后果/OCR 反证承接继续生效；子项级 OCR 仍待 EvidencePack 覆盖状态和 A/B 门槛。
4. 若继续工作台代码任务，按 `AI_CONTEXT.md` 的任务路由阅读稳定设计。

## 活跃记录（最多 10 条）

### 10. 明确否决后果与 OCR 直接反证承接（已提交、已部署）

- 根因：三原县”企业关联关系承诺书”已在 P146 OCR 到”是”的直接填写内容，但规则被旧模型列为 `other`、OCR 只写”待原图确认”，风险降为 low；重点结论只收录中高风险，致使高价值线索不可见。普通完整结论以逗号/分号收尾还被误判为截断，进一步制造空摘要和无效补评。
- 已实施：仅以招标原文的明确无效/否决/取消资格/禁止参加后果计算 `decision_impact`，义务编译不得把该类候选与普通规则合并；OCR 新增事实方向、不利影响和视觉依赖元数据，直接反证保留高风险但仍为人工复核；重点结论消费同一结构属性；EvidencePack 影子层同步保存元数据；移除逗号、分号等正常结尾的截断误判。
- 不变契约：不按项目、投标人、表单或页码写补丁；不自动作出废标；未改变 OCR/图片页数、模型并发、全文范围或评分结构；旧 API/结果字段兼容，新元数据均可选。
- 验证：新增否决类别、义务编译隔离、OCR 直接反证、重点结论和截断检测回归；工作台与 AI 网关共 488 项测试通过，文档校验与 `git diff --check` 通过；云端 `6d74e4d` 已部署，build-info `version_consistent=true`、v58 生效。
- 待办：以三原县强制重跑做质量验收；若 OCR 仍未稳定返回结构字段，再只审查输出契约与模型适配层，不在结果展示层追加文本补丁。
- 主要文件：`worker.py`、`storage.py`、`prompt_templates.py`、`tests/test_evaluation_workbench.py`、稳定设计与交接。

### 9. DeepSeek 结构化输出与复合规则分组优化（已部署，待黄金项目验收）

- 根因：三原县 2 家、55 条规则耗时约 39 分钟；230 次调用中有 50 次拆分、19 次紧凑重试。DeepSeek 多次在 4K–6.8K 上限内耗尽隐藏思考而只留下极短/空 JSON；义务编译已将多个子项合成规则卡，旧分组器仍只按卡片和评分叶子估算复杂度。
- 已实施：DeepSeek 首次评审/主观评分显式使用其支持的 `enabled`，并预留有界 12K 生成预算；M3、客观分和首次判断语义不变。事实定位/计数组触顶后才可在原证据上做一次禁用思考的严格恢复，开放语义和主观评分仍拆小并保留思考。分组和输出预算计入 `compiled_child_requirements`/`evidence_items`；全文扫描上限按目录规模最多 4800。
- 不变契约：不删规则、不缩全文、不改 OCR/多模态证据覆盖、评分和结果结构；未实施父规则到子项级 OCR 削减，待 EvidencePack 可表达逐子项覆盖后再 A/B。
- 验证：新增能力路由、M3预算不放大、复合子项分组及 DeepSeek 触顶不丢规则测试；工作台与 AI 网关共 482 项全过，`git diff --check` 通过。
- 待办：以三原县为质量基线强制重跑，重点检查此前满意结论全部保留，并统计拆分/紧凑重试是否明显下降。

### 8. 模型内容策略拒答的单元隔离（已提交、已部署）

- 根因：服务商返回内容策略拒答时，网关将 HTTP 422 作为普通 `ValueError`；全文扫描并发取回异常直接上抛，整项综合评审被标为失败。
- 已实施：网关归一化内容策略拒答为不重试的稳定故障类别；全文扫描块记录失败但保留原文供后续规则使用，规则组转人工核验并继续其他组，最终以部分完成和失败项呈现。网络/限流重试、鉴权/参数错误语义不变。
- 不变契约：不针对项目或服务商文本删改、脱敏或提示词规避；不盲目重试同一请求；成功结果立即落库，模型、提示词、规则、OCR 和 API 契约不变。
- 验证：新增 HTTP 422 归类、扫描块隔离、规则组部分完成回归；工作台与 AI 网关共 479 项测试通过，`git diff --check` 通过。
- 待办：可重跑三原县验收；确认页面显示“部分完成/仅重跑失败项”且其他投标人的已完成结果可见。
- 主要文件：`ai_gateway.py`、`worker.py`、两个工作台测试文件、稳定设计。

### 7. 全文范围候选台账完整性（已提交、已部署）

- 根因：全文扫描输出曾固定限制范围候选数，最终上下文又只按优先级截取；同类高优先级候选可挤掉其他独立类别，造成范围外工艺、对象或服务内容未进入最终审查。
- 已实施：范围候选额度按连续页块大小动态限定（正常最多 12、紧凑重试最多 6）；同类可合并、不同开放 `dimension` 在最终范围规则上下文中轮转保留并附原页；提示词要求跨类别分别输出、不得发现一类后停止扫描。即使云端有旧自定义提示词，worker 仍附加同义结构约束。没有项目、投标人、设备、地区、页码或文本特例。
- 不变契约：仍只使用本轮扫描候选，不回灌旧模型结论；范围候选只是线索，最终仍结合招标范围与原页判断；规则命中上限、OCR策略、评分和 API 不变。
- 验证：新增“单一类型密集时仍保留其他类型”和页块额度测试；工作台与 AI 网关完整回归 476 项通过。
- 待办：提交部署后重跑太原税务并检查多类别范围偏离的召回、误报和耗时；若云端自定义提示词覆盖默认，确认右上角配置中已同步 v56 范围候选完整性片段。
- 主要文件：`worker.py`、`prompt_templates.py`、`tests/test_evaluation_workbench.py`、稳定设计。

### 6. 停用人工裁决落库与查重人工处置、排队改为每项目 3 + 全局 12（已提交、已部署）

- 用户确认口径：评分/评审只展示 AI 建议，不保存人工调整（含查重不保存人工处置）；API 保留路径固定返回 410，确认 `rokid_glasses_app` 未使用后再删路由；排队改为每项目 3 个 + 全局 12 个（单 worker 全局 FIFO 不变）。
- 已实施：worker 评分输出移除 `final_score`/`effective_score`；storage 保存与结果复用查询移除两字段，删除 `update_review_final_status`/`update_final_score`/`confirm_auto_review_results`/`confirm_auto_score_results`/`initialize_compare_signal_reviews`/`update_compare_signal_review`；`collusion_signals` 移除信号初始人工字段；5 个 API 改 410（compare-signals PATCH、review-results PATCH、score-results PATCH、confirm-auto ×2）；`create_task` 用 `BEGIN IMMEDIATE` 短事务实现每项目/全局排队上限。
- 验证：472 项测试全过（新增每项目/全局/并发入队 3 项，改造 4 项）；`git diff --check` 通过；`validate_docs.py` 通过；云端 `2e3f863` 已部署，build-info `version_consistent=true`，410 接口实测返回 410。
- 不变契约：数据库列与历史数据保留（不破坏性迁移）；展示清洗正则（`final_score=` 记法清洗）与结果表字段无关，保留；worker 计算链不读人工字段，准确度零影响。
- 待办：黄金项目验收重跑（可选）；`rokid_glasses_app` 兼容性确认后删除 410 路由。
- 主要文件：`worker.py`、`storage.py`、`collusion_signals.py`、`blueprints/evaluation_workbench.py`、`tests/test_evaluation_workbench.py`。

### 4. 评分校验改为可见提醒（已提交，云端待核验）

- 用户决定：评分原文遗漏、评分叶子合计或总分不守恒都必须保留为显式提醒，但不限制人工确认、勾选、修改和继续执行。
- 已实施：提取任务不再因上述评分契约异常写入 `partial_success` 或“当前规则集不能确认”；确认流程仍校验评分规则满分有效性，并继续阻断“同一原文重复占用”或“AI 评分规则完全无原文锚点”。
- 云端审查：山西大学附中 MiniMax M3 版本 30 评分条款完整、合计 100 分；两张同名“逐项响应覆盖”分别对应不同采购内容，保留合理。太原税务 MiniMax M3 版本 23 合计 100 分，但有 2 条评分原文未挂接；应提示人工核对，不阻断确认。

### 5. 长投标文件与项目容量上限（已提交、已部署）

- 单个 PDF 的解析、综合评审和查重上限仍为 2500 页；解析文本保护仍为 250 万字符。独立三文件查重入口总页数仍为 7500 页，1200 万字符总预算、OCR/图片页数和并发限制不变。
- 每项目投标文件上限由 10 提升到 12；工作台上传路由在 multipart 解析前设置自身 500 MB 请求上限，消除 UI/存储为 500 MB、全站 Flask 默认为 300 MB 的不一致，其他模块继续受全站保护。
- 验证：新增 12 份上限、路由独立上传上限回归测试；工作台、AI 网关、查重共 562 项测试通过，文档与差异格式校验通过。提交 `3a7f096` 已推送 GitHub/Gitee 并部署，接口、镜像和部署记录版本一致；未做真实大文件压力验收。

## 已拒绝或暂停的路线

- 不把动态进度继续追加到稳定设计中；完成并验收的过程交给 Git 保存。
- 不为单个黄金项目添加生产关键词、页码或文本补丁；黄金项目只作为通用机制的回归资产。
- EvidencePack 与分层缓存仍按稳定设计第 10 节的门槛推进，未满足门槛前不一次性替换主链。

## 回退

- 本次文档重组不影响运行时；如需回退，只恢复文档路径和根 `AGENTS.md` 索引。
- 代码行为回退点以 Git 提交为准，禁止依据本文件手工复刻旧实现。
