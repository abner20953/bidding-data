# 当前 AI 接力状态

```yaml
handoff_schema: 1
updated_at: 2026-08-11
module: evaluation-workbench
status: ready_for_commit
base_commit: 72485bc
branch: main
working_tree: local_changes
remote_github: 72485bc
remote_gitee: 72485bc
production_commit: 72485bc
prompt_version: vision-evidence-contract-v56
database_change: none
user_approval: content_filter_isolation_commit_and_deploy
```

元数据不是部署事实的替代品。`production_commit` 只能来自云端 build-info、任务运行时版本或服务器核验；不知道时必须保持 `unknown`。

## 下一位先做

1. 当前本地变更待提交部署：模型内容策略拒答（包括 HTTP 422）按稳定故障类别进入单元级隔离；全文扫描块保留原文并继续，规则组落为人工核验且任务 `partial_success`，禁止删改材料后重试同一请求。工作台与 AI 网关完整回归 479 项通过。部署后优先重跑三原县项目，核验不会在全文扫描约三分之一处整体失败。
2. 云端已部署 `72485bc`：范围候选台账按开放类型轮转、全文扫描按页数动态给出范围候选额度；容量调整生效（每项目 12 份投标文件、工作台单文件上传 500 MB、单份 PDF 2500 页）。部署后需重新核验 build-info、容器和任务运行时版本一致性；未做真实大文件压力验收。
3. 已停用接口（`compare-signals` PATCH、`review-results/{id}` PATCH、`score-results/{id}` PATCH、`confirm-auto` ×2）云端验证返回 410；待确认 `rokid_glasses_app` 未调用后再彻底删除路由。

## 活跃记录（最多 10 条）

### 8. 模型内容策略拒答的单元隔离（本地完成，待提交部署）

- 根因：服务商返回内容策略拒答时，网关将 HTTP 422 作为普通 `ValueError`；全文扫描并发取回异常直接上抛，整项综合评审被标为失败。
- 已实施：网关归一化内容策略拒答为不重试的稳定故障类别；全文扫描块记录失败但保留原文供后续规则使用，规则组转人工核验并继续其他组，最终以部分完成和失败项呈现。网络/限流重试、鉴权/参数错误语义不变。
- 不变契约：不针对项目或服务商文本删改、脱敏或提示词规避；不盲目重试同一请求；成功结果立即落库，模型、提示词、规则、OCR 和 API 契约不变。
- 验证：新增 HTTP 422 归类、扫描块隔离、规则组部分完成回归；工作台与 AI 网关共 479 项测试通过，`git diff --check` 通过。
- 待办：提交部署后重跑三原县；确认页面显示“部分完成/仅重跑失败项”且其他投标人的已完成结果可见。
- 主要文件：`ai_gateway.py`、`worker.py`、两个工作台测试文件、稳定设计。

### 7. 全文范围候选台账完整性（本地完成，待提交部署）

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

### 2. 评分原文连续页组装与综合评审隔离（仓库已同步，云端待核验）

- 提交：`c28941e` 及后续提交链（当前 `287d05d`），本地 `main`、GitHub 与 Gitee 均已同步。
- 范围：评分自动重组只接受唯一明确总分；无可靠总分或多包多总分时不猜测；全表修复必须保持来源条款分值/类别锚点；非评分跨类别候选不合并；编译子项只在父规则候选页内排序，不扩张 OCR/图片预算。
- 已验证：`tests.test_evaluation_workbench` 与 `tests.test_evaluation_workbench_ai_gateway` 全量通过（2026-08-10 核对），`git diff --check` 通过。
- 待核验：云端容器 `/app/.build-commit` 为 `unknown`、宿主机 `.deploy-commit` 为 `f7137eb`；CZ 最新任务仍记录旧版本。必须先用 `redeploy.sh` 恢复镜像、容器和任务版本三者一致，再验收；否则不能归因于当前仓库代码。
- 主要文件：`storage.py`、`worker.py`、`tests/test_evaluation_workbench.py`、稳定设计。

### 3. CZ 两模型规则提取差异（已提交，云端待重建后验收）

- 对比对象：同一文件、同一 v55 提示词、同一旧运行提交 `f7137eb`；MiniMax M3 产生 48 条 AI 规则、DeepSeek V4 Flash 产生 33 条。
- 关键事实：MiniMax 有 6/18 次调用依赖本地 JSON 修复，两个关键补漏调用只返回 10/12 token，评分总分为 94/100；DeepSeek 评分为 100/100，但语义编译失败而保留了更多重复/宽泛规则。
- 已实施：镜像 `.build-commit` 存在但无效时，任务与蓝点均明确显示 `unknown`，不再用环境变量、部署记录或 Git 冒充运行版本；评分遗漏/重复归属/总分不守恒时只做定向严格重试和一次全表重组，仍异常则以 `partial_success` 保存草稿并阻断确认；义务编译 JSON 异常时只用原模板重试一次，仍失败回退确定性同源收口。
- 验证：工作台与 AI 网关全量测试通过（2026-08-10 核对）；新增镜像标记、跨组评分恢复、评分异常判断、补漏失败保留主结果和编译严格重试回归测试。
- 待完成：云端镜像仍须用 `redeploy.sh` 重建后，分别以 DeepSeek V4 Flash 与 MiniMax M3 重跑同一项目验收。不得为 CZ 或某类条款写特例。

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
