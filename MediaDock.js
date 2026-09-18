// ==UserScript==
// @name         MediaDock - Local YouTube Downloader
// @namespace    http://tampermonkey.net/
// @version      2.1
// @description  一键调用本地 yt-dlp 下载 YouTube 视频，并显示实时进度 (MediaDock Phase 1)
// @match        https://www.youtube.com/watch*
// @match        https://www.youtube.com/shorts/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==
(function () {
    'use strict';
    // =========================
    // 配置
    // =========================
    const SERVER = 'http://127.0.0.1:8765';
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
    // 创建按钮 (SPA 保活：YouTube 切视频页面不刷新)
    let button = null;
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
    let timer = null;
    let downloading = false;
    let currentTaskId = null;
    function setButton(text, background = '#ff0000') {
        ensureButton();
        button.textContent = text;
        button.style.background = background;
    }
    // =========================
    // 停止轮询
    // =========================
    function stopPolling() {
        if (timer !== null) {
            clearInterval(timer);
            timer = null;
        }
    }
    function resetToIdle(text, bg) {
        downloading = false; currentTaskId = null; stopPolling();
        setButton(text, bg);
        setTimeout(() => { if (!downloading) setButton('⬇ 下载 MP4', '#ff0000'); }, 3000);
    }
    function checkStatus() {
        if (!currentTaskId) return;
        GM_xmlhttpRequest({
            method: 'GET',
            url: SERVER + '/status?id=' + encodeURIComponent(currentTaskId),
            timeout: 5000,
            onload: function (response) {
                try {
                    const data = JSON.parse(response.responseText);
                    if (data.error && !data.status) return;
                    console.log('[MediaDock]', data);
                    // =========================
                    // 下载中
                    // =========================
                    if (data.status === 'downloading') {
                        downloading = true;
                        const percent = Number(data.percent || 0).toFixed(1);
                        const speed = data.speed || '';
                        const eta = data.eta || '';
                        let text = `⏳ ${percent}%`;
                        if (speed === 'merging') { text = `🔄 合并中 99%`; }
                        else if (speed) {
                            text += `\n${speed}`;
                        }
                        if (eta) {
                            text += `\nETA ${eta}`;
                        }
                        setButton(
                            text,
                            '#1976d2'
                        );
                        return;
                    }
                    // =========================
                    // 下载完成
                    // =========================
                    if (data.status === 'completed') {
                        resetToIdle('✅ 下载完成', '#2e7d32');
                        return;
                    }
                    if (data.status === 'error') {
                        resetToIdle('❌ 下载失败', '#d32f2f');
                        return;
                    }
                } catch (error) {
                    console.error(
                        '解析状态失败:',
                        error
                    );
                }
            },
            // =========================
            // 本地服务器连接失败
            // =========================
            onerror: function () {
                console.error(
                    '无法连接本地 yt-dlp 服务'
                );
                if (downloading) {
                    setButton(
                        '❌ 服务连接失败',
                        '#d32f2f'
                    );
                }
            }
        });
    }
    // 开始下载
    function startDownload() {
        if (downloading) return;
        const url = cleanYouTubeUrl(location.href);
        console.log('[MediaDock] 开始下载:', url);
        downloading = true;
        setButton('⏳ 正在启动...', '#1976d2');
        const requestUrl = SERVER + '/download?url=' + encodeURIComponent(url);
        GM_xmlhttpRequest({
            method: 'GET',
            url: requestUrl,
            timeout: 10000,
            onload: function (response) {
                if (response.status === 200) {
                    try {
                        const ret = JSON.parse(response.responseText);
                        currentTaskId = ret.task_id || null;
                        console.log('[MediaDock] task_id:', currentTaskId);
                    } catch (e) { currentTaskId = null; }
                    if (!currentTaskId) {
                        resetToIdle('❌ 启动失败', '#d32f2f');
                        return;
                    }
                    setButton('⏳ 准备下载...', '#1976d2');
                    checkStatus();
                    stopPolling();
                    timer = setInterval(checkStatus, 1000);
                } else {
                    resetToIdle('❌ 启动失败', '#d32f2f');
                }
            },
            onerror: function () {
                resetToIdle('❌ 本地服务未启动', '#d32f2f');
            }
        });
    }
    ensureButton();
    document.addEventListener('yt-navigate-finish', () => {
        ensureButton();
        if (!downloading) setButton('⬇ 下载 MP4', '#ff0000');
    });
    setInterval(() => {
        if (!button || !document.body.contains(button)) ensureButton();
    }, 3000);
})();
