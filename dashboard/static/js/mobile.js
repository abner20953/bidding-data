document.addEventListener('DOMContentLoaded', () => {
    // UI Elements
    const dateSelector = document.getElementById('date-selector');
    const regionSelector = document.getElementById('region-selector');
    const timeSelector = document.getElementById('time-selector');
    const totalCountBadge = document.getElementById('total-count');
    const contentArea = document.getElementById('content-area');

    let currentData = [];

    // Helper: Get Tomorrow's Date
    function getTomorrowDateStr() {
        const d = new Date();
        d.setDate(d.getDate() + 1);
        const y = d.getFullYear();
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        return `${y}年${m}月${day}日`;
    }

    // Helper: Parse Time for sorting
    function parseTime(timeStr) {
        if (!timeStr || timeStr === '待采集' || timeStr === '-' || timeStr === '未找到') return 99999;
        const parts = timeStr.split(/[:：]/);
        if (parts.length >= 2) {
            return parseInt(parts[0]) * 60 + parseInt(parts[1]);
        }
        return 99999;
    }

    // 1. Load Dates
    async function loadDates() {
        try {
            const response = await fetch('/api/dates');
            const dates = await response.json();

            dateSelector.innerHTML = '';
            if (dates.length === 0) {
                dateSelector.innerHTML = '<option>无数据</option>';
                return;
            }

            dates.forEach(date => {
                const option = document.createElement('option');
                option.value = date;
                option.text = date;
                dateSelector.add(option);
            });

            // Smart Selection: Tomorrow > Latest
            const tomorrowStr = getTomorrowDateStr();
            let targetDate = dates[0];
            if (dates.includes(tomorrowStr)) {
                targetDate = tomorrowStr;
            }
            dateSelector.value = targetDate;
            loadData(targetDate);

        } catch (error) {
            contentArea.innerHTML = '<div class="loading-state"><p>加载失败，请刷新</p></div>';
        }
    }

    // 2. Load Data
    async function loadData(date) {
        contentArea.innerHTML = `
            <div class="loading-state">
                <div class="spinner"></div>
                <p>正在加载...</p>
            </div>
        `;

        try {
            const response = await fetch(`/api/data?date=${date}`);
            const data = await response.json();

            if (data.error) {
                contentArea.innerHTML = `<div class="loading-state"><p>${data.error}</p></div>`;
                return;
            }

            currentData = data;
            initRegionSelector(data);
            initRegionSelector(data);
            updateTimeSelector(data, 'all'); // Init Time with all data

            // Mobile Optimization: Automatically select the first region that has data
            // But 'All' is also fine. Let's stick to 'All' but sorted smartly.
            renderList(data);

        } catch (error) {
            contentArea.innerHTML = '<div class="loading-state"><p>网络错误</p></div>';
        }
    }

    // 3. Init Regions
    function initRegionSelector(data) {
        const regions = new Set();
        data.forEach(item => regions.add(item['地区（市）'] || '未知'));
        const sorted = Array.from(regions).sort();

        regionSelector.innerHTML = '<option value="all">全部地区</option>';
        sorted.forEach(r => {
            const opt = document.createElement('option');
            opt.value = r;
            opt.text = r;
            regionSelector.add(opt);
        });
    }

    // New: Init Time Selector (Linked to Region)
    function updateTimeSelector(data, filterRegion) {
        let items = data;
        if (filterRegion && filterRegion !== 'all') {
            items = data.filter(item => (item['地区（市）'] || '未知') === filterRegion);
        }

        const times = new Set();
        items.forEach(item => {
            const t = item['开标具体时间'];
            if (t && t !== '待采集' && t !== '未找到' && t !== '-') {
                times.add(t);
            }
        });

        // Sort times
        const sorted = Array.from(times).sort((a, b) => parseTime(a) - parseTime(b));

        // Save current selection if possible
        const currentSelection = timeSelector.value;

        timeSelector.innerHTML = '<option value="all">全部时间</option>';
        sorted.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.text = t;
            timeSelector.add(opt);
        });

        // Restore selection if valid, else reset
        if (currentSelection && Array.from(times).includes(currentSelection)) {
            timeSelector.value = currentSelection;
        } else {
            timeSelector.value = 'all';
        }
    }

    // 4. Render List
    function renderList(data, filterRegion = null, filterTime = null) {
        // Get current values if not provided
        if (filterRegion === null) filterRegion = regionSelector.value;
        if (filterTime === null) filterTime = timeSelector.value;

        let items = data;

        // Filter Region
        if (filterRegion !== 'all') {
            items = items.filter(item => (item['地区（市）'] || '未知') === filterRegion);
        }

        // Filter Time
        if (filterTime !== 'all') {
            items = items.filter(item => item['开标具体时间'] === filterTime);
        }

        // Sort: Info > Time
        items.sort((a, b) => {
            const isInfoA = a['是否信息化'] === '是';
            const isInfoB = b['是否信息化'] === '是';
            if (isInfoA !== isInfoB) return isInfoB ? 1 : -1;

            return parseTime(a['开标具体时间']) - parseTime(b['开标具体时间']);
        });

        totalCountBadge.textContent = items.length;

        if (items.length === 0) {
            contentArea.innerHTML = '<div class="loading-state"><p>暂无数据</p></div>';
            return;
        }

        contentArea.innerHTML = items.map(item => createCard(item)).join('');
    }

    function createCard(item) {
        const isInfo = item['是否信息化'] === '是';
        const cardClass = isInfo ? 'project-card is-info' : 'project-card';
        const tagHtml = isInfo
            ? '<span class="tag info">信息化</span>'
            : '<span class="tag">普通</span>';

        return `
            <a href="${item['链接']}" target="_blank" class="${cardClass}">
                <div class="card-header">
                    <div class="card-title">${item['标题']}</div>
                    <div class="card-tags">
                        ${tagHtml}
                        <span class="tag">${item['地区（市）'] || '未知'}</span>
                    </div>
                </div>
                <div class="card-body">
                    <div class="detail-row" title="开标时间">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
                        <span>${item['开标具体时间'] || '-'}</span>
                    </div>
                    <div class="detail-row" title="预算">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20v-8"></path><path d="M6 20h12"></path><path d="M6 16h12"></path><path d="M6 4l6 8 6-8"></path></svg>
                        <span class="budget">${(item['预算限价项目'] && item['预算限价项目'] !== '-' && item['预算限价项目'] !== '待采集') ? '¥' + item['预算限价项目'] : (item['预算限价项目'] || '-')}</span>
                    </div>
                    <div class="detail-row full-width" title="开标地点">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg>
                        <span>${item['开标地点'] || '-'}</span>
                    </div>
                    <div class="detail-row full-width" title="代理机构">
                        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 21h18M5 21V7l8-4 8 4v14M8 21v-4h8v4"></path></svg>
                        <span>${item['代理机构'] || '-'}</span>
                    </div>
                </div>
            </a>
        `;
    }

    // Events
    dateSelector.addEventListener('change', (e) => loadData(e.target.value));

    regionSelector.addEventListener('change', (e) => {
        const region = e.target.value;
        updateTimeSelector(currentData, region); // Linkage: Update times based on region
        renderList(currentData);
    });

    timeSelector.addEventListener('change', (e) => renderList(currentData));

    // Init
    loadDates();
});
