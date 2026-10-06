// ==UserScript==
// @name         HDSky 掷骰子投注助手
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  在论坛列表页每个帖子显示投注汇总和投注按钮
// @match        https://hdsky.me/forums.php?action=viewforum&forumid=71
// @match        https://hdsky.me/messages.php*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    // 投注类型
    const BET_TYPES = ['豹子', '顺子', '大', '小'];

    // 数据面板：扫描全站主题时两次请求间的短延迟，缓解论坛限流
    const SCAN_FETCH_DELAY_MS = 300;
    // 一键大小的第二注按此节奏重试，论坛限制同一用户连续发帖的间隔
    const SECOND_BET_FIRST_DELAY_MS = 3000;
    const SECOND_BET_RETRY_MS = 10000;
    const SECOND_BET_MAX_ATTEMPTS = 12;
    const STATS_PANEL_COLLAPSED_KEY = 'hdsky-dice-stats-panel:collapsed';
    const STATS_PANEL_ID = 'hdsky-dice-stats-panel';

    // 清理面板：批量删除每批 POST 之间的延迟
    const BATCH_DELAY_MS = 1500;
    const CLEAN_PANEL_COLLAPSED_KEY = 'hdsky-clean-panel:collapsed';
    const CLEAN_PANEL_ID = 'hdsky-clean-panel';
    const CLEAN_BATCH_SIZE = 50;

    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

    // 获取当前用户名
    function getCurrentUsername() {
        const userLink = document.querySelector('a[class="User_Name"] b, a[class="SysOp_Name"] b, a[class="Administrator_Name"] b, a[class="NexusMaster_Name"] b, a[class="Moderator_Name"] b, a[class="VIP_Name"] b');
        return userLink ? userLink.textContent.trim() : null;
    }

    // 解析投注内容
    function parseBet(bodyText) {
        for (const type of BET_TYPES) {
            if (bodyText.startsWith(type + ' ')) {
                const amount = parseInt(bodyText.replace(type + ' ', ''), 10);
                if (!isNaN(amount)) {
                    return { type, amount };
                }
            }
        }
        return null;
    }

    // 成功回帖会返回重定向；浏览器对手动重定向可能暴露为 opaqueredirect
    function isSuccessfulBetResponse(response) {
        return response.status === 302 || response.type === 'opaqueredirect';
    }

    // 论坛拒绝发帖时不重定向，而是用 200 渲染错误页，拒绝原因只能从页面文案里取
    async function readForumRejection(response) {
        const html = await response.text().catch(() => '');
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const node = doc.querySelector('td.text') || doc.querySelector('title');
        const text = node ? node.textContent.replace(/\s+/g, ' ').trim() : '';
        return text ? text.slice(0, 120) : '状态 ' + response.status;
    }

    // 获取帖子的总页数（从解析的HTML中）
    function getTotalPagesFromDoc(doc) {
        // 查找所有包含 page= 的链接
        const allPageLinks = doc.querySelectorAll('a[href*="page="]');
        console.log('所有page链接数:', allPageLinks.length);
        let maxPage = 0;
        allPageLinks.forEach(link => {
            const href = link.getAttribute('href');
            const match = href.match(/page=(\d+)/);
            if (match) {
                const page = parseInt(match[1], 10);
                console.log('找到page链接:', href, 'page:', page);
                if (page > maxPage) maxPage = page;
            }
        });
        return maxPage;
    }

    // 获取某个主题的投注汇总
    async function getBetSummaryForTopic(topicid, username) {
        if (!username) return null;

        const summary = { '豹子': 0, '顺子': 0, '大': 0, '小': 0 };

        // 先获取第一页来确定总页数
        const baseUrl = `https://hdsky.me/forums.php?action=viewtopic&forumid=71&topicid=${topicid}`;
        try {
            const response = await fetch(baseUrl);
            const html = await response.text();
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, 'text/html');

            const totalPages = getTotalPagesFromDoc(doc);
            console.log('总页数:', totalPages);

            // 解析第一页
            parsePostsFromDoc(doc, username, summary, topicid);

            // 解析剩余页面
            for (let page = 1; page <= totalPages; page++) {
                const pageUrl = `${baseUrl}&page=${page}`;
                const pageResponse = await fetch(pageUrl);
                const pageHtml = await pageResponse.text();
                const pageDoc = parser.parseFromString(pageHtml, 'text/html');
                parsePostsFromDoc(pageDoc, username, summary, topicid);
            }
        } catch (e) {
            console.error('获取主题失败:', topicid, e);
            return null;
        }

        const total = Object.values(summary).reduce((a, b) => a + b, 0);
        if (total === 0) return null;

        return summary;
    }

    // 从文档中解析用户的投注
    function parsePostsFromDoc(doc, username, summary, topicid) {
        // 直接查找所有帖子内容 div
        const bodyDivs = doc.querySelectorAll('div[id$="body"]');

        bodyDivs.forEach((bodyDiv) => {
            const bodyId = bodyDiv.id.replace('body', '');
            const headerTable = doc.querySelector(`table#${bodyId}`);
            if (!headerTable) return;

            const userLink = headerTable.querySelector('a[class$="_Name"] > b');
            const postUsername = userLink ? userLink.textContent.trim() : '';

            if (postUsername === username) {
                const betText = bodyDiv.textContent.trim();
                const bet = parseBet(betText);
                if (bet) {
                    summary[bet.type] += bet.amount;
                }
            }
        });
    }

    // 论坛页面：添加投注按钮和汇总
    async function addBetButtons() {
        const username = getCurrentUsername();
        if (!username) {
            return;
        }

        const links = document.querySelectorAll('a');

        for (const link of links) {
            if (!link.textContent.includes('本轮开奖时间')) continue;

            const rowTr = link.closest('tr');
            if (!rowTr) continue;

            const img = rowTr.querySelector('img[class^="locked"]');
            if (img) continue;

            const href = link.getAttribute('href');
            const topicidMatch = href.match(/topicid=(\d+)/);
            if (!topicidMatch) continue;
            const topicid = topicidMatch[1];

            // 创建汇总显示
            const summarySpan = document.createElement('span');
            summarySpan.className = 'bet-summary';
            summarySpan.style.marginRight = '5px';
            summarySpan.style.color = '#666';
            summarySpan.style.fontSize = '12px';
            summarySpan.textContent = '加载中...';

            // 创建容器
            const container = document.createElement('span');
            container.className = 'bet-container';
            container.style.marginLeft = '5px';
            container.style.display = 'inline-flex';
            container.style.alignItems = 'center';
            container.style.gap = '5px';
            container.style.whiteSpace = 'nowrap';

            const input = document.createElement('input');
            input.type = 'number';
            input.min = '100';
            input.max = '100000';
            input.value = '100';
            input.style.width = '70px';
            input.style.padding = '2px 5px';
            input.style.border = '1px solid #ccc';
            input.style.borderRadius = '3px';
            input.style.fontSize = '12px';

            const submitBet = (btnName, amount) => {
                const body = encodeURIComponent(btnName + ' ' + amount);
                const formData = `id=${topicid}&type=reply&body=${body}`;

                return fetch('https://hdsky.me/forums.php?action=post', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                    },
                    body: formData,
                    redirect: 'manual'
                }).then(async response => {
                    if (isSuccessfulBetResponse(response)) return;
                    const error = new Error('论坛未确认投注（' + await readForumRejection(response) + '）');
                    // 只有论坛明确渲染了拒绝页才可安全重试；其他情况服务端可能已经收单
                    error.rejectedByForum = response.status === 200;
                    throw error;
                });
            };

            const submitBetUntilAccepted = async (btnName, amount, onWait) => {
                for (let attempt = 1; ; attempt++) {
                    onWait(attempt);
                    await sleep(attempt === 1 ? SECOND_BET_FIRST_DELAY_MS : SECOND_BET_RETRY_MS);
                    try {
                        await submitBet(btnName, amount);
                        return;
                    } catch (error) {
                        if (!error.rejectedByForum || attempt >= SECOND_BET_MAX_ATTEMPTS) throw error;
                    }
                }
            };

            const refreshSummary = () => {
                summarySpan.textContent = '加载中...';
                return getBetSummaryForTopic(topicid, username).then(summary => {
                    if (summary) {
                        let text = '';
                        BET_TYPES.forEach(type => {
                            if (summary[type] > 0) {
                                text += type + ':' + summary[type] + ' ';
                            }
                        });
                        const total = Object.values(summary).reduce((a, b) => a + b, 0);
                        summarySpan.textContent = '(' + text.trim() + ' 共' + total + ')';
                    } else {
                        summarySpan.textContent = '(无投注)';
                    }
                });
            };

            BET_TYPES.forEach(btnName => {
                const btn = document.createElement('button');
                btn.textContent = btnName;
                btn.style.padding = '2px 8px';
                btn.style.cursor = 'pointer';
                btn.style.border = '1px solid #4a90d9';
                btn.style.borderRadius = '3px';
                btn.style.backgroundColor = '#4a90d9';
                btn.style.color = 'white';
                btn.style.fontSize = '12px';
                btn.style.marginRight = '3px';

                btn.addEventListener('click', () => {
                    const amount = input.value;
                    if (amount < 100 || amount > 100000) {
                        alert('请输入100-100000之间的数字');
                        return;
                    }

                    submitBet(btnName, amount)
                        .then(() => {
                            refreshSummary();
                            alert('投注成功: ' + btnName + ' ' + amount);
                        })
                        .catch(error => alert('投注请求出错: ' + error.message));
                });

                container.appendChild(btn);
            });

            const comboBtn = document.createElement('button');
            comboBtn.textContent = '一键大小';
            comboBtn.title = '先下注大，再自动下注小；论坛限制发帖间隔时会自动重试';
            comboBtn.style.padding = '2px 8px';
            comboBtn.style.cursor = 'pointer';
            comboBtn.style.border = '1px solid #d97706';
            comboBtn.style.borderRadius = '3px';
            comboBtn.style.backgroundColor = '#f59e0b';
            comboBtn.style.color = 'white';
            comboBtn.style.fontSize = '12px';
            comboBtn.style.marginRight = '3px';

            comboBtn.addEventListener('click', async () => {
                const amount = input.value;
                if (amount < 100 || amount > 100000) {
                    alert('请输入100-100000之间的数字');
                    return;
                }

                comboBtn.disabled = true;
                comboBtn.style.cursor = 'wait';
                comboBtn.textContent = '下注中...';
                let bigPlaced = false;
                try {
                    await submitBet('大', amount);
                    bigPlaced = true;
                    await submitBetUntilAccepted('小', amount, attempt => {
                        comboBtn.textContent = '等待下注小 ' + attempt + '/' + SECOND_BET_MAX_ATTEMPTS;
                    });
                    alert('投注成功: 大 ' + amount + '，小 ' + amount);
                } catch (error) {
                    alert((bigPlaced ? '大已下注，但小投注失败: ' : '一键大小投注失败，大未下注: ') + error.message);
                } finally {
                    if (bigPlaced) refreshSummary();
                    comboBtn.disabled = false;
                    comboBtn.style.cursor = 'pointer';
                    comboBtn.textContent = '一键大小';
                }
            });

            container.appendChild(comboBtn);

            container.insertBefore(input, container.firstChild);

            // 插入位置
            const multipageSpan = rowTr.querySelector('span img[class="multipage"]');

            if (multipageSpan) {
                const insertAfterEl = multipageSpan.parentElement;
                if (insertAfterEl && insertAfterEl.nextSibling) {
                    insertAfterEl.parentNode.insertBefore(summarySpan, insertAfterEl.nextSibling);
                    insertAfterEl.parentNode.insertBefore(container, summarySpan.nextSibling);
                }
            } else {
                link.parentNode.insertBefore(summarySpan, link.nextSibling);
                link.parentNode.insertBefore(container, summarySpan.nextSibling);
            }

            // 获取汇总
            getBetSummaryForTopic(topicid, username).then(summary => {
                if (summary) {
                    let text = '';
                    BET_TYPES.forEach(type => {
                        if (summary[type] > 0) {
                            text += type + ':' + summary[type] + ' ';
                        }
                    });
                    const total = Object.values(summary).reduce((a, b) => a + b, 0);
                    summarySpan.textContent = '(' + text.trim() + ' 共' + total + ')';
                } else {
                    summarySpan.textContent = '(无投注)';
                }
            });
        }
    }

    // 从"本轮开奖时间…【 小 5,2,1 】"链接文本解析本轮开奖结果类型；未开奖返回 null
    function parseDrawResult(text) {
        const match = text.match(/【\s*(豹子|顺子|大|小)/);
        return match ? match[1] : null;
    }

    // 从一个列表页文档按 DOM 顺序（最新在前）收集每轮开奖结果；用 seen(topicid) 去重。
    // 每条形如 { topicid, 豹子:false, 顺子:false, 大:false, 小:true }，该轮开奖的类型为 true。
    function collectDrawResults(doc, seen, results) {
        doc.querySelectorAll('a').forEach(link => {
            const text = link.textContent;
            if (!text.includes('本轮开奖时间')) return;
            const href = link.getAttribute('href') || '';
            const idMatch = href.match(/topicid=(\d+)/);
            if (!idMatch) return;
            const id = idMatch[1];
            if (seen.has(id)) return;
            seen.add(id);
            const type = parseDrawResult(text);
            if (!type) return; // 未开奖的轮次没有结果，跳过
            const row = { topicid: id };
            BET_TYPES.forEach(t => { row[t] = (t === type); });
            results.push(row);
        });
    }

    // 抓取并解析一个 URL 为 HTML 文档（带超时，避免服务器无响应时永久挂起）
    async function fetchDoc(url) {
        const controller = new AbortController();
        const tid = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, { signal: controller.signal });
            const html = await response.text();
            return new DOMParser().parseFromString(html, 'text/html');
        } finally {
            clearTimeout(tid);
        }
    }

    // 获取列表页的总分页数（只统计 viewforum 分页链接，避免误取主题的多页链接）
    function getListTotalPages(doc) {
        let maxPage = 0;
        doc.querySelectorAll('a[href*="action=viewforum"]').forEach(link => {
            const href = link.getAttribute('href') || '';
            const match = href.match(/page=(\d+)/);
            if (match) {
                const page = parseInt(match[1], 10);
                if (page > maxPage) maxPage = page;
            }
        });
        return maxPage;
    }

    // 遍历列表页全部分页，直接从每条主题的"本轮开奖时间…【类型…】"链接读取开奖结果。
    // 返回按列表顺序（最新在前）的每轮结果数组；未开奖或抓取失败的轮次被跳过，整体不抛错。
    async function scanAllTopicsBetStats(onProgress, shouldStop) {
        // 用户可随时暂停：每翻一页前检查，命中即返回已收集的部分结果。
        const stopped = () => Boolean(shouldStop && shouldStop());
        const seen = new Set();
        const results = [];
        // 干旱度只看每种类型最近一次出现的位置：四种都出现过后，更老的分页不会再改变任何数字，可提前停止扫描。
        const foundAll = () => BET_TYPES.every(type => results.some(r => r[type]));

        // 当前列表页即第一页，直接复用已加载的 document（无需请求）
        collectDrawResults(document, seen, results);
        const listPages = getListTotalPages(document);
        if (onProgress) onProgress(0, listPages);
        for (let page = 1; page <= listPages && !stopped() && !foundAll(); page++) {
            await sleep(SCAN_FETCH_DELAY_MS);
            try {
                const listDoc = await fetchDoc(`https://hdsky.me/forums.php?action=viewforum&forumid=71&page=${page}`);
                collectDrawResults(listDoc, seen, results);
            } catch (e) {
                console.error('获取列表分页失败:', page, e);
            }
            if (onProgress) onProgress(page, listPages);
        }
        return results;
    }

    // 从最新一轮起，对每种类型连续累加"未出现"直到第一次出现为止；
    // 全程未出现的类型返回 topicStats 总长度。
    function computeMissedRounds(topicStats) {
        const result = {};
        BET_TYPES.forEach(type => {
            const i = topicStats.findIndex(stat => stat[type]);
            result[type] = i === -1 ? topicStats.length : i;
        });
        return result;
    }

    // 从站内信列表 HTML 中按 DOM 顺序去重收集"管理员在您的帖子#…评分"主题的 message id
    function collectRatingIds(doc, seen, ids) {
        doc.querySelectorAll('td.rowfollow a[href*="action=viewmessage"]').forEach(link => {
            const text = link.textContent.trim();
            if (!/^管理员在您的帖子#\d+评分$/.test(text)) return;
            const href = link.getAttribute('href') || '';
            const m = href.match(/[?&]id=(\d+)/);
            if (!m) return;
            const id = m[1];
            if (seen.has(id)) return;
            seen.add(id);
            ids.push(id);
        });
    }

    // 站内信列表分页数：仅统计 viewmailbox 的 page= 链接（与 getListTotalPages 仅 forum 不同）
    function getMessagesTotalPages(doc) {
        let maxPage = 0;
        doc.querySelectorAll('a[href*="action=viewmailbox"][href*="page="]').forEach(link => {
            const href = link.getAttribute('href') || '';
            const match = href.match(/page=(\d+)/);
            if (match) {
                const page = parseInt(match[1], 10);
                if (page > maxPage) maxPage = page;
            }
        });
        return maxPage;
    }

    // 翻页扫描收件箱全部管理员评分通知；返回按页顺序的 id 数组
    async function scanRatingMessages(onProgress, shouldStop) {
        const stopped = () => Boolean(shouldStop && shouldStop());
        const seen = new Set();
        const ids = [];

        // 当前页面即 page=0，直接复用已加载 document
        collectRatingIds(document, seen, ids);
        const totalPages = getMessagesTotalPages(document);
        if (onProgress) onProgress(0, totalPages);
        for (let page = 1; page <= totalPages && !stopped(); page++) {
            await sleep(SCAN_FETCH_DELAY_MS);
            try {
                const listDoc = await fetchDoc(`https://hdsky.me/messages.php?action=viewmailbox&box=1&page=${page}`);
                collectRatingIds(listDoc, seen, ids);
            } catch (e) {
                console.error('获取站内信分页失败:', page, e);
            }
            if (onProgress) onProgress(page, totalPages);
        }
        return ids;
    }

    // 按 CLEAN_BATCH_SIZE 一批 POST 删除；失败批次 id 保留便于重试
    async function deleteRatingMessages(ids, onProgress) {
        const ok = [];
        const failed = [];
        const totalBatches = Math.ceil(ids.length / CLEAN_BATCH_SIZE);
        let doneBatches = 0;
        for (let i = 0; i < ids.length; i += CLEAN_BATCH_SIZE) {
            const batch = ids.slice(i, i + CLEAN_BATCH_SIZE);
            const params = new URLSearchParams();
            params.append('action', 'moveordel');
            batch.forEach(id => params.append('messages[]', id));
            params.append('delete', '删除');
            params.append('box', '1');
            try {
                const response = await fetch('https://hdsky.me/messages.php', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: params.toString(),
                    redirect: 'manual'
                });
                // 论坛对成功的删除操作返回 302 或 opaqueredirect；其他视为该批失败。
                if (response.status === 302 || response.type === 'opaqueredirect') {
                    ok.push(...batch);
                } else {
                    console.error('批量删除被论坛拒绝,状态:', response.status);
                    failed.push(...batch);
                }
            } catch (e) {
                console.error('批量删除请求出错:', e);
                failed.push(...batch);
            }
            doneBatches++;
            if (onProgress) onProgress(doneBatches, totalBatches);
            if (i + CLEAN_BATCH_SIZE < ids.length) await sleep(BATCH_DELAY_MS);
        }
        return { ok, failed };
    }

    // 创建右上角固定浮动面板；返回根元素与渲染方法
    function createStatsPanel({
        onScan,
        onStop,
        primaryAction,
        panelId = STATS_PANEL_ID,
        titleText = '骰子干旱度',
        collapsedKey = STATS_PANEL_COLLAPSED_KEY,
        showTypes = true,
        scanLabel = '刷新'
    }) {
        const root = document.createElement('div');
        root.id = panelId;
        Object.assign(root.style, {
            position: 'fixed',
            top: '12px',
            right: '12px',
            zIndex: '99999',
            background: 'rgba(255, 255, 255, 0.95)',
            border: '1px solid #d0d7de',
            borderRadius: '8px',
            boxShadow: '0 2px 12px rgba(0, 0, 0, 0.15)',
            padding: '8px 10px',
            fontSize: '12px',
            color: '#333',
            fontFamily: 'sans-serif',
            minWidth: '120px'
        });

        // 样式仅作用于面板子树；收起态隐藏标题与状态行
        const style = document.createElement('style');
        style.textContent = `
            #${panelId} .hdsky-dice-header { display:flex; align-items:center; justify-content:space-between; cursor:pointer; margin-bottom:6px; font-weight:bold; }
            #${panelId} .hdsky-dice-toggle { margin-left:8px; user-select:none; }
            #${panelId} .hdsky-dice-types { display:flex; flex-wrap:wrap; gap:4px 10px; }
            #${panelId} .hdsky-dice-type { white-space:nowrap; }
            #${panelId} .hdsky-dice-num { font-weight:bold; color:#c0392b; }
            #${panelId} .hdsky-dice-status { margin-top:6px; color:#666; }
            #${panelId} .hdsky-dice-refresh { margin-top:6px; padding:2px 8px; cursor:pointer; border:1px solid #4a90d9; border-radius:3px; background:#4a90d9; color:#fff; font-size:12px; }
            #${panelId} .hdsky-dice-refresh:disabled { opacity:0.6; cursor:wait; }
            #${panelId} .hdsky-dice-refresh.hdsky-dice-scanning { background:#c0392b; border-color:#c0392b; }
            #${panelId} .hdsky-dice-primary { margin-top:4px; padding:2px 8px; cursor:pointer; border:1px solid #c0392b; border-radius:3px; background:#c0392b; color:#fff; font-size:12px; }
            #${panelId} .hdsky-dice-primary:disabled { opacity:0.6; cursor:not-allowed; }
            #${panelId}.hdsky-dice-collapsed .hdsky-dice-title { display:none; }
            #${panelId}.hdsky-dice-collapsed .hdsky-dice-status { display:none; }
        `;
        root.appendChild(style);

        const header = document.createElement('div');
        header.className = 'hdsky-dice-header';
        const title = document.createElement('span');
        title.className = 'hdsky-dice-title';
        title.textContent = titleText;
        const toggle = document.createElement('span');
        toggle.className = 'hdsky-dice-toggle';
        header.appendChild(title);
        header.appendChild(toggle);

        const numEls = {};
        if (showTypes) {
            const typesEl = document.createElement('div');
            typesEl.className = 'hdsky-dice-types';
            BET_TYPES.forEach(type => {
                const item = document.createElement('span');
                item.className = 'hdsky-dice-type';
                const label = document.createElement('span');
                label.textContent = type + ' ';
                const num = document.createElement('span');
                num.className = 'hdsky-dice-num';
                num.textContent = '-';
                item.appendChild(label);
                item.appendChild(num);
                typesEl.appendChild(item);
                numEls[type] = num;
            });
            root.appendChild(typesEl);
        }

        const status = document.createElement('div');
        status.className = 'hdsky-dice-status';

        const refreshBtn = document.createElement('button');
        refreshBtn.className = 'hdsky-dice-refresh';
        refreshBtn.textContent = scanLabel;

        let primaryBtn = null;
        if (primaryAction) {
            primaryBtn = document.createElement('button');
            primaryBtn.className = 'hdsky-dice-primary';
            primaryBtn.textContent = primaryAction.label;
            primaryBtn.addEventListener('click', () => {
                if (!primaryBtn.disabled && primaryAction.onClick) primaryAction.onClick();
            });
        }

        root.appendChild(header);
        root.appendChild(status);
        root.appendChild(refreshBtn);
        if (primaryBtn) root.appendChild(primaryBtn);

        const readCollapsed = () => {
            try { return localStorage.getItem(collapsedKey) === '1'; } catch (e) { return false; }
        };
        const writeCollapsed = (collapsed) => {
            try { localStorage.setItem(collapsedKey, collapsed ? '1' : '0'); } catch (e) { /* 忽略 */ }
        };
        let collapsed = readCollapsed();
        const applyCollapsed = () => {
            root.classList.toggle('hdsky-dice-collapsed', collapsed);
            toggle.textContent = collapsed ? '▸' : '▾';
        };
        applyCollapsed();

        header.addEventListener('click', () => {
            collapsed = !collapsed;
            writeCollapsed(collapsed);
            applyCollapsed();
        });

        // 扫描进行中按钮变为"暂停"，可随时中断本次扫描；空闲时恢复为 scanLabel
        let scanningNow = false;
        refreshBtn.addEventListener('click', () => {
            if (scanningNow) {
                if (onStop) onStop();
            } else if (onScan) {
                onScan();
            }
        });

        const setButtonsEnabled = (enabled) => {
            refreshBtn.disabled = !enabled;
            if (primaryBtn) primaryBtn.disabled = !enabled;
        };

        return {
            root,
            refreshBtn,
            primaryBtn,
            setScanning(isScanning) {
                scanningNow = isScanning;
                refreshBtn.textContent = isScanning ? '暂停' : scanLabel;
                refreshBtn.classList.toggle('hdsky-dice-scanning', isScanning);
                // 扫描时禁用所有按钮，避免扫描未完成时点击清理导致 POST 半截数据
                setButtonsEnabled(!isScanning);
            },
            setLoading(done, total) {
                status.textContent = total ? ('扫描中 ' + done + '/' + total) : '扫描中...';
            },
            setData(stats, note) {
                if (showTypes && stats) {
                    BET_TYPES.forEach(type => {
                        if (numEls[type]) numEls[type].textContent = stats[type];
                    });
                }
                status.textContent = note || '';
            },
            setError(msg) {
                status.textContent = '出错: ' + msg;
            },
            setPrimaryActionLabel(text) {
                if (primaryBtn) primaryBtn.textContent = text;
            },
            setPrimaryActionEnabled(enabled) {
                if (primaryBtn) primaryBtn.disabled = !enabled;
            }
        };
    }

    // 初始化面板：挂载、还原收起态、执行扫描；失败降级不阻塞主流程
    function initStatsPanel() {
        let panel;
        let scanning = false;
        let stopRequested = false;

        const runScan = async () => {
            if (scanning) return;
            scanning = true;
            stopRequested = false;
            panel.setScanning(true);
            panel.setLoading(0, 0);
            try {
                const topicStats = await scanAllTopicsBetStats(
                    (done, total) => panel.setLoading(done, total),
                    () => stopRequested
                );
                const stats = computeMissedRounds(topicStats);
                let note = '';
                if (topicStats.length === 0) note = '暂无数据';
                else if (stopRequested) note = '已暂停（已扫 ' + topicStats.length + ' 轮）';
                panel.setData(stats, note);
            } catch (e) {
                console.error('数据面板扫描失败:', e);
                panel.setError(e && e.message ? e.message : '扫描失败');
            } finally {
                scanning = false;
                stopRequested = false;
                panel.setScanning(false);
            }
        };

        const requestStop = () => { if (scanning) stopRequested = true; };

        panel = createStatsPanel({ onScan: runScan, onStop: requestStop });
        document.body.appendChild(panel.root);
        runScan();
    }

    // 清理面板：扫描收件箱 + 二次 confirm + 批量删除；保留 failed 列表便于重试
    function initCleanPanel() {
        let panel;
        let scanning = false;
        let deleting = false;
        let pendingIds = [];
        let retryFailedIds = [];

        const runScan = async () => {
            if (scanning || deleting) return;
            scanning = true;
            retryFailedIds = [];
            panel.setScanning(true);
            panel.setData(null, '扫描中...');
            try {
                pendingIds = await scanRatingMessages(
                    (done, total) => panel.setLoading(done, total),
                    () => false
                );
                if (pendingIds.length === 0) {
                    panel.setData(null, '已扫描 0 条，无可清理内容');
                    panel.setPrimaryActionLabel('清理 (—)');
                    panel.setPrimaryActionEnabled(false);
                } else {
                    panel.setData(null, '已扫描 ' + pendingIds.length + ' 条');
                    panel.setPrimaryActionLabel('清理 (' + pendingIds.length + ')');
                    panel.setPrimaryActionEnabled(true);
                }
            } catch (e) {
                console.error('清理面板扫描失败:', e);
                panel.setError(e && e.message ? e.message : '扫描失败');
                panel.setPrimaryActionEnabled(false);
            } finally {
                scanning = false;
                panel.setScanning(false);
            }
        };

        const runDelete = async (ids) => {
            if (!ids || ids.length === 0) return;
            // 隐私模式下 window.confirm 可能抛错；包裹在 try/catch 内，异常时复位 deleting 防止面板永久锁死
            let firstOk = false, secondOk = false;
            try {
                firstOk = confirm('将删除 ' + ids.length + ' 条管理员评分信，不可恢复。是否继续？');
                if (!firstOk) return;
                secondOk = confirm('最后确认：真的删除这 ' + ids.length + ' 条吗？');
            } catch (e) {
                console.error('confirm() 抛出,中止本次清理:', e);
                return;
            }
            if (!firstOk || !secondOk) return;

            deleting = true;
            const totalBatches = Math.ceil(ids.length / CLEAN_BATCH_SIZE);
            panel.setPrimaryActionEnabled(false);
            panel.setData(null, '删除中 0/' + totalBatches + '（失败批次将提示可重试,可能含服务器已删但响应丢失）');
            try {
                const { failed } = await deleteRatingMessages(ids, (done, total) => {
                    panel.setData(null, '删除中 ' + done + '/' + total);
                });
                retryFailedIds = failed;
                if (failed.length === 0) {
                    panel.setData(null, '已删除 ' + ids.length + ' 条,剩 0 条');
                    panel.setPrimaryActionLabel('清理 (—)');
                } else {
                    panel.setData(null, '已删 ' + (ids.length - failed.length) + ' 条,剩 ' + failed.length + ' 条');
                    panel.setPrimaryActionLabel('清理 (' + failed.length + ') 可重试');
                    panel.setPrimaryActionEnabled(true);
                }
            } catch (e) {
                console.error('清理面板删除失败:', e);
                panel.setError(e && e.message ? e.message : '删除失败');
            } finally {
                deleting = false;
            }
        };

        panel = createStatsPanel({
            onScan: () => { if (!deleting) runScan(); },
            primaryAction: {
                label: '清理 (—)',
                onClick: () => {
                    if (deleting) return;
                    const ids = retryFailedIds.length > 0 ? retryFailedIds : pendingIds;
                    runDelete(ids);
                }
            },
            panelId: CLEAN_PANEL_ID,
            titleText: '清理管理员评分信',
            collapsedKey: CLEAN_PANEL_COLLAPSED_KEY,
            showTypes: false,
            scanLabel: '扫描'
        });
        panel.setPrimaryActionEnabled(false);
        document.body.appendChild(panel.root);
    }

    // 按页面路由初始化：列表页 → 既有投注按钮 + 数据面板；站内信页 → 清理面板
    const href = location.href;
    if (href.indexOf('messages.php') !== -1) {
        // 仅在"收件箱列表"页面渲染清理面板:action=viewmailbox 或无 action(默认 = 收件箱),且 box=1 或无 box
        // 其他 messages 子页面(viewmessage 单条详情、editmailboxes 短讯箱管理、box=0 已发送 等)不挂任何面板
        const params = new URLSearchParams(href.split('?')[1] || '');
        const action = params.get('action');
        const box = params.get('box');
        const isInbox = (action === null || action === 'viewmailbox') && (box === null || box === '1');
        if (isInbox) {
            try {
                initCleanPanel();
            } catch (e) {
                console.error('初始化清理面板失败:', e);
            }
        }
    } else {
        addBetButtons();
        try {
            initStatsPanel();
        } catch (e) {
            console.error('初始化数据面板失败:', e);
        }
    }
})();