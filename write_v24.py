# -*- coding: utf-8 -*-
import os

target_path = r"C:\Users\ai-lilac\Desktop\晋能控股评标专家全量自动提取器 V24.js"

# Part 1: Meta Header and global states
part1 = """// ==UserScript==
// @name         Jinneng Expert Extractor V24
// @namespace    http://tampermonkey.net/
// @version      24.11
// @description  Jinneng Expert Extractor V24
// @author       AI Assistant
// @match        *://dzzb.jnkgjtdzzbgs.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    // ==================== 跨标签通信用的 localStorage 键名 ====================
    const SK_EXPERTS = 'tm_v24_experts';
    const SK_IDCARDS = 'tm_v24_idcards';
    const SK_USERIDS = 'tm_v24_userids';
    const SK_SIGNAL  = 'tm_v24_done';

    // ==================== 全局状态 ====================
    let panel, content;
    let uiInitialized = false;
    window.allExpertsData = [];
    let processedIdCards = new Set();
    let processedUserIds = new Set();

    window.__pendingExpertRequests = 0;

    async function sleep(ms) {
        return new Promise(r => setTimeout(r, ms));
    }

    // ==================== JSZip 加载与兜底 ====================
    function ensureJSZip() {
        return new Promise((resolve, reject) => {
            if (typeof window.JSZip !== 'undefined') {
                resolve(window.JSZip);
                return;
            }
            let script = document.createElement('script');
            script.src = 'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.7.1/jszip.min.js';
            script.onload = () => resolve(window.JSZip);
            script.onerror = () => reject(new Error('JSZip 依赖库加载失败，请检查网络或更换浏览器'));
            document.head.appendChild(script);
        });
    }

    // ==================== Token 捕获与读取 ====================
    function getAuthToken() {
        let token = localStorage.getItem('tm_v24_token') || 
                    localStorage.getItem('token') || 
                    sessionStorage.getItem('token') || 
                    localStorage.getItem('Authorization') || 
                    sessionStorage.getItem('Authorization') || '';
        // 去除可能携带的 Bearer 前缀
        if (token && token.toLowerCase().startsWith('bearer ')) {
            token = token.substring(7);
        }
        return token.trim();
    }

    // ==================== localStorage 读写 ====================
    function saveState() {
        localStorage.setItem(SK_EXPERTS, JSON.stringify(window.allExpertsData));
        localStorage.setItem(SK_IDCARDS, JSON.stringify([...processedIdCards]));
        localStorage.setItem(SK_USERIDS, JSON.stringify([...processedUserIds]));
    }

    function loadState() {
        try {
            let d = localStorage.getItem(SK_EXPERTS);
            if (d) window.allExpertsData = JSON.parse(d);
            let c = localStorage.getItem(SK_IDCARDS);
            if (c) processedIdCards = new Set(JSON.parse(c));
            let u = localStorage.getItem(SK_USERIDS);
            if (u) processedUserIds = new Set(JSON.parse(u));
        } catch(e) {}
    }

    function signalDone() {
        localStorage.setItem(SK_SIGNAL, Date.now().toString());
    }

    function clearSignal() {
        localStorage.removeItem(SK_SIGNAL);
    }

    function isDone() {
        return !!localStorage.getItem(SK_SIGNAL);
    }

    // 清理存储
    function clearAllStorage() {
        localStorage.removeItem(SK_EXPERTS);
        localStorage.removeItem(SK_IDCARDS);
        localStorage.removeItem(SK_USERIDS);
        localStorage.removeItem(SK_SIGNAL);
        localStorage.removeItem('tm_v24_relations');
        localStorage.removeItem('tm_v24_current_project');
        localStorage.removeItem('tm_v24_active_project_context');
    }
"""

