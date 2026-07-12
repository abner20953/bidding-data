document.addEventListener('DOMContentLoaded', () => {
    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
        })[char]);
    }

    function safeHttpUrl(value) {
        try {
            const url = new URL(String(value || ''), window.location.origin);
            return ['http:', 'https:'].includes(url.protocol) ? url.href : '#';
        } catch (_) {
            return '#';
        }
    }

    // 基础 UI 元素
    const dateSelector = document.getElementById('date-selector');
    const regionSelector = document.getElementById('region-selector');
    const refreshBtn = document.getElementById('refresh-btn');
    const regionsContainer = document.getElementById('regions-container');

    // 模态框 UI 元素
    const modal = document.getElementById('region-detail-modal');
    const closeModalBtn = document.querySelector('.close-modal-btn');
    const modalRegionTitle = document.getElementById('modal-region-title');
    const modalProjectCount = document.getElementById('modal-project-count');
    const modalTableBody = document.getElementById('modal-table-body');
    const modalTimeFilter = document.getElementById('modal-time-filter');

    // 全局数据缓存
    let currentData = [];
    // 缓存当前模态框的数据，供筛选使用
    let currentModalData = [];

    // ... (loadDates, loadData, updateRegionSelector, parseTime, renderDashboard, createCardHTML 保持不变) ...

    // --- 模态框相关逻辑 ---

    function openModal(regionName) {
        // 1. 过滤当前地区数据
        currentModalData = currentData.filter(item =>
            (item['地区（市）'] || '未知地区') === regionName
        );

        // 2. 初始化时间筛选器
        initTimeFilter(currentModalData);

        // 3. 渲染数据 (默认显示全部)
        renderModalTable(currentModalData);

        // 4. 显示模态框
        modalRegionTitle.textContent = regionName;
        modal.classList.remove('hidden');
        void modal.offsetWidth;
        modal.classList.add('visible');
    }

    // 初始化时间筛选下拉框
    function initTimeFilter(items) {
        const times = new Set();
        items.forEach(item => {
            const t = item['开标具体时间'];
            if (t && t !== '待采集' && t !== '未找到' && t !== '-') {
                times.add(t);
            }
        });

        // 排序：从早到晚
        const sortedTimes = Array.from(times).sort((a, b) => parseTime(a) - parseTime(b));

        modalTimeFilter.innerHTML = '<option value="all">全部时间</option>';
        sortedTimes.forEach(t => {
            const option = document.createElement('option');
            option.value = t;
            option.text = t;
            modalTimeFilter.add(option);
        });

        // 重置选中状态
        modalTimeFilter.value = 'all';
    }

    // 渲染模态框表格 (支持筛选)
    function renderModalTable(items) {
        // 排序规则保持不变: 信息化 > 语义 > 时间
        items.sort((a, b) => {
            const isInfoA = a['是否信息化'] === '是';
            const isInfoB = b['是否信息化'] === '是';

            if (isInfoA && !isInfoB) return -1;
            if (!isInfoA && isInfoB) return 1;

            const scoreA = parseFloat(a['语义匹配度'] || 0);
            const scoreB = parseFloat(b['语义匹配度'] || 0);

            if (Math.abs(scoreA - scoreB) > 0.0001) {
                return scoreB - scoreA;
            }

            const timeA = parseTime(a['开标具体时间']);
            const timeB = parseTime(b['开标具体时间']);
            return timeA - timeB;
        });

        // 更新计数
        modalProjectCount.textContent = items.length;

        // 填充表格
        modalTableBody.innerHTML = items.map(item => `
            <tr title="${escapeHtml(item['采购需求'] || '')}">
                <td>
                    <a href="${safeHttpUrl(item['链接'])}" target="_blank" rel="noopener noreferrer" class="project-link" title="${escapeHtml(item['标题'])}">
                        ${escapeHtml(item['标题'])}
                    </a>
                </td>
                <td>${escapeHtml(formatTime(item['开标具体时间']))}</td>
                <td>${escapeHtml(formatLocation(item['开标地点']))}</td>
                <td class="cell-budget">${escapeHtml(formatBudget(item['预算限价项目']))}</td>
                <td>${escapeHtml(item['代理机构'] || '-')}</td>
                <td>${escapeHtml(item['采购人名称'] || '-')}</td>
                <td>
                    <span class="tag is-method" style="margin-bottom: 4px;">${escapeHtml(item['采购方式'] || '公开招标')}</span>
                    <br>
                    ${item['是否信息化'] === '是'
                ? '<span class="tag is-info">信息化</span>'
                : '<span class="tag is-normal">普通</span>'}
                </td>
            </tr>
        `).join('');
    }

    function closeModal() {
        modal.classList.remove('visible');
        setTimeout(() => {
            modal.classList.add('hidden');
        }, 300);
    }

    // 监听时间筛选变化
    modalTimeFilter.addEventListener('change', (e) => {
        const selectedTime = e.target.value;
        if (selectedTime === 'all') {
            renderModalTable(currentModalData);
        } else {
            const filtered = currentModalData.filter(item => item['开标具体时间'] === selectedTime);
            renderModalTable(filtered);
        }
    });

    // --- 辅助函数 ---

    // 格式化辅助函数
    function formatBudget(val) {
        if (!val) return '未公布';
        return val.replace('待采集', '-').replace('未找到', '-');
    }

    function formatTime(val) {
        if (!val || val === '待采集' || val === '未找到') return '-';
        return val;
    }

    function formatLocation(val) {
        if (!val || val === '待采集' || val === '未找到') return '-';
        return val;
    }

    // 4. 解析时间辅助函数
    function parseTime(timeStr) {
        if (!timeStr || timeStr === '待采集' || timeStr === '-' || timeStr === '未找到') return 99999;
        const parts = timeStr.split(/[:：]/);
        if (parts.length >= 2) {
            return parseInt(parts[0]) * 60 + parseInt(parts[1]);
        }
        return 99999;
    }

    function showError(msg) {
        regionsContainer.innerHTML = `<div class="loading-state"><p style="color: #ef4444;">错误: ${msg}</p></div>`;
    }

    // --- 数据加载与渲染 ---

    // 获取明天日期的字符串 (格式：YYYY年MM月DD日)
    function getTomorrowDateStr() {
        const d = new Date();
        d.setDate(d.getDate() + 1);
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}年${m}月${day}日`;
    }

    // 1. 加载可用日期
    async function loadDates() {
        try {
            const response = await fetch('/api/dates');
            const dates = await response.json();

            dateSelector.innerHTML = '';

            if (dates.length === 0) {
                const option = document.createElement('option');
                option.text = "无可用数据";
                dateSelector.add(option);
                return;
            }

            dates.forEach((date) => {
                const option = document.createElement('option');
                option.value = date;
                option.text = date;
                dateSelector.add(option);
            });

            // 智能选择日期：如果存在明天的数据，则优先选中；否则选中最新
            const tomorrowStr = getTomorrowDateStr();
            let targetDate = dates[0]; // 默认最新

            if (dates.includes(tomorrowStr)) {
                targetDate = tomorrowStr;
            }

            // 更新下拉框选中状态
            dateSelector.value = targetDate;
            loadData(targetDate);

        } catch (error) {
            console.error('Failed to load dates:', error);
            dateSelector.innerHTML = '<option>加载失败</option>';
        }
    }

    // 2. 加载数据
    // 提取刷新已有日期逻辑
    async function refreshExistingDates() {
        try {
            const resp = await fetch('/api/dates');
            const dates = await resp.json();
            existingDates.clear();
            dates.forEach(d_str => {
                const std_date = d_str.replace('年', '-').replace('月', '-').replace('日', '');
                existingDates.add(std_date);
            });
        } catch (e) {
            console.error("Failed to fetch existing dates", e);
        }
    }

    // 打开日历模态框 (Moved to bottom with other listeners to ensuring 'autoFetchBtn' is defined)
    // 2. 加载数据
    async function loadData(date) {
        if (!date) return;

        regionsContainer.innerHTML = `
            <div class="loading-state">
                <div class="spinner"></div>
                <p>正在加载 ${date} 的数据...</p>
            </div>
        `;

        try {
            const response = await fetch(`/api/data?date=${date}`);
            const data = await response.json();

            if (data.error) {
                showError(data.error);
                return;
            }

            currentData = data;
            updateRegionSelector(data);
            renderDashboard(data);

        } catch (error) {
            console.error('Failed to load data:', error);
            showError("网络错误或服务器异常");
        }
    }

    // 3. 更新筛选框
    function updateRegionSelector(data) {
        const regions = new Set();
        data.forEach(item => {
            const r = item['地区（市）'] || '未知地区';
            regions.add(r);
        });

        const sortedRegions = Array.from(regions).sort();
        regionSelector.innerHTML = '<option value="all" selected>全部地区</option>';
        sortedRegions.forEach(r => {
            const option = document.createElement('option');
            option.value = r;
            option.text = r;
            regionSelector.add(option);
        });
    }

    // 6. 创建卡片 HTML (缩略版)
    function createCardHTML(item) {
        const title = item['标题'];
        const link = item['链接'];
        let budget = formatBudget(item['预算限价项目']);

        const agency = item['代理机构'] || '未知机构';
        const district = item['地区（县）'] ? ` • ${item['地区（县）']}` : '';
        const bidTime = formatTime(item['开标具体时间']);
        const bidLocation = formatLocation(item['开标地点']);

        const isInfo = item['是否信息化'] === '是';
        const cardClass = isInfo ? 'project-card is-info' : 'project-card';
        const infoTitleAttr = isInfo ? ' title="高置信度信息化项目"' : '';

        return `
            <a href="${safeHttpUrl(link)}" target="_blank" rel="noopener noreferrer" class="${cardClass}"${infoTitleAttr}>
                <div class="project-title">${escapeHtml(title)}</div>
                
                <div class="project-details-row">
                    <div class="detail-item" title="开标时间">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <circle cx="12" cy="12" r="10"></circle>
                            <polyline points="12 6 12 12 16 14"></polyline>
                        </svg>
                        <span>${escapeHtml(bidTime)}</span>
                    </div>
                    <div class="detail-item location" title="${escapeHtml(bidLocation)}">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path>
                            <circle cx="12" cy="10" r="3"></circle>
                        </svg>
                        <span>${escapeHtml(bidLocation)}</span>
                    </div>
                </div>

                <div class="project-meta">
                    <span class="project-agency" title="${escapeHtml(agency)}">${escapeHtml(agency)}${escapeHtml(district)}</span>
                    <span class="tag is-method">${escapeHtml(item['采购方式'] || '公开招标')}</span>
                    <span class="project-budget">${escapeHtml(budget)}</span>
                </div>
            </a>
        `;
    }

    // 5. 渲染主仪表板
    function renderDashboard(data, filterRegion = 'all') {
        regionsContainer.innerHTML = '';

        const filteredData = filterRegion === 'all'
            ? data
            : data.filter(item => (item['地区（市）'] || '未知地区') === filterRegion);

        const groupedData = {};
        filteredData.forEach(item => {
            const region = item['地区（市）'] || '未知地区';
            if (!groupedData[region]) groupedData[region] = [];
            groupedData[region].push(item);
        });

        const sortedRegions = Object.keys(groupedData).sort((a, b) => groupedData[b].length - groupedData[a].length);

        if (sortedRegions.length === 0) {
            regionsContainer.innerHTML = '<div class="loading-state"><p>没有找到相关项目。</p></div>';
            return;
        }

        sortedRegions.forEach(region => {
            const items = groupedData[region];

            // 地区内项目排序
            items.sort((a, b) => {
                const timeA = parseTime(a['开标具体时间']);
                const timeB = parseTime(b['开标具体时间']);
                if (timeA === timeB) {
                    return (b['语义匹配度'] || 0) - (a['语义匹配度'] || 0);
                }
                return timeA - timeB;
            });

            // 创建地区列
            const columnEl = document.createElement('div');
            columnEl.className = 'region-column';

            // 头部：增加点击事件支持
            columnEl.innerHTML = `
                <div class="region-header">
                    <h2 class="clickable-title" data-region="${escapeHtml(region)}">${escapeHtml(region)}</h2>
                    <span class="region-count">${items.length}</span>
                </div>
                <div class="region-content">
                    ${items.map(item => createCardHTML(item)).join('')}
                </div>
            `;

            regionsContainer.appendChild(columnEl);
        });

        // 绑定地区标题点击事件 (使用事件委托或直接绑定)
        document.querySelectorAll('.region-header h2').forEach(titleEl => {
            titleEl.addEventListener('click', () => {
                const regionName = titleEl.getAttribute('data-region');
                openModal(regionName);
            });
        });
    }

    // --- 事件监听 ---

    dateSelector.addEventListener('change', (e) => loadData(e.target.value));

    regionSelector.addEventListener('change', (e) => renderDashboard(currentData, e.target.value));

    refreshBtn.addEventListener('click', () => {
        const currentDate = dateSelector.value;
        if (currentDate) loadData(currentDate);
        else loadDates();
    });

    // 模态框关闭事件
    closeModalBtn.addEventListener('click', closeModal);
    // 点击遮罩层关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal();
    });
    // ESC 键关闭
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (modal.classList.contains('visible')) closeModal();
            // 注意：进度模态框通常不允许 ESC 关闭，直到任务完成
        }
    });

    // --- 自动采集相关 (新版 - 日历选择) ---
    const autoFetchBtn = document.getElementById('auto-fetch-btn');
    // Progress UI
    const progressModal = document.getElementById('progress-modal');
    const progressBar = document.getElementById('progress-bar');
    const progressText = document.getElementById('progress-text');
    const progressCount = document.getElementById('progress-count');
    const logContainer = document.getElementById('log-container');
    const progressModalFooter = document.getElementById('progress-modal-footer');
    const closeProgressBtn = document.querySelector('.close-progress-btn');

    // Calendar UI
    const dateModal = document.getElementById('date-modal');
    const closeDateBtn = document.querySelector('.close-date-btn');
    const prevMonthBtn = document.getElementById('prev-month');
    const nextMonthBtn = document.getElementById('next-month');
    const currentMonthLabel = document.getElementById('current-month-label');
    const calendarGrid = document.querySelector('.calendar-grid');
    const dateSelectionMsg = document.getElementById('date-selection-msg');
    const selectedNumSpan = document.getElementById('selected-num');
    const startScrapeBtn = document.getElementById('start-scrape-btn');

    let pollInterval = null;
    let selectedDates = new Set();
    let currentDateCursor = new Date(); // To track calendar month
    let existingDates = new Set(); // To mark dates that already have data

    // --- 日历功能实现 ---
    const deleteSelectedBtn = document.getElementById('delete-selected-btn');
    const cleanBeforeBtn = document.getElementById('clean-before-btn');
    const cleanHintMsg = document.getElementById('clean-hint-msg');

    // Helper function for closing modals

    // Helper function for closing modals
    function closeModalInternal(modalElement) {
        modalElement.classList.remove('visible');
        setTimeout(() => {
            modalElement.classList.add('hidden');
        }, 300);
    }

    // 提取刷新已有日期逻辑
    async function refreshExistingDates() {
        try {
            const resp = await fetch('/api/dates');
            const dates = await resp.json();
            existingDates.clear();
            dates.forEach(d_str => {
                // 将 "2026年01月01日" 转换为 "2026-01-01" 以便比较
                const std_date = d_str.replace('年', '-').replace('月', '-').replace('日', '');
                existingDates.add(std_date);
            });
        } catch (e) {
            console.error("Failed to fetch existing dates", e);
        }
    }


    // 打开日历模态框
    if (autoFetchBtn) {
        autoFetchBtn.addEventListener('click', async () => {
            dateModal.classList.remove('hidden');
            void dateModal.offsetWidth;
            dateModal.classList.add('visible');

            // 重置状态
            selectedDates.clear();
            updateSelectionUI();
            currentDateCursor = new Date(); // Reset to current month

            await refreshExistingDates();
            renderCalendar(currentDateCursor);
            await refreshExistingDates();
            renderCalendar(currentDateCursor);

            // Load Scheduler Logs
            loadSchedulerLogs();
        });
    }

    // 定时任务日志逻辑
    const schedulerLogsContainer = document.getElementById('scheduler-logs-container');
    const refreshSchedulerLogsBtn = document.getElementById('refresh-scheduler-logs');

    if (refreshSchedulerLogsBtn) {
        refreshSchedulerLogsBtn.addEventListener('click', loadSchedulerLogs);
    }

    async function loadSchedulerLogs() {
        if (!schedulerLogsContainer) return;

        schedulerLogsContainer.innerHTML = '<div style="text-align: center; padding-top: 40px;">刷新中...</div>';

        try {
            const resp = await fetch('/api/scheduler/logs');
            const data = await resp.json();

            if (data.logs && data.logs.length > 0) {
                schedulerLogsContainer.innerHTML = data.logs.map(log =>
                    `<div style="margin-bottom: 4px; border-bottom: 1px solid rgba(0,0,0,0.02); padding-bottom: 2px;">${escapeHtml(log)}</div>`
                ).join('');
            } else {
                schedulerLogsContainer.innerHTML = '<div style="text-align: center; padding-top: 40px;">暂无日志记录</div>';
            }
        } catch (e) {
            schedulerLogsContainer.innerHTML = '<div style="text-align: center; color: var(--danger-color); padding-top: 40px;">加载失败</div>';
        }
    }


    closeDateBtn.addEventListener('click', () => {
        closeModalInternal(dateModal);
    });

    // 渲染日历
    function renderCalendar(date) {
        const year = date.getFullYear();
        const month = date.getMonth();

        currentMonthLabel.textContent = `${year}年 ${month + 1}月`;

        // 清理日历网格 (保留前7个 Header)
        const days = calendarGrid.querySelectorAll('.calendar-day');
        days.forEach(d => d.remove());

        const firstDay = new Date(year, month, 1);
        const lastDay = new Date(year, month + 1, 0);

        const startDayIndex = firstDay.getDay(); // 0 is Sunday
        const totalDays = lastDay.getDate();

        // 填充空白
        for (let i = 0; i < startDayIndex; i++) {
            const emptyCell = document.createElement('div');
            emptyCell.className = 'calendar-day empty';
            calendarGrid.appendChild(emptyCell);
        }

        // 填充日期
        for (let i = 1; i <= totalDays; i++) {
            const dayCell = document.createElement('div');
            dayCell.className = 'calendar-day';
            dayCell.textContent = i;

            // 构建 YYYY-MM-DD 字符串
            const currentStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(i).padStart(2, '0')}`;
            dayCell.dataset.date = currentStr;

            const cellDate = new Date(year, month, i);
            const earliestDate = new Date();
            earliestDate.setHours(0, 0, 0, 0);
            earliestDate.setDate(earliestDate.getDate() - 90);
            if (cellDate < earliestDate) {
                dayCell.classList.add('disabled');
                dayCell.title = '超出政府采购网近 90 天查询范围';
                calendarGrid.appendChild(dayCell);
                continue;
            }

            // 样式处理
            if (selectedDates.has(currentStr)) {
                dayCell.classList.add('selected');
            }
            // 标记已存在
            if (existingDates.has(currentStr)) {
                dayCell.classList.add('has-data');
                dayCell.title = "已采集 (点击将重新获取)";
            }

            // 点击事件
            dayCell.addEventListener('click', () => toggleDateSelection(currentStr, dayCell));

            calendarGrid.appendChild(dayCell);
        }
    }

    // 切换月份
    prevMonthBtn.addEventListener('click', () => {
        currentDateCursor.setMonth(currentDateCursor.getMonth() - 1);
        renderCalendar(currentDateCursor);
    });

    nextMonthBtn.addEventListener('click', () => {
        currentDateCursor.setMonth(currentDateCursor.getMonth() + 1);
        renderCalendar(currentDateCursor);
    });

    // 选择日期逻辑
    function toggleDateSelection(dateStr, element) {
        if (selectedDates.has(dateStr)) {
            selectedDates.delete(dateStr);
            element.classList.remove('selected');
        } else {
            if (selectedDates.size >= 5) {
                showMsg('一次最多只能选择5天', 'error');
                return;
            }
            selectedDates.add(dateStr);
            element.classList.add('selected');
        }
        updateSelectionUI();
    }

    function updateSelectionUI() {
        selectedNumSpan.textContent = selectedDates.size;

        // 1. 采集按钮逻辑
        startScrapeBtn.disabled = selectedDates.size === 0;

        // 2. 删除选中逻辑
        const overwrites = [...selectedDates].filter(d => existingDates.has(d));
        if (overwrites.length > 0) {
            showMsg(`注意：${overwrites.length} 个日期将覆盖现有数据`, 'warning');
            deleteSelectedBtn.disabled = false;
            deleteSelectedBtn.innerHTML = `🗑️ 删除选中 (${overwrites.length})`;
        } else {
            deleteSelectedBtn.disabled = true;
            deleteSelectedBtn.innerHTML = `🗑️ 删除选中`;

            if (selectedDates.size > 0) {
                showMsg('', 'info');
            } else {
                showMsg('请选择日期 (最多5天)', 'info');
            }
        }

        // 3. 清理之前数据逻辑 - 按钮状态不再动态变化，逻辑后移到点击事件
        // 保持按钮一直可用，或者仅当有选择时可用？
        // 用户习惯可能是一直可用，点击后报错。这里我们设为一直可用。
    }

    function showMsg(text, type = 'info') {
        dateSelectionMsg.textContent = text;
        dateSelectionMsg.style.color = type === 'error' ? 'var(--danger-color)' :
            type === 'warning' ? '#f59e0b' : 'var(--text-secondary)';
    }

    // 开始采集 (点击日历上的按钮)
    startScrapeBtn.addEventListener('click', async () => {
        if (selectedDates.size === 0) return;

        // 关闭日历模态框
        closeModalInternal(dateModal);

        // 打开进度模态框
        // 重置 UI
        progressBar.style.width = '0%';
        progressBar.style.backgroundColor = '';
        progressText.textContent = "正在初始化...";
        progressCount.textContent = "0/0";
        logContainer.innerHTML = '';
        progressModalFooter.classList.add('hidden');
        progressModal.classList.remove('hidden');
        void progressModal.offsetWidth;
        progressModal.classList.add('visible');

        // 发送请求
        const sortedDates = Array.from(selectedDates).sort();

        try {
            const response = await fetch('/api/scrape/auto_start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ dates: sortedDates })
            });
            const result = await response.json();

            if (result.status === 'success') {
                logContainer.innerHTML += `<p class="success">✅ ${result.message}</p>`;
                logContainer.innerHTML += `<p>目标日期: ${result.target_dates.join(', ')}</p>`;
                // 开始轮询
                pollInterval = setInterval(pollStatus, 1000);
            } else {
                logContainer.innerHTML += `<p class="error">❌ 启动失败: ${result.message}</p>`;
                progressModalFooter.classList.remove('hidden');
            }
        } catch (error) {
            logContainer.innerHTML += `<p class="error">❌ 请求异常: ${error.message}</p>`;
            progressModalFooter.classList.remove('hidden');
        }
    });

    // 删除选中
    deleteSelectedBtn.addEventListener('click', async () => {
        const overwrites = [...selectedDates].filter(d => existingDates.has(d));
        if (overwrites.length === 0) return;

        const pwd = getAdminPassword(`删除选中的 ${overwrites.length} 个日期`);
        if (!pwd) return;

        if (!confirm(`确定要删除选中的 ${overwrites.length} 个日期的数据吗？`)) return;

        let successCount = 0;
        for (const dateStr of overwrites) {
            try {
                const resp = await fetch(`/api/data?date=${dateStr}`, {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: pwd })
                });
                const res = await resp.json();
                if (res.status === 'success') {
                    existingDates.delete(dateStr);
                    selectedDates.delete(dateStr); // Also unselect it
                    successCount++;
                } else {
                    alert(`删除 ${dateStr} 失败: ${res.message}`);
                }
            } catch (e) {
                console.error(`Failed to delete ${dateStr}`, e);
                alert(`删除 ${dateStr} 失败: 网络或服务器错误`);
            }
        }

        if (successCount > 0) {
            alert(`成功删除 ${successCount} 个文件。`);
            // 刷新界面
            await refreshExistingDates(); // Re-fetch from server to be sure
            renderCalendar(currentDateCursor);
            updateSelectionUI();
            loadDates(); // Refresh dropdown
        }
    });

    // 清除指定日期前数据
    cleanBeforeBtn.addEventListener('click', async () => {
        // Validation Logic
        if (selectedDates.size === 0) {
            alert("请先在日历中选择一个日期作为参考！");
            return;
        }

        if (selectedDates.size > 1) {
            alert("只能选择一个日期作为参考！\n请取消其他选择，只保留一个日期。");
            return;
        }

        const dateStr = [...selectedDates][0];

        const pwd = getAdminPassword(`删除 [${dateStr}] 之前的所有历史数据`);
        if (!pwd) return;

        if (!confirm(`⚠️ 警告：确定要删除 [${dateStr}] 之前的所有历史数据吗？\n（保留 ${dateStr} 当天及之后的数据）\n此操作不可恢复！`)) return;

        try {
            const resp = await fetch(`/api/data?before_date=${dateStr}`, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pwd })
            });
            const result = await resp.json();

            if (result.status === 'success') {
                alert(result.message);
                // 刷新状态
                selectedDates.clear();
                await refreshExistingDates(); // Refresh existing dates from server
                renderCalendar(currentDateCursor);
                updateSelectionUI();
                loadDates();
            } else {
                alert("清理失败: " + result.message);
            }
        } catch (e) {
            alert("请求异常: " + e.message);
        }
    });

    // 轮询状态 (保持不变，或微调)
    async function pollStatus() {
        try {
            const response = await fetch('/api/scrape/status');
            const status = await response.json();

            // 更新进度条
            if (status.total > 0) {
                const percentage = Math.round((status.progress / status.total) * 100);
                progressBar.style.width = `${percentage}%`;
                progressCount.textContent = `${status.progress}/${status.total}`;
                progressText.textContent = `正在处理: ${status.current_date || '...'}`;
            }

            // 更新日志
            const logsHtml = status.logs.map(log => `<p>${escapeHtml(log)}</p>`).join('');
            if (logContainer.innerHTML !== logsHtml) {
                logContainer.innerHTML = logsHtml;
                logContainer.scrollTop = logContainer.scrollHeight;
            }

            // 检查完成
            if (!status.is_running) {
                clearInterval(pollInterval);
                const statusLabels = {
                    success: '任务成功完成',
                    partial: '任务部分完成，请检查警告',
                    failed: '任务失败，请检查错误日志'
                };
                progressText.textContent = statusLabels[status.result_status] || '任务已结束';
                if (status.result_status === 'failed') {
                    progressBar.style.backgroundColor = '#ef4444';
                } else {
                    progressBar.style.width = '100%';
                }
                progressModalFooter.classList.remove('hidden');

                // 任务完成后刷新日期列表
                loadDates();
            }

        } catch (error) {
            console.error('Poll error:', error);
        }
    }

    closeProgressBtn.addEventListener('click', () => {
        progressModal.classList.remove('visible');
        setTimeout(() => {
            progressModal.classList.add('hidden');
        }, 300);
    });

    // --- 新版定时任务日志模态框逻辑 ---
    const openSchedulerBtn = document.getElementById('open-scheduler-btn');
    const schedulerModal = document.getElementById('scheduler-modal');
    const closeSchedulerBtn = document.querySelector('.close-scheduler-btn');
    const refreshSchedulerLogsMain = document.getElementById('refresh-scheduler-logs-main');
    const schedulerLogsLargeContainer = document.getElementById('scheduler-logs-large-container');

    // 打开日志模态框
    if (openSchedulerBtn) {
        openSchedulerBtn.addEventListener('click', () => {
            schedulerModal.classList.remove('hidden');
            void schedulerModal.offsetWidth;
            schedulerModal.classList.add('visible');
            loadSchedulerLogsLarge();
        });
    }

    // 关闭日志模态框
    if (closeSchedulerBtn) {
        closeSchedulerBtn.addEventListener('click', () => {
            schedulerModal.classList.remove('visible');
            setTimeout(() => {
                schedulerModal.classList.add('hidden');
            }, 300);
        });
    }

    // 刷新日志
    if (refreshSchedulerLogsMain) {
        refreshSchedulerLogsMain.addEventListener('click', loadSchedulerLogsLarge);
    }

    // 点击遮罩关闭 (仅针对 schedulerModal)
    if (schedulerModal) {
        schedulerModal.addEventListener('click', (e) => {
            if (e.target === schedulerModal) {
                schedulerModal.classList.remove('visible');
                setTimeout(() => {
                    schedulerModal.classList.add('hidden');
                }, 300);
            }
        });
    }

    async function loadSchedulerLogsLarge() {
        if (!schedulerLogsLargeContainer) return;

        const originalText = refreshSchedulerLogsMain ? refreshSchedulerLogsMain.innerHTML : 'Refresh';
        if (refreshSchedulerLogsMain) refreshSchedulerLogsMain.innerHTML = '⏳ 加载中...';

        try {
            const resp = await fetch('/api/scheduler/logs');
            const data = await resp.json();

            if (data.logs && data.logs.length > 0) {
                schedulerLogsLargeContainer.innerHTML = data.logs.map(log =>
                    `<div style="margin-bottom: 6px; border-bottom: 1px solid rgba(255,255,255,0.03); padding-bottom: 4px;">${escapeHtml(log)}</div>`
                ).join('');
            } else {
                schedulerLogsLargeContainer.innerHTML = '<div style="text-align: center; padding-top: 100px; color: rgba(255,255,255,0.3);">暂无日志记录</div>';
            }
        } catch (e) {
            schedulerLogsLargeContainer.innerHTML = '<div style="text-align: center; color: #ef4444; padding-top: 100px;">日志加载失败</div>';
        } finally {
            if (refreshSchedulerLogsMain) refreshSchedulerLogsMain.innerHTML = originalText;
        }
    }

    // 初始化
    loadDates();
});
