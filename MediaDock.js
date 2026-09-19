// ==UserScript==
// @name         MediaDock - Local YouTube Downloader
// @namespace    http://tampermonkey.net/
// @version      5.0
// @description  一键调用本地 yt-dlp 下载 YouTube 视频，并显示所有页共享的多任务进度列表 (MediaDock Stage-005)
// @match        https://www.youtube.com/watch*
// @match        https://www.youtube.com/shorts/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==
(function () {
    'use strict';
    // =========================
    // 配置 (Stage-003：任务列表改为共享 /tasks 轮询)
    // =========================
    const SERVER = 'http://127.0.0.1:8765';
    const POLL_MS = 1000;
    // 面板内直接可见的行数；超出的任务靠面板内部滚动，不隐藏未完成任务
    const MAX_VISIBLE_ROWS = 20;
    const ROW_HEIGHT = 46;
    // 已完成任务展开后的渲染上限，超出时明确提示而不是静默吞掉
    const MAX_COMPLETED_ROWS = 100;
    const SHORT_TITLE = 20;
    // 控制接口路径 (Stage-004/005)：与服务端 POST 路由一一对应
    const CONTROL_PATHS = {
        pause: '/pause',
        resume: '/resume',
        cancel: '/cancel',
        retry: '/retry',
        delete: '/delete'
    };
    // 错误码到中文提示 (Stage-005)：不改变服务端原始 error_code 语义
    const ERROR_HINTS = {
        interrupted: '服务重启中断'
    };
    // URL 归一化 (借鉴多合一脚本 cleanUrl)
    function cleanYouTubeUrl(raw) {
        try {
            const u = new URL(raw);
            if (u.hostname === 'youtu.be') {
                const id = u.pathname.split('/').filter(Boolean)[0];
                if (id) return 'https://www.youtube.com/watch?v=' + id;
            }
            if (u.pathname.startsWith('/shorts/')) {
                const sid = u.pathname.split('/')[2];
                if (sid) return 'https://www.youtube.com/shorts/' + sid;
            }
            const v = u.searchParams.get('v');
            if (v) return 'https://www.youtube.com/watch?v=' + v;
            return raw;
        } catch (e) { return raw; }
    }
    function shortTitle(text) {
        const t = String(text || '').replace(/\s+/g, ' ').trim();
        if (!t) return '(未知标题)';
        return t.length > SHORT_TITLE ? t.slice(0, SHORT_TITLE) + '…' : t;
    }
    function pct(value) {
        const n = Number(value);
        if (!isFinite(n)) return '0.0';
        return n.toFixed(1);
    }
    // =========================
    // 下载按钮 (SPA 保活：YouTube 切视频页面不刷新)
    // =========================
    let button = null;
    let submitting = false;
    let listTimer = null;
    let listCollapsed = false;
    let completedExpanded = false;
    let lastPayload = { tasks: [], active_count: 0, active_limit: 3, queued_count: 0 };
    function ensureButton() {
        if (button && document.body.contains(button)) return button;
        button = document.createElement('button');
        button.textContent = '⬇ 下载 MP4';
        Object.assign(button.style, {
            position: 'fixed',
            right: '20px',
            bottom: '20px',
            zIndex: '999999',
            width: '150px',
            minHeight: '55px',
            padding: '8px 12px',
            background: '#ff0000',
            color: '#ffffff',
            border: 'none',
            borderRadius: '10px',
            fontSize: '14px',
            fontWeight: 'bold',
            cursor: 'pointer',
            boxShadow: '0 3px 12px rgba(0,0,0,0.35)',
            whiteSpace: 'pre-line',
            transition: 'all 0.2s ease'
        });
        button.addEventListener('click', startDownload);
        document.body.appendChild(button);
        return button;
    }
    function setButton(text, background = '#ff0000') {
        ensureButton();
        button.textContent = text;
        button.style.background = background;
    }
    function idleButtonSoon() {
        setTimeout(function () {
            if (!submitting) setButton('⬇ 下载 MP4', '#ff0000');
        }, 3000);
    }
    // =========================
    // 共享任务面板 (Stage-003)
    // 所有 YouTube 页面轮询同一个 /tasks，不保存本页面私有的任务视图
    // =========================
    let panel = null;
    let headerEl = null;
    let summaryEl = null;
    let listEl = null;
    function narrowScreen() {
        return window.innerWidth < 620;
    }
    function ensurePanel() {
        if (panel && document.body.contains(panel)) return panel;
        panel = document.createElement('div');
        panel.id = 'mediadock-panel';
        Object.assign(panel.style, {
            position: 'fixed',
            right: '20px',
            bottom: '90px',
            zIndex: '999999',
            width: narrowScreen() ? 'calc(100vw - 40px)' : '330px',
            background: 'rgba(20,20,20,0.92)',
            color: '#ffffff',
            border: '1px solid rgba(255,255,255,0.15)',
            borderRadius: '10px',
            boxShadow: '0 3px 12px rgba(0,0,0,0.35)',
            fontSize: '12px',
            lineHeight: '1.4',
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden'
        });
        headerEl = document.createElement('div');
        Object.assign(headerEl.style, {
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: '8px',
            padding: '7px 10px',
            background: 'rgba(255,255,255,0.06)',
            cursor: 'pointer',
            userSelect: 'none'
        });
        const titleEl = document.createElement('span');
        titleEl.textContent = 'MediaDock';
        titleEl.style.fontWeight = 'bold';
        summaryEl = document.createElement('span');
        summaryEl.textContent = '连接中…';
        summaryEl.style.opacity = '0.85';
        summaryEl.style.textAlign = 'right';
        headerEl.appendChild(titleEl);
        headerEl.appendChild(summaryEl);
        headerEl.addEventListener('click', function () {
            listCollapsed = !listCollapsed;
            if (listEl) listEl.style.display = listCollapsed ? 'none' : 'block';
        });
        listEl = document.createElement('div');
        Object.assign(listEl.style, {
            // 内部滚动：约 20 行可见，超出不撑开页面、不遮挡 YouTube 主内容
            maxHeight: (ROW_HEIGHT * MAX_VISIBLE_ROWS) + 'px',
            overflowY: 'auto',
            overflowX: 'hidden',
            padding: '6px 8px 8px 8px'
        });
        panel.appendChild(headerEl);
        panel.appendChild(listEl);
        document.body.appendChild(panel);
        return panel;
    }
    // 状态文案 + 行内控制按钮 (Stage-004)：按钮只由服务端状态决定
    function rowTextFor(task) {
        const title = shortTitle(task.title || task.url || '');
        if (task.status === 'downloading') {
            if (task.speed === 'merging') return '🔄 ' + title + ' · 合并中';
            let text = '⏳ ' + title + ' · ' + pct(task.percent) + '%';
            if (task.speed) text += ' · ' + task.speed;
            if (task.eta) text += ' · ETA ' + task.eta;
            return text;
        }
        if (task.status === 'pending') return '🕒 ' + title + ' · 排队中';
        if (task.status === 'paused') return '⏸ ' + title + ' · 已暂停';
        if (task.status === 'cancelled') return '🚫 ' + title + ' · 已取消';
        if (task.status === 'completed') return '✅ ' + title + ' · 完成';
        if (task.status === 'error') {
            const code = task.error_code
                ? ' (' + (ERROR_HINTS[task.error_code] || task.error_code) + ')'
                : '';
            return '❌ ' + title + ' · 失败' + code;
        }
        return '• ' + title + ' · ' + String(task.status || '');
    }
    function controlsFor(status) {
        if (status === 'downloading') {
            return [{ action: 'pause', label: '暂停' },
                    { action: 'cancel', label: '取消' }];
        }
        if (status === 'paused') {
            return [{ action: 'resume', label: '继续' },
                    { action: 'cancel', label: '取消' }];
        }
        if (status === 'pending') {
            return [{ action: 'cancel', label: '取消' }];
        }
        if (status === 'error' || status === 'cancelled') {
            return [{ action: 'retry', label: '重试' },
                    { action: 'delete', label: '删除' }];
        }
        if (status === 'completed') {
            return [{ action: 'delete', label: '删除' }];
        }
        return [];
    }
    function makeControlButton(taskId, spec) {
        const btn = document.createElement('button');
        btn.textContent = spec.label;
        btn.setAttribute('data-control', spec.action);
        Object.assign(btn.style, {
            minWidth: '42px',
            padding: '2px 7px',
            fontSize: '11px',
            color: '#ffffff',
            background: spec.action === 'cancel' ? '#b3261e' : '#37474f',
            border: 'none',
            borderRadius: '5px',
            cursor: 'pointer'
        });
        btn.addEventListener('click', function (event) {
            event.stopPropagation();
            postControl(spec.action, taskId);
        });
        return btn;
    }
    function makeRow(task) {
        const row = document.createElement('div');
        // 稳定标识：刷新时按 task_id 更新，不会把标题/进度/按钮绑到别的任务
        row.setAttribute('data-task-id', task.task_id);
        Object.assign(row.style, {
            minHeight: (ROW_HEIGHT - 10) + 'px',
            padding: '5px 6px',
            borderBottom: '1px solid rgba(255,255,255,0.08)',
            wordBreak: 'break-word',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: '6px'
        });
        const label = document.createElement('span');
        label.textContent = rowTextFor(task);
        label.style.flex = '1 1 auto';
        row.appendChild(label);
        const specs = controlsFor(task.status);
        if (specs.length > 0) {
            const box = document.createElement('span');
            Object.assign(box.style, {
                flex: '0 0 auto',
                display: 'flex',
                gap: '4px'
            });
            specs.forEach(function (spec) {
                box.appendChild(makeControlButton(task.task_id, spec));
            });
            row.appendChild(box);
        }
        return row;
    }
    function makeCompletedToggle(count) {
        const toggle = document.createElement('div');
        toggle.id = 'mediadock-completed-toggle';
        Object.assign(toggle.style, {
            padding: '6px',
            cursor: 'pointer',
            opacity: '0.9',
            borderBottom: '1px solid rgba(255,255,255,0.08)'
        });
        toggle.textContent = '已完成 ' + count + ' 个 ' +
            (completedExpanded ? '▾' : '▸');
        toggle.addEventListener('click', function () {
            completedExpanded = !completedExpanded;
            renderList(lastPayload);
        });
        return toggle;
    }
    function makeHintRow(text) {
        const hint = document.createElement('div');
        hint.style.padding = '6px';
        hint.style.opacity = '0.7';
        hint.textContent = text;
        return hint;
    }
    function renderList(payload) {
        ensurePanel();
        lastPayload = payload || { tasks: [] };
        const tasks = Array.isArray(lastPayload.tasks) ? lastPayload.tasks : [];
        const unfinished = tasks.filter(function (t) {
            return t && t.status !== 'completed';
        });
        const finished = tasks.filter(function (t) {
            return t && t.status === 'completed';
        });
        summaryEl.textContent = '运行 ' + (lastPayload.active_count || 0) +
            '/' + (lastPayload.active_limit || 3) +
            ' · 排队 ' + (lastPayload.queued_count || 0);
        listEl.textContent = '';
        // 未完成任务全部渲染，超出可见行数时由面板内部滚动承载
        unfinished.forEach(function (t) {
            listEl.appendChild(makeRow(t));
        });
        if (finished.length > 0) {
            listEl.appendChild(makeCompletedToggle(finished.length));
            if (completedExpanded) {
                finished.slice(0, MAX_COMPLETED_ROWS).forEach(function (t) {
                    listEl.appendChild(makeRow(t));
                });
                if (finished.length > MAX_COMPLETED_ROWS) {
                    listEl.appendChild(makeHintRow('仅显示最近 ' +
                        MAX_COMPLETED_ROWS + ' 个，共 ' + finished.length + ' 个'));
                }
            }
        }
        if (tasks.length === 0) {
            listEl.appendChild(makeHintRow('暂无任务'));
        }
    }
    function setServerOffline() {
        ensurePanel();
        summaryEl.textContent = '服务未启动（127.0.0.1:8765）';
        if (submitting) setButton('❌ 本地服务未启动', '#d32f2f');
    }
    // =========================
    // 控制请求 (Stage-004)：POST 后立即刷新，按钮状态始终来自服务端
    // =========================
    function postControl(action, taskId) {
        const path = CONTROL_PATHS[action];
        if (!path || !taskId) return;
        GM_xmlhttpRequest({
            method: 'POST',
            url: SERVER + path,
            headers: { 'Content-Type': 'application/json' },
            data: JSON.stringify({ task_id: taskId }),
            timeout: 15000,
            onload: function (response) {
                if (response.status !== 200) {
                    let code = '';
                    try {
                        code = JSON.parse(response.responseText).error_code || '';
                    } catch (e) { code = ''; }
                    console.warn('[MediaDock] 控制失败:', action, taskId, code);
                    if (summaryEl) {
                        summaryEl.textContent = '控制失败' +
                            (code ? ' ' + code : '');
                    }
                }
                refreshTasks();
            },
            onerror: function () {
                console.error('[MediaDock] 无法连接本地服务:', SERVER);
                setServerOffline();
            }
        });
    }
    // =========================
    // 轮询共享任务列表 (单一计时器，所有页面同一数据源)
    // =========================
    function refreshTasks() {
        GM_xmlhttpRequest({
            method: 'GET',
            url: SERVER + '/tasks',
            timeout: 5000,
            onload: function (response) {
                if (response.status !== 200) {
                    setServerOffline();
                    return;
                }
                try {
                    const data = JSON.parse(response.responseText);
                    renderList(data);
                } catch (error) {
                    console.error('[MediaDock] 解析任务列表失败:', error);
                    setServerOffline();
                }
            },
            onerror: function () {
                console.error('[MediaDock] 无法连接本地服务:', SERVER);
                setServerOffline();
            }
        });
    }
    function stopPolling() {
        if (listTimer !== null) {
            clearInterval(listTimer);
            listTimer = null;
        }
    }
    function startPolling() {
        stopPolling();
        refreshTasks();
        listTimer = setInterval(refreshTasks, POLL_MS);
    }
    // =========================
    // 提交当前页面下载 (列表由共享轮询更新，不再依赖单一 currentTaskId)
    // =========================
    function startDownload() {
        if (submitting) return;
        const url = cleanYouTubeUrl(location.href);
        console.log('[MediaDock] 提交下载:', url);
        submitting = true;
        setButton('⏳ 正在提交...', '#1976d2');
        GM_xmlhttpRequest({
            method: 'GET',
            url: SERVER + '/download?url=' + encodeURIComponent(url),
            timeout: 10000,
            onload: function (response) {
                submitting = false;
                if (response.status !== 200) {
                    let code = '';
                    try {
                        code = JSON.parse(response.responseText).error_code || '';
                    } catch (e) { code = ''; }
                    setButton('❌ 提交失败' + (code ? '\n' + code : ''), '#d32f2f');
                    idleButtonSoon();
                    return;
                }
                let taskId = null;
                try {
                    taskId = JSON.parse(response.responseText).task_id || null;
                } catch (e) { taskId = null; }
                if (!taskId) {
                    setButton('❌ 启动失败', '#d32f2f');
                    idleButtonSoon();
                    return;
                }
                console.log('[MediaDock] task_id:', taskId);
                setButton('✅ 已加入任务列表', '#2e7d32');
                idleButtonSoon();
                refreshTasks();
            },
            onerror: function () {
                submitting = false;
                setButton('❌ 本地服务未启动', '#d32f2f');
                idleButtonSoon();
            }
        });
    }
    // =========================
    // 初始化与 SPA 保活
    // =========================
    ensureButton();
    ensurePanel();
    startPolling();
    document.addEventListener('yt-navigate-finish', function () {
        // SPA 导航不清空共享列表，只确保按钮/面板仍挂在 DOM 上
        ensureButton();
        ensurePanel();
        if (!submitting) setButton('⬇ 下载 MP4', '#ff0000');
    });
    setInterval(function () {
        if (!button || !document.body.contains(button)) ensureButton();
        if (!panel || !document.body.contains(panel)) ensurePanel();
    }, 3000);
    window.addEventListener('resize', function () {
        if (panel) panel.style.width = narrowScreen() ? 'calc(100vw - 40px)' : '330px';
    });
})();