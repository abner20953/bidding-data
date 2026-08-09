# 当前 AI 接力状态

```yaml
handoff_schema: 1
updated_at: 2026-08-10
module: evaluation-workbench
status: local_changes
base_commit: c28941e
branch: main
working_tree: documentation_staged_code_changes_unstaged
remote_github: c28941e
remote_gitee: c28941e
production_commit: unknown
prompt_version: vision-evidence-contract-v55
database_change: none
user_approval: approve_general_cz_reliability_fix_no_commit_push_deploy
```

元数据不是部署事实的替代品。`production_commit` 只能来自云端 build-info、任务运行时版本或服务器核验；不知道时必须保持 `unknown`。

## 下一位先做

1. 执行 `git status --short` 和 `git log -5 --oneline --decorate`，核对本文件元数据。
2. 若继续工作台代码任务，按 `AI_CONTEXT.md` 的任务路由阅读稳定设计。
3. 若处理 CZ 规则提取验收，先确认云端运行版本已更新到本轮提交，再决定是否重跑；禁止把旧云端结果归因于当前代码。

## 活跃记录（最多 10 条）

### 1. 跨 AI 文档重组（本地完成，已暂存）

- 目标：将稳定设计与动态接力分离，为 Codex、其他 AI 工具和人工开发者提供同一份入口、事实来源和更新规则。
- 范围：新增本目录入口、交接、协作规范和校验脚本；稳定设计移入本目录并删除动态日志；根 `AGENTS.md` 只增加入口链接。
- 不变契约：不修改业务代码、数据库、API、提示词、任务调度、OCR/多模态或云端配置。
- 验证：`validate_docs.py`、工作树与暂存区 `git diff --check` 均通过；Git 已将稳定设计识别为重命名，未丢失正文。
- 未完成：尚未提交、推送或部署；是否提交仍需用户确认。

### 2. 评分原文连续页组装与综合评审隔离（仓库已同步，云端待核验）

- 提交：`c28941e`，本地 `main`、GitHub 与 Gitee 当前均指向该提交。
- 范围：评分自动重组只接受唯一明确总分；无可靠总分或多包多总分时不猜测；全表修复必须保持来源条款分值/类别锚点；非评分跨类别候选不合并；编译子项只在父规则候选页内排序，不扩张 OCR/图片预算。
- 已验证：工作台与 AI 网关 455 项测试通过，`git diff --check` 通过（依据该提交前记录）。
- 待核验：探测到云端仓库为 `c28941e`，但运行容器的 `/app/.build-commit` 为 `unknown`、`DEPLOY_COMMIT` 为空；CZ 最新任务仍记录 `f7137eb`。必须先用 `redeploy.sh` 恢复镜像、容器和任务版本三者一致，再验收；否则不能归因于当前仓库代码。
- 主要文件：`storage.py`、`worker.py`、`tests/test_evaluation_workbench.py`、稳定设计。

### 3. CZ 两模型规则提取差异（本地修复完成，待提交部署）

- 对比对象：同一文件、同一 v55 提示词、同一旧运行提交 `f7137eb`；MiniMax M3 产生 48 条 AI 规则、DeepSeek V4 Flash 产生 33 条。
- 关键事实：MiniMax 有 6/18 次调用依赖本地 JSON 修复，两个关键补漏调用只返回 10/12 token，评分总分为 94/100；DeepSeek 评分为 100/100，但语义编译失败而保留了更多重复/宽泛规则。
- 已实施：镜像 `.build-commit` 存在但无效时，任务与蓝点均明确显示 `unknown`，不再用环境变量、部署记录或 Git 冒充运行版本；评分遗漏/重复归属/总分不守恒时只做定向严格重试和一次全表重组，仍异常则以 `partial_success` 保存草稿并阻断确认；义务编译 JSON 异常时只用原模板重试一次，仍失败回退确定性同源收口。
- 验证：`tests.test_evaluation_workbench` 与 `tests.test_evaluation_workbench_ai_gateway` 共 461 项通过；新增镜像标记、跨组评分恢复、评分异常判断、补漏失败保留主结果和编译严格重试回归测试。
- 待完成：尚未提交、推送或部署；云端镜像仍须用 `redeploy.sh` 重建后，分别以 DeepSeek V4 Flash 与 MiniMax M3 重跑同一项目验收。不得为 CZ 或某类条款写特例。

## 已拒绝或暂停的路线

- 不把动态进度继续追加到稳定设计中；完成并验收的过程交给 Git 保存。
- 不为单个黄金项目添加生产关键词、页码或文本补丁；黄金项目只作为通用机制的回归资产。
- EvidencePack 与分层缓存仍按稳定设计第 10 节的门槛推进，未满足门槛前不一次性替换主链。

## 回退

- 本次文档重组不影响运行时；如需回退，只恢复文档路径和根 `AGENTS.md` 索引。
- 代码行为回退点以 Git 提交为准，禁止依据本文件手工复刻旧实现。
