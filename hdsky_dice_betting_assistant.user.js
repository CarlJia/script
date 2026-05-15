// ==UserScript==
// @name         HDSky 掷骰子投注助手
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  在论坛列表页每个帖子显示投注汇总和投注按钮
// @match        https://hdsky.me/forums.php?action=viewforum&forumid=71
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    // 投注类型
    const BET_TYPES = ['豹子', '顺子', '大', '小'];

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

                    const body = encodeURIComponent(btnName + ' ' + amount);
                    const formData = `id=${topicid}&type=reply&body=${body}`;

                    fetch('https://hdsky.me/forums.php?action=post', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded',
                        },
                        body: formData
                    })
                    .then(response => {
                        if (response.ok) {
                            alert('投注成功: ' + btnName + ' ' + amount);
                        } else {
                            alert('投注失败');
                        }
                    })
                    .catch(error => {
                        alert('投注请求出错: ' + error);
                    });
                });

                container.appendChild(btn);
            });

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

    // 执行
    addBetButtons();
})();