# Part 2: recordProjectRelation, isListPage, processResponse, fetchExpertDetails
# 这里的 relations.push 里移除了项目编号、评标房间号、招标代理机构
part2 = """    // 记录当前项目与专家的关联关系
    function recordProjectRelation(userId, userName, idCard) {
        try {
            let activeProjStr = localStorage.getItem('tm_v24_active_project_context');
            if (!activeProjStr) return;
            let proj = JSON.parse(activeProjStr);

            let ex = window.allExpertsData.find(x => x.userId === userId);
            if (!userName) {
                userName = ex ? ex.name : '未知专家';
            }
            if (!idCard) {
                idCard = ex ? ex.idCard : '';
            }

            let relations = [];
            let rStr = localStorage.getItem('tm_v24_relations');
            if (rStr) relations = JSON.parse(rStr);

            let exists = relations.some(x => x.projectName === proj.projectName && x.expertId === userId);
            if (!exists) {
                relations.push({
                    projectName: proj.projectName,
                    processTime: proj.processTime,
                    handlerName: proj.handlerName,
                    handlerDept: proj.handlerDept,
                    projectNameFromAPI: proj.projectNameFromAPI || '',
                    packageCode: proj.packageCode || '',
                    packageId: proj.packageId || '',
                    expertId: userId,
                    expertName: userName,
                    idCard: idCard
                });
                localStorage.setItem('tm_v24_relations', JSON.stringify(relations));
            }
        } catch(e) {}
    }

    function isListPage() {
        return location.hash.includes('handleList');
    }

    // ==================== V10 原版拦截器（带 Token 截获逻辑） ====================
    function processResponse(url, text) {
        try {
            if (!url.includes('roomInfo')) return;

            // 预处理防止 BigInt 精度丢失：将超长裸数字包装成字符串
            let safeText = text.replace(/:\\s*(\\d{15,})\\b/g, ': "$1"');
            const jsonObj = JSON.parse(safeText);

            let rData = jsonObj.data || jsonObj;
            let packageCode = rData.packageCode || rData.projectCode || rData.tenderProjectCode || '';
            let packageId = rData.packageId || rData.bidSectionCode || rData.bidSectionId || '';

            // 从 localStorage 读取当前正在点击的项目属性
            let curProjStr = localStorage.getItem('tm_v24_current_project');
            let curProj = curProjStr ? JSON.parse(curProjStr) : {};

            let projectNameFromAPI = rData.projectName || rData.tenderProjectName || rData.bidSectionName || '';

            let projectContext = {
                projectName: curProj.projectName || '',
                processTime: curProj.processTime || '',
                handlerName: curProj.handlerName || '',
                handlerDept: curProj.handlerDept || '',
                projectNameFromAPI: projectNameFromAPI,
                packageCode: packageCode,
                packageId: packageId
            };
            localStorage.setItem('tm_v24_active_project_context', JSON.stringify(projectContext));

            let userJsonStr = (jsonObj.data && jsonObj.data.userJson) ? jsonObj.data.userJson : jsonObj.userJson;
            let userJsonObj = null;

            if (typeof userJsonStr === 'string') {
                try { userJsonObj = JSON.parse(userJsonStr); } catch(e) {}
            } else {
                userJsonObj = userJsonStr;
            }

            let targetStr = '';
            if (userJsonObj) {
                if (userJsonObj.params && userJsonObj.params.arrUserNameandRight) {
                    targetStr = userJsonObj.params.arrUserNameandRight;
                } else if (userJsonObj.arrUserNameandRight) {
                    targetStr = userJsonObj.arrUserNameandRight;
                }
            }
            if (!targetStr && jsonObj.data && jsonObj.data.arrUserNameandRight) {
                targetStr = jsonObj.data.arrUserNameandRight;
            }

            let userIds = [];
            if (typeof targetStr === 'string') {
                let matches = targetStr.match(/\\d{16,20}/g);
                if (matches) userIds = Array.from(new Set(matches));
            }

            if (userIds.length > 0) {
                loadState();
                window.__pendingExpertRequests = userIds.length;
                userIds.forEach(uid => fetchExpertDetails(uid));
            } else {
                signalDone();
            }
        } catch(e) {
            signalDone();
        }
    }

    // ==================== 专家获取（增加身份证去重与专业加载） ====================
    async function fetchExpertDetails(userId) {
        if (!userId || processedUserIds.has(userId)) {
            // 即使该专家在全局中已被拉取并去重，但由于他属于当前项目，依然需要记录绑定关系
            recordProjectRelation(userId);
            
            window.__pendingExpertRequests--;
            if (window.__pendingExpertRequests <= 0) {
                saveState();
                signalDone();
            }
            return;
        }
        processedUserIds.add(userId);

        try {
            let res = await fetch('/ebidding/api/ess/lib/experts/getExpertsBaseInfo?userId=' + userId);
            let json = await res.json();
            let data = json.data || json.expert || json;

            if (json.msg === "暂无承载数据" || (!data.expertName && !data.name && !data.realName)) {
                return;
            }

            // 身份证去重
            let idCard = data.idCard || data.idNumber || data.idCode || "";
            let alreadyExists = false;
            if (idCard && processedIdCards.has(idCard)) {
                alreadyExists = true;
            }
            if (idCard) processedIdCards.add(idCard);

            // 获取专业信息
            let profStr = "";
            let libExpertsId = data.libExpertsId || data.id;
            if (libExpertsId && !alreadyExists) {
                try {
                    let profRes = await fetch('/ebidding/api/ess/lib/experts/view?libexpertsId=' + libExpertsId);
                    let profJson = await profRes.json();
                    let pList = (profJson.data && profJson.data.professionsList) || [];
                    if (pList && pList.length > 0) profStr = pList.map(p => p.professionName).filter(Boolean).join('，');
                } catch(err) {}
            }

            let name = data.name || data.expertName || data.realName || data.userName || '';
            let expertData = {
                userId: userId,
                name: name,
                phone: data.telephone || data.officePhone || data.phone || data.mobile || data.contactPhone,
                idCard: idCard,
                unit: data.organizationName || data.unitName || data.company || data.workUnit,
                professions: profStr,
                rawJson: data
            };

            if (!alreadyExists) {
                window.allExpertsData.push(expertData);
            }

            // 记录当前项目与专家的对应关系（同一次 API 响应中直接记录）
            recordProjectRelation(userId, name, idCard);
        } catch(e) {
        } finally {
            window.__pendingExpertRequests--;
            if (window.__pendingExpertRequests <= 0) {
                saveState();
                signalDone();
            }
        }
    }
"""

# Part 3: XHR/Fetch interceptor and getActionButtons helper (merges all layout row cells)
# 这里的正则 \d 都写成了 \\d
part3 = """    // ==================== 安装 XHR/fetch 拦截器（劫持以捕获 Token） ====================
    let capturedToken = '';
    const originalXhrOpen = XMLHttpRequest.prototype.open;
    const originalXhrSend = XMLHttpRequest.prototype.send;
    const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function(method, url) {
        this._reqUrl = url;
        return originalXhrOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.setRequestHeader = function(header, value) {
        if (header.toLowerCase() === 'token' || header.toLowerCase() === 'authorization') {
            let t = value;
            if (t.toLowerCase().startsWith('bearer ')) t = t.substring(7);
            capturedToken = t.trim();
            localStorage.setItem('tm_v24_token', capturedToken);
        }
        return originalSetRequestHeader.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(...args) {
        this.addEventListener('load', function() {
            if (this._reqUrl && this._reqUrl.includes('roomInfo')) {
                processResponse(this._reqUrl, this.responseText);
            }
        });
        return originalXhrSend.apply(this, args);
    };

    const originalFetch = window.fetch;
    window.fetch = async (...args) => {
        let url = (args[0] && args[0].url) || args[0] || '';
        let options = args[1] || {};
        let headers = options.headers;
        if (headers) {
            let t = '';
            if (headers instanceof Headers) {
                t = headers.get('token') || headers.get('Authorization');
            } else if (typeof headers === 'object') {
                t = headers['token'] || headers['Authorization'] || headers['token'] || headers['authorization'];
            }
            if (t) {
                if (t.toLowerCase().startsWith('bearer ')) t = t.substring(7);
                capturedToken = t.trim();
                localStorage.setItem('tm_v24_token', capturedToken);
            }
        }
        const response = await originalFetch(...args);
        if (typeof url === 'string' && url.includes('roomInfo')) {
            response.clone().text().then(text => processResponse(url, text)).catch(e => {});
        }
        return response;
    };

    // ==================== 翻页与操作逻辑 ====================

    function updateProgress(text) {
        let el = document.getElementById('tm-progress-text');
        if (el) el.innerText = text;
    }

    function getActionButtons() {
        // 1. 找到页面上所有的数据 tbody（包括主滚动数据区和固定列数据区）
        let tbodies = Array.from(document.querySelectorAll('tbody, .vxe-table--body, .el-table__body'));
        if (tbodies.length === 0) return [];

        // 2. 提取出每一个 tbody 内部的有效行 tr
        let groups = tbodies.map(tbody => {
            let rows = Array.from(tbody.querySelectorAll('tr, .vxe-body--row'));
            return rows.filter(row => row.querySelectorAll('td, .vxe-cell, .ivu-table-cell, [class*="cell"]').length > 0);
        }).filter(group => group.length > 0);

        if (groups.length === 0) return [];

        // 3. 确定“主数据组”：即列数最多（或单元格最多）的行组，它必定包含“项目类型”这一列
        let mainGroup = groups[0];
        let maxCells = 0;
        groups.forEach(g => {
            let firstRow = g[0];
            let cellCount = firstRow.querySelectorAll('td, .vxe-cell, .ivu-table-cell, [class*="cell"]').length;
            if (cellCount > maxCells) {
                maxCells = cellCount;
                mainGroup = g;
            }
        });

        // 4. 遍历主数据组的每一行，将所有 groups 同一行的单元格合并，以获取最完整的数据，防止固定列滚动导致的项目名称截断丢失
        let buttons = [];
        let rowCount = mainGroup.length;

        for (let k = 0; k < rowCount; k++) {
            // 合并所有 groups 的第 k 行的单元格
            let allRowCells = [];
            for (let g of groups) {
                let row = g[k];
                if (row) {
                    let rowCells = Array.from(row.querySelectorAll('td, .vxe-cell, .ivu-table-cell, [class*="cell"]'));
                    allRowCells.push(...rowCells);
                }
            }

            // 过滤项目类型：包含“评标”字样且长度在 10 个字符以内（防止将项目名称中包含“评标”的长文本误判）
            let isEvaluation = allRowCells.some(cell => {
                let txt = cell.innerText ? cell.innerText.trim() : '';
                return txt.includes('评标') && txt.length <= 10;
            });
            
            if (!isEvaluation) continue;

            // 提取列表页数据：项目名称、处理时间、经办人、经办部门
            let cellTexts = allRowCells.map(td => td.innerText ? td.innerText.trim() : '').filter(Boolean);
            // 简单去重，避免相同单元格被重复遍历
            cellTexts = Array.from(new Set(cellTexts));

            let processTime = cellTexts.find(txt => /\\d{4}-\\d{2}-\\d{2}/.test(txt)) || '';
            let handlerDept = cellTexts.find(txt => (txt.includes('部') || txt.includes('组') || txt.includes('科') || txt.includes('处')) && txt.length <= 10) || '';
            let handlerName = '';
            let chineseNames = cellTexts.filter(txt => /^[\\u4e00-\\u9fa5]{2,3}$/.test(txt) && !['评标', '查看', '办理', '详情', '评审', '定标', '开标', '谈判'].includes(txt));
            if (chineseNames.length > 0) {
                let nameCand = chineseNames.find(n => !n.includes('部') && !n.includes('组') && !n.includes('科') && !n.includes('处'));
                handlerName = nameCand || chineseNames[0];
            }

            // 精准过滤出真正的项目名称（排除时间、姓名、部门和系统固定状态词）
            let projectNameCandidates = cellTexts.filter(txt => {
                if (!txt) return false;
                if (txt === processTime) return false;
                if (txt === handlerDept) return false;
                if (txt === handlerName) return false;
                if (['评标', '查看', '办理', '详情', '评审', '定标', '开标', '谈判', '操作'].includes(txt)) return false;
                if (/\\d{4}-\\d{2}-\\d{2}/.test(txt)) return false;
                return true;
            });

            let projectName = projectNameCandidates.reduce((max, cur) => cur.length > max.length ? cur : max, '');
            // 兜底：如果项目名称为空，但提取出的文本有其他内容，则取最长的
            if (!projectName && cellTexts.length > 0) {
                projectName = cellTexts[0];
            }

            // 在所有行组的第 k 行中，寻找可见且未禁用的操作按钮
            let foundBtn = null;
            for (let g of groups) {
                let row = g[k];
                if (!row) continue;
                let btns = Array.from(row.querySelectorAll('button, a'));
                let targetBtn = btns.find(b => {
                    let text = b.innerText.trim();
                    return text.includes('查看') || text.includes('办理') || text.includes('详情') || text.includes('评审');
                });
                if (targetBtn && targetBtn.offsetParent !== null && !targetBtn.disabled) {
                    foundBtn = targetBtn;
                    break;
                }
            }

            if (foundBtn) {
                foundBtn._projectInfo = {
                    projectName: projectName,
                    processTime: processTime,
                    handlerName: handlerName,
                    handlerDept: handlerDept
                };
                buttons.push(foundBtn);
            }
        }
        return buttons;
    }
"""

