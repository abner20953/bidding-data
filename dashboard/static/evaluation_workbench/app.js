(() => {
  const api = '/api/evaluation-workbench';
  let activeProject = null;
  let poller = null;
  let wasTaskActive = false;
  let lastActiveTaskId = null;
  let lastCompareTask = null;
  let lastPartialDocumentsKey = '';
  let globalRules = [];
  let visionConfiguration = {enabled:false, default_profile_id:null};
  let ocrConfiguration = {enabled:false, services:[]};
  let hasCurrentRules = false;
  const defaultDocumentTitle = document.title;
  let completionTicker = null;
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  async function request(path, options = {}) { let response; try { response = await fetch(`${api}${path}`, options); } catch (_) { throw new Error('无法连接本地服务，请确认程序仍在运行后刷新页面重试'); } let data; try { data = await response.json(); } catch (_) { data = {error:`请求失败（HTTP ${response.status}）`}; } if (!response.ok) throw new Error(data.error || '请求失败'); return data; }
  // 检查规则已单列展示；结果区只保留投标文件事实与 AI 判断，避免模型偶尔再复述规则。
  function resultExplanation(value, rule) {
    let text = String(value || '').trim();
    const candidates = [rule?.check_rule, rule?.title].map((item) => String(item || '').trim()).filter((item) => item.length >= 6).sort((a, b) => b.length - a.length);
    candidates.forEach((item) => { text = text.split(item).join(''); });
    return text.replace(/(?:本|该)?规则(?:要求|规定|需|是)?[：:，,；;\s]*/g, '').replace(/^[，。；:：\s]+|[，；:：\s]+$/g, '').trim();
  }
  function compactObjectiveOcrText(value) {
    let text = String(value || '');
    text = text.replace(/【(腾讯OCR|本地OCR|OCR)原文·([^】]+)】[\s\S]*?(?=【(?:图片识别|腾讯OCR|本地OCR|OCR)|$)/g, (_all, source, meta) => `【${source}摘要·${meta}】已完成候选页文字识别；原始识别明细已省略，评分依据以前述AI总结为准。\n`);
    return text.replace(/【(腾讯OCR|本地OCR|OCR)·([^】]+)】([\s\S]*?)(?=【(?:图片识别|腾讯OCR|本地OCR|OCR)|$)/g, (_all, source, meta, body) => {
      const concise = String(body || '').replace(/\s+/g, ' ').trim();
      const summary = concise.length > 220 ? `${concise.slice(0, 220)}…` : concise;
      return `【${source}摘要·${meta}】${summary || '已完成候选页文字识别。'}\n`;
    }).trim();
  }
  // 结果库为审计兼容保留了文字、OCR、图片多阶段的完整记录；页面主表只显示
  // 可立即判断的结论。这样不会把逐页 OCR 转写和旧阶段结论混在一起淹没重点。
  function conciseText(value, limit = 180) {
    const normalized = String(value || '').replace(/【[^】]+】/g, ' ').replace(/\s+/g, ' ').trim();
    if (!normalized) return '';
    const sentences = normalized.split(/(?<=[。；;！？!?])/).map((item) => item.trim()).filter(Boolean);
    const useful = sentences.filter((item) => !/^(?:本|该)?规则(?:要求|规定|需|是)?[：:，,；;\s]*/.test(item));
    const text = (useful[0] || sentences[0] || normalized).trim();
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  }
  function evidenceLayerSummary(result) {
    const layers = Array.isArray(result?.evidence_layers) ? result.evidence_layers.filter((item) => item && typeof item === 'object' && item.summary) : [];
    // 最新的图片/ OCR 已是对文字阶段的补充，优先展示；原始完整内容仍可展开追溯。
    const preferred = [...layers].reverse().find((item) => ['vision', 'tencent_ocr', 'local_ocr'].includes(item.source));
    if (!preferred) return '';
    const pages = Array.isArray(preferred.evidence_pages) && preferred.evidence_pages.length
      ? preferred.evidence_pages : (Array.isArray(preferred.checked_pages) ? preferred.checked_pages : []);
    const source = {vision:'图片识别', tencent_ocr:'腾讯 OCR', local_ocr:'本地 RapidOCR'}[preferred.source] || '补充识别';
    const pageText = pages.length ? `（${pages.map((page) => `P${page}`).join('、')}）` : '';
    const rawSummary = result?.max_score != null ? compactObjectiveOcrText(preferred.summary) : preferred.summary;
    const summary = conciseText(rawSummary, result?.max_score != null ? 110 : 150);
    return `${source}${pageText}${summary ? `：${summary}` : '：已完成关键页核验。'}`;
  }
  function conciseResultEvidence(result) {
    return evidenceLayerSummary(result) || conciseText(result?.evidence, 180) || '-';
  }
  function conciseResultReason(result) {
    const status = String(result?.vision_status || '');
    const verification = /(?:applied|partial|uncovered|conflict)/.test(status) ? conciseText(result?.vision_message, 120) : '';
    const reason = result?.max_score != null ? compactObjectiveOcrText(result?.reason) : result?.reason;
    return verification || conciseText(reason, result?.max_score != null ? 120 : 150);
  }
  function rawResultDetailHtml(result) {
    const evidence = String(result?.evidence || '').trim();
    const reason = String(result?.reason || '').trim();
    if (!evidence && !reason) return '';
    return `<details class="evidence-chain"><summary>查看完整文字结论</summary>${evidence ? `<div class="evidence-layer"><strong>文字证据</strong><span>${escapeHtml(evidence)}</span></div>` : ''}${reason ? `<div class="evidence-layer"><strong>文字理由</strong><span>${escapeHtml(reason)}</span></div>` : ''}</details>`;
  }
  function roleLabel(role) { return {tender:'主招标文件', tender_attachment:'招标附件', bid:'投标文件'}[role] || role; }
  function parseStatusLabel(status) { return {pending:'待解析',queued:'排队中',running:'解析中',success:'解析完成',error:'解析失败'}[status] || status || '-'; }
  // 项目名称允许重复，标段/包号才是日常操作时最直观的区分信息。
  function projectDisplayName(project) { const name = String(project?.name || '未命名项目'); const section = String(project?.section_name || '').trim(); return section ? `${name} · ${section}` : name; }
  function projectMeta(project) { const number = String(project?.project_number || '').trim(); return number ? `项目编号：${number}` : '未填写项目编号'; }
  function taskElapsed(task) {
    if (!task || task.status !== 'success' || !['extract_rules', 'evaluate_all'].includes(task.task_type)) return '';
    const startedAt = Date.parse(task.started_at || task.created_at || '');
    const finishedAt = Date.parse(task.finished_at || task.updated_at || '');
    if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt) || finishedAt < startedAt) return '';
    const elapsedSeconds = Math.round((finishedAt - startedAt) / 1000);
    const minutes = Math.floor(elapsedSeconds / 60);
    const seconds = elapsedSeconds % 60;
    return ` · 耗时 ${minutes ? `${minutes}分${seconds}秒` : `${seconds}秒`}`;
  }
  function taskTypeLabel(type) { return {parse_documents:'文件解析',compare_documents:'文件查重',extract_rules:'规则提取',review_documents:'文件审查',score_objective:'客观评分',score_subjective:'主观评分',evaluate_all:'综合评审'}[type] || type || '任务'; }
  function queueDetail(context) {
    if (!context) return '';
    const waiting = Number(context.waiting_count || 0);
    const active = context.active_task;
    if (!active) return `<br><small class="queue-detail">已进入全局队列${waiting ? `，前方还有 ${waiting} 个任务` : '，正在等待工作进程启动'}。</small>`;
    const activeName = projectDisplayName({name:active.project_name, section_name:active.section_name});
    const progress = Math.max(0, Math.min(100, Number(active.progress || 0)));
    const phase = taskTypeLabel(active.task_type);
    const message = String(active.message || '').trim();
    return `<br><small class="queue-detail">正在执行「${escapeHtml(activeName)}」的${escapeHtml(phase)}（${progress}%）${message ? `：${escapeHtml(message)}` : ''}；前方还有 ${waiting} 个任务。</small>`;
  }
  function taskText(task, queueContext = null) { const labels = {queued:'排队中',running:'运行中',success:'已完成',error:'失败',cancelled:'已取消',interrupted:'已中断'}; const partial = task?.result?.completion_state === 'partial_success'; const failed = partial && Array.isArray(task?.result?.failed_units) ? task.result.failed_units.length : 0; const label = partial ? '部分完成' : (labels[task?.status] || escapeHtml(task?.status)); const retry = partial && task?.task_type === 'evaluate_all' ? `<br><button class="retry-failed-evaluation" data-task="${escapeHtml(task.task_id)}">仅重跑失败项</button>` : ''; return task ? `<span class="status-${task.status}">${label} ${task.progress || 0}% ${escapeHtml(task.message || '')}${partial ? `<br><small>有 ${failed} 个规则组可单独重跑；其余结果已保留。</small>` : ''}${retry}${task.status === 'queued' ? queueDetail(queueContext) : ''}${taskElapsed(task)}${task.error ? `<br>${escapeHtml(task.error)}` : ''}</span>` : '暂无任务'; }
  function stopCompletionTicker() { if (completionTicker) clearInterval(completionTicker); completionTicker = null; document.title = defaultDocumentTitle; }
  function startCompletionTicker(task) {
    const labels = {parse_documents:'文件解析', compare_documents:'文件查重', extract_rules:'规则提取', evaluate_all:'综合评审'};
    const label = labels[task?.task_type];
    if (!label) return;
    if (completionTicker) clearInterval(completionTicker);
    const message = `【${label}已完成】点击页面停止播报　　`;
    let offset = 0;
    const render = () => { document.title = `${message.slice(offset)}${message.slice(0, offset)}`; offset = (offset + 1) % message.length; };
    render();
    completionTicker = setInterval(render, 350);
  }
  document.addEventListener('click', () => { if (completionTicker) stopCompletionTicker(); }, true);
  async function loadProjects() { const data = await request('/projects'); $('projects').innerHTML = data.projects.length ? data.projects.map((p) => `<article class="card" data-project="${p.project_id}"><h3>${escapeHtml(projectDisplayName(p))}</h3><p>${escapeHtml(projectMeta(p))}</p><p>${p.document_count || 0} 份文件 · ${p.bid_count || 0} 份投标文件</p></article>`).join('') : '<p class="muted">尚未创建评标项目。</p>'; document.querySelectorAll('[data-project]').forEach((node) => node.onclick = () => openProject(node.dataset.project)); }
  function stopPolling() { if (poller) clearInterval(poller); poller = null; }
  function startPolling() { if (!poller) poller = setInterval(() => refreshProject().catch((error) => { stopPolling(); $('task-status').textContent = error.message; }), 2500); }
  function resetProjectPanels() { ['documents','rules','review-results','objective-results','subjective-results'].forEach((id) => { const node = $(id); if (node) node.innerHTML = '<p class="muted">正在加载当前项目…</p>'; }); $('token-usage').textContent = '模型用量：正在加载当前项目…'; $('task-status').textContent = '正在加载当前项目…'; lastPartialDocumentsKey = ''; }
  async function openProject(id) { activeProject = id; wasTaskActive = false; lastActiveTaskId = null; lastCompareTask = null; resetProjectPanels(); stopCompletionTicker(); $('projects-panel').classList.add('hidden'); $('project-form').classList.add('hidden'); $('workspace').classList.remove('hidden'); await refreshProject(); await loadProfiles(); await refreshRules(); await refreshReview(); await refreshScores(); await refreshUsage(); }
  async function refreshUsage() { if (!activeProject) return; const data = await request(`/projects/${activeProject}/token-usage`); const u = data.usage; const localPerf = u.local_ocr_performance || {}; if (!u.call_count && !u.ocr_requests && !u.local_ocr_pages && !localPerf.run_count) { $('token-usage').textContent = '模型用量：尚无调用记录'; return; } const detail = u.metered_calls ? `输入 ${u.prompt_tokens.toLocaleString()} / 输出 ${u.completion_tokens.toLocaleString()} / 合计 ${u.total_tokens.toLocaleString()} Token` : `模型接口未返回 Token；已发送 ${u.input_chars.toLocaleString()} 字符`; const families = u.families || {}; const extras = []; if (families.vision && families.vision.call_count) extras.push(`图片识别 ${families.vision.call_count} 次`); if (u.ocr_requests) extras.push(`腾讯 OCR ${u.ocr_requests} 页`); if (u.local_ocr_pages || localPerf.run_count) { let localLabel = `本地 OCR ${u.local_ocr_pages || 0} 页`; if (localPerf.average_ms_per_page) localLabel += `，平均 ${(localPerf.average_ms_per_page / 1000).toFixed(1)} 秒/页`; if (localPerf.peak_rss_kb) localLabel += `，峰值约 ${Math.ceil(localPerf.peak_rss_kb / 1024)} MB`; extras.push(localLabel); } const cache = u.prompt_tokens ? `；缓存命中 ${Math.round((u.cache_hit_tokens || 0) * 100 / u.prompt_tokens)}%` : ''; $('token-usage').textContent = `模型用量：${detail}（${u.call_count} 次调用${extras.length ? '；其中' + extras.join('、') : ''}${cache}）`; }
  async function refreshProject() { if (!activeProject) return; const data = await request(`/projects/${activeProject}`); const p = data.project; $('workspace-name').textContent = projectDisplayName(p); $('workspace-meta').textContent = projectMeta(p); const active = data.tasks.find((t) => ['queued','running'].includes(t.status)); if (active) lastActiveTaskId = active.task_id; $('task-status').innerHTML = taskText(active || data.tasks[0], data.queue_contexts?.[active?.task_id]); document.querySelectorAll('.retry-failed-evaluation').forEach((button) => button.onclick = () => queue('evaluate_all', {retry_failed_task_id:button.dataset.task})); if (active) startPolling(); else stopPolling(); renderDocuments(data.documents); const completed = data.tasks.find((t) => t.task_type === 'compare_documents' && t.status === 'success'); if (completed && completed.task_id !== lastCompareTask) { lastCompareTask = completed.task_id; await renderCompare(completed.task_id, data.documents); } const completedDocuments = active?.task_type === 'evaluate_all' ? (active.completed_documents || []) : []; const partialKey = completedDocuments.length ? `${active.task_id}:${completedDocuments.map((item) => item.document_id).sort().join(',')}` : ''; if (partialKey && partialKey !== lastPartialDocumentsKey) { lastPartialDocumentsKey = partialKey; await Promise.all([refreshReview(), refreshScores()]); } if (!active) lastPartialDocumentsKey = ''; const justFinished = wasTaskActive && !active; const finishedTask = justFinished ? data.tasks.find((task) => task.task_id === lastActiveTaskId && task.status === 'success') : null; wasTaskActive = Boolean(active); if (justFinished) { lastActiveTaskId = null; await Promise.all([refreshRules(), refreshReview(), refreshScores(), refreshUsage()]); if (finishedTask) startCompletionTicker(finishedTask); } }
  function renderDocuments(documents) { $('documents').innerHTML = documents.length ? `<table><thead><tr><th>角色</th><th>文件</th><th>投标人</th><th>解析</th><th>页数/字符</th><th>操作</th></tr></thead><tbody>${documents.map((d) => `<tr><td><span class="tag">${roleLabel(d.role)}</span></td><td>${escapeHtml(d.original_name)}</td><td>${escapeHtml(d.bidder_name || '-')}</td><td class="status-${d.parse_status}">${escapeHtml(parseStatusLabel(d.parse_status))}${d.parse_error ? `<br>${escapeHtml(d.parse_error)}` : ''}</td><td>${d.page_count ?? '-'} / ${d.text_length ?? '-'}</td><td><button class="delete-document" data-document="${d.document_id}">删除</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">尚未上传文件。</p>'; $('documents').querySelectorAll('.delete-document').forEach((button) => button.onclick = async () => { if (!confirm('删除文件会同时移除其历史审查和评分结果，是否继续？')) return; try { await request(`/projects/${activeProject}/documents/${button.dataset.document}`, {method:'DELETE'}); await refreshProject(); await refreshReview(); await refreshScores(); } catch (error) { alert(error.message); } }); }
  function priorityLabel(value) { return {high:'高',medium:'中',normal:'常规',none:'无'}[value] || value; }
  function evidenceText(signal) { return (signal.evidence || []).map((item) => { const pages = item.page_a || item.page_b ? `${signal.bidder_a || '文件 A'}第${item.page_a || '?'}页 / ${signal.bidder_b || '文件 B'}第${item.page_b || '?'}页：` : ''; const content = item.text_a || item.value || item.field || ''; const right = item.text_b && item.text_b !== item.text_a ? ` ↔ ${item.text_b}` : ''; return `${pages}${content}${right}`; }).join('\n'); }
  async function renderCompare(taskId, documents) {
    const data = await request(`/tasks/${taskId}/compare-results`); const names = Object.fromEntries(documents.map((d) => [d.document_id, d.bidder_name || d.original_name])); const analysis = data.analysis;
    const rawTable = data.pairs.length ? `<h4>底层比对摘要（固定信号汇总与 AI 复核前）</h4><p class="hint">本表用于算法追溯，数量包含底层查重保留、但随后可能被固定表单规则或 AI 公共来源判断排除的候选；人工复核请以上方有效线索为准。</p><table><thead><tr><th>文件对</th><th>完全/近似候选</th><th>共同错误候选</th><th>敏感实体</th><th>候选匹配占比 A/B</th></tr></thead><tbody>${data.pairs.map((pair) => { const s = pair.result.summary || {}; return `<tr><td>${escapeHtml(names[pair.document_a_id])}<br>↔ ${escapeHtml(names[pair.document_b_id])}</td><td>${s.exact || 0} / ${s.fuzzy || 0}</td><td>${s.shared_error || 0}</td><td>${s.entity || 0}</td><td>${s.matched_ratio_a || 0}% / ${s.matched_ratio_b || 0}%</td></tr>`; }).join('')}</tbody></table>` : '<p class="muted">任务尚未生成文件对结果。</p>';
    if (!analysis) { $('compare-results').innerHTML = rawTable; return; }
    const dimLabels = Object.fromEntries((analysis.executed_dimensions || []).map((item) => [item.dimension, item.label]));
    const pairTable = `<h4>横向复核优先级</h4><table><thead><tr><th>文件对</th><th>有效独立维度</th><th>有效线索</th><th>复核优先级</th></tr></thead><tbody>${(analysis.pair_summaries || []).map((item) => { const rawCount = Number(item.raw_signal_count ?? item.signal_count ?? 0); const effectiveCount = Number(item.signal_count || 0); const countText = rawCount > effectiveCount ? `${effectiveCount}（底层 ${rawCount}）` : String(effectiveCount); return `<tr><td>${escapeHtml(item.bidder_a)} ↔ ${escapeHtml(item.bidder_b)}</td><td>${item.independent_dimension_count}（${escapeHtml(item.dimensions.map((key) => dimLabels[key] || key).join('、') || '未发现')}）</td><td>${escapeHtml(countText)}</td><td><span class="priority-${item.review_priority}">${priorityLabel(item.review_priority)}</span></td></tr>`; }).join('')}</tbody></table>`;
    const signals = analysis.signals || [];
    // 固定规则线索按 AI 风险从高到低展示；同一风险保持原始检测顺序，便于回溯原始结果。
    const orderedSignals = [...signals].sort((left, right) => riskRank(right.ai_assessment?.risk_level) - riskRank(left.ai_assessment?.risk_level));
    const ai = analysis.ai_assessment || {}; const aiDecision = {confirmed_clue:'AI确认线索', suspected_clue:'AI疑似线索', excluded:'AI倾向排除', unassessable:'AI证据不足'};
    const signalTable = signals.length ? `<h4>固定规则线索与 AI 判定</h4><p class="hint">AI仅复核本地算法提取的短证据包，不读取完整文件对；仍不作串标、废标或扣分认定。${ai.status === 'success' ? `已判定 ${ai.assessed_count || 0} 条线索。` : escapeHtml(ai.reason || '')}</p><table><thead><tr><th>文件对 / 维度</th><th>证据</th><th>AI 判定</th><th>反证提示</th></tr></thead><tbody>${orderedSignals.map((signal) => { const assessment = signal.ai_assessment || {}; return `<tr><td>${escapeHtml(signal.bidder_a)} ↔ ${escapeHtml(signal.bidder_b)}<br><span class="tag">${escapeHtml(signal.dimension_label)}</span></td><td>${escapeHtml(signal.basis)}<pre class="evidence">${escapeHtml(evidenceText(signal) || '详见原始文件对结果')}</pre></td><td>${escapeHtml(aiDecision[assessment.decision] || '等待 AI 判定')}<br><small>${escapeHtml(assessment.reason || '')}</small><br><small>风险：${escapeHtml(riskLabel(assessment.risk_level))} · 置信度：${escapeHtml(confidenceLabel(assessment.confidence))}<br>${escapeHtml(assessment.suggested_check || '')}</small></td><td>${escapeHtml((signal.counter_evidence || []).join('；') || '-')}</td></tr>`; }).join('')}</tbody></table>` : '<p class="muted">本次未检出可报告的横向异常线索。未检出不等同于不存在其他风险。</p>';
    const skipped = (analysis.not_executed_dimensions || []).map((item) => `${item.label}：${item.reason}`).join('；');
    $('compare-results').innerHTML = `<div class="decision-boundary">${escapeHtml(analysis.decision_boundary)}</div><p class="hint">${escapeHtml(analysis.methodology.template_filter_note)}。多维命中仅提高人工复核优先级。未执行维度：${escapeHtml(skipped)}</p>${pairTable}${signalTable}${rawTable}`;
  }
  async function queue(taskType, options = {}) { try { await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:taskType, ...options})}); await refreshProject(); } catch (error) { alert(error.message); } }
  let modelProfiles = [];
  const modelPresets = {
    'minimax-m27': {displayName:'MiniMax M2.7', baseUrl:'https://api.minimaxi.com/v1', modelName:'MiniMax-M2.7', jsonMode:'false', thinking:'default', supportsVision:false},
    // M3 当前通过提示词与本地恢复保证结构化输出；保留兼容开关，但界面会如实显示
    // 其不是服务商强制 JSON Schema，避免误以为可以完全消除格式异常。
    // 即使已知型号可能支持多模态，也不自动声明能力；须由管理员按实际账号/API 权限手动勾选。
    'minimax-m3': {displayName:'MiniMax M3', baseUrl:'https://api.minimaxi.com/v1', modelName:'MiniMax-M3', jsonMode:'true', thinking:'disabled', supportsVision:false},
  };
  function normalizedThinkingMode(modelName, thinkingMode) { if (String(modelName).toLowerCase() === 'minimax-m3' && thinkingMode === 'enabled') return 'adaptive'; if (String(modelName).toLowerCase().startsWith('minimax-m2') && thinkingMode === 'disabled') return 'default'; return thinkingMode; }
  function thinkingLabel(profile) { return {default:'默认', enabled:'启用', adaptive:'自适应', disabled:'禁用'}[normalizedThinkingMode(profile.model_name, profile.thinking_mode)] || profile.thinking_mode; }
  function resetModelForm() { $('model-profile-id').value = ''; $('model-form-title').textContent = '新增 OpenAI-compatible 模型'; for (const id of ['model-display-name','model-base-url','model-name','model-api-key']) $(id).value = ''; $('model-preset').value = ''; $('model-json-mode').value = 'true'; $('model-thinking').value = 'default'; $('model-supports-vision').checked = false; }
  function applyModelPreset() { const preset = modelPresets[$('model-preset').value]; if (!preset) return; $('model-display-name').value = preset.displayName; $('model-base-url').value = preset.baseUrl; $('model-name').value = preset.modelName; $('model-json-mode').value = preset.jsonMode; $('model-thinking').value = preset.thinking; $('model-supports-vision').checked = Boolean(preset.supportsVision); }
  function renderOcrConfiguration() {
    const tencentEnabled = Boolean(ocrConfiguration.tencent_enabled ?? ocrConfiguration.enabled); $('ocr-region').value = ocrConfiguration.region || 'ap-guangzhou';
    $('tencent-ocr-enabled').checked = tencentEnabled;
    const source = {manual:'手动保存', environment:'运行环境变量', none:'未配置'}[ocrConfiguration.credentials_source] || '未配置';
    const readiness = ocrConfiguration.local?.readiness || {};
    const readinessHint = readiness.ready_for_manual_validation ? '已积累至少30页、3个项目的本地OCR样本：建议按设计文档发起第二阶段人工验收；未人工确认前不会自动切换优先级。' : `本地验收样本：${Number(readiness.sample_pages) || 0}/30页、${Number(readiness.sample_projects) || 0}/3个项目。`;
    const localReady = Boolean(ocrConfiguration.local?.runtime_available);
    $('local-ocr-status').textContent = localReady ? `本地 RapidOCR 已就绪：作为所有图片文字核验的基础路径，按批启动、结束即释放内存。${readinessHint}` : '本地 RapidOCR 运行环境未就绪；重新部署后会自动恢复。系统会保留原文字结论并提示未完成基础识别。';
    $('ocr-configuration-hint').textContent = `腾讯云状态：${tencentEnabled ? `已启用（凭据：${source}）` : '未启用，不会发送腾讯云 OCR 请求'}。本月 ${ocrConfiguration.month_key || '-'}；开启后只对本地 OCR 已定位的关键字段、证照或低置信页面升级复核；缓存命中不消耗额度。`;
    $('ocr-services').innerHTML = (ocrConfiguration.services || []).map((item) => `<div class="ocr-service-row"><label class="inline-check"><input class="ocr-service-enabled" data-service="${escapeHtml(item.service)}" type="checkbox" ${item.enabled ? 'checked' : ''}> ${escapeHtml(item.label)}${item.legacy ? '（仅账号支持时启用）' : ''}</label><label>安全上限<input class="ocr-service-limit" data-service="${escapeHtml(item.service)}" type="number" min="1" max="1000" value="${Number(item.monthly_limit) || 900}"></label><small>${escapeHtml(item.usage || '由系统按规则场景自动选择')} · 本月已用 ${Number(item.used) || 0} · 预计可用 ${Number(item.remaining) || 0}</small></div>`).join('') || '<p class="muted">暂无 OCR 接口设置。</p>';
  }
  async function loadProfiles() {
    const [data, visionData, ocrData] = await Promise.all([request('/model-profiles'), request('/vision-configuration'), request('/tencent-ocr-configuration')]); modelProfiles = data.profiles; visionConfiguration = visionData.configuration || visionConfiguration; ocrConfiguration = ocrData.configuration || ocrConfiguration; renderOcrConfiguration();
    const active = data.profiles.filter((p) => p.enabled);
    const options = active.map((p) => `<option value="${p.profile_id}">${escapeHtml(p.display_name)} · ${escapeHtml(p.model_name)}${p.api_key_configured ? '' : '（未配置密钥）'}</option>`).join('') || '<option value="">暂无可用模型档案</option>';
    for (const id of ['rule-profile','all-profile']) $(id).innerHTML = options;
    const defaultProfile = active.find((p) => p.is_default) || active[0];
    if (defaultProfile) for (const id of ['rule-profile','all-profile']) $(id).value = defaultProfile.profile_id;
    const visionProfiles = active.filter((p) => p.capabilities?.vision);
    $('vision-default-profile').innerHTML = `<option value="">请选择多模态模型</option>${visionProfiles.map((p) => `<option value="${p.profile_id}">${escapeHtml(p.display_name)} · ${escapeHtml(p.model_name)}</option>`).join('')}`;
    $('vision-default-profile').value = visionProfiles.some((p) => p.profile_id === visionConfiguration.default_profile_id) ? visionConfiguration.default_profile_id : '';
    $('vision-enabled').checked = Boolean(visionConfiguration.enabled);
    $('vision-model-field').classList.toggle('is-disabled', !visionConfiguration.enabled);
    $('vision-default-profile').disabled = !visionConfiguration.enabled;
    const rows = data.profiles.map((p) => {
      const keyStatus = p.api_key_configured ? `<span class="tag">已配置</span>（${p.api_key_source === 'manual' ? '手动' : '环境变量'}）` : '<span class="muted">未配置</span>';
      const enabled = Boolean(p.enabled);
      const caps = p.capabilities || {}; const structure = caps.structured_output === 'json_object' ? '原生 JSON' : '提示词约束 JSON'; const vision = caps.vision ? '支持图片识别' : '仅文本';
      return `<article class="model-profile-card${enabled ? '' : ' is-disabled'}"><div><h4>${escapeHtml(p.display_name)}${p.is_default ? ' <span class="tag">默认模型</span>' : ''}${p.is_default_vision ? ' <span class="tag">默认图片模型</span>' : ''}${enabled ? '' : ' <span class="tag model-disabled-tag">已禁用</span>'}</h4><dl class="model-profile-details"><div><dt>模型 ID</dt><dd>${escapeHtml(p.model_name)}</dd></div><div><dt>Base URL</dt><dd>${escapeHtml(p.base_url)}</dd></div><div><dt>API Key</dt><dd>${keyStatus}</dd></div><div><dt>思考模式</dt><dd>${escapeHtml(thinkingLabel(p))}</dd></div><div><dt>结构化输出</dt><dd>${escapeHtml(structure)}；${escapeHtml(vision)}</dd></div></dl></div><div class="model-profile-actions">${enabled ? (p.is_default ? '<span class="muted">当前全局默认</span>' : `<button class="set-default-model" data-profile="${p.profile_id}">设为默认</button>`) : '<span class="muted">不会出现在项目模型列表</span>'}${enabled && caps.vision && !p.is_default_vision ? `<button class="set-default-vision" data-profile="${p.profile_id}">设为默认图片模型</button>` : ''}<button class="edit-model" data-profile="${p.profile_id}">编辑</button>${enabled ? `<button class="test-model" data-profile="${p.profile_id}">测试连接</button>` : ''}<button class="toggle-model" data-profile="${p.profile_id}" data-enabled="${enabled ? 'true' : 'false'}">${enabled ? '禁用模型' : '启用模型'}</button><button class="delete-model danger" data-profile="${p.profile_id}">删除</button></div></article>`;
    }).join('');
    $('model-profiles').innerHTML = data.profiles.length ? `<div class="model-profile-cards">${rows}</div>` : '<p class="muted">暂无模型档案。</p>';
    $('model-profiles').querySelectorAll('.edit-model').forEach((button) => button.onclick = () => { const profile = modelProfiles.find((item) => item.profile_id === button.dataset.profile); if (!profile) return; $('models-panel').classList.remove('hidden'); $('model-profile-id').value = profile.profile_id; $('model-form-title').textContent = `编辑模型：${profile.display_name}`; $('model-preset').value = ''; $('model-display-name').value = profile.display_name; $('model-base-url').value = profile.base_url; $('model-name').value = profile.model_name; $('model-api-key').value = ''; $('model-json-mode').value = String(Boolean(profile.json_mode)); $('model-thinking').value = normalizedThinkingMode(profile.model_name, profile.thinking_mode); $('model-supports-vision').checked = Boolean(profile.supports_vision); });
    $('model-profiles').querySelectorAll('.test-model').forEach((button) => button.onclick = async () => { try { const data = await request(`/model-profiles/${button.dataset.profile}/test`, {method:'POST'}); alert(data.message); } catch (error) { alert(error.message); } });
    $('model-profiles').querySelectorAll('.set-default-model').forEach((button) => button.onclick = async () => { try { await request(`/model-profiles/${button.dataset.profile}/default`, {method:'POST'}); await loadProfiles(); } catch (error) { alert(error.message); } });
    $('model-profiles').querySelectorAll('.set-default-vision').forEach((button) => button.onclick = async () => { try { await request('/vision-configuration', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:Boolean(visionConfiguration.enabled), default_profile_id:button.dataset.profile})}); await loadProfiles(); } catch (error) { alert(error.message); } });
    $('model-profiles').querySelectorAll('.toggle-model').forEach((button) => button.onclick = async () => { const enabled = button.dataset.enabled !== 'true'; const action = enabled ? '启用' : '禁用'; if (!confirm(`${action}该模型${enabled ? '后将重新出现在项目模型列表中' : '后将不再出现在项目模型列表中'}，是否继续？`)) return; try { await request(`/model-profiles/${button.dataset.profile}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled})}); if ($('model-profile-id').value === button.dataset.profile) resetModelForm(); await loadProfiles(); } catch (error) { alert(error.message); } });
    $('model-profiles').querySelectorAll('.delete-model').forEach((button) => button.onclick = async () => { if (!confirm('删除模型档案后，所有项目将无法再选择该模型；历史结果会保留。是否继续？')) return; try { await request(`/model-profiles/${button.dataset.profile}`, {method:'DELETE'}); if ($('model-profile-id').value === button.dataset.profile) resetModelForm(); await loadProfiles(); } catch (error) { alert(error.message); } });
  }
  function categoryLabel(value) { return {qualification:'资格性', compliance:'符合性', substantive:'实质性/废标项', rejection:'实质性/废标项', other:'其他规则', objective:'客观分', subjective:'主观分'}[value] || value; }
  const acquisitionPresetLabels = {smart:'智能升级', always:'每次都升级', text:'腾讯 OCR 字段复核', visual:'签章/外观核验', dual:'双重增强核验', off:'仅基础识别', custom:'自定义高级设置'};
  const acquisitionLevelLabels = {off:'关闭', low:'快速', standard:'标准', high:'充分'};
  function inferredAcquisitionPreset(rule) {
    if (acquisitionPresetLabels[rule?.acquisition_preset]) return rule.acquisition_preset;
    return ({ocr_only:'text', vision_only:'visual', combined:'dual', off:'off'})[rule?.image_mode] || 'smart';
  }
  function simpleAcquisitionMode(rule) {
    const mode = String(rule?.image_mode || 'auto'); const trigger = String(rule?.vision_trigger || 'off'); const level = String(rule?.vision_level || 'off');
    if (mode === 'off' || trigger === 'off' || level === 'off') return 'off';
    const preset = inferredAcquisitionPreset(rule);
    if (['always', 'visual', 'dual'].includes(preset) || (preset === 'custom' && mode === 'auto' && trigger === 'required')) return 'always';
    if (['smart', 'text'].includes(preset) || (mode === 'auto' && trigger === 'text_fallback')) return 'smart';
    return 'custom';
  }
  function acquisitionSummary(rule) {
    const level = String(rule?.vision_level || 'off'); const choice = simpleAcquisitionMode(rule);
    if (choice === 'off') return {label:'仅基础识别', state:'off', detail:'仍会对材料、字段和扫描件按需运行本地 OCR，不追加增强核验'};
    if (choice === 'custom') return {label:`专家自定义 · ${acquisitionLevelLabels[level] || '标准'}强度`, state:'warning', detail:'已由专家模式单独指定通道或启动方式'};
    return {label:`${acquisitionPresetLabels[choice]} · ${acquisitionLevelLabels[level] || '标准'}强度`, state:'active', detail:choice === 'always' ? '不因文字已充分而跳过，仍会取证候选材料' : '先审全文文字，仅在确有必要时取证'};
  }
  function acquisitionRecommendationScene(rule) {
    const preset = String(rule?.acquisition_recommendation?.acquisition_preset || 'off');
    if (preset === 'visual') return '系统建议重点核验签章、外观或图片事实。';
    if (preset === 'text') return '系统建议重点核验扫描文字与关键字段。';
    if (preset === 'smart' || preset === 'dual') return '系统建议按材料与关键字段需要协同取证。';
    return '系统建议该规则通常可由全文文字审查。';
  }
  function acquisitionCapabilityNote() {
    const ocr = '本地 OCR 基础识别已启用';
    const visionProfile = modelProfiles.find((profile) => profile.enabled && (profile.capabilities?.vision || profile.supports_vision) && profile.profile_id === visionConfiguration.default_profile_id);
    const vision = visionConfiguration.enabled && visionProfile ? `多模态已开启（${visionProfile.display_name}）` : visionConfiguration.enabled ? '多模态已开启，但尚未选择可用图片模型' : '多模态未开启';
    return `${ocr}；${vision}。`;
  }
  function acquisitionPreview(rule) {
    const mode = String(rule?.image_mode || 'auto'); const trigger = String(rule?.vision_trigger || 'off'); const level = String(rule?.vision_level || 'off');
    if (mode === 'off' || trigger === 'off' || level === 'off') return '先进行全文文字审查；材料、字段或扫描件类规则仍会按需使用本地 OCR 识别候选页，不进行腾讯 OCR 或多模态增强核验。';
    const itemCount = Array.isArray(rule?.evidence_items) ? rule.evidence_items.length : 0;
    const compound = itemCount > 1;
    const ocrLimit = compound && level !== 'low' ? Math.min(level === 'high' ? 12 : 8, Math.max(level === 'high' ? 10 : 6, itemCount * 2)) : (level === 'high' ? 10 : 6);
    const visionLimit = compound && level !== 'low' ? Math.min(level === 'high' ? 8 : 6, Math.max(level === 'high' ? 6 : 4, itemCount)) : (level === 'high' ? 6 : 4);
    const budget = level === 'low' ? '最多处理少量候选页，适合单页材料或快速抽查' : level === 'standard' ? `OCR 最多 ${ocrLimit} 页；图片首批最多 ${visionLimit} 页、必要时可补看 4 页` : level === 'high' ? `OCR 最多 ${ocrLimit} 页；图片首批最多 ${visionLimit} 页、必要时可补看 6 页` : '按当前覆盖上限执行';
    const choice = simpleAcquisitionMode(rule);
    const channel = {ocr_only:'仅核验扫描文字，不向模型发送图片', vision_only:'仅核验图片外观', combined:'OCR 与图片外观均会执行', auto:'由系统按材料与关键字段选择 OCR 或图片复核'}[mode] || '按规则决定取证路径';
    const compoundNote = compound ? `该规则含 ${itemCount} 个独立子项，候选页会按子项轮转覆盖，不由前一项占满。` : '';
    if (choice === 'always') return `${acquisitionRecommendationScene(rule)} 本地 OCR 先识别候选页；无论文字证据是否充分，随后都会追加一次增强核验。${channel}；${budget}。${compoundNote}`;
    if (choice === 'smart') return `${acquisitionRecommendationScene(rule)} 本地 OCR 先识别候选页；只有关键字段需精确复核、识别不完整或规则确需核验外观时，才追加增强核验。${channel}；${budget}。${compoundNote}`;
    return `${acquisitionRecommendationScene(rule)} ${channel}；${budget}。`;
  }
  function presetPayload(preset, level, rule = {}) {
    const presetMap = {
      smart:{image_mode:'auto', vision_trigger:'text_fallback'}, always:{image_mode:'auto', vision_trigger:'required'}, text:{image_mode:'ocr_only', vision_trigger:'text_fallback'},
      visual:{image_mode:'vision_only', vision_trigger:'required'}, dual:{image_mode:'combined', vision_trigger:'required'},
      off:{image_mode:'off', vision_trigger:'off'},
    };
    const mapped = presetMap[preset] || {image_mode:rule.image_mode || 'auto', vision_trigger:rule.vision_trigger || 'off'};
    const active = preset !== 'off' && mapped.vision_trigger !== 'off';
    return {acquisition_preset:preset, image_mode:mapped.image_mode, vision_trigger:mapped.vision_trigger, vision_level:active ? level : 'off'};
  }
  function simpleAcquisitionPayload(choice, level, rule = {}) {
    if (choice === 'off') return presetPayload('off', 'off', rule);
    return presetPayload(choice === 'always' ? 'always' : 'smart', ['low', 'standard', 'high'].includes(level) ? level : 'standard', rule);
  }
  async function refreshRules() {
    if (!activeProject) return;
    const expandedRuleIds = new Set([...$('rules').querySelectorAll('details.rule-card[open]')]
      .map((card) => card.dataset.rule || card.querySelector('[data-rule]')?.dataset.rule)
      .filter(Boolean));
    const [data, validation] = await Promise.all([
      request(`/projects/${activeProject}/rules`),
      request(`/projects/${activeProject}/rules/acquisition-validation`).catch(() => ({issues:[]})),
    ]);
    const set = data.rule_set; const isDraft = set?.status === 'draft'; hasCurrentRules = data.rules.length > 0;
    const acquisitionIssuesByRule = new Map();
    for (const issue of Array.isArray(validation?.issues) ? validation.issues : []) {
      if (!issue?.rule_id) continue;
      const values = acquisitionIssuesByRule.get(issue.rule_id) || [];
      values.push(issue);
      acquisitionIssuesByRule.set(issue.rule_id, values);
    }
    // 展示顺序只服务于人工确认：先看本项目人工补充，再看 AI 提取，最后看自动导入的
    // 通用规则。不会改写 sort_order，也不会改变后端综合评审的规则执行口径。
    const sourceRank = {manual:0, ai:1, ai_edited:1, ai_locked:1, global:2};
    const displayRules = [...data.rules].sort((left, right) => (
      (sourceRank[left.source_type] ?? 1) - (sourceRank[right.source_type] ?? 1)
      || Number(left.sort_order || 0) - Number(right.sort_order || 0)
      || String(left.title || '').localeCompare(String(right.title || ''), 'zh-CN')
    ));
    const enabledCount = data.rules.filter((r) => Boolean(r.enabled)).length; $('rule-set-meta').textContent = set ? `版本 ${set.version} · ${set.status === 'confirmed' ? '已确认' : set.status === 'draft' ? '待确认' : '已替换'} · 已启用 ${enabledCount}/${data.rules.length} 条${set.source_task_id ? ' · AI 提取结果' : ''}` : '尚未提取或添加规则。'; $('confirm-rules').disabled = !isDraft;
    const acquisitionCounts = displayRules.reduce((counts, rule) => { const key = simpleAcquisitionMode(rule); counts[key] = (counts[key] || 0) + 1; return counts; }, {off:0, smart:0, always:0, custom:0});
    const toolbar = isDraft && displayRules.length ? `<div class="rule-acquisition-toolbar"><div><strong>增强核验设置</strong><span class="muted"> 本地 OCR 始终按需识别；日常只选择是否升级以及强度，腾讯 OCR、多模态通道由系统判断。</span><small>当前：仅基础 ${acquisitionCounts.off || 0} 条 · 智能升级 ${acquisitionCounts.smart || 0} 条 · 每次都升级 ${acquisitionCounts.always || 0} 条${acquisitionCounts.custom ? ` · 专家自定义 ${acquisitionCounts.custom} 条` : ''}</small><small>${escapeHtml(acquisitionCapabilityNote())}</small></div><div class="rule-acquisition-toolbar-actions"><button class="restore-acquisition-recommendations" type="button">已启用规则采用系统建议</button><details class="rule-acquisition-help"><summary>如何选择？</summary><div class="rule-acquisition-help-popover"><p><strong>仅基础识别：</strong>只在需要时以本地 OCR 阅读候选材料。<strong>智能升级：</strong>适合大多数规则，关键字段、识别不完整或外观事实需要时才追加复核。<strong>每次都升级：</strong>每次评审均追加复核，适合签章、勾选或图片材料不能漏看的事项。<strong>快速／标准／充分：</strong>分别适合单页抽查、常规材料、材料分散或风险较高的情形。</p><p>关闭腾讯 OCR 或多模态不会影响本地基础识别；不能执行的增强通道会在规则卡片中明确提示。</p></div></details></div></div>` : '';
    $('rules').innerHTML = displayRules.length ? `${toolbar}<div class="rule-card-list">${displayRules.map((r) => {
      const checkContent = isDraft ? `<textarea class="rule-check-rule" data-rule="${r.rule_id}" rows="4">${escapeHtml(r.check_rule || r.title)}</textarea>` : `<div class="rule-text">${escapeHtml(r.check_rule || r.title)}</div>`;
      const summary = acquisitionSummary(r);
      const ocrCell = `<span class="tag acquisition-tag acquisition-${summary.state}" title="${escapeHtml(summary.detail)}">${escapeHtml(summary.label)}</span>`;
      const sourceLabel = r.source_type === 'ai' ? 'AI 提取' : r.source_type === 'global' ? '通用规则库' : ['ai_edited', 'ai_locked'].includes(r.source_type) ? 'AI 提取 · 人工修改' : '人工补充';
      const enabledControl = isDraft ? `<label class="rule-enabled-control" title="仅影响当前规则集；重新提取规则后会重新选择"><input class="rule-enabled" data-rule="${r.rule_id}" type="checkbox" ${r.enabled ? 'checked' : ''}><span>启用</span></label>` : (r.enabled ? '' : '<span class="tag rule-disabled">未启用</span>');
      const recommendation = r.acquisition_recommendation || {acquisition_preset:'off', vision_level:'off'};
      const simpleChoice = simpleAcquisitionMode(r);
      const selectionLevel = ['low', 'standard', 'high'].includes(r.vision_level) ? r.vision_level : 'standard';
      const isCustomAcquisition = simpleChoice === 'custom';
      const strengthControl = ['smart', 'always'].includes(simpleChoice) ? `<label>取证强度<select class="rule-simple-coverage" data-rule="${r.rule_id}"><option value="low" ${selectionLevel === 'low' ? 'selected' : ''}>快速：单页材料或快速抽查</option><option value="standard" ${selectionLevel === 'standard' ? 'selected' : ''}>标准（推荐）：覆盖常见材料与必要补页</option><option value="high" ${selectionLevel === 'high' ? 'selected' : ''}>充分：材料分散、页数较多或风险较高</option></select></label>` : '';
      const choiceControl = isCustomAcquisition ? `<div class="rule-acquisition-custom-state"><strong>专家自定义生效中</strong><button class="open-rule-expert" data-rule="${r.rule_id}" type="button">查看/调整专家模式</button></div>` : `<label>增强核验<select class="rule-simple-acquisition" data-rule="${r.rule_id}"><option value="off" ${simpleChoice === 'off' ? 'selected' : ''}>仅基础识别</option><option value="smart" ${simpleChoice === 'smart' ? 'selected' : ''}>智能升级（推荐）</option><option value="always" ${simpleChoice === 'always' ? 'selected' : ''}>每次都升级</option></select></label>`;
      const recommendationPayload = presetPayload(recommendation.acquisition_preset, recommendation.vision_level, r);
      const matchesRecommendation = ['image_mode', 'vision_trigger', 'vision_level'].every((key) => String(r[key] || '') === String(recommendationPayload[key] || ''));
      const ruleIssues = acquisitionIssuesByRule.get(r.rule_id) || [];
      const acquisitionWarning = ruleIssues.length ? `<div class="rule-acquisition-warning"><strong>当前设置提示：</strong>${escapeHtml(ruleIssues.map((issue) => issue.message).join('；'))}</div>` : '';
      const visionControl = isDraft ? `<div class="rule-vision-controls"><div class="rule-vision-heading"><strong>增强核验</strong><small>本地 OCR 会先按需识别材料页；这里仅控制是否追加腾讯 OCR 或多模态复核。</small></div>${choiceControl}${strengthControl}<div class="rule-acquisition-preview"><strong>执行预览</strong><span>${escapeHtml(acquisitionPreview(r))}</span></div>${acquisitionWarning}<div class="rule-acquisition-actions"><button class="restore-rule-acquisition" data-rule="${r.rule_id}" type="button" ${matchesRecommendation ? 'disabled title="当前已采用系统建议"' : ''}>采用系统建议</button><small>系统建议：${escapeHtml(acquisitionPresetLabels[recommendation.acquisition_preset] || '智能升级')} · ${escapeHtml(acquisitionLevelLabels[recommendation.vision_level] || '标准')}强度</small></div><details class="rule-image-advanced"><summary>专家模式：增强通道与启动方式</summary><p class="hint">一般无需修改。本地 OCR 仍会先运行；仅当需限定腾讯精确复核、多模态外观核验或双通道时使用。</p><div class="rule-image-advanced-grid"><label>增强通道<select class="rule-image-mode" data-rule="${r.rule_id}"><option value="auto" ${r.image_mode === 'auto' ? 'selected' : ''}>系统自动选择（推荐）</option><option value="ocr_only" ${r.image_mode === 'ocr_only' ? 'selected' : ''}>腾讯 OCR：精确字段复核</option><option value="vision_only" ${r.image_mode === 'vision_only' ? 'selected' : ''}>多模态：签章、外观等</option><option value="combined" ${r.image_mode === 'combined' ? 'selected' : ''}>腾讯 OCR＋多模态：双重复核</option><option value="off" ${r.image_mode === 'off' ? 'selected' : ''}>仅保留基础识别</option></select></label><label>启动方式<select class="rule-vision-trigger" data-rule="${r.rule_id}"><option value="off" ${r.vision_trigger === 'off' ? 'selected' : ''}>不升级</option><option value="text_fallback" ${r.vision_trigger === 'text_fallback' ? 'selected' : ''}>基础证据不足时升级</option><option value="required" ${r.vision_trigger === 'required' ? 'selected' : ''}>每次均升级</option></select></label></div></details></div>` : '';
      return `<details class="rule-card"><summary><span class="rule-card-summary">${enabledControl}<span class="tag">${categoryLabel(r.category)}</span><strong class="rule-card-title">${escapeHtml(r.title)}</strong><span class="tag">${sourceLabel}</span>${ocrCell}</span></summary><div class="rule-card-body"><div class="rule-card-grid"><label>检查规则${checkContent}</label><div class="rule-field"><span class="rule-field-label">招标原文依据</span><div class="rule-text">${escapeHtml(r.source_text || '未提供')}</div></div></div>${visionControl}${isDraft ? `<div class="actions rule-card-actions"><button class="save-check-rule primary" data-rule="${r.rule_id}">保存检查规则</button></div>` : ''}</div></details>`;
    }).join('')}</div>` : '<p class="muted">暂无规则。</p>';
    $('rules').querySelectorAll('details.rule-card').forEach((card, index) => {
      const ruleId = displayRules[index]?.rule_id;
      card.dataset.rule = ruleId || '';
      card.open = Boolean(ruleId && expandedRuleIds.has(ruleId));
    });
    $('rules').querySelectorAll('.rule-enabled').forEach((input) => { input.onclick = (event) => event.stopPropagation(); input.onchange = async () => { try { await request(`/projects/${activeProject}/rules/${input.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:input.checked})}); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    const ruleById = new Map(displayRules.map((rule) => [rule.rule_id, rule]));
    const saveAcquisition = async (ruleId, payload) => request(`/projects/${activeProject}/rules/${ruleId}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    $('rules').querySelectorAll('.rule-simple-acquisition').forEach((input) => { input.onchange = async () => { const rule = ruleById.get(input.dataset.rule); try { const level = input.closest('.rule-vision-controls')?.querySelector('.rule-simple-coverage')?.value || 'standard'; await saveAcquisition(input.dataset.rule, simpleAcquisitionPayload(input.value, level, rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.rule-simple-coverage').forEach((input) => { input.onchange = async () => { try { const control = input.closest('.rule-vision-controls'); const choice = control?.querySelector(`.rule-simple-acquisition[data-rule="${input.dataset.rule}"]`)?.value || 'smart'; const rule = ruleById.get(input.dataset.rule); await saveAcquisition(input.dataset.rule, simpleAcquisitionPayload(choice, input.value, rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.rule-image-mode, .rule-vision-trigger').forEach((input) => { input.onchange = async () => { try { const control = input.closest('.rule-vision-controls'); const level = control?.querySelector(`.rule-simple-coverage[data-rule="${input.dataset.rule}"]`)?.value || 'standard'; const mode = control?.querySelector(`.rule-image-mode[data-rule="${input.dataset.rule}"]`)?.value || 'auto'; const trigger = control?.querySelector(`.rule-vision-trigger[data-rule="${input.dataset.rule}"]`)?.value || 'off'; await saveAcquisition(input.dataset.rule, {acquisition_preset:'custom', image_mode:mode, vision_trigger:trigger, vision_level:(mode === 'off' || trigger === 'off') ? 'off' : level}); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.open-rule-expert').forEach((button) => button.onclick = () => { const advanced = button.closest('.rule-vision-controls')?.querySelector('.rule-image-advanced'); if (advanced) { advanced.open = true; advanced.scrollIntoView({block:'nearest', behavior:'smooth'}); } });
    $('rules').querySelectorAll('.restore-rule-acquisition').forEach((button) => button.onclick = async () => { try { const rule = ruleById.get(button.dataset.rule); const recommendation = rule?.acquisition_recommendation || {acquisition_preset:'off', vision_level:'off'}; await saveAcquisition(button.dataset.rule, presetPayload(recommendation.acquisition_preset, recommendation.vision_level, rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } });
    $('rules').querySelectorAll('.restore-acquisition-recommendations').forEach((button) => button.onclick = async () => { const candidates = displayRules.filter((rule) => rule.enabled && rule.acquisition_recommendation); if (!candidates.length || !confirm(`将为 ${candidates.length} 条已启用规则恢复系统建议；已有自定义取证设置会被替换。是否继续？`)) return; button.disabled = true; try { for (const rule of candidates) { const recommendation = rule.acquisition_recommendation; await saveAcquisition(rule.rule_id, presetPayload(recommendation.acquisition_preset, recommendation.vision_level, rule)); } await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } finally { button.disabled = false; } });
    $('rules').querySelectorAll('.save-check-rule').forEach((button) => button.onclick = async () => { try { const input = $('rules').querySelector(`.rule-check-rule[data-rule="${button.dataset.rule}"]`); await request(`/projects/${activeProject}/rules/${button.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({check_rule:input.value})}); await refreshRules(); } catch (error) { alert(error.message); } });
  }
  function groupByBidder(results) { const groups = new Map(); results.forEach((item) => { const bidder = item.bidder_name || item.original_name || '未填写投标人'; if (!groups.has(bidder)) groups.set(bidder, []); groups.get(bidder).push(item); }); return [...groups.entries()]; }
  function statusLabel(status) { return ({satisfied:'满足', not_satisfied:'不满足', partial:'部分满足', not_found:'未找到证据', manual:'需人工判断', ocr_required:'需 OCR 后判定'})[status] || status || '-'; }
  function riskLabel(risk) { return ({high:'高风险', medium:'中风险', low:'低风险'})[risk] || risk || '-'; }
  function riskRank(risk) { return ({high:3, medium:2, low:1})[risk] || 0; }
  function confidenceLabel(value) { return ({high:'高', medium:'中', low:'低'})[value] || value || '-'; }
  function evidenceQualityLabel(value) { return ({sufficient:'充分', limited:'有限', missing:'缺失'})[value] || value || '-'; }
  function visionStatusHtml(result) {
    const status = String(result?.vision_status || 'not_requested');
    const ocrStatus = String(result?.ocr_status || (status.startsWith('ocr_') ? status : 'not_requested'));
    const multimodalStatus = String(result?.multimodal_status || (status.startsWith('ocr_') ? 'not_requested' : status));
    if (ocrStatus === 'not_requested' && multimodalStatus === 'not_requested') return '';
    const labels = {
      applied:'✓ 图片检查已完成并采纳',
      applied_partial:'图片检查已补充部分事实',
      conflict:'⚠ 图片检查发现疑似字段冲突',
      uncovered:'图片检查已执行，未覆盖关键材料',
      failed:'图片识别失败，已保留文字结论',
      unavailable:'未获得可用的多模态模型',
      not_located:'未定位到可靠图片页',
      skipped_text_sufficient:'文字证据充分，未调用图片模型',
      ocr_applied:'✓ OCR 已核验并采纳',
      ocr_applied_partial:'OCR 已补充部分文字事实',
      ocr_uncovered:'OCR 已执行，未覆盖关键材料',
      ocr_failed:'OCR 未获得可用文字，已保留文字结论',
      ocr_quota_exhausted:'腾讯 OCR 额度不足，已转图片识别',
      ocr_not_located:'未定位到可靠 OCR 候选页',
      ocr_skipped_text_sufficient:'文字证据充分，未调用 OCR',
      ocr_vision_applied:'✓ OCR 与图片检查均已采纳',
      ocr_vision_applied_partial:'OCR 与图片检查已补充部分事实',
      ocr_vision_conflict:'⚠ OCR后图片检查发现疑似字段冲突',
    };
    const label = [ocrStatus, multimodalStatus].filter((item) => item !== 'not_requested').map((item) => labels[item] || '图片识别状态').join('；');
    const pages = Array.isArray(result?.vision_pages) ? result.vision_pages.filter((page) => Number.isInteger(page) && page > 0).map((page) => `P${page}`).join('、') : '';
    const evidencePages = Array.isArray(result?.vision_evidence_pages) ? result.vision_evidence_pages.filter((page) => Number.isInteger(page) && page > 0).map((page) => `P${page}`).join('、') : '';
    const meta = [result?.vision_model, pages ? `检查页：${pages}` : '', evidencePages ? `证据页：${evidencePages}` : ''].filter(Boolean).join(' · ');
    const message = String(result?.vision_message || '').trim();
    return `<div class="vision-result vision-result-${escapeHtml(status)}"><strong>${escapeHtml(label)}</strong>${meta ? `<span>${escapeHtml(meta)}</span>` : ''}${message ? `<small>${escapeHtml(message)}</small>` : ''}</div>`;
  }
  function visionBadgeHtml(result) {
    const status = String(result?.vision_status || 'not_requested');
    const labels = {
      applied:'✓ 已图片检查',
      applied_partial:'图片检查（部分补充）',
      conflict:'⚠ 图片字段疑似冲突',
      uncovered:'已图片检查（未覆盖）',
      failed:'图片检查失败',
      ocr_applied:'✓ 已 OCR 核验',
      ocr_applied_partial:'OCR（部分补充）',
      ocr_vision_applied:'✓ OCR＋图片检查',
      ocr_vision_applied_partial:'OCR＋图片检查（部分补充）',
      ocr_vision_conflict:'⚠ OCR后图片字段疑似冲突',
    };
    return labels[status] ? `<strong class="vision-badge vision-badge-${escapeHtml(status)}">${escapeHtml(labels[status])}</strong>` : '';
  }
  function evidenceChainHtml(result) {
    const layers = Array.isArray(result?.evidence_layers) ? result.evidence_layers.filter((item) => item && typeof item === 'object' && item.summary) : [];
    if (!layers.length) return '';
    const labels = {text:'文字解析', tencent_ocr:'腾讯 OCR', local_ocr:'本地 RapidOCR', vision:'图片识别'};
    return `<details class="evidence-chain"><summary>证据链详情</summary>${layers.map((layer) => {
      const checked = Array.isArray(layer.checked_pages) ? layer.checked_pages.map((page) => `P${page}`).join('、') : '';
      const evidence = Array.isArray(layer.evidence_pages) ? layer.evidence_pages.map((page) => `P${page}`).join('、') : '';
      const meta = [layer.service || layer.model || '', checked ? `检查页：${checked}` : '', evidence ? `证据页：${evidence}` : ''].filter(Boolean).join(' · ');
      return `<div class="evidence-layer"><strong>${escapeHtml(labels[layer.source] || '补充证据')}</strong>${meta ? `<small>${escapeHtml(meta)}</small>` : ''}<span>${escapeHtml(layer.summary)}</span></div>`;
    }).join('')}</details>`;
  }
  function scoreOcrHint(result) {
    if (result?.coverage_status === 'uncovered') return '<br><small>扫描件证据未覆盖，暂不建议计分</small>';
    if (result?.coverage_status === 'partial') return '<br><small>仅覆盖部分证据，建议人工复核计分</small>';
    if (result?.check_mode !== 'ocr') return '';
    const status = String(result?.vision_status || ''); const ocrStatus = String(result?.ocr_status || (status.startsWith('ocr_') ? status : ''));
    if (/^ocr_applied/.test(ocrStatus)) return '<br><small>已完成 OCR 文字核验</small>';
    if (['applied', 'applied_partial'].includes(String(result?.multimodal_status || status))) return '<br><small>已完成图片外观核验</small>';
    if (/^(ocr_uncovered|ocr_not_located|ocr_quota_exhausted|ocr_failed)/.test(status)) return '<br><small>图片材料待进一步核验</small>';
    return '<br><small>需 OCR 识别</small>';
  }
  function scoreSuggestionLabel(result) {
    if (result?.coverage_status === 'uncovered') return '待 OCR 后评分';
    if (result?.suggested_score != null) return result.suggested_score;
    return result?.check_mode === 'ocr' ? '需 OCR 后评分' : '-';
  }
  function scoreVerificationSummary(result) {
    const status = String(result?.vision_status || '');
    const labels = {
      applied:'已完成图片核验', applied_partial:'已完成图片抽核',
      ocr_applied:'已完成 OCR 核验', ocr_applied_partial:'已完成 OCR 抽核',
      ocr_vision_applied:'已完成 OCR 与图片核验', ocr_vision_applied_partial:'已完成 OCR 与图片抽核',
      conflict:'图片字段存在待复核差异', ocr_vision_conflict:'OCR 后图片字段存在待复核差异',
      uncovered:'已检查候选页，未覆盖目标材料', ocr_uncovered:'已 OCR 检查候选页，未覆盖目标材料',
    };
    const label = labels[status];
    return label ? `<small class="result-evidence">${escapeHtml(label)}。</small>` : '';
  }
  function partialResultNotice(run) { if (run?.task_status === 'running') return `<p class="hint">综合评审仍在运行，以下仅展示已完整完成投标人的 AI 建议。</p>`; return run?.task_status === 'error' ? `<p class="hint">本次综合评审未全部完成，以下为已成功保存的部分 AI 建议（进度 ${run.task_progress ?? 0}%）：${escapeHtml(run.task_error || '请修正模型配置后重新运行。')}</p>` : ''; }
  function visibleCompletedResults(run, results) { if (run?.task_status !== 'running') return results; const completed = new Set(run.completed_document_ids || []); return results.filter((item) => completed.has(item.document_id)); }
  function renderEvaluationHighlights(summaries) {
    const values = (summaries || []).filter((summary) => Array.isArray(summary.highlights) && summary.highlights.length);
    $('evaluation-highlights-panel').classList.toggle('hidden', !values.length);
    $('evaluation-highlights').innerHTML = values.map((summary) => `<section class="evaluation-highlight-group"><h4>${escapeHtml(summary.bidder_name || '未命名投标人')}</h4>${summary.headline ? `<p>${escapeHtml(summary.headline)}</p>` : ''}<ul>${summary.highlights.map((item) => `<li class="evaluation-highlight-${escapeHtml(item.level || 'attention')}"><strong>${escapeHtml(item.keyword)}</strong><span>${escapeHtml(item.conclusion)}</span>${item.basis ? `<small>${escapeHtml(item.basis)}</small>` : ''}</li>`).join('')}</ul></section>`).join('');
  }
  async function refreshReview() { if (!activeProject) return; const data = await request(`/projects/${activeProject}/review-results`); renderEvaluationHighlights(data.review_run?.highlights || []); const groups = groupByBidder(visibleCompletedResults(data.review_run, data.results)); $('review-results').innerHTML = groups.length ? `${partialResultNotice(data.review_run)}<p class="hint">以下为 AI 基于电子文件生成的审查建议；主表展示结论摘要，完整文字和取证过程可展开查看。</p>${groups.map(([bidder, results]) => { const ordered = [...results].sort((left, right) => { const leftOcr = left.status === 'ocr_required' ? 1 : 0; const rightOcr = right.status === 'ocr_required' ? 1 : 0; return leftOcr - rightOcr || riskRank(right.risk_level) - riskRank(left.risk_level); }); return `<details class="result-group"><summary>投标人：${escapeHtml(bidder)}（${ordered.length} 项）</summary><div class="review-result-table-wrap"><table class="review-result-table"><colgroup><col class="review-col-category"><col class="review-col-rule"><col class="review-col-advice"><col class="review-col-risk"><col class="review-col-evidence"></colgroup><thead><tr><th>分类</th><th>检查规则</th><th>AI建议</th><th>风险</th><th>关键证据与理由</th></tr></thead><tbody>${ordered.map((r) => `<tr><td><span class="tag">${categoryLabel(r.category)}</span></td><td>${escapeHtml(r.check_rule || r.title)}</td><td>${escapeHtml(statusLabel(r.status))}<br><small>置信度：${escapeHtml(confidenceLabel(r.confidence))}；证据：${escapeHtml(evidenceQualityLabel(r.evidence_quality))}</small>${visionBadgeHtml(r)}</td><td>${escapeHtml(riskLabel(r.status === 'ocr_required' ? 'low' : r.risk_level))}</td><td><div class="result-evidence">${escapeHtml(resultExplanation(conciseResultEvidence(r), r) || '-')}</div><small class="result-evidence">${escapeHtml(resultExplanation(conciseResultReason(r), r))}</small>${visionStatusHtml(r)}${evidenceChainHtml(r)}${rawResultDetailHtml(r)}</td></tr>`).join('')}</tbody></table></div></details>`; }).join('')}` : `<p class="muted">${data.review_run ? '正在生成审查结果。' : '本项目没有审查规则。'}</p>`; }
  async function refreshScores() { for (const type of ['objective','subjective']) { const data = await request(`/projects/${activeProject}/score-results/${type}`); const target = $(`${type}-results`); const groups = groupByBidder(visibleCompletedResults(data.score_run, data.results)); target.innerHTML = groups.length ? `${partialResultNotice(data.score_run)}<p class="hint">以下为 AI 基于电子文件生成的评分建议；扫描件证据未覆盖时不会将“未找到”误作 0 分，主表会提示待 OCR 后评分。</p>${groups.map(([bidder, results]) => `<details class="result-group"><summary>投标人：${escapeHtml(bidder)}（${results.length} 项）</summary><table><thead><tr><th>检查规则</th><th>AI 建议得分</th><th>满分</th><th>置信度</th><th>关键证据与理由</th></tr></thead><tbody>${results.map((r) => `<tr><td>${escapeHtml(r.check_rule || r.title)}${scoreOcrHint(r)}</td><td>${escapeHtml(scoreSuggestionLabel(r))}</td><td>${r.max_score ?? '-'}</td><td>${escapeHtml(confidenceLabel(r.confidence))}${visionBadgeHtml(r)}</td><td><div class="result-evidence">${escapeHtml(resultExplanation(conciseResultEvidence(r), r) || '-')}</div><small class="result-evidence">${escapeHtml(resultExplanation(conciseResultReason(r), r))}</small>${scoreVerificationSummary(r)}${evidenceChainHtml(r)}${rawResultDetailHtml(r)}</td></tr>`).join('')}</tbody></table></details>`).join('')}` : `<p class="muted">${data.score_run ? '正在生成评分结果。' : `本项目没有${type === 'objective' ? '客观分' : '主观分'}规则。`}</p>`; } }
  $('create-project').onclick = () => $('project-form').classList.remove('hidden'); $('cancel-project').onclick = () => $('project-form').classList.add('hidden');
  $('save-project').onclick = async () => { try { const data = await request('/projects', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('project-name').value, project_number:$('project-number').value, section_name:$('section-name').value, password:$('project-password').value})}); $('project-password').value = ''; await loadProjects(); openProject(data.project.project_id); } catch (error) { alert(error.message); } };
  $('back-projects').onclick = () => { activeProject = null; stopPolling(); $('workspace').classList.add('hidden'); $('projects-panel').classList.remove('hidden'); loadProjects(); };
  $('delete-project').onclick = async () => { if (!activeProject || !confirm('删除项目会永久移除该项目的原始文件、解析缓存、查重/审查/评分结果和任务记录，无法恢复。是否继续？')) return; try { await request(`/projects/${activeProject}`, {method:'DELETE'}); activeProject = null; stopPolling(); $('workspace').classList.add('hidden'); $('projects-panel').classList.remove('hidden'); await loadProjects(); } catch (error) { alert(error.message); } };
  let selectedUploadFile = null;
  function suggestedBidderName(file) { return String(file?.name || '').replace(/\.(pdf|docx)$/i, '').trim(); }
  function fillBidderNameFromSelectedFile() { const input = $('bidder-name'); if ($('file-role').value !== 'bid' || !selectedUploadFile) return; if (!input.value.trim() || input.dataset.autoFilled === 'true') { input.value = suggestedBidderName(selectedUploadFile); input.dataset.autoFilled = 'true'; } }
  function setSelectedUploadFile(file) { if (!file) return; if (!/\.(pdf|docx)$/i.test(file.name || '')) { alert('仅支持 PDF 或 DOCX 文件'); return; } selectedUploadFile = file; $('file-selected-name').textContent = file.name; fillBidderNameFromSelectedFile(); }
  function clearSelectedUploadFile() { selectedUploadFile = null; $('file-input').value = ''; $('file-selected-name').textContent = '尚未选择文件'; $('bidder-name').dataset.autoFilled = 'false'; }
  function updateUploadProgress(percent, message, state = '') { const container = $('upload-progress'); container.className = `upload-progress${state ? ` is-${state}` : ''}`; const safePercent = Math.max(0, Math.min(100, Math.round(percent || 0))); $('upload-progress-bar').style.width = `${safePercent}%`; $('upload-progress-percent').textContent = `${safePercent}%`; $('upload-progress-text').textContent = message; }
  function uploadFileWithProgress(form, file) { return new Promise((resolve, reject) => { const xhr = new XMLHttpRequest(); xhr.open('POST', `${api}/projects/${activeProject}/documents`, true); xhr.upload.onloadstart = () => updateUploadProgress(0, `正在上传：${file.name}`); xhr.upload.onprogress = (event) => { if (event.lengthComputable) updateUploadProgress(event.loaded / event.total * 100, `正在上传：${file.name}`); else updateUploadProgress(0, `正在上传：${file.name}`); }; xhr.upload.onload = () => updateUploadProgress(100, '文件已传至服务器，正在保存…'); xhr.onerror = () => reject(new Error('上传网络异常，请检查网络或稍后重试')); xhr.onabort = () => reject(new Error('上传已取消')); xhr.onload = () => { let data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) { data = {error:`上传失败（HTTP ${xhr.status}）`}; } if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(new Error(data.error || `上传失败（HTTP ${xhr.status}）`)); }; xhr.send(form); }); }
  function updateBidderRequirement() { const isBid = $('file-role').value === 'bid'; $('bidder-field').classList.toggle('hidden', !isBid); $('bidder-name').required = isBid; if (isBid) fillBidderNameFromSelectedFile(); }
  $('file-role').onchange = updateBidderRequirement;
  updateBidderRequirement();
  $('bidder-name').oninput = () => { $('bidder-name').dataset.autoFilled = 'false'; };
  $('file-input').onchange = () => setSelectedUploadFile($('file-input').files[0]);
  $('file-drop-zone').onclick = () => $('file-input').click();
  $('file-drop-zone').onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('file-input').click(); } };
  for (const eventName of ['dragenter', 'dragover']) $('file-drop-zone').addEventListener(eventName, (event) => { event.preventDefault(); $('file-drop-zone').classList.add('is-dragging'); });
  for (const eventName of ['dragleave', 'drop']) $('file-drop-zone').addEventListener(eventName, (event) => { event.preventDefault(); $('file-drop-zone').classList.remove('is-dragging'); });
  $('file-drop-zone').addEventListener('drop', (event) => { const files = event.dataTransfer?.files; if (!files?.length) return; if (files.length > 1) alert('请一次拖入一个文件，并分别选择对应的文件角色和投标人。'); setSelectedUploadFile(files[0]); });
  $('upload-file').onclick = async () => { const file = selectedUploadFile || $('file-input').files[0]; const role = $('file-role').value; const bidderName = $('bidder-name').value.trim(); if (!file) return alert('请选择或拖入文件'); if (role === 'bid' && !bidderName) { $('bidder-name').focus(); return alert('上传投标文件时必须填写投标人名称'); } const form = new FormData(); form.append('file', file); form.append('role', role); form.append('bidder_name', bidderName); const button = $('upload-file'); button.disabled = true; try { await uploadFileWithProgress(form, file); updateUploadProgress(100, '上传完成，正在刷新文件清单…', 'success'); clearSelectedUploadFile(); $('bidder-name').value = ''; await refreshProject(); } catch (error) { updateUploadProgress(0, `上传失败：${error.message}`, 'error'); alert(error.message); } finally { button.disabled = false; } };
  $('parse-documents').onclick = () => queue('parse_documents'); $('start-compare').onclick = () => queue('compare_documents', {force_rerun:true});
  $('extract-rules').onclick = async () => { if (hasCurrentRules && !confirm('重新提取会以新的 AI 结果和全部通用规则替换当前待确认规则集；通用规则按规则库的默认选择状态进入项目。已确认的历史规则和结果不会删除，是否继续？')) return; try { const profile_id = $('rule-profile').value; await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:'extract_rules', profile_id, force_rerun:true})}); await refreshProject(); } catch (error) { alert(error.message); } };
  function updateManualRuleScoringFields() { const category = $('manual-rule-category').value; const isScoring = ['objective', 'subjective'].includes(category); $('manual-rule-max-score-field').classList.toggle('hidden', !isScoring); $('manual-rule-max-score').required = isScoring; $('manual-rule-score-kind-field').classList.toggle('hidden', category !== 'objective'); }
  $('manual-rule-category').onchange = updateManualRuleScoringFields;
  updateManualRuleScoringFields();
  $('add-manual-rule').onclick = async () => { try { const category = $('manual-rule-category').value; const isScoring = ['objective', 'subjective'].includes(category); const rawMaxScore = $('manual-rule-max-score').value; const maxScore = Number(rawMaxScore); if (isScoring && (!Number.isFinite(maxScore) || maxScore <= 0)) { alert('客观分和主观分规则必须填写大于 0 的满分。'); return; } const payload = {category, title:$('manual-rule-title').value, check_rule:$('manual-rule-check').value, source_text:$('manual-rule-source').value, ocr_required:$('manual-rule-ocr').checked}; if (isScoring) payload.scoring = {max_score:maxScore, kind:category === 'objective' ? $('manual-rule-score-kind').value : 'manual'}; await request(`/projects/${activeProject}/rules`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); $('manual-rule-title').value = ''; $('manual-rule-check').value = ''; $('manual-rule-source').value = ''; $('manual-rule-max-score').value = ''; $('manual-rule-ocr').checked = false; updateManualRuleScoringFields(); await refreshRules(); } catch (error) { alert(error.message); } };
  $('confirm-rules').onclick = async () => { try { const validation = await request(`/projects/${activeProject}/rules/acquisition-validation`); const issues = Array.isArray(validation.issues) ? validation.issues : []; if (issues.length) { const brief = issues.slice(0, 6).map((item) => `• ${item.title}：${item.message}`).join('\n'); const extra = issues.length > 6 ? `\n另有 ${issues.length - 6} 条提示。` : ''; if (!confirm(`增强核验预检发现 ${issues.length} 条提示：\n${brief}${extra}\n\n仍要确认当前规则集吗？`)) return; } await request(`/projects/${activeProject}/rules/confirm`, {method:'POST'}); await refreshRules(); } catch (error) { alert(error.message); } };
  $('start-evaluate-all').onclick = async () => { try { const profile_id = $('all-profile').value; const rulesData = await request(`/projects/${activeProject}/rules`); const visionRules = rulesData.rules.filter((rule) => rule.enabled && rule.vision_trigger !== 'off' && rule.vision_level !== 'off' && !['ocr_only', 'off'].includes(rule.image_mode || 'auto')); if (visionConfiguration.enabled && visionRules.length) { const selected = modelProfiles.find((item) => item.profile_id === profile_id); const fallback = modelProfiles.find((item) => item.profile_id === visionConfiguration.default_profile_id && item.enabled && item.capabilities?.vision); if (!selected?.capabilities?.vision && fallback && !confirm(`当前评审模型“${selected?.display_name || '所选模型'}”不是多模态模型；仅需要图片外观核验的规则将改用“${fallback.display_name}”，文字评审与 OCR 仍使用当前模型。是否继续？`)) return; } await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:'evaluate_all', profile_id})}); await refreshProject(); await Promise.all([refreshReview(), refreshScores(), refreshUsage()]); } catch (error) { alert(error.message); } };
  function closeModels() { $('models-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); resetModelForm(); }
  function closePrompts() { $('prompts-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); }
  function syncGlobalRuleAcquisitionControls() { const off = $('global-rule-acquisition').value === 'off'; $('global-rule-coverage-field').classList.toggle('is-hidden', off); }
  function resetGlobalRuleForm() { $('global-rule-id').value = ''; $('global-rule-form-title').textContent = '新增通用规则'; $('global-rule-category').value = 'substantive'; $('global-rule-title').value = ''; $('global-rule-check').value = ''; $('global-rule-source').value = ''; $('global-rule-acquisition').value = 'off'; $('global-rule-coverage').value = 'standard'; $('global-rule-ocr').checked = false; $('global-rule-enabled').checked = true; syncGlobalRuleAcquisitionControls(); }
  function closeGlobalRules() { $('global-rules-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); resetGlobalRuleForm(); }
  async function loadGlobalRules() {
    const data = await request('/global-rules'); globalRules = data.rules;
    $('global-rules').innerHTML = globalRules.length ? `<div class="model-profile-cards">${globalRules.map((rule) => { const summary = acquisitionSummary(rule); return `<article class="global-rule-card"><div><h4>${escapeHtml(rule.title)} ${rule.enabled ? '<span class="tag">默认选择</span>' : '<span class="muted">默认不选</span>'}</h4><p><span class="tag">${categoryLabel(rule.category)}</span> <span class="tag acquisition-${summary.state}">${escapeHtml(summary.label)}</span></p><p><strong>检查规则：</strong>${escapeHtml(rule.check_rule)}</p>${rule.source_text ? `<p class="muted">招标原文依据：${escapeHtml(rule.source_text)}</p>` : ''}</div><div class="model-profile-actions"><button class="edit-global-rule" data-rule="${rule.global_rule_id}">编辑</button><button class="delete-global-rule danger" data-rule="${rule.global_rule_id}">删除</button></div></article>`; }).join('')}</div>` : '<p class="muted">暂无通用规则。保存后会自动导入今后新建的项目。</p>';
    $('global-rules').querySelectorAll('.edit-global-rule').forEach((button) => button.onclick = () => { const rule = globalRules.find((item) => item.global_rule_id === button.dataset.rule); if (!rule) return; $('global-rule-id').value = rule.global_rule_id; $('global-rule-form-title').textContent = `编辑通用规则：${rule.title}`; $('global-rule-category').value = rule.category; $('global-rule-title').value = rule.title; $('global-rule-check').value = rule.check_rule; $('global-rule-source').value = rule.source_text || ''; $('global-rule-acquisition').value = simpleAcquisitionMode(rule); $('global-rule-coverage').value = ['low', 'standard', 'high'].includes(rule.vision_level) ? rule.vision_level : 'standard'; $('global-rule-ocr').checked = rule.check_mode === 'ocr'; $('global-rule-enabled').checked = Boolean(rule.enabled); syncGlobalRuleAcquisitionControls(); });
    $('global-rules').querySelectorAll('.delete-global-rule').forEach((button) => button.onclick = async () => { if (!confirm('删除此通用规则不会影响已有项目；是否继续？')) return; const password = window.prompt('请输入通用规则库操作口令'); if (password === null) return; try { await request(`/global-rules/${button.dataset.rule}`, {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})}); if ($('global-rule-id').value === button.dataset.rule) resetGlobalRuleForm(); await loadGlobalRules(); } catch (error) { alert(error.message); } });
  }
  function promptTemplateCard(item, open = false) {
    const levelLabel = {recommended:'推荐修改', careful:'谨慎修改', advanced:'系统模板'}[item.change_level] || '系统模板';
    const placeholders = item.placeholders?.length ? `<p class="prompt-placeholders"><strong>必须保留：</strong>${item.placeholders.map((value) => `{{${escapeHtml(value)}}}`).join('、')}</p>` : '';
    return `<details class="prompt-template-card prompt-level-${escapeHtml(item.change_level || 'advanced')}" ${open ? 'open' : ''}>
      <summary><span class="prompt-template-title">${escapeHtml(item.name)}</span><span class="prompt-level-badge">${levelLabel}</span>${item.is_custom ? '<span class="tag">已自定义</span>' : ''}</summary>
      <div class="prompt-template-body"><p class="hint">${escapeHtml(item.description)}</p>${placeholders}<textarea class="prompt-template-content" data-template="${item.template_id}" rows="8">${escapeHtml(item.content)}</textarea><div class="actions"><button class="save-prompt-template primary" data-template="${item.template_id}">保存提示词</button><button class="reset-prompt-template" data-template="${item.template_id}" ${item.is_custom ? '' : 'disabled'}>恢复默认</button></div></div>
    </details>`;
  }
  function promptSections(items, openCards = false) {
    const sections = new Map();
    for (const item of items) { const section = item.section || '其他'; if (!sections.has(section)) sections.set(section, []); sections.get(section).push(item); }
    return [...sections.entries()].map(([section, values]) => `<section class="prompt-workflow-section"><h4>${escapeHtml(section)}</h4><div class="prompt-section-cards">${values.map((item) => promptTemplateCard(item, openCards)).join('')}</div></section>`).join('');
  }
  async function loadPromptTemplates() {
    const data = await request('/prompt-templates');
    const business = data.templates.filter((item) => item.configuration_group === 'business');
    const workflow = data.templates.filter((item) => item.configuration_group === 'workflow');
    const system = data.templates.filter((item) => item.configuration_group === 'system');
    $('prompt-templates').innerHTML = `
      <section class="prompt-config-group prompt-config-business"><div class="prompt-group-heading"><div><h3>常用业务指令</h3><p>推荐优先修改这里。用于调整查重、规则提取和综合评审的判断原则，不涉及 JSON 输出结构。</p></div><span class="prompt-group-count">${business.length} 项</span></div>${promptSections(business, true)}</section>
      <details class="prompt-config-group prompt-config-workflow"><summary><div><h3>业务流程细节</h3><p>需要细调某一步时再修改；这些模板同时包含输出字段和运行时变量。</p></div><span class="prompt-group-count">${workflow.length} 项</span></summary><div class="prompt-group-body"><div class="prompt-warning">修改时请保留 JSON 字段、枚举值和所有 {{变量}}，否则模型结果可能无法解析。</div>${promptSections(workflow)}</div></details>
      <details class="prompt-config-group prompt-config-system"><summary><div><h3>系统与结构模板</h3><p>系统角色、连接测试、格式修复及兼容流程，通常无需修改。</p></div><span class="prompt-group-count">${system.length} 项</span></summary><div class="prompt-group-body"><div class="prompt-warning prompt-warning-system">建议仅在排查模型兼容或结构化输出问题时修改。修改不当可能导致任务格式异常。</div>${promptSections(system)}</div></details>`;
    $('prompt-templates').querySelectorAll('.save-prompt-template').forEach((button) => button.onclick = async () => { const password = window.prompt('请输入提示词修改口令'); if (password === null) return; try { const input = $('prompt-templates').querySelector(`.prompt-template-content[data-template="${button.dataset.template}"]`); await request(`/prompt-templates/${button.dataset.template}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({content:input.value, password})}); await loadPromptTemplates(); } catch (error) { alert(error.message); } });
    $('prompt-templates').querySelectorAll('.reset-prompt-template').forEach((button) => button.onclick = async () => { if (!confirm('恢复该流程的默认提示词？')) return; const password = window.prompt('请输入提示词恢复默认口令'); if (password === null) return; try { await request(`/prompt-templates/${button.dataset.template}`, {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})}); await loadPromptTemplates(); } catch (error) { alert(error.message); } });
  }
  $('manage-models').onclick = async () => { try { const password = window.prompt('请输入模型配置口令'); if (password === null) return; await request('/model-configuration/unlock', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({password})}); $('models-panel').classList.remove('hidden'); document.body.classList.add('modal-open'); await loadProfiles(); } catch (error) { alert(error.message); } };
  $('manage-prompts').onclick = async () => { try { $('prompts-panel').classList.remove('hidden'); document.body.classList.add('modal-open'); await loadPromptTemplates(); } catch (error) { alert(error.message); } };
  $('manage-global-rules').onclick = async () => { try { $('global-rules-panel').classList.remove('hidden'); document.body.classList.add('modal-open'); await loadGlobalRules(); } catch (error) { alert(error.message); } };
  $('close-models').onclick = closeModels;
  $('close-prompts').onclick = closePrompts;
  $('close-global-rules').onclick = closeGlobalRules;
  // 配置弹窗仅允许通过各自的“关闭 ×”按钮退出，避免误点遮罩或 Esc 丢失正在编辑的内容。
  $('reset-global-rule-form').onclick = resetGlobalRuleForm;
  $('global-rule-acquisition').onchange = syncGlobalRuleAcquisitionControls;
  syncGlobalRuleAcquisitionControls();
  $('save-global-rule').onclick = async () => { const password = window.prompt('请输入通用规则库操作口令'); if (password === null) return; try { const ruleId = $('global-rule-id').value; const preset = $('global-rule-acquisition').value; const level = $('global-rule-coverage').value; const existing = globalRules.find((rule) => rule.global_rule_id === ruleId); const payload = {category:$('global-rule-category').value, title:$('global-rule-title').value, check_rule:$('global-rule-check').value, source_text:$('global-rule-source').value, ocr_required:$('global-rule-ocr').checked, enabled:$('global-rule-enabled').checked, password, ...presetPayload(preset, level, existing)}; const data = await request(ruleId ? `/global-rules/${ruleId}` : '/global-rules', {method:ruleId ? 'PATCH' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); resetGlobalRuleForm(); await loadGlobalRules(); if (!ruleId && activeProject) await refreshRules(); if (!ruleId && data.rule?.synced_draft_rule_sets) $('global-rule-form-title').textContent = `已同步至 ${data.rule.synced_draft_rule_sets} 个待确认项目`; } catch (error) { alert(error.message); } };
  $('reset-model-form').onclick = resetModelForm;
  $('model-preset').onchange = applyModelPreset;
  $('save-model-profile').onclick = async () => { try { const profileId = $('model-profile-id').value; const modelName = $('model-name').value; const payload = {display_name:$('model-display-name').value, base_url:$('model-base-url').value, model_name:modelName, api_key:$('model-api-key').value, json_mode:$('model-json-mode').value === 'true', thinking_mode:normalizedThinkingMode(modelName, $('model-thinking').value), supports_vision:$('model-supports-vision').checked}; await request(profileId ? `/model-profiles/${profileId}` : '/model-profiles', {method:profileId ? 'PATCH' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); resetModelForm(); await loadProfiles(); } catch (error) { alert(error.message); } };
  $('vision-enabled').onchange = () => { $('vision-model-field').classList.toggle('is-disabled', !$('vision-enabled').checked); $('vision-default-profile').disabled = !$('vision-enabled').checked; };
  $('save-ocr-feature-configuration').onclick = async () => { try {
    await request('/tencent-ocr-configuration', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tencent_enabled:$('tencent-ocr-enabled').checked})});
    await loadProfiles();
  } catch (error) { alert(error.message); } };
  $('save-vision-configuration').onclick = async () => { try { await request('/vision-configuration', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:$('vision-enabled').checked, default_profile_id:$('vision-default-profile').value || null})}); await loadProfiles(); } catch (error) { alert(error.message); } };
  $('save-ocr-configuration').onclick = async () => { try { const services = {}; $('ocr-services').querySelectorAll('.ocr-service-enabled').forEach((input) => { const limit = $('ocr-services').querySelector(`.ocr-service-limit[data-service="${input.dataset.service}"]`); services[input.dataset.service] = {enabled:input.checked, monthly_limit:Number(limit?.value || 900)}; }); await request('/tencent-ocr-configuration', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tencent_enabled:$('tencent-ocr-enabled').checked, region:$('ocr-region').value, secret_id:$('ocr-secret-id').value, secret_key:$('ocr-secret-key').value, services})}); $('ocr-secret-id').value = ''; $('ocr-secret-key').value = ''; await loadProfiles(); } catch (error) { alert(error.message); } };
  $('test-ocr-configuration').onclick = async () => { try { const data = await request('/tencent-ocr-configuration/test', {method:'POST'}); alert(data.message); } catch (error) { alert(error.message); } };
  $('open-report').onclick = () => window.open(`/pingbiao/projects/${activeProject}/report`, '_blank', 'noopener');
  $('export-score-csv').onclick = async () => { try { const [objective, subjective] = await Promise.all(['objective', 'subjective'].map((type) => request(`/projects/${activeProject}/score-results/${type}`))); const rows = [['评分类型','投标人','检查规则','AI建议得分','满分','置信度','证据','理由']]; for (const [type, data] of [['客观分', objective], ['主观分', subjective]]) for (const item of data.results) { const compactOcr = type === '客观分'; rows.push([type, item.bidder_name || item.original_name, item.check_rule || item.title, item.suggested_score ?? '', item.max_score ?? '', confidenceLabel(item.confidence), compactOcr ? compactObjectiveOcrText(item.evidence) : (item.evidence || ''), compactOcr ? compactObjectiveOcrText(item.reason) : (item.reason || '')]); } const csv = '\ufeff' + rows.map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(',')).join('\r\n'); const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], {type:'text/csv;charset=utf-8'})); link.download = '评标评分汇总.csv'; link.click(); URL.revokeObjectURL(link.href); } catch (error) { alert(error.message); } };
  document.querySelectorAll('[data-tab]').forEach((button) => button.onclick = () => { if (button.disabled) return; document.querySelectorAll('[data-tab]').forEach((item) => item.classList.toggle('active', item === button)); document.querySelectorAll('[data-pane]').forEach((item) => item.classList.toggle('active', item.dataset.pane === button.dataset.tab)); });
  Promise.all([loadProjects(), loadProfiles()]).catch((error) => { $('projects').textContent = error.message; });
})();
