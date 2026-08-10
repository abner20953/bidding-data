# 当前 AI 接力状态

```yaml
handoff_schema: 1
updated_at: 2026-08-10
module: evaluation-workbench
status: repository_synced_cloud_unverified
base_commit: 287d05d
branch: main
working_tree: clean
remote_github: 287d05d
remote_gitee: 287d05d
production_commit: unknown
prompt_version: vision-evidence-contract-v55
database_change: none
user_approval: review_docs_only
```

元数据不是部署事实的替代品。`production_commit` 只能来自云端 build-info、任务运行时版本或服务器核验；不知道时必须保持 `unknown`。

## 下一位先做

1. 云端版本一致性未恢复：build-info 的 `commit=unknown`、`deploy_record_commit=f7137eb`、`version_consistent=false`；容器 `deployed_at=2026-08-10 15:09:57`；服务器仓库与 GitHub/Gitee 均为 `287d05d`，但镜像 `.build-commit` 缺失、宿主机 `.deploy-commit` 为 `f7137eb`。先用 `redeploy.sh` 重建镜像恢复“镜像、容器、部署记录”三者一致，再决定重跑验收；禁止把旧云端结果归因于当前代码。
2. 若继续工作台代码任务，按 `AI_CONTEXT.md` 的任务路由阅读稳定设计。
3. 当前工作树干净；本地/远程均已在 `287d05d`，无未提交代码变更。

## 活跃记录（最多 10 条）

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

### 5. 长投标文件上限统一（已提交）

- 单个 PDF 的解析、综合评审和查重上限由 2000 页统一提高到 2500 页；解析文本保护同步由 200 万提高到 250 万字符。
- 独立三文件查重入口总页数预算由 4000 页调整为 7500 页，以容纳一份招标文件和两份投标文件；1200 万字符总预算、OCR/图片页数和并发限制保持不变，避免放大 2 核 2 GB 服务器峰值资源。
- 未改变全文分块、查重算法、AI 提示词、OCR/多模态策略、API 字段或存量结果。

## 已拒绝或暂停的路线

- 不把动态进度继续追加到稳定设计中；完成并验收的过程交给 Git 保存。
- 不为单个黄金项目添加生产关键词、页码或文本补丁；黄金项目只作为通用机制的回归资产。
- EvidencePack 与分层缓存仍按稳定设计第 10 节的门槛推进，未满足门槛前不一次性替换主链。

## 回退

- 本次文档重组不影响运行时；如需回退，只恢复文档路径和根 `AGENTS.md` 索引。
- 代码行为回退点以 Git 提交为准，禁止依据本文件手工复刻旧实现。