# Part 4: Crawling progress loop, JSZip exporter & Tampermonkey HTML UI
# 这里的所有的 ${} 都不加任何转义
part4 = """    // 获取当前表格第一行内容的文本，用于判断翻页数据是否已经加载更新
    function getFirstRowText() {
        let rows = document.querySelectorAll('.el-table__row, .ivu-table-row, .vxe-table--body tr, tr');
        for (let row of rows) {
            let cells = row.querySelectorAll('td');
            if (cells.length > 0) {
                return row.innerText.trim();
            }
        }
        return '';
    }

    // 判断元素或其任意祖先元素是否被禁用，防止因父级禁用而子元素未标记禁用导致的误判
    function isElementDisabled(el) {
        if (!el) return true;
        
        const checkDisabled = (node) => {
            if (!node) return false;
            if (node.disabled) return true;
            
            const classList = node.classList;
            if (classList && (
                classList.contains('disabled') || 
                classList.contains('is-disabled') || 
                classList.contains('is--disabled') || 
                classList.contains('ivu-page-disabled') ||
                classList.contains('ant-pagination-disabled')
            )) {
                return true;
            }
            
            if (node.getAttribute('aria-disabled') === 'true' || 
                node.getAttribute('disabled') !== null) {
                return true;
            }
            
            try {
                let style = window.getComputedStyle(node);
                if (style.pointerEvents === 'none' || style.cursor === 'not-allowed') {
                    return true;
                }
            } catch(e) {}
            
            return false;
        };

        if (checkDisabled(el)) return true;

        let parent = el.parentElement;
        while (parent && parent !== document.body) {
            if (checkDisabled(parent)) return true;
            parent = parent.parentElement;
        }
        return false;
    }

    // 获取当前激活的页码，用于辅助识别翻页状态
    function getCurrentPageNumber() {
        // 尝试从常见的分页激活元素寻找
        let activeEl = document.querySelector('.vxe-pager--num-btn.is--active, .el-pager li.active, .ivu-page-item-active, .ant-pagination-item-active');
        if (activeEl) {
            let num = parseInt(activeEl.innerText.trim());
            if (!isNaN(num)) return num;
        }

        // 尝试从输入框中寻找
        let gotoInput = document.querySelector('input.vxe-pager--goto, .el-pagination__editor input, .ivu-page-options-elevator input');
        if (gotoInput && gotoInput.value) {
            let num = parseInt(gotoInput.value);
            if (!isNaN(num)) return num;
        }

        // 备用：遍历所有数字按钮，看哪个加粗或变蓝了
        let allLinks = document.querySelectorAll('a, span, li, button');
        for (let el of allLinks) {
            let text = el.innerText ? el.innerText.trim() : '';
            if (/^\\d+$/.test(text) && el.offsetParent !== null) {
                let parent = el.closest('.el-pagination, .vxe-pager, .ivu-page, .pagination, [class*="pager"]');
                if (parent) {
                    let cls = el.className || '';
                    let parentCls = (el.parentElement && el.parentElement.className) || '';
                    if (cls.includes('active') || cls.includes('current') || cls.includes('is-active') ||
                        parentCls.includes('active') || parentCls.includes('current') || parentCls.includes('is-active')) {
                        let num = parseInt(text);
                        if (!isNaN(num)) return num;
                    }
                    let style = window.getComputedStyle(el);
                    if (style.fontWeight === 'bold' || style.fontWeight === '700' || style.color === 'rgb(64, 158, 255)' || style.backgroundColor === 'rgb(64, 158, 255)') {
                        let num = parseInt(text);
                        if (!isNaN(num)) return num;
                    }
                }
            }
        }
        return 0; // 无法获取返回0
    }

    // 从页面右上方（含top层级）寻找并解析当前登录的专家姓名
    function getExpertNameFromDOM() {
        let name = '';
        const sanitizeName = (str) => {
            if (!str) return '';
            // 剥离常见的系统前后缀、符号和菜单词汇
            str = str.replace(/欢迎您|欢迎|当前用户|登录用户|当前登录|用户|：|:|丨|\\|/g, '');
            str = str.replace(/\\s+/g, '');
            str = str.replace(/\\(专家\\)|\\（专家\\）|专家/g, '');
            str = str.replace(/退出登录|退出|注销|修改密码|安全退出|系统管理|修改/g, '');
            return str.trim();
        };

        const docs = [document];
        if (window.top && window.top.document) {
            docs.push(window.top.document);
        }

        for (let doc of docs) {
            // 策略 1：经典的用户姓名选择器
            const selectors = [
                '.user-name', '.username', '.nick-name', '.nickname', 
                '.login-user', '.user-info', '.header-right', '.top-right',
                '.el-dropdown-link', '[class*="user-name"]', '[class*="username"]',
                '[class*="userInfo"]', '[class*="user-info"]', '[class*="login-user"]'
            ];
            for (let sel of selectors) {
                let els = doc.querySelectorAll(sel);
                for (let el of els) {
                    if (el && el.offsetParent !== null) {
                        let txt = el.innerText ? el.innerText.trim() : '';
                        if (txt && txt.length < 20 && !txt.includes('修改密码') && !txt.includes('系统管理')) {
                            let clean = sanitizeName(txt);
                            if (clean && clean.length >= 2 && clean.length <= 10) {
                                return clean;
                            }
                        }
                    }
                }
            }

            // 策略 2：基于“欢迎”等常见欢迎词扫描
            let allElements = doc.querySelectorAll('span, a, div, p');
            for (let el of allElements) {
                if (el.offsetParent === null) continue;
                let txt = el.innerText ? el.innerText.trim() : '';
                if (txt && txt.length < 30) {
                    if (txt.includes('欢迎您') || txt.includes('欢迎') || txt.includes('当前用户') || txt.includes('当前登录')) {
                        let clean = sanitizeName(txt);
                        if (clean && clean.length >= 2 && clean.length <= 10) {
                            return clean;
                        }
                    }
                }
            }

            // 策略 3：基于“退出”按钮位置逆向推算
            for (let el of allElements) {
                if (el.offsetParent === null) continue;
                let txt = el.innerText ? el.innerText.trim() : '';
                if (txt === '退出' || txt === '注销' || txt === '安全退出' || txt === '退出登录') {
                    let parent = el.parentElement;
                    if (parent) {
                        let pTxt = parent.innerText ? parent.innerText.trim() : '';
                        let clean = sanitizeName(pTxt);
                        if (clean && clean.length >= 2 && clean.length <= 10) {
                            return clean;
                        }
                    }
                    let prev = el.previousElementSibling;
                    if (prev) {
                        let prevTxt = prev.innerText ? prev.innerText.trim() : '';
                        let clean = sanitizeName(prevTxt);
                        if (clean && clean.length >= 2 && clean.length <= 10) {
                            return clean;
                        }
                    }
                }
            }
        }
        return '';
    }

    // 针对各种分页组件（特别是 vxe-pager 及 Element UI 等）的翻页判定
    function getNextPageButton() {
        // 1. 优先尝试使用常见的分页器“下一页”类名/选择器
        const selectors = [
            '.vxe-pager--next-page',
            '.vxe-pager--next-btn',
            '.btn-next',
            '.ivu-page-next',
            '.ant-pagination-next',
            '[title="下一页"]',
            '[aria-label="Next page"]',
            '[aria-label="下一页"]'
        ];
        
        for (let sel of selectors) {
            let btns = document.querySelectorAll(sel);
            for (let btn of btns) {
                if (btn && btn.offsetParent !== null && !isElementDisabled(btn)) {
                    return btn;
                }
            }
        }

        // 2. 遍历页面所有可见的、未禁用的交互元素，寻找文本/属性包含“下一页”或符合下一页图标特征的
        let allInteractive = document.querySelectorAll('button, a, span, li, i');
        for (let el of allInteractive) {
            if (el.offsetParent === null) continue; // 必须可见
            if (isElementDisabled(el)) continue;

            let text = el.innerText ? el.innerText.trim() : '';
            let title = el.getAttribute('title') || '';
            let className = el.className || '';

            // 如果文字是“下一页”或标题是“下一页”
            if (text === '下一页' || title === '下一页') {
                return el;
            }

            // 特殊匹配 vxe-pager 下一页类名
            if (className.includes('vxe-pager--next-page') || className.includes('vxe-pager--next-btn')) {
                return el;
            }
            
            // 如果是下一页的图标，且在分页器内部
            if (className.includes('el-icon-arrow-right') || className.includes('chevron-right') || className.includes('arrow-right')) {
                let parent = el.closest('.el-pagination, .vxe-pager, .ivu-page, .pagination, [class*="pager"]');
                if (parent) {
                    return el;
                }
            }
        }

        // 3. 备用：基于激活页码寻找它的下一页数字按钮
        let allLinks = document.querySelectorAll('a, span, li, button');
        let pageNumbers = [];
        allLinks.forEach(el => {
            let text = el.innerText ? el.innerText.trim() : '';
            if (/^\\d+$/.test(text) && el.offsetParent !== null) {
                let parent = el.closest('.el-pagination, .vxe-pager, .ivu-page, .pagination, [class*="pager"]');
                if (parent) {
                    pageNumbers.push({ el: el, num: parseInt(text) });
                } else {
                    let parentEl = el.parentElement;
                    let grandParent = parentEl ? parentEl.parentElement : null;
                    let areaText = (parentEl ? parentEl.innerText : '') + (grandParent ? grandParent.innerText : '');
                    if (areaText.includes('前往') || areaText.includes('条/页') || areaText.includes('共') ||
                        areaText.includes('条记录') || (parentEl && parentEl.className && parentEl.className.includes('pag'))) {
                        pageNumbers.push({ el: el, num: parseInt(text) });
                    }
                }
            }
        });

        if (pageNumbers.length > 0) {
            let activePage = 1;
            pageNumbers.forEach(p => {
                let cls = p.el.className || '';
                let parentCls = (p.el.parentElement && p.el.parentElement.className) || '';
                if (cls.includes('active') || cls.includes('current') || cls.includes('is-active') ||
                    parentCls.includes('active') || parentCls.includes('current') || parentCls.includes('is-active')) {
                    activePage = p.num;
                }
                let style = window.getComputedStyle(p.el);
                if (style.fontWeight === 'bold' || style.fontWeight === '700' || style.color === 'rgb(64, 158, 255)' || style.backgroundColor === 'rgb(64, 158, 255)') {
                    activePage = p.num;
                }
            });

            let nextPage = pageNumbers.find(p => p.num === activePage + 1);
            if (nextPage) return nextPage.el;
        }

        // 4. 备用：基于跳转输入框强制递增并回车翻页
        let allText = document.body.innerText;
        if (allText.includes('前往')) {
            let inputs = document.querySelectorAll('input[type="text"], input:not([type]), input.vxe-pager--goto');
            for (let input of inputs) {
                let parent = input.parentElement;
                let parentText = parent ? parent.innerText : '';
                let isGotoInput = input.classList.contains('vxe-pager--goto') || (parentText.includes('前往') && parentText.includes('页'));
                if (isGotoInput) {
                    let currentVal = parseInt(input.value) || 1;
                    let totalMatch = allText.match(/共\\s*(\\d+)\\s*(?:条|记录)/);
                    let sizeMatch = allText.match(/(\\d+)\\s*条\\/页/);
                    if (totalMatch && sizeMatch) {
                        let total = parseInt(totalMatch[1]);
                        let size = parseInt(sizeMatch[1]);
                        let totalPages = Math.ceil(total / size);
                        if (currentVal < totalPages) {
                            return {
                                _isJumpInput: true,
                                _input: input,
                                _nextPage: currentVal + 1,
                                click: function() {
                                    let nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                    nativeInputValueSetter.call(this._input, String(this._nextPage));
                                    this._input.dispatchEvent(new Event('input', { bubbles: true }));
                                    this._input.dispatchEvent(new Event('change', { bubbles: true }));
                                    this._input.dispatchEvent(new Event('keyup', { key: 'Enter', keyCode: 13, bubbles: true }));
                                    this._input.dispatchEvent(new Event('keydown', { key: 'Enter', keyCode: 13, bubbles: true }));
                                    this._input.dispatchEvent(new Event('keypress', { key: 'Enter', keyCode: 13, bubbles: true }));
                                }
                            };
                        }
                    }
                }
            }
        }
        return null;
    }

    // ==================== 核心爬取流程 ====================
    let isCrawling = false;
    let stopCrawl = false;

    async function startCrawling() {
        if (isCrawling) return;
        isCrawling = true;
        stopCrawl = false;
        let projectIndex = 0;
        let currentPage = 1;

        // 读取日期过滤器（可选，为空则不过滤）
        let filterDateStr = (document.getElementById('tm-filter-date') || {}).value || '';
        let filterDate = filterDateStr ? new Date(filterDateStr + 'T00:00:00') : null;
        if (filterDate) {
            updateProgress(`日期过滤已启用：仅遍历 ${filterDateStr} 及之后的项目`);
        }

        window.allExpertsData = [];
        processedIdCards.clear();
        processedUserIds.clear();
        clearAllStorage();
        if (content) content.innerHTML = '';

        document.getElementById('tm-crawl-status').innerText = '🕷️ 运行中';
        document.getElementById('tm-crawl-status').style.color = '#cf1322';
        updateProgress('正在启动，请勿操作鼠标...');

        while (isCrawling && !stopCrawl) {
            let waitLoad = 0;
            let buttons = [];
            let hasRows = false;
            
            while (waitLoad < 10000) {
                let allRows = document.querySelectorAll('.el-table__row, .ivu-table-row, .vxe-table--body tr, .vxe-body--row, tbody tr');
                let realRows = Array.from(allRows).filter(row => row.querySelectorAll('td, .vxe-cell, .ivu-table-cell, [class*="cell"]').length > 0);
                if (realRows.length > 0) {
                    hasRows = true;
                    buttons = getActionButtons();
                    break;
                }
                await sleep(500);
                waitLoad += 500;
            }

            if (!hasRows) {
                updateProgress('未找到任何项目列表行，已结束。');
                break;
            }

            if (buttons.length === 0) {
                updateProgress(`第${currentPage}页无“评标”类型项目，跳过本页...`);
            } else {
                updateProgress(`第${currentPage}页共${buttons.length}个“评标”项目，开始遍历...`);

                // 若启用日期过滤，且本页所有评标项目都早于过滤日期，则提前终止遍历
                if (filterDate) {
                    let allOld = buttons.every(btn => {
                        let t = btn._projectInfo && btn._projectInfo.processTime;
                        if (!t) return false;
                        return new Date(t.substring(0, 10) + 'T00:00:00') < filterDate;
                    });
                    if (allOld) {
                        updateProgress(`第${currentPage}页所有项目均早于 ${filterDateStr}，遍历提前结束。`);
                        isCrawling = false;
                        break;
                    }
                }

                for (let i = 0; i < buttons.length; i++) {
                    if (stopCrawl) break;

                    let currentButtons = getActionButtons();
                    if (!currentButtons[i]) break;

                    // 日期过滤：跳过早于指定日期的项目
                    let btn = currentButtons[i];
                    if (filterDate && btn._projectInfo && btn._projectInfo.processTime) {
                        let projDate = new Date(btn._projectInfo.processTime.substring(0, 10) + 'T00:00:00');
                        if (projDate < filterDate) {
                            updateProgress(`跳过（早于过滤日期）：${btn._projectInfo.projectName}`);
                            continue;
                        }
                    }

                    projectIndex++;
                    let countBefore = window.allExpertsData.length;

                    updateProgress(`第${currentPage}页 | 项目${i+1}/${buttons.length} | 总第${projectIndex}个 | 已提取${countBefore}人 | 正在打开...`);

                    clearSignal();
                    if (btn._projectInfo) {
                        localStorage.setItem('tm_v24_current_project', JSON.stringify(btn._projectInfo));
                    } else {
                        localStorage.setItem('tm_v24_current_project', JSON.stringify({
                            projectName: '未知项目',
                            processTime: '',
                            handlerName: '',
                            handlerDept: ''
                        }));
                    }
                    let link = btn.closest('a') || (btn.tagName === 'A' ? btn : null);
                    let href = link ? (link.href || link.getAttribute('href')) : null;

                    let detailWin = null;
                    if (href) {
                        detailWin = window.open(href, '_blank');
                    } else {
                        let origOpen = window.open;
                        window.open = function(url, ...rest) {
                            detailWin = origOpen.call(window, url, '_blank');
                            return detailWin;
                        };
                        btn.click();
                        await sleep(300);
                        window.open = origOpen;
                    }

                    let waitTime = 0;
                    while (!isDone() && waitTime < 25000) {
                        await sleep(500);
                        waitTime += 500;
                    }

                    loadState();

                    if (detailWin) {
                        try { detailWin.close(); } catch(e) {}
                    }

                    let newExperts = window.allExpertsData.slice(countBefore);
                    newExperts.forEach(exp => renderExpert(exp));

                    updateProgress(`第${currentPage}页 | 项目${i+1}/${buttons.length} | 总第${projectIndex}个 | 已提取${window.allExpertsData.length}人`);

                    await sleep(1500);
                }
            }

            if (stopCrawl) break;

            await sleep(500);
            let nextBtn = getNextPageButton();
            if (nextBtn) {
                let oldPageNum = getCurrentPageNumber();
                let oldFirstRowText = getFirstRowText();
                updateProgress(`第${currentPage}页遍历完毕，正在翻到第${currentPage+1}页...`);
                nextBtn.click();
                
                // 等待页面数据加载更新（页码增加，或者首行内容改变）
                let newPageWait = 0;
                let loaded = false;
                while (newPageWait < 8000) {
                    await sleep(500);
                    newPageWait += 500;
                    let currentPageNum = getCurrentPageNumber();
                    let currentFirstRowText = getFirstRowText();
                    let newBtns = getActionButtons();
                    
                    let pageChanged = (currentPageNum === oldPageNum + 1) || (oldPageNum > 0 && currentPageNum > oldPageNum);
                    let textChanged = (currentFirstRowText !== oldFirstRowText && currentFirstRowText !== '');
                    
                    if (newBtns.length > 0 && (pageChanged || textChanged)) {
                        loaded = true;
                        currentPage = (currentPageNum > oldPageNum) ? currentPageNum : (currentPage + 1);
                        break;
                    }
                }
                if (!loaded) {
                    // 若超时仍未检测到数据变动，极可能是因为该按钮实际已被禁用（或点击无效），无需强行继续“假翻页”
                    updateProgress(`未检测到第 ${currentPage + 1} 页数据加载，视为已到末页并终止遍历。`);
                    await sleep(1000);
                    updateProgress(`🎉 完成！共遍历${projectIndex}个项目，提取${window.allExpertsData.length}名专家`);
                    alert(`🎉 全部完成！\n\n共遍历 ${currentPage} 页、${projectIndex} 个项目\n提取 ${window.allExpertsData.length} 名专家（已按身份证去重）\n\n点击【📥 一键打包导出】一次性打包下载 Excel 和专家照片`);
                    isCrawling = false;
                    break;
                }
            } else {
                updateProgress(`🎉 完成！共遍历${projectIndex}个项目，提取${window.allExpertsData.length}名专家`);
                alert(`🎉 全部完成！\n\n共遍历 ${currentPage} 页、${projectIndex} 个项目\n提取 ${window.allExpertsData.length} 名专家（已按身份证去重）\n\n点击【📥 一键打包导出】一次性打包下载 Excel 和专家照片`);
                isCrawling = false;
                break;
            }
        }

        document.getElementById('tm-crawl-status').innerText = '⏸️ 已停止';
        document.getElementById('tm-crawl-status').style.color = '#389e0d';
        isCrawling = false;
    }

    function stopCrawling() {
        stopCrawl = true;
        isCrawling = false;
        document.getElementById('tm-crawl-status').innerText = '⏸️ 已停止';
        document.getElementById('tm-crawl-status').style.color = '#389e0d';
        updateProgress(`已手动中断。共提取 ${window.allExpertsData.length} 名专家。`);
    }

    async function getCurrentUserInfo() {
        try {
            let res = await fetch('/ebidding/api/ess/lib/experts/queryExpertJoinRecord');
            let json = await res.json();
            let list = (json.data) || [];
            if (list.length > 0 && list[0].libexpertsId) {
                let libexpertsId = list[0].libexpertsId;
                let vRes = await fetch('/ebidding/api/ess/lib/experts/view?libexpertsId=' + libexpertsId);
                let vJson = await vRes.json();
                let d = (vJson.data) || {};
                let name = d.name || d.expertName || d.realName || '';
                let phone = d.telephone || d.officePhone || d.phone || d.mobile || d.contactPhone || '';
                if (name) return { name, phone };
            }
        } catch(e) {}
        return { name: '', phone: '' };
    }

    // ==================== 一键导出打包（Excel + 照片） ====================
    async function exportAllBundled() {
        if (!window.allExpertsData || window.allExpertsData.length === 0) {
            alert("暂无获取到专家数据，请先【开始遍历】！");
            return;
        }

        let token = getAuthToken();
        if (!token) {
            alert("未捕获到当前用户的登录Token，请在此页面上任意点击或查询一次，再重新点击此按钮！");
            return;
        }

        updateProgress("正在初始化导出，正在加载 JSZip...");
        let JSZipClass;
        try {
            JSZipClass = await ensureJSZip();
        } catch(e) {
            alert(e.message);
            return;
        }

        let zip = new JSZipClass();

        // 1. 生成 Excel HTML 内容
        let domName = getExpertNameFromDOM();
        let userInfo = await getCurrentUserInfo();
        let expertName = domName || userInfo.name || '专家';
        let fileTag = `${expertName}_${window.allExpertsData.length}人`;
        let dateStr = new Date().toLocaleDateString('zh-CN').replace(/\\//g,'-');
        
        let tableHtml = `<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel" xmlns="http://www.w3.org/TR/REC-html40">
        <head><meta charset="UTF-8">
        <xml><x:ExcelWorkbook><x:ExcelWorksheets><x:ExcelWorksheet>
        <x:Name>专家列表</x:Name>
        <x:WorksheetOptions><x:DefaultRowHeight>15</x:DefaultRowHeight></x:WorksheetOptions>
        </x:ExcelWorksheet></x:ExcelWorksheets></x:ExcelWorkbook></xml>
        </head>
        <body><table border="1" style="border-collapse:collapse;">
        <tr>
            <th>姓名</th><th>标记</th><th>单位</th><th>电话</th><th>身份证</th><th>专业信息</th><th>原始完整JSON数据</th>
        </tr>`;
        window.allExpertsData.forEach(item => {
            let rawStr = (item.rawJson ? JSON.stringify(item.rawJson) : "").replace(/</g, '&lt;').replace(/>/g, '&gt;');
            tableHtml += `<tr>
                <td>${item.name || ""}</td>
                <td></td>
                <td>${item.unit || ""}</td>
                <td>${item.phone || ""}</td>
                <td style="mso-number-format:'\\\\@';">${item.idCard || ""}</td>
                <td>${item.professions || ""}</td>
                <td style="white-space:nowrap; overflow:hidden; max-width:200px;">${rawStr}</td>
            </tr>`;
        });
        tableHtml += `</table></body></html>`;
        let excelBlob = new Blob([tableHtml], { type: "application/vnd.ms-excel" });

        // 将 Excel 文件放入 ZIP 压缩包中
        let excelName = `${fileTag}_${dateStr}.xls`;
        zip.file(excelName, excelBlob);

        // 1.2 生成项目与专家关联的 MD 关系表并塞入 ZIP（按项目聚合展示，已去除空白列，多专家换行并排）
        let relations = [];
        try {
            let rStr = localStorage.getItem('tm_v24_relations');
            if (rStr) relations = JSON.parse(rStr);
        } catch(e) {}

        // 按项目名称进行分组聚合
        let projectGroups = {};
        relations.forEach(item => {
            let key = item.projectName || '未知项目';
            if (!projectGroups[key]) {
                projectGroups[key] = {
                    projectName: key,
                    processTime: item.processTime || '',
                    handlerName: item.handlerName || '',
                    handlerDept: item.handlerDept || '',
                    projectNameFromAPI: item.projectNameFromAPI || '',
                    packageCode: item.packageCode || '',
                    packageId: item.packageId || '',
                    experts: []
                };
            }
            let expertExists = projectGroups[key].experts.some(e => e.expertId === item.expertId);
            if (!expertExists) {
                projectGroups[key].experts.push({
                    expertId: item.expertId || '',
                    expertName: item.expertName || '',
                    idCard: item.idCard || ''
                });
            }
        });

        let mdContent = `# 评标项目与评审专家关系表\n\n`;
        mdContent += `* **生成时间**：${new Date().toLocaleString()}\n`;
        mdContent += `* **导出人（当前登录专家）**：${expertName}\n\n`;
        mdContent += `| 项目名称 | Project name | Project code | Project ID | 处理时间 | 经办人姓名 | 经办人部门 | 评审专家（姓名 / 身份证 / 专家ID） |\n`;
        mdContent += `| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n`;

        Object.values(projectGroups).forEach(proj => {
            let validExperts = proj.experts.filter(e => e.expertName && e.expertName !== "未知专家");
            if (validExperts.length === 0) return;
            let expertsStr = validExperts.map(e => {
                let parts = [e.expertName || ""];
                if (e.idCard) parts.push(e.idCard);
                if (e.expertId) parts.push(e.expertId);
                return parts.join(" / ");
            }).join("<br>");
            mdContent += `| ${proj.projectName} | ${proj.projectNameFromAPI} | ${proj.packageCode} | ${proj.packageId} | ${proj.processTime} | ${proj.handlerName} | ${proj.handlerDept} | ${expertsStr} |\n`;
        });

        let mdBlob = new Blob([mdContent], { type: "text/markdown;charset=utf-8" });
        let mdName = `${fileTag}_${dateStr}.md`;
        zip.file(mdName, mdBlob);

        // 2. 下载并打包专家照片
        let downloadCount = 0;
        let skipCount = 0;
        let errorCount = 0;

        for (let i = 0; i < window.allExpertsData.length; i++) {
            let expert = window.allExpertsData[i];
            let name = expert.name || `未知专家_${i+1}`;
            let phone = expert.phone || '无电话';
            let rawJson = expert.rawJson || {};
            
            // 获取身份证附件文件ID
            let idCardFileStr = rawJson.idCardFile || rawJson.idPhotoFile || '';
            if (!idCardFileStr) {
                skipCount++;
                continue;
            }

            let fileIds = idCardFileStr.split(',').map(x => x.trim()).filter(Boolean);
            if (fileIds.length === 0) {
                skipCount++;
                continue;
            }

            updateProgress(`一键打包进度：正在下载第 ${i+1}/${window.allExpertsData.length} 名专家 [${name}] 的照片...`);

            for (let idx = 0; idx < fileIds.length; idx++) {
                let fileId = fileIds[idx];
                let downloadUrl = `https://dzzb.jnkgjtdzzbgs.com/ebidding/api/base/file/download?fileId=${fileId}&token=${token}`;
                
                try {
                    let res = await fetch(downloadUrl);
                    if (!res.ok) throw new Error("HTTP " + res.status);
                    
                    let blob = await res.blob();
                    let ext = 'jpg';
                    let contentType = res.headers.get('Content-Type') || '';
                    if (contentType.includes('png')) ext = 'png';
                    else if (contentType.includes('gif')) ext = 'gif';
                    else if (contentType.includes('webp')) ext = 'webp';
                    else if (contentType.includes('jpeg')) ext = 'jpg';

                    // 照片以 姓名+手机号 方式保存
                    let fileName = '';
                    let fileBase = `${name}_${phone}`;
                    if (fileIds.length === 1) {
                        fileName = `${fileBase}.${ext}`;
                    } else {
                        fileName = `${fileBase}_${idx + 1}.${ext}`;
                    }

                    zip.file(fileName, blob);
                    downloadCount++;
                } catch(err) {
                    console.error(`下载照片失败 (专家: ${name}, 手机: ${phone}, ID: ${fileId}):`, err);
                    errorCount++;
                }
            }
            // 延迟保护服务器
            await sleep(150);
        }

        updateProgress("正在生成一键打包 ZIP 文件...");
        try {
            let zipBlob = await zip.generateAsync({type: "blob"});
            let zipName = `${fileTag}_${dateStr}.zip`;

            let a = document.createElement("a");
            a.href = URL.createObjectURL(zipBlob);
            a.download = zipName;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);

            updateProgress(`🎉 一键打包成功！包含 1 个 Excel，1 个 MD 关系表，照片成功 ${downloadCount} 张，跳过无照 ${skipCount} 人，失败 ${errorCount} 张`);
            alert(`✅ 一键打包下载成功！\\n\\n文件包含：\\n- Excel 列表: ${excelName}\\n- MD 关系表: ${mdName}\\n- 照片数量: ${downloadCount} 张\\n\\n压缩包名称：${zipName}`);
        } catch(err) {
            updateProgress("打包文件生成失败");
            alert("生成一键打包 ZIP 压缩包失败: " + err.message);
        }
    }

    // ==================== 复制剪贴板 ====================
    function copyToClipboard() {
        if (!window.allExpertsData || window.allExpertsData.length === 0) {
            alert("暂无获取到专家数据可复制！");
            return;
        }
        let tsv = "姓名\\t单位\\t电话\\t身份证\\t专业信息\\t原始完整JSON数据\\n";
        window.allExpertsData.forEach(item => {
            tsv += [item.name||"", item.unit||"", item.phone||"", item.idCard||"", item.professions||"", JSON.stringify(item.rawJson)||""].join("\\t") + "\\n";
        });
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(tsv).then(() => alert(`✅ 已复制 ${window.allExpertsData.length} 名专家数据！`));
        } else {
            let ta = document.createElement("textarea");
            ta.value = tsv;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("Copy");
            ta.remove();
            alert(`✅ 已复制 ${window.allExpertsData.length} 名专家数据！`);
        }
    }

    // ==================== UI 面板 ====================
    function renderExpert(expertObj) {
        if (!content) return;
        const div = document.createElement('div');
        div.style.cssText = 'margin-bottom:12px; padding:12px; border:1px solid #e8e8e8; border-radius:6px; background:#f0f5ff; box-shadow:0 1px 3px rgba(0,0,0,0.05);';
        div.innerHTML = `
            <div style="font-weight:bold; color:#096dd9; font-size:16px; border-bottom:1px solid #91d5ff; padding-bottom:6px; margin-bottom:8px;">
                👤 ${expertObj.name}
            </div>
            <div style="margin-bottom:4px;">🏢 <b>单位:</b> ${expertObj.unit || '无'}</div>
            <div style="margin-bottom:4px;">📞 <b>电话:</b> <span style="color:#cf1322;font-weight:bold;">${expertObj.phone || '无'}</span></div>
            <div style="margin-bottom:4px;">💳 <b>身份证:</b> ${expertObj.idCard || '无'}</div>
            <div style="margin-bottom:4px;">🎓 <b>专业:</b> ${expertObj.professions || '无'}</div>
        `;
        if (content.firstChild) content.insertBefore(div, content.firstChild);
        else content.appendChild(div);
    }

    function initUI() {
        if (!isListPage()) return;
        if (document.getElementById('tm-expert-spider-panel')) return;
        if (!document.body) { setTimeout(initUI, 200); return; }

        panel = document.createElement('div');
        panel.id = 'tm-expert-spider-panel';
        panel.style.cssText = `
            position: fixed; left: 20px; top: 50%; transform: translateY(-50%);
            width: 420px; max-height: 85vh;
            background: #fff; border: 2px solid #096dd9;
            border-radius: 10px; box-shadow: 4px 0 24px rgba(0,0,0,0.25);
            z-index: 2147483647; font-family: 'Microsoft YaHei', sans-serif;
            display: flex; flex-direction: column; transition: width 0.2s;
        `;

        let collapsed = false;

        const header = document.createElement('div');
        header.style.cssText = `
            background: #096dd9; color: #fff; padding: 10px 14px; font-weight: bold;
            display: flex; justify-content: space-between; align-items: center; font-size: 15px;
            border-radius: 8px 8px 0 0; cursor: default; flex-shrink: 0;
        `;
        header.innerHTML = `
            <span>🕷️ 专家提取器 V24 <span id="tm-crawl-status" style="font-size:11px; margin-left:8px; background:#fff; color:#389e0d; padding:2px 6px; border-radius:4px;">⏸️ 就绪</span></span>
            <div style="display:flex;gap:6px;">
                <button id="tm-collapse-btn" title="折叠/展开" style="background:rgba(255,255,255,0.2);border:none;color:#fff;cursor:pointer;font-size:15px;padding:2px 8px;border-radius:4px;line-height:1;">▲</button>
                <button id="tm-close-btn" title="隐藏" style="background:rgba(255,255,255,0.2);border:none;color:#fff;cursor:pointer;font-size:15px;padding:2px 8px;border-radius:4px;line-height:1;">✖</button>
            </div>
        `;
        panel.appendChild(header);

        const collapseArea = document.createElement('div');
        collapseArea.id = 'tm-collapse-area';
        collapseArea.style.cssText = 'display:flex; flex-direction:column; overflow:hidden; flex:1; min-height:0;';

        const prog = document.createElement('div');
        prog.style.cssText = 'padding: 7px 14px; background: #fffbe6; color: #d46b08; font-size: 12px; font-weight: bold; border-bottom: 1px solid #ffe58f; flex-shrink:0;';
        prog.innerHTML = `📊 进度：<span id="tm-progress-text">点击【开始遍历】启动</span>`;
        collapseArea.appendChild(prog);

        const toolbar = document.createElement('div');
        toolbar.style.cssText = 'padding: 8px 10px; background: #e6f7ff; border-bottom: 1px solid #91d5ff; display: flex; flex-wrap: wrap; gap: 7px; flex-shrink:0;';
        toolbar.innerHTML = `
            <div style="width:100%;display:flex;align-items:center;gap:6px;font-size:12px;margin-bottom:2px;">
                <label for="tm-filter-date" style="white-space:nowrap;color:#555;font-weight:bold;">📅 起始日期</label>
                <input type="date" id="tm-filter-date" style="flex:1;padding:3px 6px;border:1px solid #91d5ff;border-radius:4px;font-size:12px;" title="留空则遍历所有项目；填写后只遍历该日期及之后的项目">
                <button id="tm-clear-date" style="background:#d9d9d9;color:#555;border:none;padding:3px 7px;border-radius:4px;cursor:pointer;font-size:12px;" title="清空日期，遍历全部">清空</button>
            </div>
            <button id="tm-start" style="background:#52c41a;color:#fff;border:none;padding:5px 11px;border-radius:4px;cursor:pointer;font-weight:bold;font-size:13px;">▶️ 开始遍历</button>
            <button id="tm-stop" style="background:#f5222d;color:#fff;border:none;padding:5px 11px;border-radius:4px;cursor:pointer;font-weight:bold;font-size:13px;">⏹ 停止</button>
            <button id="tm-export-all" style="background:#1890ff;color:#fff;border:none;padding:5px 11px;border-radius:4px;cursor:pointer;font-weight:bold;font-size:13px;">📥 一键打包导出</button>
            <button id="tm-copy" style="background:#fa8c16;color:#fff;border:none;padding:5px 11px;border-radius:4px;cursor:pointer;font-weight:bold;font-size:13px;">📋 复制</button>
        `;
        collapseArea.appendChild(toolbar);

        content = document.createElement('div');
        content.style.cssText = 'padding: 12px; font-size: 13px; line-height: 1.6; color: #333; display: flex; flex-direction: column; overflow-y: auto; flex:1;';
        collapseArea.appendChild(content);

        panel.appendChild(collapseArea);
        document.body.appendChild(panel);

        document.getElementById('tm-collapse-btn').addEventListener('click', () => {
            collapsed = !collapsed;
            collapseArea.style.display = collapsed ? 'none' : 'flex';
            document.getElementById('tm-collapse-btn').innerText = collapsed ? '▼' : '▲';
            panel.style.borderRadius = collapsed ? '10px' : '10px 10px 0 0';
        });

        document.getElementById('tm-close-btn').addEventListener('click', () => panel.style.display = 'none');
        document.getElementById('tm-clear-date').addEventListener('click', () => { document.getElementById('tm-filter-date').value = ''; });
        document.getElementById('tm-start').addEventListener('click', startCrawling);
        document.getElementById('tm-stop').addEventListener('click', stopCrawling);
        document.getElementById('tm-export-all').addEventListener('click', exportAllBundled);
        document.getElementById('tm-copy').addEventListener('click', copyToClipboard);

        uiInitialized = true;
    }

    window.addEventListener('load', initUI);
    setTimeout(initUI, 2000);

})();
"""

final_code = part1 + "\n" + part2 + "\n" + part3 + "\n" + part4

# 写入，不调用 .replace('\n', '\r\n') 而是由 python 智能处理换行符。并且我们在 write() 中写回为 utf-8-sig (带BOM)
with open(target_path, 'w', encoding='utf-8-sig') as f:
    f.write(final_code)

print("SUCCESS: Patch script executed successfully!")
