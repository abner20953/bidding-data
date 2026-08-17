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
  let currentRuleSet = null;
  const defaultDocumentTitle = document.title;
  let completionTicker = null;
  let cachedHighlights = [];
  let focusRefreshInFlight = false;
  let currentPriceSheet = null;
  let lastObservedPriceTaskId = null;
  let selectedPriceRuleId = '';
  let priceDraft = null;
  const $ = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  // 展示层统一清洗：去掉内部编号（SI-1/SI-2）、JSON 字段名记法（status=、suggested_score=）、
  // 统一页码格式（第P55页 → 第55页）。只影响展示，不修改已保存结果。
  const fieldNotationPattern = /\b(?:status|risk_level|evidence_quality|confidence|suggested_score|max_score|matched_count|needs_ocr|coverage_status|final_score|effective_score|scope|validity|met)\s*=\s*[^，。；;：:\s]+/g;
  function normalizePageRefs(value) {
    let text = String(value || '');
    text = text.replace(/第P(\d+)-P?(\d+)页/g, '第$1-$2页');
    text = text.replace(/第P(\d+)页/g, '第$1页');
    text = text.replace(/(^|[^0-9A-Za-z])P(\d+)-P?(\d+)(?![0-9A-Za-z])/g, '$1第$2-$3页');
    text = text.replace(/(^|[^0-9A-Za-z])P(\d+)(?![0-9A-Za-z])/g, '$1第$2页');
    return text;
  }
  function cleanDisplayText(value) {
    let text = String(value || '').replace(/\bSI-\d+\b/g, '').replace(/[（(]\s*[）)]/g, '').replace(fieldNotationPattern, '').replace(/计分过程：/g, '');
    return normalizePageRefs(text).replace(/\s+/g, ' ').trim();
  }
  function sortedPageList(pages) {
    return Array.isArray(pages) ? pages.filter((page) => Number.isInteger(page) && page > 0).sort((a, b) => a - b) : [];
  }
  function ruleTitle(result) {
    const title = String(result?.title || '').trim();
    return title || conciseText(String(result?.check_rule || ''), 48);
  }
  function ruleCellHtml(result) {
    const title = escapeHtml(ruleTitle(result));
    const full = String(result?.check_rule || '').trim();
    const short = String(result?.title || '').trim();
    if (full && full !== short) {
      return `${title}<details class="rule-full"><summary>完整规则</summary><span>${escapeHtml(full)}</span></details>`;
    }
    return title;
  }
  const highlightLevelRank = {critical:3, high:2, attention:1, none:0};
  async function request(path, options = {}) { let response; try { response = await fetch(`${api}${path}`, options); } catch (_) { throw new Error('无法连接本地服务，请确认程序仍在运行后刷新页面重试'); } let data; try { data = await response.json(); } catch (_) { data = {error:`请求失败（HTTP ${response.status}）`}; } if (!response.ok) throw new Error(data.error || '请求失败'); return data; }
  function formatLocalTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value || '');
    const pad = (n) => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }
  function initUsagePopover() {
    const wrap = $('usage-trigger-wrap');
    const trigger = $('usage-trigger');
    const popover = $('usage-popover');
    if (!wrap || !trigger || !popover) return;
    let hideTimer = null;
    const show = () => { clearTimeout(hideTimer); popover.classList.remove('hidden'); trigger.setAttribute('aria-expanded', 'true'); };
    const scheduleHide = () => { hideTimer = setTimeout(() => { popover.classList.add('hidden'); trigger.setAttribute('aria-expanded', 'false'); }, 250); };
    trigger.addEventListener('click', (event) => { event.stopPropagation(); popover.classList.contains('hidden') ? show() : scheduleHide(); });
    wrap.addEventListener('mouseenter', show);
    wrap.addEventListener('mouseleave', scheduleHide);
    document.addEventListener('click', (event) => { if (!wrap.contains(event.target)) { popover.classList.add('hidden'); trigger.setAttribute('aria-expanded', 'false'); } });
  }
  function initDeploymentPopover() {
    const wrap = $('deploy-version-wrap');
    const trigger = $('deploy-version');
    const popover = $('deploy-version-popover');
    if (!wrap || !trigger || !popover) return;
    const hide = () => { popover.classList.add('hidden'); trigger.setAttribute('aria-expanded', 'false'); };
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      const hidden = popover.classList.toggle('hidden');
      trigger.setAttribute('aria-expanded', hidden ? 'false' : 'true');
      if (!hidden) loadBuildInfo().catch(() => {});
    });
    document.addEventListener('click', (event) => { if (!wrap.contains(event.target)) hide(); });
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') hide(); });
  }
  async function loadBuildInfo() {
    try {
      const info = await request('/build-info');
      // 与“模型用量 · 最后一次运行”的 deploy_commit 共用后端运行时版本来源。
      // 镜像构建标记和宿主机部署记录仅用于接口诊断，不能再干扰用户看到的版本号。
      const runtimeVersion = info.runtime_release_commit || info.commit || '未知';
      const lines = [`运行版本：${runtimeVersion}`, `运行进程启动：${info.deployed_at || '未知'}`];
      if (info.prompt_version) lines.push(`提示词版本：${info.prompt_version}`);
      $('deploy-version').title = lines.join('\n');
      $('deploy-version-details').textContent = lines.join('\n');
    } catch (_) {
      $('deploy-version').title = '部署信息不可用';
      $('deploy-version-details').textContent = '部署信息不可用';
    }
  }
  function renderLatestRunUsage(run) {
    const node = $('latest-run-usage');
    if (!node) return;
    if (!run || !run.finished_at) { node.textContent = '暂无记录'; return; }
    const taskLabels = {evaluate_all:'综合评审', compare_documents:'文件查重', extract_rules:'规则提取', review_documents:'评审', score_objective:'客观评分', score_subjective:'主观评分', parse_documents:'文件解析'};
    const taskLabel = taskLabels[run.task_type] || run.task_type || '';
    const detail = run.metered_calls
      ? `输入 ${Number(run.prompt_tokens || 0).toLocaleString()} / 输出 ${Number(run.completion_tokens || 0).toLocaleString()} / 合计 ${Number(run.total_tokens || 0).toLocaleString()} Token`
      : `模型接口未返回 Token；已发送 ${Number(run.input_chars || 0).toLocaleString()} 字符`;
    const families = run.families || {};
    const extras = [];
    if (families.vision && families.vision.call_count) extras.push(`图片识别 ${families.vision.call_count} 次`);
    if (run.ocr_requests) extras.push(`腾讯 OCR ${run.ocr_requests} 页`);
    if (run.local_ocr_pages) extras.push(`本地 OCR ${run.local_ocr_pages} 页`);
    const cache = run.prompt_tokens ? `；缓存命中 ${Math.round((run.cache_hit_tokens || 0) * 100 / run.prompt_tokens)}%` : '';
    const version = run.deploy_commit ? `版本 ${run.deploy_commit}` : '版本未记录';
    const prompt = run.prompt_version ? `（${run.prompt_version}）` : '';
    const prefix = taskLabel ? `${taskLabel}：` : '';
    node.textContent = `${prefix}${detail}（${run.call_count || 0} 次调用${extras.length ? '；其中' + extras.join('、') : ''}${cache}）· 结束于 ${formatLocalTime(run.finished_at)} · ${version}${prompt}`;
  }
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
    const pages = sortedPageList(
      Array.isArray(preferred.evidence_pages) && preferred.evidence_pages.length
        ? preferred.evidence_pages : (Array.isArray(preferred.checked_pages) ? preferred.checked_pages : [])
    );
    const source = {vision:'图片识别', tencent_ocr:'腾讯 OCR', local_ocr:'本地 RapidOCR'}[preferred.source] || '补充识别';
    const pageText = pages.length ? `（${pages.map((page) => `P${page}`).join('、')}）` : '';
    const rawSummary = result?.max_score != null ? compactObjectiveOcrText(preferred.summary) : preferred.summary;
    const summary = conciseText(cleanDisplayText(rawSummary), result?.max_score != null ? 110 : 150);
    return `${source}${pageText}${summary ? `：${summary}` : '：已完成关键页核验。'}`;
  }
  function conciseResultEvidence(result) {
    return evidenceLayerSummary(result) || conciseText(cleanDisplayText(result?.evidence), 180) || '-';
  }
  function latestLayerSegment(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    const parts = raw.split(/(?=【(?:图片识别|腾讯OCR|本地OCR|OCR)[^】]*】)/).filter(Boolean);
    if (parts.length <= 1) return raw;
    return parts[parts.length - 1].replace(/^【(?:图片识别|腾讯OCR|本地OCR|OCR)[^】]*】/, '');
  }
  function conciseResultReason(result) {
    const segment = latestLayerSegment(result?.reason);
    const compacted = result?.max_score != null ? compactObjectiveOcrText(segment) : segment;
    const cleaned = cleanDisplayText(compacted);
    if (cleaned) return conciseText(cleaned, result?.max_score != null ? 120 : 150);
    const status = String(result?.vision_status || '');
    return /(?:applied|partial|uncovered|conflict)/.test(status) ? conciseText(cleanDisplayText(result?.vision_message), 120) : '';
  }
  // 主表摘要只取“结论句”：层文本首句常是过程描述（“本次图片覆盖…”），真正结论
  // 在中间或末尾；按结论语义词定位并取最后一个结论子句，找不到再回退首句。
  const conclusionMarkers = /建议|判|得分|得\d+分|满足|不满足|需复核|不一致|未提供|需人工|未发现|缺失|不予|不符|无效|不通过|通过|采纳|应计|暂计|应为|封顶/;
  function extractConclusionClause(value) {
    const text = cleanDisplayText(value);
    if (!text) return '';
    const clauses = text.split(/(?<=[。；;])/).map((item) => item.trim()).filter(Boolean);
    if (!clauses.length) return '';
    const matches = clauses.filter((item) => conclusionMarkers.test(item));
    const selected = matches.length ? matches[matches.length - 1] : clauses[0];
    if (selected.length <= 90) return selected;
    const cut = selected.slice(0, 90);
    const boundary = Math.max(cut.lastIndexOf('，'), cut.lastIndexOf(','), cut.lastIndexOf('、'));
    return boundary > 20 ? `${cut.slice(0, boundary)}…` : `${cut}…`;
  }
  const summaryScorePattern = /((?:建议|暂计|应计|应为|合计|总计|得|共)[^。；;]{0,16}?)(?<![0-9.\-—–～~至])(\d+(?:\.\d+)?)\s*分/g;
  function reconcileSummaryScore(summary, suggested) {
    if (!summary || suggested == null || !Number.isFinite(Number(suggested))) return summary;
    const values = Array.from(summary.matchAll(summaryScorePattern)).map((match) => Number(match[2]));
    if (!values.length) return summary;
    if (values.every((value) => Math.abs(value - Number(suggested)) <= 1e-6)) return summary;
    return summary.replace(summaryScorePattern, '$1').replace(/\s+/g, ' ').trim();
  }
  function conciseResultSummary(result) {
    const stored = reconcileSummaryScore(cleanDisplayText(result?.conclusion_summary), result?.suggested_score);
    if (stored) return stored.length > 90 ? `${stored.slice(0, 90)}…` : stored;
    const segment = latestLayerSegment(result?.reason);
    const compacted = result?.max_score != null ? compactObjectiveOcrText(segment) : segment;
    const fromReason = extractConclusionClause(compacted);
    if (fromReason) return fromReason;
    const layer = evidenceLayerSummary(result);
    if (layer) return extractConclusionClause(layer);
    return extractConclusionClause(result?.evidence);
  }
  function verificationLineHtml(result) {
    const status = String(result?.vision_status || '');
    const ocrStatus = String(result?.ocr_status || (status.startsWith('ocr_') ? status : 'not_requested'));
    const multimodal = String(result?.multimodal_status || (status.startsWith('ocr_') ? 'not_requested' : status));
    const means = ['文字解析'];
    const layerList = Array.isArray(result?.evidence_layers) ? result.evidence_layers : [];
    const sources = new Set(layerList.map((layer) => layer && layer.source).filter(Boolean));
    if (sources.has('local_ocr') || ocrStatus !== 'not_requested') means.push('本地OCR');
    if (sources.has('tencent_ocr')) means.push('腾讯OCR');
    if (sources.has('vision') || (multimodal !== 'not_requested' && !/^ocr_/.test(status))) means.push('图片识别');
    const pages = [];
    const addPages = (list) => { (Array.isArray(list) ? list : []).forEach((page) => { if (Number.isInteger(page) && page > 0 && !pages.includes(page)) pages.push(page); }); };
    layerList.forEach((layer) => { if (layer) { addPages(layer.checked_pages); addPages(layer.evidence_pages); } });
    addPages(result?.vision_pages);
    addPages(result?.vision_evidence_pages);
    pages.sort((a, b) => a - b);
    const pageText = pages.length
      ? (pages.length <= 6
        ? ` · 证据页 ${pages.map((page) => `P${page}`).join('、')}`
        : ` · 证据页 ${pages.slice(0, 6).map((page) => `P${page}`).join('、')} 等 ${pages.length} 页`)
      : '';
    const abnormal = /failed|unavailable|quota_exhausted|not_located|uncovered/.test(`${status} ${ocrStatus} ${multimodal}`);
    return `${abnormal ? '核验状态' : '已核验'}：${means.join('＋')}${pageText}`;
  }
  const layerSourceLabels = {图片识别:'图片补充', '腾讯OCR':'腾讯 OCR 补充', 本地OCR:'本地 OCR 补充', OCR:'OCR 补充'};
  function layerBlockHtml(label, text, updated) {
    const full = cleanDisplayText(text);
    if (!full) return '';
    const badge = updated ? '<small class="layer-updated">已被后续核验更新</small>' : '';
    const labelHtml = label ? `<strong>${escapeHtml(label)}</strong>` : '';
    if (full.length <= 160) {
      return `<div class="layer-block layer-block-plain">${labelHtml}${badge}<span>${escapeHtml(full)}</span></div>`;
    }
    const preview = `${full.slice(0, 160)}…`;
    // 折叠时摘要行显示前 160 字加省略号；展开后预览整行隐藏，完整内容在同一处
    // 连贯显示，不再出现“省略号残留”或预览与正文分两段造成的断句。
    return `<details class="layer-block"><summary>${labelHtml}${badge}<span class="layer-preview">${escapeHtml(preview)}</span><span class="layer-full">${escapeHtml(full)}</span></summary></details>`;
  }
  function layeredBlocksHtml(text) {
    const parts = String(text || '').split(/(?=【(?:图片识别|腾讯OCR|本地OCR|OCR)[^】]*】)/).filter(Boolean);
    if (!parts.length) return '';
    return parts.map((part, index) => {
      const marker = part.match(/^【(图片识别|腾讯OCR|本地OCR|OCR)[^】]*】/);
      // 第一个文字层块由外层“文字证据/文字理由”标题覆盖，不再重复显示标题；
      // 只有 OCR/图片补充块才带各自的小标题。
      let label = '';
      let body = part;
      if (marker) {
        label = layerSourceLabels[marker[1]] || '补充';
        body = part.slice(marker[0].length);
      }
      return layerBlockHtml(label, body, parts.length > 1 && index < parts.length - 1);
    }).join('');
  }
  function rawResultDetailHtml(result) {
    const evidence = String(result?.evidence || '').trim();
    const reason = String(result?.reason || '').trim();
    if (!evidence && !reason) return '';
    return `<details class="evidence-chain"><summary>查看完整文字结论</summary>${evidence ? `<div class="evidence-layer"><strong>文字证据</strong>${layeredBlocksHtml(evidence)}</div>` : ''}${reason ? `<div class="evidence-layer"><strong>文字理由</strong>${layeredBlocksHtml(reason)}</div>` : ''}</details>`;
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
  function taskTypeLabel(type) { return {parse_documents:'文件解析',compare_documents:'文件查重',extract_rules:'规则提取',extract_price_rules:'价格规则提取',calculate_price_scores:'AI 价格分计算',review_documents:'文件审查',score_objective:'客观评分',score_subjective:'主观评分',evaluate_all:'综合评审'}[type] || type || '任务'; }
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
    const labels = {parse_documents:'文件解析', compare_documents:'文件查重', extract_rules:'规则提取', extract_price_rules:'价格规则提取', calculate_price_scores:'AI 价格分计算', evaluate_all:'综合评审'};
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
  function resetProjectPanels() { ['documents','rules','price-sheet-content','review-results','objective-results','subjective-results'].forEach((id) => { const node = $(id); if (node) node.innerHTML = '<p class="muted">正在加载当前项目…</p>'; }); $('token-usage').textContent = '正在加载当前项目…'; $('latest-run-usage').textContent = '正在加载当前项目…'; $('task-status').textContent = '正在加载当前项目…'; lastPartialDocumentsKey = ''; currentPriceSheet = null; selectedPriceRuleId = ''; priceDraft = null; lastObservedPriceTaskId = null; }
  async function openProject(id) { activeProject = id; wasTaskActive = false; lastActiveTaskId = null; lastCompareTask = null; resetProjectPanels(); stopCompletionTicker(); $('projects-panel').classList.add('hidden'); $('project-form').classList.add('hidden'); $('workspace').classList.remove('hidden'); await refreshProject(); await loadProfiles(); await refreshRules(); await refreshPriceSheet(false, true); await refreshReview(); await refreshScores(); await refreshUsage(); }
  async function refreshUsage() { if (!activeProject) return; const data = await request(`/projects/${activeProject}/token-usage`); const u = data.usage; const localPerf = u.local_ocr_performance || {}; if (!u.call_count && !u.ocr_requests && !u.local_ocr_pages && !localPerf.run_count) { $('token-usage').textContent = '尚无调用记录'; } else { const detail = u.metered_calls ? `输入 ${u.prompt_tokens.toLocaleString()} / 输出 ${u.completion_tokens.toLocaleString()} / 合计 ${u.total_tokens.toLocaleString()} Token` : `模型接口未返回 Token；已发送 ${u.input_chars.toLocaleString()} 字符`; const families = u.families || {}; const extras = []; if (families.vision && families.vision.call_count) extras.push(`图片识别 ${families.vision.call_count} 次`); if (u.ocr_requests) extras.push(`腾讯 OCR ${u.ocr_requests} 页`); if (u.local_ocr_pages || localPerf.run_count) { let localLabel = `本地 OCR ${u.local_ocr_pages || 0} 页`; if (localPerf.average_ms_per_page) localLabel += `，平均 ${(localPerf.average_ms_per_page / 1000).toFixed(1)} 秒/页`; if (localPerf.peak_rss_kb) localLabel += `，峰值约 ${Math.ceil(localPerf.peak_rss_kb / 1024)} MB`; extras.push(localLabel); } const cache = u.prompt_tokens ? `；缓存命中 ${Math.round((u.cache_hit_tokens || 0) * 100 / u.prompt_tokens)}%` : ''; $('token-usage').textContent = `${detail}（${u.call_count} 次调用${extras.length ? '；其中' + extras.join('、') : ''}${cache}）`; } renderLatestRunUsage(data.latest_run); }
  async function refreshProject() { if (!activeProject) return; const data = await request(`/projects/${activeProject}`); const p = data.project; $('workspace-name').textContent = projectDisplayName(p); $('workspace-meta').textContent = projectMeta(p); const active = data.tasks.find((t) => ['queued','running'].includes(t.status)); if (active) lastActiveTaskId = active.task_id; $('task-status').innerHTML = taskText(active || data.tasks[0], data.queue_contexts?.[active?.task_id]); document.querySelectorAll('.retry-failed-evaluation').forEach((button) => button.onclick = () => queue('evaluate_all', {retry_failed_task_id:button.dataset.task})); if (active) startPolling(); else stopPolling(); renderDocuments(data.documents); const completed = data.tasks.find((t) => t.task_type === 'compare_documents' && t.status === 'success'); if (completed && completed.task_id !== lastCompareTask) { lastCompareTask = completed.task_id; await renderCompare(completed.task_id, data.documents); } const completedDocuments = active?.task_type === 'evaluate_all' ? (active.completed_documents || []) : []; const partialKey = completedDocuments.length ? `${active.task_id}:${completedDocuments.map((item) => item.document_id).sort().join(',')}` : ''; if (partialKey && partialKey !== lastPartialDocumentsKey) { lastPartialDocumentsKey = partialKey; await Promise.all([refreshReview(), refreshScores()]); } if (!active) lastPartialDocumentsKey = ''; const justFinished = wasTaskActive && !active; const finishedTask = justFinished ? data.tasks.find((task) => task.task_id === lastActiveTaskId && task.status === 'success') : null; wasTaskActive = Boolean(active); const priceCompleted = data.tasks.find((task) => ['extract_price_rules','calculate_price_scores'].includes(task.task_type) && task.status === 'success'); const priceJustCompleted = priceCompleted && priceCompleted.task_id !== lastObservedPriceTaskId; if (priceCompleted) lastObservedPriceTaskId = priceCompleted.task_id; if (justFinished || priceJustCompleted) { lastActiveTaskId = null; await Promise.all([refreshRules(), refreshPriceSheet(false, true), refreshReview(), refreshScores(), refreshUsage()]); if (finishedTask) startCompletionTicker(finishedTask); } }
  function renderDocuments(documents) { $('documents').innerHTML = documents.length ? `<table><thead><tr><th>角色</th><th>文件</th><th>投标人</th><th>解析</th><th>页数/字符</th><th>操作</th></tr></thead><tbody>${documents.map((d) => `<tr><td><span class="tag">${roleLabel(d.role)}</span></td><td>${escapeHtml(d.original_name)}</td><td>${escapeHtml(d.bidder_name || '-')}</td><td class="status-${d.parse_status}">${escapeHtml(parseStatusLabel(d.parse_status))}${d.parse_error ? `<br>${escapeHtml(d.parse_error)}` : ''}</td><td>${d.page_count ?? '-'} / ${d.text_length ?? '-'}</td><td><a class="download-document" href="/api/evaluation-workbench/projects/${activeProject}/documents/${d.document_id}/download">下载</a><button class="delete-document" data-document="${d.document_id}">删除</button></td></tr>`).join('')}</tbody></table>` : '<p class="muted">尚未上传文件。</p>'; $('documents').querySelectorAll('.delete-document').forEach((button) => button.onclick = async () => { if (!confirm('删除文件会同时移除其历史审查和评分结果，是否继续？')) return; try { await request(`/projects/${activeProject}/documents/${button.dataset.document}`, {method:'DELETE'}); await refreshProject(); await refreshPriceSheet(false, true); await refreshReview(); await refreshScores(); } catch (error) { alert(error.message); } }); }
  function priceValue(value) {
    if (value == null || String(value).trim() === '') return '-';
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString('zh-CN', {maximumFractionDigits:4}) : String(value);
  }
  function priceQuoteSource(entry) {
    if (entry.manual_quote) return ['人工修正', entry.extracted_quote ? `自动提取：${priceValue(entry.extracted_quote)}` : ''];
    const labels = {parsed_text:'文件文字自动提取', ocr_cache:'已有 OCR 缓存'};
    if (entry.source_type === 'manual') return ['手工补录', ''];
    const status = {pending:'等待识别',found:'已识别唯一报价',ambiguous:'发现多个金额，待核对',missing:'未识别到唯一报价',unavailable:'文件尚未解析'}[entry.extraction_status] || '待核对';
    return [labels[entry.quote_source] || status, entry.quote_excerpt || status];
  }
  function priceSheetRule() {
    const rules = Array.isArray(currentPriceSheet?.rules) ? currentPriceSheet.rules : [];
    if (!rules.some((rule) => rule.rule_id === selectedPriceRuleId)) selectedPriceRuleId = rules[0]?.rule_id || '';
    return rules.find((rule) => rule.rule_id === selectedPriceRuleId) || null;
  }
  function copyPriceValue(value) { return JSON.parse(JSON.stringify(value)); }
  function normaliseDraftAdjustment(entry) {
    const saved = entry.adjustment && typeof entry.adjustment === 'object' ? copyPriceValue(entry.adjustment) : {};
    if (saved.mode) return saved;
    return entry.evaluation_price ? {mode:'manual', note:''} : {mode:'none', note:''};
  }
  function beginPriceDraft() {
    priceDraft = {entries:(currentPriceSheet?.entries || []).map((entry) => ({...copyPriceValue(entry), adjustment:normaliseDraftAdjustment(entry), draft_id:entry.price_entry_id})), deleted:[], dirty:false};
  }
  function priceDraftEntry(key) { return priceDraft?.entries.find((entry) => entry.draft_id === key) || null; }
  function renderPriceSheet() {
    if (!document.querySelector('[data-pane="price"]')?.classList.contains('active') || !currentPriceSheet) return;
    if (!priceDraft) beginPriceDraft();
    renderPriceSheetPane(); updatePriceSheetFooter();
  }
  function draftFieldHtml(entry, field, label, extra = '') {
    // 调整类字段（优惠适用金额/比例/说明）保存在 entry.adjustment 内层；
    // 若读取扁平键会导致已保存的数值渲染为空。
    let value = entry[field] ?? '';
    if (field.startsWith('adjustment_')) {
      value = entry.adjustment?.[field.replace('adjustment_', '')] ?? '';
    }
    return `<label>${label}<input class="price-draft-field" data-key="${escapeHtml(entry.draft_id)}" data-field="${field}" ${extra} value="${escapeHtml(value)}"></label>`;
  }
  function adjustmentControlHtml(entry) {
    const adjustment = entry.adjustment || {mode:'none'}; const mode = adjustment.mode || 'none';
    const modeSelect = `<label>计分价处理<select class="price-adjustment-mode" data-key="${escapeHtml(entry.draft_id)}"><option value="none" ${mode === 'none' ? 'selected' : ''}>按确认报价计分</option><option value="discount" ${mode === 'discount' ? 'selected' : ''}>价格优惠扣除</option><option value="tax_excluded" ${mode === 'tax_excluded' ? 'selected' : ''}>剔除税率部分</option><option value="manual" ${mode === 'manual' ? 'selected' : ''}>直接填写计分价</option></select></label>`;
    if (mode === 'manual') return `${modeSelect}${draftFieldHtml(entry, 'evaluation_price', '计分价（元）', 'type="number" min="0" step="0.01" placeholder="人工确认后的计分价"')}${draftFieldHtml(entry, 'adjustment_note', '调整说明（可选）', 'type="text" placeholder="例如政策扣除、税率差异"')}`;
    if (mode === 'discount' || mode === 'tax_excluded') {
      return `${modeSelect}${draftFieldHtml(entry, 'adjustment_base_amount', mode === 'discount' ? '优惠适用金额（元）' : '含税适用金额（元）', 'type="number" min="0" step="0.01" placeholder="默认全部报价"')}${draftFieldHtml(entry, 'adjustment_rate_percent', mode === 'discount' ? '优惠比例（%）' : '税率（%）', 'type="number" min="0" max="100" step="0.01"')}${draftFieldHtml(entry, 'adjustment_note', '调整说明（可选）', 'type="text" placeholder="请注明适用依据"')}`;
    }
    return modeSelect;
  }
  function renderPriceSheetPane() {
    const target = $('price-sheet-content'); if (!target || !currentPriceSheet || !priceDraft) return;
    const rules = currentPriceSheet.rules || []; const rule = priceSheetRule();
    const entries = priceDraft.entries; const included = entries.filter((entry) => entry.included).length;
    const rulePicker = rules.length ? `<label class="price-rule-picker">价格评分规则<select id="price-rule-select">${rules.map((item) => `<option value="${item.rule_id}" ${item.rule_id === selectedPriceRuleId ? 'selected' : ''}>${escapeHtml(item.title)} · 满分 ${item.max_score ?? '-'}</option>`).join('')}</select></label>` : '<p class="muted">尚未提取到价格评分规则；当前可先核对报价并补录。</p>';
    const ruleDetails = rule ? `<details class="price-rule-details"><summary>查看报价分规则</summary><p><strong>${escapeHtml(rule.title)}</strong> · ${escapeHtml(rule.formula_label)} · 满分 ${escapeHtml(rule.max_score ?? '-')}</p><p>${escapeHtml(rule.check_rule || '未提供明确计算说明。')}</p>${rule.formula_reason ? `<p class="muted">本地公式校验说明：${escapeHtml(rule.formula_reason)}</p>` : ''}${rule.source_text ? `<p class="muted">原文：${escapeHtml(rule.source_text)}</p>` : ''}</details>` : '';
    const ruleStatus = currentPriceSheet.rule_set?.status === 'confirmed' ? '已确认' : (currentPriceSheet.rule_set?.status === 'price_only' ? '独立提取已就绪' : '按当前规则草稿试算');
    const summary = `<div class="price-sheet-summary"><span class="price-summary-item">参与：<strong>${included} 家</strong></span><span class="price-summary-item">规则状态：<strong>${ruleStatus}</strong></span>${rule ? `<span class="price-summary-item">公式：<strong>${escapeHtml(rule.formula_label)}</strong></span><span class="price-summary-item">当前基准价：<strong>${rule.benchmark_price ? `${priceValue(rule.benchmark_price)} 元` : '待 AI 计算'}</strong></span>` : ''}</div>`;
    const rows = entries.map((entry) => {
      const saved = (currentPriceSheet.entries || []).find((item) => item.price_entry_id === entry.price_entry_id) || entry;
      const source = priceQuoteSource(saved); const score = rule ? saved.scores?.[rule.rule_id] : null;
      const scoreText = priceDraft.dirty ? '保存后统一重算' : (score ? `${priceValue(score.score)} 分` : (entry.included ? '待计算' : '不参与'));
      const name = entry.source_type === 'manual' ? draftFieldHtml(entry, 'bidder_name', '投标人名称', 'type="text"') : `<strong>${escapeHtml(entry.bidder_name)}</strong><small class="muted">已上传文件</small>`;
      const quote = `${draftFieldHtml(entry, 'manual_quote', '确认报价（元）', `type="number" min="0" step="0.01" placeholder="${escapeHtml(entry.extracted_quote || '请填写')}"`)}<small class="muted">自动识别：${escapeHtml(priceValue(entry.extracted_quote))}</small>${entry.source_type === 'document' && entry.manual_quote ? `<button class="price-use-extracted" data-key="${escapeHtml(entry.draft_id)}" type="button">恢复自动报价</button>` : ''}<details class="price-source-details"><summary>${escapeHtml(source[0])}</summary><span>${escapeHtml(source[1] || '无额外摘录')}</span></details>`;
      const scope = `<label class="inline-check price-participation-toggle"><input class="price-draft-field" data-key="${escapeHtml(entry.draft_id)}" data-field="included" type="checkbox" ${entry.included ? 'checked' : ''}><span>参与计算</span></label>${entry.included ? '<small class="muted">参与基准价与价格分计算</small>' : draftFieldHtml(entry, 'exclusion_reason', '不参与原因（可选）', 'type="text" placeholder="例如已否决"')}`;
      const remove = entry.source_type === 'manual' ? `<button class="delete-price-draft danger" data-key="${escapeHtml(entry.draft_id)}" type="button">删除补录</button>` : '';
      return `<tr class="${entry.included ? '' : 'price-excluded'}"><td class="price-bidder-cell">${name}</td><td>${quote}</td><td class="price-adjustment-cell">${adjustmentControlHtml(entry)}</td><td>${scope}</td><td><span class="${score?.source === 'manual' ? 'price-score price-score-manual' : score ? 'price-score' : 'price-score price-score-unavailable'}" title="${escapeHtml(score?.calculation || '')}">${escapeHtml(scoreText)}</span>${remove}</td></tr>`;
    }).join('');
    const coverageWarning = rule?.automatic && !rule.calculation_ready && rule.calculation_block_reason ? `<p class="price-sheet-warning">${escapeHtml(rule.calculation_block_reason)}</p>` : '';
    target.innerHTML = `<div class="price-sheet-toolbar">${rulePicker}${ruleDetails}</div>${summary}${coverageWarning}<p class="price-sheet-note">${escapeHtml(currentPriceSheet.notice || '')} 价格优惠、税率差异等由人工选择并填写依据；系统只按明确输入统一计算，不自动判断政策资格。</p><div class="price-table-wrap"><table class="price-table price-workbench-table"><thead><tr><th>投标人</th><th>确认报价</th><th>计分价调整</th><th>参与范围</th><th>价格分</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    $('price-rule-select')?.addEventListener('change', (event) => { selectedPriceRuleId = event.target.value; renderPriceSheetPane(); });
    target.querySelectorAll('.price-draft-field').forEach((input) => input.addEventListener('change', () => {
      const entry = priceDraftEntry(input.dataset.key); if (!entry) return;
      const field = input.dataset.field;
      if (field === 'included') entry.included = input.checked;
      else if (field.startsWith('adjustment_')) { entry.adjustment = entry.adjustment || {mode:'none'}; entry.adjustment[field.replace('adjustment_', '')] = input.value; }
      else entry[field] = input.value;
      priceDraft.dirty = true;
      if (field === 'included') renderPriceSheetPane();
      updatePriceSheetFooter();
    }));
    target.querySelectorAll('.price-adjustment-mode').forEach((input) => input.addEventListener('change', () => { const entry = priceDraftEntry(input.dataset.key); if (!entry) return; entry.adjustment = {...(entry.adjustment || {}), mode:input.value}; priceDraft.dirty = true; renderPriceSheetPane(); updatePriceSheetFooter(); }));
    target.querySelectorAll('.price-use-extracted').forEach((button) => button.onclick = () => { const entry = priceDraftEntry(button.dataset.key); if (!entry) return; entry.manual_quote = ''; priceDraft.dirty = true; renderPriceSheetPane(); updatePriceSheetFooter(); });
    target.querySelectorAll('.delete-price-draft').forEach((button) => button.onclick = () => { const entry = priceDraftEntry(button.dataset.key); if (!entry || !confirm('删除这条未上传投标人的补录记录吗？')) return; if (entry.price_entry_id) priceDraft.deleted.push(entry.price_entry_id); priceDraft.entries = priceDraft.entries.filter((item) => item.draft_id !== entry.draft_id); priceDraft.dirty = true; renderPriceSheetPane(); updatePriceSheetFooter(); });
  }
  function updatePriceSheetFooter() {
    const save = $('save-price-sheet-batch'); if (!save) return;
    const changes = priceDraft?.dirty ? '保存全部修改并由 AI 重算' : '由 AI 重新计算价格分';
    save.textContent = changes;
    $('price-sheet-dirty').textContent = priceDraft?.dirty ? '有未保存修改；保存后会使用上方模型统一计算价格分。' : '报价、优惠和参与范围可先集中编辑，再由上方所选模型统一计算。';
  }
  function batchPayload() {
    const cleanEntry = (entry) => {
      const adjustment = entry.adjustment || {mode:'none'};
      return {price_entry_id:entry.price_entry_id, bidder_name:entry.bidder_name, manual_quote:entry.manual_quote, evaluation_price:entry.evaluation_price, included:Boolean(entry.included), exclusion_reason:entry.exclusion_reason, adjustment:{mode:adjustment.mode || 'none', base_amount:adjustment.base_amount || '', rate_percent:adjustment.rate_percent || '', note:adjustment.note || ''}};
    };
    return {entries:priceDraft.entries.filter((entry) => entry.price_entry_id).map(cleanEntry), new_entries:priceDraft.entries.filter((entry) => !entry.price_entry_id).map(cleanEntry), delete_manual_entry_ids:priceDraft.deleted};
  }
  async function savePriceSheetBatch() {
    const button = $('save-price-sheet-batch'); button.disabled = true;
    try { const data = await request(`/projects/${activeProject}/price-sheet/batch`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(batchPayload())}); currentPriceSheet = data.price_sheet; beginPriceDraft(); renderPriceSheetPane(); updatePriceSheetFooter(); await queue('calculate_price_scores', {profile_id:$('price-profile').value, force_rerun:true}); }
    catch (error) { alert(error.message); }
    finally { button.disabled = false; }
  }
  function preparePriceSheetPane() {
    if (!currentPriceSheet) { $('price-sheet-content').innerHTML = '<p class="muted">正在加载价格工作表…</p>'; return; }
    if (!priceDraft) beginPriceDraft();
    renderPriceSheetPane(); updatePriceSheetFooter();
  }
  async function refreshPriceSheet(force = false, quiet = false) {
    if (!activeProject) return;
    if (priceDraft?.dirty && !force) return;
    const projectId = activeProject;
    try {
      let data = await request(`/projects/${projectId}/price-sheet${force ? '/refresh' : ''}`, force ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({force:true})} : {});
      if (projectId !== activeProject) return;
      currentPriceSheet = data.price_sheet;
      renderPriceSheet();
      // GET 始终纯读取。只有检测到新文件或解析内容变更时，前端才发起一次明确的 POST 刷新。
      if (!force && currentPriceSheet.needs_refresh && !currentPriceSheet.deferred) {
        data = await request(`/projects/${projectId}/price-sheet/refresh`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({force:false})});
        if (projectId !== activeProject) return;
        currentPriceSheet = data.price_sheet;
        renderPriceSheet();
      }
    } catch (error) {
      if (projectId !== activeProject) return;
      if (!quiet) throw error;
      console.warn('报价工作表自动刷新失败：', error);
      if (currentPriceSheet) {
        currentPriceSheet = {...currentPriceSheet, notice:`${currentPriceSheet.notice || ''} 报价工作表自动刷新失败，可稍后点击“重新识别文件报价”。`};
        renderPriceSheet();
      }
    }
  }
  function priorityLabel(value) { return {high:'高',medium:'中',normal:'常规',none:'无'}[value] || value; }
  function evidenceText(signal) { return (signal.evidence || []).map((item) => { const pages = item.page_a || item.page_b ? `${signal.bidder_a || '文件 A'}第${item.page_a || '?'}页 / ${signal.bidder_b || '文件 B'}第${item.page_b || '?'}页：` : ''; const content = item.text_a || item.value || item.field || ''; const right = item.text_b && item.text_b !== item.text_a ? ` ↔ ${item.text_b}` : ''; const context = item.context_a || item.context_b ? `\n字段语境：${item.context_a || '-'} ↔ ${item.context_b || '-'}` : ''; return `${pages}${content}${right}${context}`; }).join('\n'); }
  function compareCoverageLabel(value) { return ({complete:'文字覆盖基本充分', limited:'文字覆盖有限', severely_limited:'文字覆盖严重不足', unknown:'文字覆盖未知'})[value] || '文字覆盖未知'; }
  function compareSourceLabel(value) { return ({bidder_specific:'投标人特有内容', third_party_common:'共同第三方资料', public_template:'公共模板/公开资料', placeholder_or_extraction:'占位值或提取伪影', unknown:'来源待核'})[value] || '来源待核'; }
  function compareMaterialityLabel(value) { return ({substantive:'实质内容', formatting:'格式性内容', unknown:'实质性待核'})[value] || '实质性待核'; }
  async function renderCompare(taskId, documents) {
    const data = await request(`/tasks/${taskId}/compare-results`); const names = Object.fromEntries(documents.map((d) => [d.document_id, d.bidder_name || d.original_name])); const analysis = data.analysis;
    const rawTable = data.pairs.length ? `<h4>底层比对摘要（固定信号汇总与 AI 复核前）</h4><p class="hint">本表用于算法追溯，数量包含底层查重保留、但随后可能被固定表单规则或 AI 公共来源判断排除的候选；人工复核请以上方有效线索为准。</p><table><thead><tr><th>文件对</th><th>完全/近似候选</th><th>共同错误候选</th><th>敏感实体</th><th>候选匹配占比 A/B</th></tr></thead><tbody>${data.pairs.map((pair) => { const s = pair.result.summary || {}; return `<tr><td>${escapeHtml(names[pair.document_a_id])}<br>↔ ${escapeHtml(names[pair.document_b_id])}</td><td>${s.exact || 0} / ${s.fuzzy || 0}</td><td>${s.shared_error || 0}</td><td>${s.entity || 0}</td><td>${s.matched_ratio_a || 0}% / ${s.matched_ratio_b || 0}%</td></tr>`; }).join('')}</tbody></table>` : '<p class="muted">任务尚未生成文件对结果。</p>';
    if (!analysis) { $('compare-results').innerHTML = rawTable; return; }
    const dimLabels = Object.fromEntries((analysis.executed_dimensions || []).map((item) => [item.dimension, item.label]));
    const coverage = analysis.text_coverage || {}; const coverageDocs = coverage.documents || [];
    const coverageText = coverageDocs.length ? `${compareCoverageLabel(coverage.status)}：${coverageDocs.map((item) => `${item.bidder_name}（疑似扫描页 ${item.suspected_scan_pages ?? '-'}/${item.total_pages ?? '-'}）`).join('；')}` : '未取得文字覆盖统计';
    const pipeline = analysis.pipeline_status || {}; const pipelineText = pipeline.current ? '当前版本结果' : (pipeline.reason || '历史结果，建议重新运行');
    const pairTable = `<h4>横向复核优先级</h4><table><thead><tr><th>文件对</th><th>文字覆盖</th><th>有效独立维度</th><th>有效线索</th><th>复核优先级</th></tr></thead><tbody>${(analysis.pair_summaries || []).map((item) => { const rawCount = Number(item.raw_signal_count ?? item.signal_count ?? 0); const effectiveCount = Number(item.signal_count || 0); const countText = rawCount > effectiveCount ? `${effectiveCount}（底层 ${rawCount}）` : String(effectiveCount); return `<tr><td>${escapeHtml(item.bidder_a)} ↔ ${escapeHtml(item.bidder_b)}</td><td>${escapeHtml(compareCoverageLabel(item.text_coverage?.status))}</td><td>${item.independent_dimension_count}（${escapeHtml(item.dimensions.map((key) => dimLabels[key] || key).join('、') || '未发现')}）</td><td>${escapeHtml(countText)}</td><td><span class="priority-${item.review_priority}">${priorityLabel(item.review_priority)}</span></td></tr>`; }).join('')}</tbody></table>`;
    const signals = analysis.signals || [];
    // 固定规则线索按 AI 风险从高到低展示；同一风险保持原始检测顺序，便于回溯原始结果。
    const orderedSignals = [...signals].sort((left, right) => riskRank(right.ai_assessment?.risk_level) - riskRank(left.ai_assessment?.risk_level));
    const ai = analysis.ai_assessment || {}; const aiDecision = {confirmed_clue:'AI确认线索', suspected_clue:'AI疑似线索', excluded:'AI倾向排除', unassessable:'AI证据不足'};
    const signalRows = (items) => items.map((signal) => { const assessment = signal.ai_assessment || {}; const atomicHint = (signal.atomic_assessments || []).length > 1 ? `<br><small>本条已按 ${(signal.atomic_assessments || []).length} 项独立事实复核，当前展示优先结论。</small>` : ''; return `<tr><td>${escapeHtml(signal.bidder_a)}${signal.bidder_b ? ` ↔ ${escapeHtml(signal.bidder_b)}` : ''}<br><span class="tag">${escapeHtml(signal.dimension_label)}</span></td><td>${escapeHtml(signal.basis)}<pre class="evidence">${escapeHtml(evidenceText(signal) || '详见原始文件对结果')}</pre></td><td>${escapeHtml(aiDecision[assessment.decision] || '等待 AI 判定')}<br><small>${escapeHtml(assessment.reason || '')}</small><br><small>来源：${escapeHtml(compareSourceLabel(assessment.source_class))} · ${escapeHtml(compareMaterialityLabel(assessment.materiality))}<br>风险：${escapeHtml(riskLabel(assessment.risk_level))} · 置信度：${escapeHtml(confidenceLabel(assessment.confidence))}<br>${escapeHtml(assessment.suggested_check || '')}</small>${atomicHint}</td><td>${escapeHtml((signal.counter_evidence || []).join('；') || '-')}</td></tr>`; }).join('');
    const primarySignals = orderedSignals.filter((signal) => !signal.display_group || signal.display_group === 'main');
    const contextualSignals = orderedSignals.filter((signal) => signal.display_group === 'contextual');
    const lowValueSignals = orderedSignals.filter((signal) => signal.display_group === 'low_value');
    const signalTable = signals.length ? `<h4>固定规则线索与 AI 判定</h4><p class="hint">AI仅复核本地算法提取的短证据包，不读取完整文件对；本次为纯文字查重，不调用 OCR 或图片识别。${ai.status === 'success' ? `已判定 ${ai.assessed_count || 0} 条线索。` : escapeHtml(ai.reason || '')}</p>${primarySignals.length ? `<table><thead><tr><th>文件对 / 维度</th><th>证据</th><th>AI 判定</th><th>反证提示</th></tr></thead><tbody>${signalRows(primarySignals)}</tbody></table>` : '<p class="muted">没有需要优先复核的主线索。</p>'}${contextualSignals.length ? `<details><summary>需结合背景复核的辅助线索（${contextualSignals.length} 条，不计入文件对优先级）</summary><p class="hint">该组保留实质性内容，但存在共同第三方资料或公共来源等合理替代解释；请结合授权关系、答疑文件或原页判断。</p><table><thead><tr><th>文件对 / 维度</th><th>证据</th><th>AI 判定</th><th>反证提示</th></tr></thead><tbody>${signalRows(contextualSignals)}</tbody></table></details>` : ''}${lowValueSignals.length ? `<details><summary>已排除或低价值线索（${lowValueSignals.length} 条，不计入复核优先级）</summary><table><thead><tr><th>文件对 / 维度</th><th>证据</th><th>AI 判定</th><th>反证提示</th></tr></thead><tbody>${signalRows(lowValueSignals)}</tbody></table></details>` : ''}` : '<p class="muted">本次未检出可报告的横向异常线索。未检出不等同于不存在其他风险。</p>';
    const skipped = (analysis.not_executed_dimensions || []).map((item) => `${item.label}：${item.reason}`).join('；');
    $('compare-results').innerHTML = `<div class="decision-boundary">${escapeHtml(analysis.decision_boundary)}</div><p class="hint">结果版本：${escapeHtml(pipelineText)}。${escapeHtml(coverage.note || '本次仅比较可提取文字。')} ${escapeHtml(coverageText)}。</p><p class="hint">${escapeHtml(analysis.methodology.template_filter_note)}。多维命中仅提高人工复核优先级。未执行维度：${escapeHtml(skipped)}</p>${pairTable}${signalTable}${rawTable}`;
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
    for (const id of ['rule-profile','compare-profile','all-profile','price-profile']) $(id).innerHTML = options;
    const defaultProfile = active.find((p) => p.is_default) || active[0];
    if (defaultProfile) for (const id of ['rule-profile','compare-profile','all-profile','price-profile']) $(id).value = defaultProfile.profile_id;
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
  const acquisitionPresetLabels = {recommended:'按 AI 建议', adaptive:'文字优先', text_only:'纯文字核验', local_ocr:'本地 OCR 核验', smart:'智能增强', always:'强制增强', text:'腾讯 OCR 字段复核', visual:'签章/外观核验', dual:'双重增强核验', off:'不作增强核验', custom:'自定义高级设置'};
  const acquisitionLevelLabels = {off:'关闭', low:'快速', standard:'标准', high:'充分'};
  function inferredAcquisitionPreset(rule) {
    if (acquisitionPresetLabels[rule?.acquisition_preset]) return rule.acquisition_preset;
    return ({ocr_only:'text', vision_only:'visual', combined:'dual', off:'off'})[rule?.image_mode] || 'smart';
  }
  function simpleAcquisitionMode(rule) {
    const mode = String(rule?.image_mode || 'auto'); const trigger = String(rule?.vision_trigger || 'off'); const level = String(rule?.vision_level || 'off');
    const baseline = String(rule?.baseline_ocr_mode || 'auto');
    const recommendation = acquisitionRecommendationPayload(rule);
    if (['image_mode', 'vision_trigger', 'vision_level', 'baseline_ocr_mode'].every((key) => String(rule?.[key] || (key === 'baseline_ocr_mode' ? 'auto' : '')) === String(recommendation[key] || ''))) return 'recommended';
    if (mode === 'off' || trigger === 'off' || level === 'off') {
      if (baseline === 'text_only') return 'text_only';
      if (baseline === 'local_ocr') return 'local_ocr';
      return 'adaptive';
    }
    const preset = inferredAcquisitionPreset(rule);
    if (['always', 'visual', 'dual'].includes(preset) || (preset === 'custom' && mode === 'auto' && trigger === 'required')) return 'always';
    if (['smart', 'text'].includes(preset) || (mode === 'auto' && trigger === 'text_fallback')) return 'smart';
    return 'custom';
  }
  function baseVerificationSummary(rule) {
    const requirements = new Set(Array.isArray(rule?.evidence_requirements) ? rule.evidence_requirements.map(String) : []);
    const baseline = String(rule?.baseline_ocr_mode || 'auto');
    if (baseline === 'text_only') return {label:'纯文字核验', detail:'仅使用已解析全文文字，不调用本地 OCR、腾讯 OCR 或多模态。'};
    if (baseline === 'local_ocr') return {label:'本地 OCR 核验', detail:'在全文审查后对有限候选页执行本地 OCR，不调用腾讯 OCR 或多模态。'};
    if (rule?.check_mode === 'ocr' || Boolean(rule?.ocr_required)) {
      return {label:'本地 OCR 核验', detail:'该规则明确需要读取扫描件或图片文字；不作增强时仍会按有限候选页执行本地 OCR。'};
    }
    if (['document', 'field', 'visual'].some((item) => requirements.has(item))) {
      return {label:'按需本地 OCR 核验', detail:'常规先审全文文字；仅在扫描型文件或文字证据不足时，才按有限候选页调用本地 OCR。'};
    }
    return {label:'纯文字优先', detail:'常规仅使用已解析全文；只有机器可读文字覆盖不足时，才对有限候选页补做本地 OCR。'};
  }
  function acquisitionSummary(rule) {
    const level = String(rule?.vision_level || 'off'); const choice = simpleAcquisitionMode(rule);
    if (choice === 'recommended') { const base = baseVerificationSummary(rule); const enhanced = String(rule?.vision_trigger || 'off') !== 'off' && String(rule?.vision_level || 'off') !== 'off' && String(rule?.image_mode || 'off') !== 'off'; return {label:`AI 建议 · ${enhanced ? (acquisitionPresetLabels[inferredAcquisitionPreset(rule)] || '增强核验') : base.label}`, state:enhanced ? 'active' : 'off', detail:`系统已按规则证据类型选择：${base.detail}${enhanced ? '必要时会继续执行增强核验。' : ''}`}; }
    if (choice === 'adaptive') { const base = baseVerificationSummary(rule); return {label:`文字优先 · ${base.label}`, state:'off', detail:base.detail}; }
    if (choice === 'text_only') return {label:'纯文字核验', state:'off', detail:'人工指定仅审查已解析全文，不调用任何 OCR 或图片模型。'};
    if (choice === 'local_ocr') return {label:'本地 OCR 核验', state:'active', detail:'人工指定在全文审查后执行有限候选页本地 OCR，不调用增强服务。'};
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
    const choice = simpleAcquisitionMode(rule);
    if (choice === 'text_only') return '纯文字核验：仅使用已解析全文文字，不调用本地 OCR、腾讯 OCR或多模态。';
    if (choice === 'local_ocr') return '本地 OCR 核验：先审全文，再对有限候选页执行本地 RapidOCR；不调用腾讯 OCR 或多模态。';
    if (mode === 'off' || trigger === 'off' || level === 'off') { const base = baseVerificationSummary(rule); return `${choice === 'recommended' ? '按 AI 建议：' : choice === 'adaptive' ? '文字优先：' : ''}${base.label}。${base.detail}`; }
    const itemCount = Array.isArray(rule?.evidence_items) ? rule.evidence_items.length : 0;
    const compound = itemCount > 1;
    const ocrLimit = compound && level !== 'low' ? Math.min(level === 'high' ? 12 : 8, Math.max(level === 'high' ? 10 : 6, itemCount * 2)) : (level === 'high' ? 10 : 6);
    const visionLimit = compound && level !== 'low' ? Math.min(level === 'high' ? 8 : 6, Math.max(level === 'high' ? 6 : 4, itemCount)) : (level === 'high' ? 6 : 4);
    const budget = level === 'low' ? '最多处理少量候选页，适合单页材料或快速抽查' : level === 'standard' ? `OCR 最多 ${ocrLimit} 页；图片首批最多 ${visionLimit} 页、必要时可补看 4 页` : level === 'high' ? `OCR 最多 ${ocrLimit} 页；图片首批最多 ${visionLimit} 页、必要时可补看 6 页` : '按当前覆盖上限执行';
    const channel = {ocr_only:'仅核验扫描文字，不向模型发送图片', vision_only:'仅核验图片外观', combined:'OCR 与图片外观均会执行', auto:'由系统按材料与关键字段选择 OCR 或图片复核'}[mode] || '按规则决定取证路径';
    const compoundNote = compound ? `该规则含 ${itemCount} 个独立子项，候选页会按子项轮转覆盖，不由前一项占满。` : '';
    if (choice === 'recommended') return `按 AI 建议：${acquisitionRecommendationScene(rule)} ${channel}；${budget}。${compoundNote}`;
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
    return {acquisition_preset:preset, image_mode:mapped.image_mode, vision_trigger:mapped.vision_trigger, vision_level:active ? level : 'off', baseline_ocr_mode:'auto'};
  }
  function acquisitionRecommendationPayload(rule = {}) {
    const recommendation = rule?.acquisition_recommendation || {acquisition_preset:'off', vision_level:'off', baseline_ocr_mode:'auto'};
    return {...presetPayload(recommendation.acquisition_preset, recommendation.vision_level, rule), baseline_ocr_mode:recommendation.baseline_ocr_mode || 'auto'};
  }
  function simpleAcquisitionPayload(choice, level, rule = {}) {
    if (choice === 'recommended') return acquisitionRecommendationPayload(rule);
    if (choice === 'adaptive') return {...presetPayload('off', 'off', rule), baseline_ocr_mode:'auto'};
    if (choice === 'text_only') return {...presetPayload('off', 'off', rule), baseline_ocr_mode:'text_only'};
    if (choice === 'local_ocr') return {...presetPayload('off', 'off', rule), baseline_ocr_mode:'local_ocr'};
    return {...presetPayload(choice === 'always' ? 'always' : 'smart', ['low', 'standard', 'high'].includes(level) ? level : 'standard', rule), baseline_ocr_mode:'auto'};
  }
  function compiledSubRequirements(rule = {}) {
    return Array.isArray(rule?.compiled_child_requirements)
      ? rule.compiled_child_requirements.filter((item) => item && typeof item === 'object')
      : [];
  }
  function compiledRuleSummary(rule = {}) {
    const children = compiledSubRequirements(rule);
    if (children.length < 2) return '';
    const visible = children.slice(0, 24);
    const items = visible.map((item, index) => {
      const title = String(item.title || item.verification_target || item.check_rule || `子检查项 ${index + 1}`).trim();
      const detail = String(item.check_rule || item.verification_target || '').trim();
      const page = Number(item.source_page) > 0 ? `（第 ${Number(item.source_page)} 页）` : '';
      return `<li><strong>${escapeHtml(title)}</strong>${page}${detail && detail !== title ? `<span>${escapeHtml(detail)}</span>` : ''}</li>`;
    }).join('');
    const remaining = children.length - visible.length;
    return `<div class="rule-compiled-summary"><div><strong>已汇总 ${children.length} 项子检查</strong><span>页面合并展示；综合评审仍逐项核验。</span></div><details><summary>查看子检查项</summary><ol>${items}</ol>${remaining > 0 ? `<p class="muted">另有 ${remaining} 项，完整要求可在下方“合并后的完整检查规则”中查看。</p>` : ''}</details></div>`;
  }
  function compiledRuleTextContent(rule, field, isDraft) {
    const value = String(rule?.[field] || (field === 'check_rule' ? rule?.title || '' : '未提供')).trim();
    const children = compiledSubRequirements(rule);
    if (children.length < 2) {
      return isDraft && field === 'check_rule'
        ? `<textarea class="rule-check-rule" data-rule="${rule.rule_id}" rows="4">${escapeHtml(value)}</textarea>`
        : `<div class="rule-text">${escapeHtml(value || '未提供')}</div>`;
    }
    const label = field === 'check_rule' ? '合并后的完整检查规则' : '合并后的完整招标原文依据';
    const body = isDraft && field === 'check_rule'
      ? `<textarea class="rule-check-rule" data-rule="${rule.rule_id}" rows="6">${escapeHtml(value)}</textarea>`
      : `<div class="rule-text">${escapeHtml(value || '未提供')}</div>`;
    return `<details class="rule-compiled-fulltext"><summary>${label}</summary>${body}</details>`;
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
    const set = data.rule_set; currentRuleSet = set || null; const isDraft = set?.status === 'draft'; const isConfirmed = set?.status === 'confirmed'; hasCurrentRules = data.rules.length > 0;
    const acquisitionIssuesByRule = new Map();
    for (const issue of Array.isArray(validation?.issues) ? validation.issues : []) {
      const ids = Array.isArray(issue?.rule_ids) && issue.rule_ids.length ? issue.rule_ids : (issue?.rule_id ? [issue.rule_id] : []);
      for (const rid of ids) {
        const values = acquisitionIssuesByRule.get(rid) || [];
        values.push(issue);
        acquisitionIssuesByRule.set(rid, values);
      }
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
    const acquisitionCounts = displayRules.reduce((counts, rule) => { const key = simpleAcquisitionMode(rule); counts[key] = (counts[key] || 0) + 1; return counts; }, {recommended:0, adaptive:0, text_only:0, local_ocr:0, smart:0, always:0, custom:0});
    const toolbar = isDraft && displayRules.length ? `<div class="rule-acquisition-toolbar"><div><strong>核验方式</strong><span class="muted"> 默认按 AI 对证据类型的判断执行；也可明确限定为纯文字、本地 OCR，或追加腾讯 OCR／多模态增强。</span><small>当前：AI 建议 ${acquisitionCounts.recommended || 0} 条 · 文字优先 ${acquisitionCounts.adaptive || 0} 条 · 纯文字 ${acquisitionCounts.text_only || 0} 条 · 本地 OCR ${acquisitionCounts.local_ocr || 0} 条 · 智能增强 ${acquisitionCounts.smart || 0} 条 · 强制增强 ${acquisitionCounts.always || 0} 条${acquisitionCounts.custom ? ` · 专家自定义 ${acquisitionCounts.custom} 条` : ''}</small><small>${escapeHtml(acquisitionCapabilityNote())}</small></div><div class="rule-acquisition-toolbar-actions"><button class="restore-acquisition-recommendations" type="button">采用 AI 建议（高计费）</button><button class="restore-low-cost-acquisition" type="button">恢复默认（低成本）</button><details class="rule-acquisition-help"><summary>如何选择？</summary><div class="rule-acquisition-help-popover"><p><strong>恢复默认（低成本）：</strong>恢复为规则刚提取后的基础路径，只审全文；证据不足时最多补本地 OCR，不调用腾讯 OCR 或多模态。<strong>采用 AI 建议（高计费）：</strong>按每条规则的证据类型自动启用必要的腾讯 OCR／多模态增强，可能产生按量费用。<strong>纯文字核验：</strong>只审已解析全文，最快。<strong>文字优先：</strong>先审全文，不足才补本地 OCR（免费）。<strong>本地 OCR 核验：</strong>全文之外固定补扫关键页（免费）。</p><p>快速／标准／充分只控制增强核验的页数上限；本地 OCR 仍按有限候选页和缓存执行。</p></div></details></div></div>` : '';
    $('rules').innerHTML = displayRules.length ? `${toolbar}<div class="rule-card-list">${displayRules.map((r) => {
      const checkContent = compiledRuleTextContent(r, 'check_rule', isDraft);
      const sourceContent = compiledRuleTextContent(r, 'source_text', false);
      const compilationSummary = compiledRuleSummary(r);
      const summary = acquisitionSummary(r);
      const sourceLabel = r.source_type === 'ai' ? 'AI 提取' : r.source_type === 'global' ? '通用规则库' : ['ai_edited', 'ai_locked'].includes(r.source_type) ? 'AI 提取 · 人工修改' : '人工补充';
      const enabledControl = (isDraft || isConfirmed) ? `<label class="rule-enabled-control" title="停用只影响后续评审执行，历史结果保留；启停状态会随下次重新提取继承"><input class="rule-enabled" data-rule="${r.rule_id}" type="checkbox" ${r.enabled ? 'checked' : ''}><span>启用</span></label>` : (r.enabled ? '' : '<span class="tag rule-disabled">未启用</span>');
      const recommendation = r.acquisition_recommendation || {acquisition_preset:'off', vision_level:'off'};
      const simpleChoice = simpleAcquisitionMode(r);
      const selectionLevel = ['low', 'standard', 'high'].includes(r.vision_level) ? r.vision_level : 'standard';
      const isCustomAcquisition = simpleChoice === 'custom';
      const basePath = ['recommended', 'adaptive', 'text_only', 'local_ocr'].includes(simpleChoice) ? `<p class="hint rule-base-verification"><strong>当前基础路径：</strong>${escapeHtml(baseVerificationSummary(r).label)}。${escapeHtml(baseVerificationSummary(r).detail)}</p>` : '';
      const strengthControl = ['smart', 'always'].includes(simpleChoice) ? `<label>取证强度<select class="rule-simple-coverage" data-rule="${r.rule_id}"><option value="low" ${selectionLevel === 'low' ? 'selected' : ''}>快速：单页材料或快速抽查</option><option value="standard" ${selectionLevel === 'standard' ? 'selected' : ''}>标准（推荐）：覆盖常见材料与必要补页</option><option value="high" ${selectionLevel === 'high' ? 'selected' : ''}>充分：材料分散、页数较多或风险较高</option></select></label>` : '';
      const choiceControl = isCustomAcquisition ? `<div class="rule-acquisition-custom-state"><strong>专家自定义生效中</strong><button class="open-rule-expert" data-rule="${r.rule_id}" type="button">查看/调整专家模式</button></div>` : `<label>核验方式<select class="rule-simple-acquisition" data-rule="${r.rule_id}"><option value="recommended" ${simpleChoice === 'recommended' ? 'selected' : ''}>按 AI 建议（默认）</option><option value="adaptive" ${simpleChoice === 'adaptive' ? 'selected' : ''}>文字优先（不足时补本地 OCR · 免费）</option><option value="text_only" ${simpleChoice === 'text_only' ? 'selected' : ''}>纯文字核验（只看全文 · 最快）</option><option value="local_ocr" ${simpleChoice === 'local_ocr' ? 'selected' : ''}>本地 OCR 核验（固定补扫关键页 · 免费）</option><option value="smart" ${simpleChoice === 'smart' ? 'selected' : ''}>智能增强核验（不足才调腾讯/多模态 · 计费）</option><option value="always" ${simpleChoice === 'always' ? 'selected' : ''}>强制增强核验（固定调腾讯/多模态 · 计费）</option></select></label>`;
      const recommendationPayload = acquisitionRecommendationPayload(r);
      const matchesRecommendation = ['image_mode', 'vision_trigger', 'vision_level', 'baseline_ocr_mode'].every((key) => String(r[key] || (key === 'baseline_ocr_mode' ? 'auto' : '')) === String(recommendationPayload[key] || ''));
      const ruleIssues = acquisitionIssuesByRule.get(r.rule_id) || [];
      const acquisitionWarning = ruleIssues.length ? `<div class="rule-acquisition-warning"><strong>当前设置提示：</strong>${escapeHtml(ruleIssues.map((issue) => issue.message).join('；'))}</div>` : '';
      const duplicateIssue = ruleIssues.find((issue) => ['duplicate_score_rule', 'duplicate_review_rule'].includes(issue.code));
      const duplicateWarning = duplicateIssue ? `<div class="rule-duplicate-warning"><strong>疑似重复评分规则</strong><span>${escapeHtml(duplicateIssue.message)}${isConfirmed ? ' 停用只影响后续评审执行，历史结果保留。' : ''}</span>${(isDraft || isConfirmed) ? `<button class="keep-rule-only" data-rule="${r.rule_id}" data-group="${escapeHtml((duplicateIssue.rule_ids || []).join(','))}" type="button">保留此条并停用同组其他</button>` : ''}</div>` : '';
      const recommendationLabel = `${baseVerificationSummary({...r, baseline_ocr_mode:recommendation.baseline_ocr_mode || 'auto'}).label}${recommendation.acquisition_preset !== 'off' ? ` · ${acquisitionPresetLabels[recommendation.acquisition_preset] || '智能增强'} · ${acquisitionLevelLabels[recommendation.vision_level] || '标准'}强度` : ''}`;
      const visionReadonly = isDraft ? '' : `<div class="rule-vision-readonly"><span class="rule-field-label">核验方式</span><span>${escapeHtml(acquisitionSummary(r).label)}</span>${matchesRecommendation ? '<small class="ai-rec ai-rec-match">（与 AI 建议一致）</small>' : `<span class="ai-rec-current">AI 建议：${escapeHtml(recommendationLabel)}</span>`}</div>`;
      const recCell = matchesRecommendation ? `<span class="ai-rec-head ai-rec-head-match" title="当前核验方式与 AI 建议一致">${escapeHtml(recommendationLabel)}（当前）</span>` : `<span class="ai-rec-head" title="AI 建议的核验方式（当前未采用）">AI 建议：${escapeHtml(recommendationLabel)}</span><span class="ai-rec-current">当前：${escapeHtml(summary.label)}</span>`;
      const visionControl = isDraft ? `<div class="rule-vision-controls"><div class="rule-vision-heading"><strong>核验方式</strong><small>默认按 AI 建议；纯文字与本地 OCR 是基础路径，智能／强制增强才会追加腾讯 OCR 或多模态。</small></div>${choiceControl}${strengthControl}${basePath}<div class="rule-acquisition-preview"><strong>执行预览</strong><span>${escapeHtml(acquisitionPreview(r))}</span></div>${acquisitionWarning}<div class="rule-acquisition-actions"><button class="restore-rule-acquisition" data-rule="${r.rule_id}" type="button" ${matchesRecommendation ? 'disabled title="当前已采用 AI 建议"' : ''}>采用 AI 建议</button><small class="${matchesRecommendation ? 'ai-rec ai-rec-match' : 'ai-rec'}">AI 建议：${escapeHtml(baseVerificationSummary({...r, baseline_ocr_mode:recommendation.baseline_ocr_mode || 'auto'}).label)}${recommendation.acquisition_preset !== 'off' ? ` · ${escapeHtml(acquisitionPresetLabels[recommendation.acquisition_preset] || '智能增强')} · ${escapeHtml(acquisitionLevelLabels[recommendation.vision_level] || '标准')}强度` : ''}${matchesRecommendation ? '（当前）' : ''}</small>${matchesRecommendation ? '' : `<span class="ai-rec-current">当前：${escapeHtml(acquisitionSummary(r).label)}</span>`}</div><details class="rule-image-advanced"><summary>专家模式：增强通道与启动方式</summary><p class="hint">一般无需修改。基础核验方式请在上方选择；这里只限定腾讯精确复核、多模态外观核验或双通道。</p><div class="rule-image-advanced-grid"><label>增强通道<select class="rule-image-mode" data-rule="${r.rule_id}"><option value="auto" ${r.image_mode === 'auto' ? 'selected' : ''}>系统自动选择（推荐）</option><option value="ocr_only" ${r.image_mode === 'ocr_only' ? 'selected' : ''}>腾讯 OCR：精确字段复核</option><option value="vision_only" ${r.image_mode === 'vision_only' ? 'selected' : ''}>多模态：签章、外观等</option><option value="combined" ${r.image_mode === 'combined' ? 'selected' : ''}>腾讯 OCR＋多模态：双重复核</option><option value="off" ${r.image_mode === 'off' ? 'selected' : ''}>不作增强核验</option></select></label><label>启动方式<select class="rule-vision-trigger" data-rule="${r.rule_id}"><option value="off" ${r.vision_trigger === 'off' ? 'selected' : ''}>不追加增强</option><option value="text_fallback" ${r.vision_trigger === 'text_fallback' ? 'selected' : ''}>基础证据不足时追加增强</option><option value="required" ${r.vision_trigger === 'required' ? 'selected' : ''}>强制追加增强（无论基础证据是否充分）</option></select></label></div></details></div>` : '';
      return `<details class="rule-card${duplicateIssue ? ' rule-card-duplicate' : ''}"><summary><span class="rule-card-summary">${enabledControl}<span class="tag">${categoryLabel(r.category)}</span><strong class="rule-card-title">${escapeHtml(r.title)}</strong><span class="tag">${sourceLabel}</span>${recCell}</span></summary><div class="rule-card-body">${duplicateWarning}${compilationSummary}<div class="rule-card-grid"><label>检查规则${checkContent}</label><div class="rule-field"><span class="rule-field-label">招标原文依据</span>${sourceContent}</div></div>${visionControl}${visionReadonly}${isDraft ? `<div class="actions rule-card-actions"><button class="save-check-rule primary" data-rule="${r.rule_id}">保存检查规则</button></div>` : ''}</div></details>`;
    }).join('')}</div>` : '<p class="muted">暂无规则。</p>';
    $('rules').querySelectorAll('details.rule-card').forEach((card, index) => {
      const ruleId = displayRules[index]?.rule_id;
      card.dataset.rule = ruleId || '';
      card.open = Boolean(ruleId && expandedRuleIds.has(ruleId));
    });
    $('rules').querySelectorAll('.rule-enabled').forEach((input) => { input.onclick = (event) => event.stopPropagation(); input.onchange = async () => { try { await request(`/projects/${activeProject}/rules/${input.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:input.checked})}); await refreshRules(); await refreshPriceSheet(false, true); } catch (error) { alert(error.message); await refreshRules(); } }; });
    const ruleById = new Map(displayRules.map((rule) => [rule.rule_id, rule]));
    const saveAcquisition = async (ruleId, payload) => request(`/projects/${activeProject}/rules/${ruleId}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    $('rules').querySelectorAll('.rule-simple-acquisition').forEach((input) => { input.onchange = async () => { const rule = ruleById.get(input.dataset.rule); try { const level = input.closest('.rule-vision-controls')?.querySelector('.rule-simple-coverage')?.value || 'standard'; await saveAcquisition(input.dataset.rule, simpleAcquisitionPayload(input.value, level, rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.rule-simple-coverage').forEach((input) => { input.onchange = async () => { try { const control = input.closest('.rule-vision-controls'); const choice = control?.querySelector(`.rule-simple-acquisition[data-rule="${input.dataset.rule}"]`)?.value || 'smart'; const rule = ruleById.get(input.dataset.rule); await saveAcquisition(input.dataset.rule, simpleAcquisitionPayload(choice, input.value, rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.rule-image-mode, .rule-vision-trigger').forEach((input) => { input.onchange = async () => { try { const control = input.closest('.rule-vision-controls'); const level = control?.querySelector(`.rule-simple-coverage[data-rule="${input.dataset.rule}"]`)?.value || 'standard'; const mode = control?.querySelector(`.rule-image-mode[data-rule="${input.dataset.rule}"]`)?.value || 'auto'; const trigger = control?.querySelector(`.rule-vision-trigger[data-rule="${input.dataset.rule}"]`)?.value || 'off'; await saveAcquisition(input.dataset.rule, {acquisition_preset:'custom', image_mode:mode, vision_trigger:trigger, vision_level:(mode === 'off' || trigger === 'off') ? 'off' : level}); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } }; });
    $('rules').querySelectorAll('.open-rule-expert').forEach((button) => button.onclick = () => { const advanced = button.closest('.rule-vision-controls')?.querySelector('.rule-image-advanced'); if (advanced) { advanced.open = true; advanced.scrollIntoView({block:'nearest', behavior:'smooth'}); } });
    $('rules').querySelectorAll('.restore-rule-acquisition').forEach((button) => button.onclick = async () => { try { const rule = ruleById.get(button.dataset.rule); await saveAcquisition(button.dataset.rule, acquisitionRecommendationPayload(rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } });
    $('rules').querySelectorAll('.restore-acquisition-recommendations').forEach((button) => button.onclick = async () => { const candidates = displayRules.filter((rule) => rule.enabled && rule.acquisition_recommendation); if (!candidates.length || !confirm(`将为 ${candidates.length} 条已启用规则恢复 AI 建议；已有自定义取证设置会被替换。是否继续？`)) return; button.disabled = true; try { for (const rule of candidates) await saveAcquisition(rule.rule_id, acquisitionRecommendationPayload(rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } finally { button.disabled = false; } });
    $('rules').querySelectorAll('.restore-low-cost-acquisition').forEach((button) => button.onclick = async () => { const candidates = displayRules.filter((rule) => rule.enabled); if (!candidates.length || !confirm(`将为 ${candidates.length} 条已启用规则恢复低成本默认设置：只审全文，必要时最多补本地 OCR；不会调用腾讯 OCR 或多模态。是否继续？`)) return; button.disabled = true; try { for (const rule of candidates) await saveAcquisition(rule.rule_id, simpleAcquisitionPayload('adaptive', 'standard', rule)); await refreshRules(); } catch (error) { alert(error.message); await refreshRules(); } finally { button.disabled = false; } });
    $('rules').querySelectorAll('.save-check-rule').forEach((button) => button.onclick = async () => { try { const input = $('rules').querySelector(`.rule-check-rule[data-rule="${button.dataset.rule}"]`); await request(`/projects/${activeProject}/rules/${button.dataset.rule}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({check_rule:input.value})}); await refreshRules(); await refreshPriceSheet(false, true); } catch (error) { alert(error.message); } });
    $('rules').querySelectorAll('.keep-rule-only').forEach((button) => button.onclick = async (event) => {
      event.stopPropagation();
      const ruleId = button.dataset.rule;
      const group = (button.dataset.group || '').split(',').filter(Boolean);
      const others = group.filter((id) => id !== ruleId);
      if (!confirm(`将保留当前规则，并停用同组 ${others.length} 条疑似重复的评分规则（可随时重新启用）。是否继续？`)) return;
      button.disabled = true;
      try {
        await request(`/projects/${activeProject}/rules/${ruleId}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:true})});
        for (const id of others) {
          await request(`/projects/${activeProject}/rules/${id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:false})});
        }
        await refreshRules();
        await refreshPriceSheet(false, true);
      } catch (error) { alert(error.message); await refreshRules(); } finally { button.disabled = false; }
    });
  }
  function groupByBidder(results) { const groups = new Map(); results.forEach((item) => { const bidder = item.bidder_name || item.original_name || '未填写投标人'; if (!groups.has(bidder)) groups.set(bidder, []); groups.get(bidder).push(item); }); return [...groups.entries()]; }
  function statusLabel(status) { return ({satisfied:'满足', not_satisfied:'不满足', partial:'部分符合，需复核', not_found:'未找到，需核实', manual:'待人工复核', ocr_required:'需识别扫描件'})[status] || status || '-'; }
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
    const pages = sortedPageList(result?.vision_pages).map((page) => `P${page}`).join('、');
    const evidencePages = sortedPageList(result?.vision_evidence_pages).map((page) => `P${page}`).join('、');
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
  function conflictBadgeHtml(result) {
    const status = String(result?.vision_status || '');
    const ocrStatus = String(result?.ocr_status || (status.startsWith('ocr_') ? status : ''));
    const multimodal = String(result?.multimodal_status || (status.startsWith('ocr_') ? 'not_requested' : status));
    if (![ocrStatus, multimodal, status].some((item) => /conflict/.test(item))) return '';
    return '<strong class="conflict-banner">文字与图片不一致，需复核</strong>';
  }
  function evidenceChainHtml(result) {
    const layers = Array.isArray(result?.evidence_layers) ? result.evidence_layers.filter((item) => item && typeof item === 'object' && item.summary) : [];
    const statusMessage = String(result?.vision_message || '').trim();
    if (!layers.length && !statusMessage) return '';
    const labels = {text:'文字解析', tencent_ocr:'腾讯 OCR', local_ocr:'本地 RapidOCR', vision:'图片识别', score_calculation:'计分过程'};
    const statusNote = statusMessage ? `<div class="evidence-layer"><strong>识别状态</strong><span>${escapeHtml(cleanDisplayText(statusMessage))}</span></div>` : '';
    return `<details class="evidence-chain"><summary>证据链详情</summary>${statusNote}${layers.map((layer, index) => {
      const checked = sortedPageList(layer.checked_pages).map((page) => `P${page}`).join('、');
      const evidence = sortedPageList(layer.evidence_pages).map((page) => `P${page}`).join('、');
      const meta = [layer.service || layer.model || '', checked ? `检查页：${checked}` : '', evidence ? `证据页：${evidence}` : ''].filter(Boolean).join(' · ');
      const updated = layers.length > 1 && index < layers.length - 1 ? '<small class="layer-updated">已被后续核验更新</small>' : '';
      const summary = cleanDisplayText(layer.summary);
      const body = summary.length > 160
        // 与文字结论一致：展开后预览隐藏，完整内容连贯显示。
        ? `<details class="layer-block"><summary><span class="layer-preview">${escapeHtml(summary.slice(0, 160))}…</span><span class="layer-full">${escapeHtml(summary)}</span></summary></details>`
        : `<div class="layer-block layer-block-plain"><span>${escapeHtml(summary)}</span></div>`;
      return `<div class="evidence-layer"><strong>${escapeHtml(labels[layer.source] || '补充证据')}</strong>${meta ? `<small>${escapeHtml(meta)}</small>` : ''}${updated}${body}</div>`;
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
  function ruleSetVersionNotice(run) {
    if (!run?.rule_set_id || !currentRuleSet?.rule_set_id || run.rule_set_id === currentRuleSet.rule_set_id) return '';
    const version = currentRuleSet.version ? ` v${currentRuleSet.version}` : '';
    return `<p class="hint"><strong>版本提示：</strong>以下结果基于此前已确认的规则集；当前页面已是待确认规则集${escapeHtml(version)}。确认新规则后请重新运行综合评审，避免将两版规则的结果混作比较。</p>`;
  }
  function partialResultNotice(run) { const versionNotice = ruleSetVersionNotice(run); if (run?.task_status === 'running') return `${versionNotice}<p class="hint">综合评审仍在运行，以下仅展示已完整完成投标人的 AI 建议。</p>`; return `${versionNotice}${run?.task_status === 'error' ? `<p class="hint">本次综合评审未全部完成，以下为已成功保存的部分 AI 建议（进度 ${run.task_progress ?? 0}%）：${escapeHtml(run.task_error || '请修正模型配置后重新运行。')}</p>` : ''}`; }
  function visibleCompletedResults(run, results) { if (run?.task_status !== 'running') return results; const completed = new Set(run.completed_document_ids || []); return results.filter((item) => completed.has(item.document_id)); }
  function renderEvaluationHighlights(summaries) {
    const values = (summaries || []).filter((summary) => Array.isArray(summary.highlights) && summary.highlights.length);
    $('evaluation-highlights-panel').classList.toggle('hidden', !values.length);
    cachedHighlights = values;
    if (!values.length) return;
    const counts = {critical:0, high:0, attention:0};
    values.forEach((summary) => (summary.highlights || []).forEach((item) => { if (counts[item.level] != null) counts[item.level] += 1; }));
    const total = values.reduce((sum, summary) => sum + (summary.highlights || []).length, 0);
    const stats = [];
    if (counts.critical) stats.push(`${counts.critical} 条高风险`);
    if (counts.high) stats.push(`${counts.high} 条重点关注`);
    if (counts.attention) stats.push(`${counts.attention} 条关注项`);
    const highOnly = $('highlights-high-only')?.classList.contains('active');
    const toggleBtn = $('highlights-high-only');
    if (toggleBtn) toggleBtn.textContent = highOnly ? '显示全部' : '只看高风险';
    const statsNode = $('evaluation-highlights-stats');
    if (statsNode) {
      statsNode.textContent = highOnly
        ? `只看高风险：共 ${counts.critical + counts.high} 条（${counts.critical} 条高风险、${counts.high} 条重点关注）。`
        : `共 ${total} 条重要结论：${stats.join('、') || '无'}。按风险从高到低排列，可切换只看高风险。`;
    }
    let ordered = [...values].sort((a, b) => (highlightLevelRank[b.overall_level] || 0) - (highlightLevelRank[a.overall_level] || 0));
    if (highOnly) {
      ordered = ordered.filter((summary) => (summary.highlights || []).some((item) => item.level === 'critical' || item.level === 'high'));
    }
    $('evaluation-highlights').innerHTML = ordered.map((summary) => {
      const items = summary.highlights || [];
      const criticalHigh = items.filter((item) => item.level === 'critical' || item.level === 'high');
      const attention = items.filter((item) => item.level === 'attention');
      // 不超过 3 条时全部平铺显示；超过 3 条才按重要程度折叠：主列表展开
      // critical/high，attention 收进“另有 N 条关注项”，避免重复也避免面板过载。
      const showAll = !highOnly && items.length <= 3;
      const visible = showAll ? items : criticalHigh;
      const listHtml = (list) => list.map((item) => {
        const basis = cleanDisplayText(item.basis);
        return `<li class="evaluation-highlight-${escapeHtml(item.level || 'attention')}"><strong>${escapeHtml(item.keyword)}</strong><span>${escapeHtml(cleanDisplayText(item.conclusion))}</span>${basis ? `<small>${escapeHtml(basis)}</small>` : ''}</li>`;
      }).join('');
      const headline = cleanDisplayText(summary.headline);
      const attentionBlock = (!highOnly && items.length > 3 && attention.length)
        ? `<details class="evaluation-highlight-more"><summary>另有 ${attention.length} 条关注项</summary><ul>${listHtml(attention)}</ul></details>` : '';
      return `<section class="evaluation-highlight-group"><h4>${escapeHtml(summary.bidder_name || '未命名投标人')}</h4>${headline ? `<p>${escapeHtml(headline.length > 60 ? `${headline.slice(0, 60)}…` : headline)}</p>` : ''}<ul>${listHtml(visible)}</ul>${attentionBlock}</section>`;
    }).join('') || '<p class="muted">当前筛选下没有高风险或重点关注事项。</p>';
  }
  async function refreshReview() { if (!activeProject) return; const data = await request(`/projects/${activeProject}/review-results`); renderEvaluationHighlights(data.review_run?.highlights || []); const groups = groupByBidder(visibleCompletedResults(data.review_run, data.results)); $('review-results').innerHTML = groups.length ? `${partialResultNotice(data.review_run)}<p class="hint">以下为 AI 基于电子文件生成的审查建议；主表展示结论摘要，完整文字和取证过程可展开查看。</p>${groups.map(([bidder, results]) => { const ordered = [...results].sort((left, right) => { const leftOcr = left.status === 'ocr_required' ? 1 : 0; const rightOcr = right.status === 'ocr_required' ? 1 : 0; return leftOcr - rightOcr || riskRank(right.risk_level) - riskRank(left.risk_level); }); return `<details class="result-group"><summary>投标人：${escapeHtml(bidder)}（${ordered.length} 项）</summary><div class="review-result-table-wrap"><table class="review-result-table"><colgroup><col class="review-col-category"><col class="review-col-rule"><col class="review-col-advice"><col class="review-col-risk"><col class="review-col-evidence"></colgroup><thead><tr><th>分类</th><th>检查规则</th><th>AI建议</th><th>风险</th><th>关键证据与理由</th></tr></thead><tbody>${ordered.map((r) => `<tr><td><span class="tag">${categoryLabel(r.category)}</span></td><td>${ruleCellHtml(r)}</td><td>${escapeHtml(statusLabel(r.status))}<br><small>置信度：${escapeHtml(confidenceLabel(r.confidence))}；证据：${escapeHtml(evidenceQualityLabel(r.evidence_quality))}</small>${visionBadgeHtml(r)}</td><td>${escapeHtml(riskLabel(r.status === 'ocr_required' ? 'low' : r.risk_level))}</td><td><div class="result-evidence result-summary">${escapeHtml(resultExplanation(conciseResultSummary(r), r) || '-')}</div><small class="result-evidence result-verification">${escapeHtml(verificationLineHtml(r))}</small>${conflictBadgeHtml(r)}${evidenceChainHtml(r)}${rawResultDetailHtml(r)}</td></tr>`).join('')}</tbody></table></div></details>`; }).join('')}` : `<p class="muted">${data.review_run ? '正在生成审查结果。' : '本项目没有审查规则。'}</p>`; }
  async function refreshScores() { for (const type of ['objective','subjective']) { const data = await request(`/projects/${activeProject}/score-results/${type}`); const target = $(`${type}-results`); const visible = (data.results || []).filter((item) => !item.price_managed_by_sheet); const groups = groupByBidder(visibleCompletedResults(data.score_run, visible)); const priceNotice = type === 'objective' && (data.results || []).some((item) => item.price_managed_by_sheet) ? '<p class="hint">价格分已移至“报价与价格分”，按确认报价、价格调整和参与范围统一计算。</p>' : ''; target.innerHTML = groups.length ? `${partialResultNotice(data.score_run)}${priceNotice}<p class="hint">以下为 AI 基于电子文件生成的评分建议；扫描件证据未覆盖时不会将“未找到”误作 0 分，主表会提示待 OCR 后评分。</p>${groups.map(([bidder, results]) => `<details class="result-group"><summary>投标人：${escapeHtml(bidder)}（${results.length} 项）</summary><table><thead><tr><th>检查规则</th><th>AI 建议得分</th><th>满分</th><th>置信度</th><th>关键证据与理由</th></tr></thead><tbody>${results.map((r) => `<tr><td>${ruleCellHtml(r)}${scoreOcrHint(r)}</td><td>${escapeHtml(scoreSuggestionLabel(r))}</td><td>${r.max_score ?? '-'}</td><td>${escapeHtml(confidenceLabel(r.confidence))}${visionBadgeHtml(r)}</td><td><div class="result-evidence result-summary">${escapeHtml(resultExplanation(conciseResultSummary(r), r) || '-')}</div><small class="result-evidence result-verification">${escapeHtml(verificationLineHtml(r))}</small>${conflictBadgeHtml(r)}${evidenceChainHtml(r)}${rawResultDetailHtml(r)}</td></tr>`).join('')}</tbody></table></details>`).join('')}` : `<p class="muted">${priceNotice || (data.score_run ? '正在生成评分结果。' : `本项目没有${type === 'objective' ? '客观分' : '主观分'}规则。`)}</p>`; } }
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
  $('upload-file').onclick = async () => { const file = selectedUploadFile || $('file-input').files[0]; const role = $('file-role').value; const bidderName = $('bidder-name').value.trim(); if (!file) return alert('请选择或拖入文件'); if (role === 'bid' && !bidderName) { $('bidder-name').focus(); return alert('上传投标文件时必须填写投标人名称'); } const form = new FormData(); form.append('file', file); form.append('role', role); form.append('bidder_name', bidderName); const button = $('upload-file'); button.disabled = true; try { await uploadFileWithProgress(form, file); updateUploadProgress(100, '上传完成，正在刷新文件清单…', 'success'); clearSelectedUploadFile(); $('bidder-name').value = ''; await refreshProject(); await refreshPriceSheet(false, true); } catch (error) { updateUploadProgress(0, `上传失败：${error.message}`, 'error'); alert(error.message); } finally { button.disabled = false; } };
  $('parse-documents').onclick = () => queue('parse_documents'); $('start-compare').onclick = () => queue('compare_documents', {profile_id:$('compare-profile').value, force_rerun:true});
  $('extract-price-rules').onclick = () => queue('extract_price_rules', {profile_id:$('price-profile').value, force_rerun:true});
  const highlightToggle = $('highlights-high-only');
  if (highlightToggle) highlightToggle.onclick = () => { highlightToggle.classList.toggle('active'); renderEvaluationHighlights(cachedHighlights); };
  $('extract-rules').onclick = async () => { if (hasCurrentRules && !confirm('重新提取会按当前招标文件重新生成一套全新 AI 规则，并重新导入通用规则；人工补充规则会保留，上一轮 AI 规则、勾选、取证设置及综合评审结果将不再保留。是否继续？')) return; try { const profile_id = $('rule-profile').value; await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:'extract_rules', profile_id, force_rerun:true})}); await refreshProject(); } catch (error) { alert(error.message); } };
  function updateManualRuleScoringFields() { const category = $('manual-rule-category').value; const isScoring = ['objective', 'subjective'].includes(category); $('manual-rule-max-score-field').classList.toggle('hidden', !isScoring); $('manual-rule-max-score').required = isScoring; $('manual-rule-score-kind-field').classList.toggle('hidden', category !== 'objective'); }
  $('manual-rule-category').onchange = updateManualRuleScoringFields;
  updateManualRuleScoringFields();
  $('add-manual-rule').onclick = async () => { try { const category = $('manual-rule-category').value; const isScoring = ['objective', 'subjective'].includes(category); const rawMaxScore = $('manual-rule-max-score').value; const maxScore = Number(rawMaxScore); if (isScoring && (!Number.isFinite(maxScore) || maxScore <= 0)) { alert('客观分和主观分规则必须填写大于 0 的满分。'); return; } const payload = {category, title:$('manual-rule-title').value, check_rule:$('manual-rule-check').value, source_text:$('manual-rule-source').value, ocr_required:$('manual-rule-ocr').checked}; if (isScoring) payload.scoring = {max_score:maxScore, kind:category === 'objective' ? $('manual-rule-score-kind').value : 'manual'}; await request(`/projects/${activeProject}/rules`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); $('manual-rule-title').value = ''; $('manual-rule-check').value = ''; $('manual-rule-source').value = ''; $('manual-rule-max-score').value = ''; $('manual-rule-ocr').checked = false; updateManualRuleScoringFields(); await refreshRules(); await refreshPriceSheet(false, true); } catch (error) { alert(error.message); } };
  $('confirm-rules').onclick = async () => { try { const validation = await request(`/projects/${activeProject}/rules/acquisition-validation`); const issues = Array.isArray(validation.issues) ? validation.issues : []; if (issues.length) { const brief = issues.slice(0, 6).map((item) => `• ${item.title}：${item.message}`).join('\n'); const extra = issues.length > 6 ? `\n另有 ${issues.length - 6} 条提示。` : ''; if (!confirm(`增强核验预检发现 ${issues.length} 条提示：\n${brief}${extra}\n\n仍要确认当前规则集吗？`)) return; } await request(`/projects/${activeProject}/rules/confirm`, {method:'POST'}); await refreshRules(); await refreshPriceSheet(false, true); } catch (error) { alert(error.message); } };
  $('start-evaluate-all').onclick = async () => { try { const profile_id = $('all-profile').value; const rulesData = await request(`/projects/${activeProject}/rules`); const visionRules = rulesData.rules.filter((rule) => rule.enabled && rule.vision_trigger !== 'off' && rule.vision_level !== 'off' && !['ocr_only', 'off'].includes(rule.image_mode || 'auto')); if (visionConfiguration.enabled && visionRules.length) { const selected = modelProfiles.find((item) => item.profile_id === profile_id); const fallback = modelProfiles.find((item) => item.profile_id === visionConfiguration.default_profile_id && item.enabled && item.capabilities?.vision); if (!selected?.capabilities?.vision && fallback && !confirm(`当前评审模型“${selected?.display_name || '所选模型'}”不是多模态模型；仅需要图片外观核验的规则将改用“${fallback.display_name}”，文字评审与 OCR 仍使用当前模型。是否继续？`)) return; } const projectData = await request(`/projects/${activeProject}`); const lastSuccessfulEvaluation = (projectData.tasks || []).find((task) => task.task_type === 'evaluate_all' && task.status === 'success'); if (lastSuccessfulEvaluation && !confirm('该项目已完成过综合评审。重新评审将从当前文件、规则和模型完整开始，旧综合评审结果不会保留或复用。是否继续？')) return; await request(`/projects/${activeProject}/tasks`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({task_type:'evaluate_all', profile_id, calculate_price:true, ...(lastSuccessfulEvaluation ? {force_rerun:true} : {})})}); await refreshProject(); await Promise.all([refreshReview(), refreshScores(), refreshUsage()]); } catch (error) { alert(error.message); } };
  function closeModels() { $('models-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); resetModelForm(); }
  function closePrompts() { $('prompts-panel').classList.add('hidden'); document.body.classList.remove('modal-open'); }
  function syncGlobalRuleAcquisitionControls() { const choice = $('global-rule-acquisition').value; const active = ['smart', 'always'].includes(choice); $('global-rule-coverage-field').classList.toggle('is-hidden', !active); $('global-rule-ocr').disabled = choice === 'text_only'; if (choice === 'text_only') $('global-rule-ocr').checked = false; }
  function resetGlobalRuleForm() { $('global-rule-id').value = ''; $('global-rule-form-title').textContent = '新增通用规则'; $('global-rule-category').value = 'substantive'; $('global-rule-title').value = ''; $('global-rule-check').value = ''; $('global-rule-source').value = ''; $('global-rule-acquisition').value = 'recommended'; $('global-rule-coverage').value = 'standard'; $('global-rule-ocr').checked = false; $('global-rule-enabled').checked = true; syncGlobalRuleAcquisitionControls(); }
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
  $('add-price-entry').onclick = () => {
    if (!priceDraft) return;
    const key = `new-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    priceDraft.entries.push({draft_id:key, price_entry_id:'', bidder_name:'', source_type:'manual', extracted_quote:null, manual_quote:'', evaluation_price:'', effective_quote:null, calculation_price:null, included:true, exclusion_reason:'', manual_scores:{}, adjustment:{mode:'none', note:''}});
    priceDraft.dirty = true; renderPriceSheetPane(); updatePriceSheetFooter();
  };
  $('refresh-price-sheet').onclick = async () => {
    if (priceDraft?.dirty && !confirm('重新识别会放弃当前未保存的报价调整，是否继续？')) return;
    const button = $('refresh-price-sheet'); button.disabled = true;
    try { priceDraft = null; await refreshPriceSheet(true); beginPriceDraft(); renderPriceSheetPane(); updatePriceSheetFooter(); }
    catch (error) { alert(error.message); }
    finally { button.disabled = false; }
  };
  $('save-price-sheet-batch').onclick = savePriceSheetBatch;
  // 配置弹窗仅允许通过各自的“关闭 ×”按钮退出，避免误点遮罩或 Esc 丢失正在编辑的内容。
  $('reset-global-rule-form').onclick = resetGlobalRuleForm;
  $('global-rule-acquisition').onchange = syncGlobalRuleAcquisitionControls;
  syncGlobalRuleAcquisitionControls();
  $('save-global-rule').onclick = async () => { const password = window.prompt('请输入通用规则库操作口令'); if (password === null) return; try { const ruleId = $('global-rule-id').value; const choice = $('global-rule-acquisition').value; const level = $('global-rule-coverage').value; const existing = globalRules.find((rule) => rule.global_rule_id === ruleId); const payload = {category:$('global-rule-category').value, title:$('global-rule-title').value, check_rule:$('global-rule-check').value, source_text:$('global-rule-source').value, ocr_required:$('global-rule-ocr').checked, enabled:$('global-rule-enabled').checked, password, ...simpleAcquisitionPayload(choice, level, existing)}; const data = await request(ruleId ? `/global-rules/${ruleId}` : '/global-rules', {method:ruleId ? 'PATCH' : 'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)}); resetGlobalRuleForm(); await loadGlobalRules(); if (!ruleId && activeProject) await refreshRules(); if (!ruleId && data.rule?.synced_draft_rule_sets) $('global-rule-form-title').textContent = `已同步至 ${data.rule.synced_draft_rule_sets} 个待确认项目`; } catch (error) { alert(error.message); } };
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
  $('export-score-csv').onclick = async () => { try { const [objective, subjective, price] = await Promise.all(['objective', 'subjective'].map((type) => request(`/projects/${activeProject}/score-results/${type}`)).concat(request(`/projects/${activeProject}/price-sheet`))); const rows = [['评分类型','投标人','规则名称','检查规则','结论','AI建议得分','满分','置信度','证据','理由']]; for (const [type, data] of [['客观分', objective], ['主观分', subjective]]) for (const item of data.results.filter((value) => !value.price_managed_by_sheet)) { const compactOcr = type === '客观分'; rows.push([type, item.bidder_name || item.original_name, ruleTitle(item), item.check_rule || '', cleanDisplayText(item.conclusion_summary), item.suggested_score ?? '', item.max_score ?? '', confidenceLabel(item.confidence), compactOcr ? cleanDisplayText(compactObjectiveOcrText(item.evidence)) : cleanDisplayText(item.evidence), compactOcr ? cleanDisplayText(compactObjectiveOcrText(item.reason)) : cleanDisplayText(item.reason)]); } for (const entry of price.price_sheet?.entries || []) for (const rule of price.price_sheet?.rules || []) { const score = entry.scores?.[rule.rule_id]; rows.push(['价格分', entry.bidder_name || '', rule.title || '价格评分', rule.check_rule || '', entry.included ? '参与计算' : `不参与：${entry.exclusion_reason || ''}`, score?.score ?? '', rule.max_score ?? '', score?.source === 'manual' ? '人工填写' : (score ? '系统计算' : '待计算'), entry.calculation_price ? `计分价：${entry.calculation_price}` : '', score?.calculation || rule.calculation_block_reason || '']); } const csv = '\ufeff' + rows.map((row) => row.map((cell) => `"${String(cell).replaceAll('"', '""')}"`).join(',')).join('\r\n'); const link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], {type:'text/csv;charset=utf-8'})); link.download = '评标评分汇总.csv'; link.click(); URL.revokeObjectURL(link.href); } catch (error) { alert(error.message); } };
  document.querySelectorAll('[data-tab]').forEach((button) => button.onclick = async () => { if (button.disabled) return; document.querySelectorAll('[data-tab]').forEach((item) => item.classList.toggle('active', item === button)); document.querySelectorAll('[data-pane]').forEach((item) => item.classList.toggle('active', item.dataset.pane === button.dataset.tab)); if (!activeProject) return; try { if (button.dataset.tab === 'rules') await refreshRules(); if (button.dataset.tab === 'price') { await refreshPriceSheet(false); preparePriceSheetPane(); } } catch (error) { $('task-status').textContent = error.message; } });
  // 任务可能由另一个浏览器标签或会话发起。空闲时不轮询；用户返回页面时只做
  // 一次按需同步，避免继续显示旧规则集，又不增加 2 核 2 GB 服务器的常驻负担。
  async function refreshFocusedProject() {
    if (!activeProject || document.hidden || focusRefreshInFlight) return;
    focusRefreshInFlight = true;
    try {
      await refreshProject();
      const activePane = document.querySelector('[data-pane].active')?.dataset.pane;
      if (activePane === 'rules') await refreshRules();
    } finally {
      focusRefreshInFlight = false;
    }
  }
  window.addEventListener('focus', () => refreshFocusedProject().catch(() => {}));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshFocusedProject().catch(() => {}); });
  initUsagePopover();
  initDeploymentPopover();
  Promise.all([loadProjects(), loadProfiles(), loadBuildInfo()]).catch((error) => { $('projects').textContent = error.message; });
})();
