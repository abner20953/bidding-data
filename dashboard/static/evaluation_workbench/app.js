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
  function taskText(task) { const labels = {queued:'排队中',running:'运行中',success:'已完成',error:'失败',cancelled:'已取消',interrupted:'已中断'}; return task ? `<span class="status-${task.status}">${labels[task.status] || escapeHtml(task.status)} ${task.progress || 0}% ${escapeHtml(task.message || '')}${taskElapsed(task)}${task.error ? `<br>${escapeHtml(task.error)}` : ''}</span>` : '暂无任务'; }
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
  async function openProject(id) { activeProject = id; wasTaskActive = false; lastActiveTaskId = null; lastCompareTask = null; stopCompletionTicker(); $('projects-panel').classList.add('hidden'); $('project-form').classList.add('hidden'); $('workspace').classList.remove('hidden'); await refreshProject(); await loadProfiles(); await refreshRules(); await refreshReview(); await refreshScores(); await refreshUsage(); }
  async function refreshUsage() { if (!activeProject) return; const data = await request(`/projects/${activeProject}/token-usage`); const u = data.usage; if (!u.call_count) { $('token-usage').textContent = '模型用量：尚无调用记录'; return; } const detail = u.metered_calls ? `输入 ${u.prompt_tokens.toLocaleString()} / 输出 ${u.completion_tokens.toLocaleString()} / 合计 ${u.total_tokens.toLocaleString()} Token` : `模型接口未返回 Token；已发送 ${u.input_chars.toLocaleString()} 字符`; $('token-usage').textContent = `模型用量：${detail}（${u.call_count} 次调用）`; }
  async function refreshProject() { if (!activeProject) return; const data = await request(`/projects/${activeProject}`); const p = data.project; $('workspace-name').textContent = projectDisplayName(p); $('workspace-meta').textContent = projectMeta(p); const active = data.tasks.find((t) => ['queued','running'].includes(t.status)); if (active) lastActiveTaskId = active.task_id; $('task-status').innerHTML = taskText(active || data.tasks[0]); if (active) startPolling(); else stopPolling(); renderDocuments(data.documents); const completed = data.tasks.find((t) => t.task_type === 'compare_documents' && t.status === 'success'); if (completed && completed.task_id !== lastCompareTask) { lastCompareTask = completed.task_id; await renderCompare(completed.task_id, data.documents); } const completedDocuments = active?.task_type === 'evaluate_all' ? (active.completed_documents || []) : []; const partialKey = completedDocuments.length ? `${active.task_id}:${completedDocuments.map((item) => item.document_id).sort().join(',')}` : ''; if (partialKey && partialKey !== lastPartialDocumentsKey) { lastPartialDocumentsKey = partialKey; await Promise.all([refreshReview(), refreshScores()]); } if (!active) lastPartialDocumentsKey = ''; const justFinished = wasTaskActive && !active; const finishedTask = justFinished ? data.tasks.find((task) => task.task_id === lastActiveTaskId && task.status === 'success') : null; wasTaskActive = Boolean(active); if (justFinished) { lastActiveTaskId = null; await Promise.all([refreshRules(), refreshReview(), refreshScores(), refreshUsage()]); if (finishedTask) startCompletionTicker(finishedTask); } }
  function renderDocuments(documents) { $('documents').innerHTML = documents.length ? `<table><thead><tr><th>角色</th><th>文件</th><th>投标人</th><th>解析</th><th>页数/字符</th><th>操作</th></tr></thead><tbody>${documents.map((d) => `<tr><td><span class="tag">${roleLabel(d.role)}</span></td><td>${escapeHtml(d.original_name)}</td><td>${escapeHtml(d.bidder_name || '-')}</td><td class="status-${d.parse_status}">${escapeHtml(parseStatusLabel(d.parse_status))}${d.parse_error ? `<br>${escapeHtml(d.parse_error)}` : ''}</td><td>${d.page_count ?? '-'} / ${d.text_length ?? '-'}</td><td><button class="delete-document" data-document="${d.document_id}">删除</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">尚未上传文件。</p>'; $('documents').querySelectorAll('.delete-document').forEach((button) => button.onclick = async () => { if (!confirm('删除文件会同时移除其历史审查和评分结果，是否继续？')) return; try { await request(`/projects/${activeProject}/documents/${button.dataset.document}`, {method:'DELETE'}); await refreshProject(); await refreshReview(); await refreshScores(); } catch (error) { alert(error.message); } }); }
  function priorityLabel(value) { return {high:'高',medium:'中',normal:'常规',none:'无'}[value] || value; }
  function evidenceText(signal) { return (signal.evidence || []).map((item) => { const pages = item.page_a || item.page_b ? `${signal.bidder_a || '文件 A'}第${item.page_a || '?'}页 / ${signal.bidder_b || '文件 B'}第${item.page_b || '?'}页：` : ''; const content = item.text_a || item.value || item.field || ''; const right = item.text_b && item.text_b !== item.text_a ? ` ↔ ${item.text_b}` : ''; return `${pages}${content}${right}`; }).join('\n'); }
  async function renderCompare(taskId, documents) {
    const data = await request(`/tasks/${taskId}/compare-results`); const names = Object.fromEntries(documents.map((d) => [d.document_id, d.bidder_name || d.original_name])); const analysis = data.analysis;
    const rawTable = data.pairs.length ? `<h4>两两比对摘要</h4><table><thead><tr><th>文件对</th><th>完全/近似</th><th>共同错误</th><th>敏感实体</th><th>匹配占比 A/B</th></tr></thead><tbody>${data.pairs.map((pair) => { const s = pair.result.summary || {}; return `<tr><td>${escapeHtml(names[pair.document_a_id])}<br>↔ ${escapeHtml(names[pair.document_b_id])}</td><td>${s.exact || 0} / ${s.fuzzy || 0}</td><td>${s.shared_error || 0}</td><td>${s.entity || 0}</td><td>${s.matched_ratio_a || 0}% / ${s.matched_ratio_b || 0}%</td></tr>`; }).join('')}</tbody></table>` : '<p class="muted">任务尚未生成文件对结果。</p>';
    if (!analysis) { $('compare-results').innerHTML = rawTable; return; }
    const dimLabels = Object.fromEntries((analysis.executed_dimensions || []).map((item) => [item.dimension, item.label]));
    const pairTable = `<h4>横向复核优先级</h4><table><thead><tr><th>文件对</th><th>独立维度</th><th>线索数</th><th>复核优先级</th></tr></thead><tbody>${(analysis.pair_summaries || []).map((item) => `<tr><td>${escapeHtml(item.bidder_a)} ↔ ${escapeHtml(item.bidder_b)}</td><td>${item.independent_dimension_count}（${escapeHtml(item.dimensions.map((key) => dimLabels[key] || key).join('、') || '未发现')}）</td><td>${item.signal_count}</td><td><span class="priority-${item.review_priority}">${priorityLabel(item.review_priority)}</span></td></tr>`).join('')}</tbody></table>`;
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
    $('ocr-enabled').checked = Boolean(ocrConfiguration.enabled); $('ocr-region').value = ocrConfiguration.region || 'ap-guangzhou';
    const source = {manual:'手动保存', environment:'运行环境变量', none:'未配置'}[ocrConfiguration.credentials_source] || '未配置';
    const imageGate = visionConfiguration.enabled ? '全系统图片识别已开启。' : '提示：全系统图片识别总开关当前关闭，OCR 不会在评审中运行。';
    $('ocr-configuration-hint').textContent = `凭据：${source}。本月 ${ocrConfiguration.month_key || '-'}；系统每次真实请求保守计入一次，缓存命中不消耗额度。${imageGate}`;
    $('ocr-services').innerHTML = (ocrConfiguration.services || []).map((item) => `<div class="ocr-service-row"><label class="inline-check"><input class="ocr-service-enabled" data-service="${escapeHtml(item.service)}" type="checkbox" ${item.enabled ? 'checked' : ''}> ${escapeHtml(item.label)}${item.legacy ? '（仅账号支持时启用）' : ''}</label><label>安全上限<input class="ocr-service-limit" data-service="${escapeHtml(item.service)}" type="number" min="1" max="1000" value="${Number(item.monthly_limit) || 900}"></label><small>本月已用 ${Number(item.used) || 0} · 预计可用 ${Number(item.remaining) || 0}</small></div>`).join('') || '<p class="muted">暂无 OCR 接口设置。</p>';
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
  async function refreshRules() {
    if (!activeProject) return;
    const expandedRuleIds = new Set([...$('rules').querySelectorAll('details.rule-card[open]')]
      .map((card) => card.dataset.rule || card.querySelector('[data-rule]')?.dataset.rule)
      .filter(Boolean));
    const data = await request(`/projects/${activeProject}/rules`); const set = data.rule_set; const isDraft = set?.status === 'draft'; hasCurrentRules = data.rules.length > 0;
    const enabledCount = data.rules.filter((r) => Boolean(r.enabled)).length; $('rule-set-meta').textContent = set ? `版本 ${set.version} · ${set.status === 'confirmed' ? '已确认' : set.status === 'draft' ? '待确认' : '已替换'} · 已启用 ${enabledCount}/${data.rules.length} 条${set.source_task_id ? ' · AI 提取结果' : ''}` : '尚未提取或添加规则。'; $('confirm-rules').disabled = !isDraft;
    $('rules').innerHTML = data.rules.length ? `<div class="rule-card-list">${data.rules.map((r) => {
      const checkContent = isDraft ? `<textarea class="rule-check-rule" data-rule="${r.rule_id}" rows="4">${escapeHtml(r.check_rule || r.title)}</textarea>` : `<div class="rule-text">${escapeHtml(r.check_rule || r.title)}</div>`;
      const visionLabel = {off:'不识图', text_fallback:'文字不足时识图', required:'必须识图'}[r.vision_trigger] || '不识图';
      const levelLabel = {off:'关闭', low:'快速', standard:'标准', high:'精细'}[r.vision_level] || '关闭';
      const ocrCell = r.vision_trigger !== 'off' ? `<span class="tag">图片识别：${visionLabel} · ${levelLabel}</span>` : (r.check_mode === 'ocr' ? '<span class="tag">需要 OCR（未启用图片识别）</span>' : '<span class="muted">无需图片识别</span>');
      const sourceLabel = r.source_type === 'ai' ? 'AI 提取' : r.source_type === 'global' ? '通用规则库' : ['ai_edited', 'ai_locked'].includes(r.source_type) ? 'AI 提取 · 人工修改' : '人工补充';
      const enabledControl = isDraft ? `<label class="rule-enabled-control" title="仅影响当前规则集；重新提取规则后会重新选择"><input class="rule-enabled" data-rule="${r.rule_id}" type="checkbox" ${r.enabled ? 'checked' : ''}><span>启用</span></label>` : (r.enabled ? '' : '<span class="tag rule-disabled">未启用</span>');
      const visionControl = isDraft ? `<div class="rule-vision-controls"><label>图片识别条件<select class="rule-vision-trigger" data-rule="${r.rule_id}"><option value="off" ${r.vision_trigger === 'off' ? 'selected' : ''}>不使用图片</option><option value="text_fallback" ${r.vision_trigger === 'text_fallback' ? 'selected' : ''}>文字不足时识图</option><option value="required" ${r.vision_trigger === 'required' ? 'selected' : ''}>必须识图</option></select></label><label>识图强度<select class="rule-vision-level" data-rule="${r.rule_id}" ${r.vision_trigger === 'off' ? 'disabled' : ''}><option value="off" ${r.vision_level === 'off' ? 'selected' : ''}>暂不执行</option><option value="low" ${r.vision_level === 'low' ? 'selected' : ''}>快速（1个最强候选页）</option><option value="standard" ${r.vision_level === 'standard' ? 'selected' : ''}>标准（2页，未覆盖时补页）</option><option value="high" ${r.vision_level === 'high' ? 'selected' : ''}>精细（3页；扫描件先找页）</option></select></label><small>${visionConfiguration.enabled ? '已开启全系统图片识别；优先读取文字评审已定位的真实页码，未覆盖时才有限补页。精细模式可为纯扫描件先进行低清找页。' : '全系统图片识别当前关闭，保存后不会产生图片调用。'}</small></div>` : '';
      return `<details class="rule-card"><summary><span class="rule-card-summary">${enabledControl}<span class="tag">${categoryLabel(r.category)}</span><strong class="rule-card-title">${escapeHtml(r.title)}</strong><span class="tag">${sourceLabel}</span>${ocrCell}</span></summary><div class="rule-card-body"><div class="rule-card-grid"><label>检查规则${checkContent}</label><div class="rule-field"><span class="rule-field-label">招标原文依据</span><div class="rule-text">${escapeHtml(r.source_text || '未提供')}</div></div></div>${visionControl}${isDraft ? `<div class="actions rule-card-actions"><button class="save-check-rule primary" data-rule="${r.rule_id}">保存检查规则</button></div>` : ''}</div></details>`;
    }).join('')}</div>` : '<p class="muted">暂无规则。</p>';
    $('rules').querySelectorAll('details.rule-card').forEach((card, index) => {
      const ruleId = data.rules[index]?.rule_id;
      card.dataset.rule = ruleId || '';
      card.open = Boolean(ruleId && expandedRuleIds.has(ruleId));
    });
    $('rules').querySelectorAll('.rule-enabled').forEach((input) => { input.onclick = (event) => event.stopPropagation(); input.onchange = async () => { try { await request(`/projects/${activeProject}/rules/${input.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:input.checked})}); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.rule-vision-trigger').forEach((input) => { input.onchange = async () => { const level = $('rules').querySelector(`.rule-vision-level[data-rule="${input.dataset.rule}"]`)?.value || 'off'; try { await request(`/projects/${activeProject}/rules/${input.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vision_trigger:input.value, vision_level:input.value === 'off' ? 'off' : level})}); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.rule-vision-level').forEach((input) => { input.onchange = async () => { const trigger = $('rules').querySelector(`.rule-vision-trigger[data-rule="${input.dataset.rule}"]`)?.value || 'off'; try { await request(`/projects/${activeProject}/rules/${input.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({vision_trigger:trigger, vision_level:input.value})}); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
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
    if (status === 'not_requested') return '';
    const labels = {
      applied:'✓ 图片检查已完成并采纳',
      applied_partial:'✓ 图片检查已完成（部分事实）',
      conflict:'⚠ 图片检查发现疑似字段冲突',
      uncovered:'图片检查已执行，未覆盖关键材料',
      failed:'图片识别失败，已保留文字结论',
      unavailable:'未获得可用的多模态模型',
      not_located:'未定位到可靠图片页',
      skipped_text_sufficient:'文字证据充分，未调用图片模型',
      ocr_applied:'✓ 腾讯 OCR 已核验并采纳',
      ocr_applied_partial:'✓ 腾讯 OCR 已补充部分文字事实',
      ocr_uncovered:'腾讯 OCR 已执行，未覆盖关键材料',
      ocr_failed:'腾讯 OCR 失败，已保留文字结论',
      ocr_quota_exhausted:'腾讯 OCR 额度不足，已转图片识别',
      ocr_not_located:'未定位到可靠 OCR 候选页',
      ocr_skipped_text_sufficient:'文字证据充分，未调用腾讯 OCR',
      ocr_vision_applied:'✓ 腾讯 OCR 与图片检查均已采纳',
      ocr_vision_applied_partial:'✓ 腾讯 OCR 与图片检查已补充部分事实',
      ocr_vision_conflict:'⚠ OCR后图片检查发现疑似字段冲突',
    };
    const label = labels[status] || '图片识别状态';
    const pages = Array.isArray(result?.vision_pages) ? result.vision_pages.filter((page) => Number.isInteger(page) && page > 0).map((page) => `P${page}`).join('、') : '';
    const meta = [result?.vision_model, pages].filter(Boolean).join(' · ');
    const message = String(result?.vision_message || '').trim();
    return `<div class="vision-result vision-result-${escapeHtml(status)}"><strong>${escapeHtml(label)}</strong>${meta ? `<span>${escapeHtml(meta)}</span>` : ''}${message ? `<small>${escapeHtml(message)}</small>` : ''}</div>`;
  }
  function visionBadgeHtml(result) {
    const status = String(result?.vision_status || 'not_requested');
    const labels = {
      applied:'✓ 已图片检查',
      applied_partial:'✓ 已图片检查（部分）',
      conflict:'⚠ 图片字段疑似冲突',
      uncovered:'已图片检查（未覆盖）',
      failed:'图片检查失败',
      ocr_applied:'✓ 已腾讯 OCR 核验',
      ocr_applied_partial:'✓ 已腾讯 OCR（部分）',
      ocr_vision_applied:'✓ OCR＋图片检查',
      ocr_vision_applied_partial:'✓ OCR＋图片检查（部分）',
      ocr_vision_conflict:'⚠ OCR后图片字段疑似冲突',
    };
    return labels[status] ? `<strong class="vision-badge vision-badge-${escapeHtml(status)}">${escapeHtml(labels[status])}</strong>` : '';
  }
  function partialResultNotice(run) { if (run?.task_status === 'running') return `<p class="hint">综合评审仍在运行，以下仅展示已完整完成投标人的 AI 建议。</p>`; return run?.task_status === 'error' ? `<p class="hint">本次综合评审未全部完成，以下为已成功保存的部分 AI 建议（进度 ${run.task_progress ?? 0}%）：${escapeHtml(run.task_error || '请修正模型配置后重新运行。')}</p>` : ''; }
  function visibleCompletedResults(run, results) { if (run?.task_status !== 'running') return results; const completed = new Set(run.completed_document_ids || []); return results.filter((item) => completed.has(item.document_id)); }
  function renderEvaluationHighlights(summaries) {
    const values = (summaries || []).filter((summary) => Array.isArray(summary.highlights) && summary.highlights.length);
    $('evaluation-highlights-panel').classList.toggle('hidden', !values.length);
    $('evaluation-highlights').innerHTML = values.map((summary) => `<section class="evaluation-highlight-group"><h4>${escapeHtml(summary.bidder_name || '未命名投标人')}</h4>${summary.headline ? `<p>${escapeHtml(summary.headline)}</p>` : ''}<ul>${summary.highlights.map((item) => `<li class="evaluation-highlight-${escapeHtml(item.level || 'attention')}"><strong>${escapeHtml(item.keyword)}</strong><span>${escapeHtml(item.conclusion)}</span>${item.basis ? `<small>${escapeHtml(item.basis)}</small>` : ''}</li>`).join('')}</ul></section>`).join('');
  }
  async function refreshReview() { if (!activeProject) return; const data = await request(`/projects/${activeProject}/review-results`); renderEvaluationHighlights(data.review_run?.highlights || []); data.results = data.results.map((item) => ({...item, evidence:resultExplanation(item.evidence, item), reason:resultExplanation(item.reason, item)})); const groups = groupByBidder(visibleCompletedResults(data.review_run, data.results)); $('review-results').innerHTML = groups.length ? `${partialResultNotice(data.review_run)}<p class="hint">以下为 AI 基于电子文件生成的审查建议；展开投标人可查看明细。</p>${groups.map(([bidder, results]) => { const ordered = [...results].sort((left, right) => { const leftOcr = left.status === 'ocr_required' ? 1 : 0; const rightOcr = right.status === 'ocr_required' ? 1 : 0; return leftOcr - rightOcr || riskRank(right.risk_level) - riskRank(left.risk_level); }); return `<details class="result-group"><summary>投标人：${escapeHtml(bidder)}（${ordered.length} 项）</summary><div class="review-result-table-wrap"><table class="review-result-table"><colgroup><col class="review-col-category"><col class="review-col-rule"><col class="review-col-advice"><col class="review-col-risk"><col class="review-col-evidence"></colgroup><thead><tr><th>分类</th><th>检查规则</th><th>AI建议</th><th>风险</th><th>证据与理由</th></tr></thead><tbody>${ordered.map((r) => `<tr><td><span class="tag">${categoryLabel(r.category)}</span></td><td>${escapeHtml(r.check_rule || r.title)}</td><td>${escapeHtml(statusLabel(r.status))}<br><small>置信度：${escapeHtml(confidenceLabel(r.confidence))}；证据：${escapeHtml(evidenceQualityLabel(r.evidence_quality))}</small>${visionBadgeHtml(r)}</td><td>${escapeHtml(riskLabel(r.status === 'ocr_required' ? 'low' : r.risk_level))}</td><td><div class="result-evidence">${escapeHtml(r.evidence || '-')}</div><small class="result-evidence">${escapeHtml(r.reason || '')}</small>${visionStatusHtml(r)}</td></tr>`).join('')}</tbody></table></div></details>`; }).join('')}` : `<p class="muted">${data.review_run ? '正在生成审查结果。' : '本项目没有审查规则。'}</p>`; }
  async function refreshScores() { for (const type of ['objective','subjective']) { const data = await request(`/projects/${activeProject}/score-results/${type}`); const target = $(`${type}-results`); const groups = groupByBidder(visibleCompletedResults(data.score_run, data.results)); target.innerHTML = groups.length ? `${partialResultNotice(data.score_run)}<p class="hint">以下为 AI 基于电子文件生成的评分建议；展开投标人可查看明细。</p>${groups.map(([bidder, results]) => `<details class="result-group"><summary>投标人：${escapeHtml(bidder)}（${results.length} 项）</summary><table><thead><tr><th>检查规则</th><th>AI 建议得分</th><th>满分</th><th>置信度</th><th>证据与理由</th></tr></thead><tbody>${results.map((r) => `<tr><td>${escapeHtml(r.check_rule || r.title)}${r.check_mode === 'ocr' ? '<br><small>需 OCR 识别</small>' : ''}</td><td>${r.suggested_score ?? (r.check_mode === 'ocr' ? '需 OCR 后评分' : '-')}</td><td>${r.max_score ?? '-'}</td><td>${escapeHtml(confidenceLabel(r.confidence))}${visionBadgeHtml(r)}</td><td><div class="result-evidence">${escapeHtml(r.evidence || '-')}</div><small class="result-evidence">${escapeHtml(r.reason || '')}</small>${visionStatusHtml(r)}</td></tr>`).join('')}</tbody></table></details>`).join('')}` : `<p class="muted">${data.score_run ? '正在生成评分结果。' : `本项目没有${type === 'objective' ? '客观分' : '主观分'}规则。`}</p>`; } }
  $('create-project').onclick = () => $('project-form').classList.remove('hidden'); $('cancel-project').onclick = () => $('project-form').classList.add('hidden');
  $('save-project').onclick = async () => { try { const data = await request('/projects', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('project-name').value, project_number:$('project-number').value, section_name:$('section-name').value, password:$('project-password').value})}); $('project-password').value = ''; await loadProjects(); openProject(data.project.project_id); } catch (error) { alert(error.message); } };
  $('back-projects').onclick = () => { activeProject = null; stopPolling(); $('workspace').classList.add('hidden'); $('projects-panel').classList.remove('hidden'); loadProjects(); };
  $('delete-project').onclick = async () => { if (!activeProject || !confirm('删除项目会永久移除该项目的原始文件、解析缓存、查重/审查/评分结果和任务记录，无法恢复。是否继续？')) return; try { await request(`/projects/${activeProject}`, {method:'DELETE'}); activeProject = null; stopPolling(); $('workspace').classList.add('hidden'); $('projects-panel').classList.remove('hidden'); await loadProjects(); } catch (error) { alert(error.message); } };
  let selectedUploadFile = null;
  function setSelectedUploadFile(file) { if (!file) return; if (!/\.(pdf|docx)$/i.test(file.name || '')) { alert('仅支持 PDF 或 DOCX 文件'); return; } selectedUploadFile = file; $('file-selected-name').textContent = file.name; }
  function clearSelectedUploadFile() { selectedUploadFile = null; $('file-input').value = ''; $('file-selected-name').textContent = '尚未选择文件'; }
  function updateUploadProgress(percent, message, state = '') { const container = $('upload-progress'); container.className = `upload-progress${state ? ` is-${state}` : ''}`; const safePercent = Math.max(0, Math.min(100, Math.round(percent || 0))); $('upload-progress-bar').style.width = `${safePercent}%`; $('upload-progress-percent').textContent = `${safePercent}%`; $('upload-progress-text').textContent = message; }
  function uploadFileWithProgress(form, file) { return new Promise((resolve, reject) => { const xhr = new XMLHttpRequest(); xhr.open('POST', `${api}/projects/${activeProject}/documents`, true); xhr.upload.onloadstart = () => updateUploadProgress(0, `正在上传：${file.name}`); xhr.upload.onprogress = (event) => { if (event.lengthComputable) updateUploadProgress(event.loaded / event.total * 100, `正在上传：${file.name}`); else updateUploadProgress(0, `正在上传：${file.name}`); }; xhr.upload.onload = () => updateUploadProgress(100, '文件已传至服务器，正在保存…'); xhr.onerror = () => reject(new Error('上传网络异常，请检查网络或稍后重试')); xhr.onabort = () => reject(new Error('上传已取消')); xhr.onload = () => { let data = {}; try { data = JSON.parse(xhr.responseText || '{}'); } catch (_) { data = {error:`上传失败（HTTP ${xhr.status}）`}; } if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(new Error(data.error || `上传失败（HTTP ${xhr.status}）`)); }; xhr.send(form); }); }
  function updateBidderRequirement() { const isBid = $('file-role').value === 'bid'; $('bidder-field').classList.toggle('hidden', !isBid); $('bidder-name').required = isBid; }
  $('file-role').onchange = updateBidderRequirement;
  updateBidderRequirement();
  $('file-input').onchange = () => setSelectedUploadFile($('file-input').files[0]);
  $('file-drop-zone').onclick = () => $('file-input').click();
  $('file-drop-zone').onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('file-input').click(); } };
  for (const eventName of ['dragenter', 'dragover']) $('file-drop-zone').addEventListener(eventName, (event) => { event.preventDefault(); $('file-drop-zone').classList.add('is-dragging'); });
  for (const eventName of ['dragleave', 'drop']) $('file-drop-zone').addEventListener(eventName, (event) => { event.preventDefault(); $('file-drop-zone').classList.remove('is-dragging'); });
  $('file-drop-zone').addEventListener('drop', (event) => { const files = event.dataTransfer?.files; if (!files?.length) return; if (files.length > 1) alert('请一次拖入一个文件，并分别选择对应的文件角色和投标人。'); setSelectedUploadFile(files[0]); });
  $('upload-file').onclick = async () => { const file = selectedUploadFile || $('file-input').files[0]; const role = $('file-role').value; const bidderName = $('bidder-name').value.trim(); if (!file) return alert('请选择或拖入文件'); if (role === 'bid' && !bidderName) { $('bidder-name').focus(); return alert('上传投标文件时必须填写投标人名称'); } const form = new FormData(); form.append('file', file); form.append('role', role); form.append('bidder_name', bidderName); const button = $('upload-file'); button.disabled = true; try { await uploadFileWithProgress(form, file); updateUploadProgress(100, '上传完成，正在刷新文件清单…', 'success'); clearSelectedUploadFile(); $('bidder-name').value = ''; await refreshProject(); } catch (error) { updateUploadProgress(0, `上传失败：${error.message}`, 'error'); alert(error.message); } finally { button.disabled = false; } };
  $('parse-documents').onclick = () => queue('parse_documents'); $('start-compare').onclick = () => queue('compare_documents', {force_rerun:true});
  $('extract-rules').onclick = async () => { if (hasCurrentRules && !confirm('重新提取会以新的 AI 结果和当前启用的通用规则替换当前待确认规则集。已确认的历史规则和结果不会删除，是否继续？')) return; try { const profile_id = $('rule-profile').value; await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:'extract_rules', profile_id, force_rerun:true})}); await refreshProject(); } catch (error) { alert(error.message); } };
  function updateManualRuleScoringFields() { const category = $('manual-rule-category').value; const isScoring = ['objective', 'subjective'].includes(category); $('manual-rule-max-score-field').classList.toggle('hidden', !isScoring); $('manual-rule-max-score').required = isScoring; $('manual-rule-score-kind-field').classList.toggle('hidden', category !== 'objective'); }
  $('manual-rule-category').onchange = updateManualRuleScoringFields;
  updateManualRuleScoringFields();
  $('add-manual-rule').onclick = async () => { try { const category = $('manual-rule-category').value; const isScoring = ['objective', 'subjective'].includes(category); const rawMaxScore = $('manual-rule-max-score').value; const maxScore = Number(rawMaxScore); if (isScoring && (!Number.isFinite(maxScore) || maxScore <= 0)) { alert('客观分和主观分规则必须填写大于 0 的满分。'); return; } const payload = {category, title:$('manual-rule-title').value, check_rule:$('manual-rule-check').value, source_text:$('manual-rule-source').value, ocr_required:$('manual-rule-ocr').checked}; if (isScoring) payload.scoring = {max_score:maxScore, kind:category === 'objective' ? $('manual-rule-score-kind').value : 'manual'}; await request(`/projects/${activeProject}/rules`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); $('manual-rule-title').value = ''; $('manual-rule-check').value = ''; $('manual-rule-source').value = ''; $('manual-rule-max-score').value = ''; $('manual-rule-ocr').checked = false; updateManualRuleScoringFields(); await refreshRules(); } catch (error) { alert(error.message); } };
  $('confirm-rules').onclick = async () => { try { await request(`/projects/${activeProject}/rules/confirm`, {method:'POST'}); await refreshRules(); } catch (error) { alert(error.message); } };
  $('start-evaluate-all').onclick = async () => { try { const profile_id = $('all-profile').value; const rulesData = await request(`/projects/${activeProject}/rules`); const visualRules = rulesData.rules.filter((rule) => rule.enabled && rule.vision_trigger !== 'off' && rule.vision_level !== 'off'); if (visionConfiguration.enabled && visualRules.length) { const selected = modelProfiles.find((item) => item.profile_id === profile_id); const fallback = modelProfiles.find((item) => item.profile_id === visionConfiguration.default_profile_id && item.enabled && item.capabilities?.vision); if (!selected?.capabilities?.vision && !fallback) { alert('已勾选图片识别规则，但当前评审模型不是多模态模型，且未配置可用的默认图片识别模型。请在模型配置中勾选多模态模型并设为默认图片模型。'); return; } if (!selected?.capabilities?.vision && fallback && !confirm(`当前评审模型“${selected?.display_name || '所选模型'}”不是多模态模型；本次图片识别将改用“${fallback.display_name}”，文字评审仍使用当前模型。是否继续？`)) return; } await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:'evaluate_all', profile_id})}); await refreshProject(); await Promise.all([refreshReview(), refreshScores(), refreshUsage()]); } catch (error) { alert(error.message); } };
  function closeModels() { $('models-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); resetModelForm(); }
  function closePrompts() { $('prompts-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); }
  function resetGlobalRuleForm() { $('global-rule-id').value = ''; $('global-rule-form-title').textContent = '新增通用规则'; $('global-rule-category').value = 'substantive'; $('global-rule-title').value = ''; $('global-rule-check').value = ''; $('global-rule-source').value = ''; $('global-rule-ocr').checked = false; $('global-rule-enabled').checked = true; }
  function closeGlobalRules() { $('global-rules-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); resetGlobalRuleForm(); }
  async function loadGlobalRules() {
    const data = await request('/global-rules'); globalRules = data.rules;
    $('global-rules').innerHTML = globalRules.length ? `<div class="model-profile-cards">${globalRules.map((rule) => `<article class="global-rule-card"><div><h4>${escapeHtml(rule.title)} ${rule.enabled ? '<span class="tag">自动导入</span>' : '<span class="muted">未启用</span>'}</h4><p><span class="tag">${categoryLabel(rule.category)}</span>${rule.check_mode === 'ocr' ? ' <span class="tag">需 OCR</span>' : ''}</p><p><strong>检查规则：</strong>${escapeHtml(rule.check_rule)}</p>${rule.source_text ? `<p class="muted">招标原文依据：${escapeHtml(rule.source_text)}</p>` : ''}</div><div class="model-profile-actions"><button class="edit-global-rule" data-rule="${rule.global_rule_id}">编辑</button><button class="delete-global-rule danger" data-rule="${rule.global_rule_id}">删除</button></div></article>`).join('')}</div>` : '<p class="muted">暂无通用规则。保存并启用的规则会自动导入今后新建的项目。</p>';
    $('global-rules').querySelectorAll('.edit-global-rule').forEach((button) => button.onclick = () => { const rule = globalRules.find((item) => item.global_rule_id === button.dataset.rule); if (!rule) return; $('global-rule-id').value = rule.global_rule_id; $('global-rule-form-title').textContent = `编辑通用规则：${rule.title}`; $('global-rule-category').value = rule.category; $('global-rule-title').value = rule.title; $('global-rule-check').value = rule.check_rule; $('global-rule-source').value = rule.source_text || ''; $('global-rule-ocr').checked = rule.check_mode === 'ocr'; $('global-rule-enabled').checked = Boolean(rule.enabled); });
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
  $('models-panel').onclick = (event) => { if (event.target === $('models-panel')) closeModels(); };
  $('prompts-panel').onclick = (event) => { if (event.target === $('prompts-panel')) closePrompts(); };
  $('global-rules-panel').onclick = (event) => { if (event.target === $('global-rules-panel')) closeGlobalRules(); };
  document.addEventListener('keydown', (event) => { if (event.key !== 'Escape') return; if (!$('models-panel').classList.contains('hidden')) closeModels(); if (!$('prompts-panel').classList.contains('hidden')) closePrompts(); if (!$('global-rules-panel').classList.contains('hidden')) closeGlobalRules(); });
  $('reset-global-rule-form').onclick = resetGlobalRuleForm;
  $('save-global-rule').onclick = async () => { const password = window.prompt('请输入通用规则库操作口令'); if (password === null) return; try { const ruleId = $('global-rule-id').value; const payload = {category:$('global-rule-category').value, title:$('global-rule-title').value, check_rule:$('global-rule-check').value, source_text:$('global-rule-source').value, ocr_required:$('global-rule-ocr').checked, enabled:$('global-rule-enabled').checked, password}; await request(ruleId ? `/global-rules/${ruleId}` : '/global-rules', {method:ruleId ? 'PATCH' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); resetGlobalRuleForm(); await loadGlobalRules(); } catch (error) { alert(error.message); } };
  $('reset-model-form').onclick = resetModelForm;
  $('model-preset').onchange = applyModelPreset;
  $('save-model-profile').onclick = async () => { try { const profileId = $('model-profile-id').value; const modelName = $('model-name').value; const payload = {display_name:$('model-display-name').value, base_url:$('model-base-url').value, model_name:modelName, api_key:$('model-api-key').value, json_mode:$('model-json-mode').value === 'true', thinking_mode:normalizedThinkingMode(modelName, $('model-thinking').value), supports_vision:$('model-supports-vision').checked}; await request(profileId ? `/model-profiles/${profileId}` : '/model-profiles', {method:profileId ? 'PATCH' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); resetModelForm(); await loadProfiles(); } catch (error) { alert(error.message); } };
  $('save-vision-configuration').onclick = async () => { try { await request('/vision-configuration', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:$('vision-enabled').checked, default_profile_id:$('vision-default-profile').value || null})}); await loadProfiles(); } catch (error) { alert(error.message); } };
  $('save-ocr-configuration').onclick = async () => { try { const services = {}; $('ocr-services').querySelectorAll('.ocr-service-enabled').forEach((input) => { const limit = $('ocr-services').querySelector(`.ocr-service-limit[data-service="${input.dataset.service}"]`); services[input.dataset.service] = {enabled:input.checked, monthly_limit:Number(limit?.value || 900)}; }); await request('/tencent-ocr-configuration', {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:$('ocr-enabled').checked, region:$('ocr-region').value, secret_id:$('ocr-secret-id').value, secret_key:$('ocr-secret-key').value, services})}); $('ocr-secret-id').value = ''; $('ocr-secret-key').value = ''; await loadProfiles(); } catch (error) { alert(error.message); } };
  $('test-ocr-configuration').onclick = async () => { try { const data = await request('/tencent-ocr-configuration/test', {method:'POST'}); alert(data.message); } catch (error) { alert(error.message); } };
  $('open-report').onclick = () => window.open(`/pingbiao/projects/${activeProject}/report`, '_blank', 'noopener');
  $('export-score-csv').onclick = async () => { try { const [objective, subjective] = await Promise.all(['objective', 'subjective'].map((type) => request(`/projects/${activeProject}/score-results/${type}`))); const rows = [['评分类型','投标人','检查规则','AI建议得分','满分','置信度','证据','理由']]; for (const [type, data] of [['客观分', objective], ['主观分', subjective]]) for (const item of data.results) rows.push([type, item.bidder_name || item.original_name, item.check_rule || item.title, item.suggested_score ?? '', item.max_score ?? '', confidenceLabel(item.confidence), item.evidence || '', item.reason || '']); const csv = '\ufeff' + rows.map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(',')).join('\r\n'); const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], {type:'text/csv;charset=utf-8'})); link.download = '评标评分汇总.csv'; link.click(); URL.revokeObjectURL(link.href); } catch (error) { alert(error.message); } };
  document.querySelectorAll('[data-tab]').forEach((button) => button.onclick = () => { if (button.disabled) return; document.querySelectorAll('[data-tab]').forEach((item) => item.classList.toggle('active', item === button)); document.querySelectorAll('[data-pane]').forEach((item) => item.classList.toggle('active', item.dataset.pane === button.dataset.tab)); });
  Promise.all([loadProjects(), loadProfiles()]).catch((error) => { $('projects').textContent = error.message; });
})();
