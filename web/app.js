/* DSH 对话管理器 - 前端逻辑 */
(() => {
  'use strict';

  const state = {
    data: null,
    busy: false,
    filter: '',
    collapsed: new Set(),
    selectedSessions: new Set(),
    selectedWorkspaces: new Set(),
    dialogPayload: null,
  };

  const els = {
    statusPill: document.getElementById('status-pill'),
    statusText: document.getElementById('status-text'),
    refresh: document.getElementById('refresh'),
    banner: document.getElementById('banner'),
    bannerTitle: document.getElementById('banner-title'),
    bannerDesc: document.getElementById('banner-desc'),
    bannerTips: document.getElementById('banner-tips'),
    bannerProcs: document.getElementById('banner-procs'),
    recheck: document.getElementById('recheck'),
    search: document.getElementById('search'),
    selectAll: document.getElementById('select-all'),
    clearSelection: document.getElementById('clear-selection'),
    expandAll: document.getElementById('expand-all'),
    collapseAll: document.getElementById('collapse-all'),
    selectionText: document.getElementById('selection-text'),
    lockHint: document.getElementById('lock-hint'),
    deleteSelected: document.getElementById('delete-selected'),
    actionbar: document.querySelector('.actionbar'),
    list: document.getElementById('list'),
    homePath: document.getElementById('home-path'),
    backupDir: document.getElementById('backup-dir'),
    statsText: document.getElementById('stats-text'),
    dialog: document.getElementById('confirm-dialog'),
    dialogSubtitle: document.getElementById('dialog-subtitle'),
    dialogBody: document.getElementById('dialog-body'),
    dialogWarning: document.getElementById('dialog-warning'),
    dialogConfirm: document.getElementById('dialog-confirm'),
    toast: document.getElementById('toast'),
    toastIcon: document.getElementById('toast-icon'),
    toastText: document.getElementById('toast-text'),
  };

  /* ------------------------------------------------------------- helpers */

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'checked' || key === 'disabled' || key === 'indeterminate') node[key] = Boolean(value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else node.setAttribute(key, String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function isLocked() {
    return Boolean(state.data && state.data.dsh.running && !state.data.force);
  }

  function isForce() {
    return Boolean(state.data && state.data.force);
  }

  const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });

  function formatDate(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '—';
    return dateFormatter.format(date);
  }

  function formatRelative(iso) {
    if (!iso) return '—';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '—';
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds < 0) return formatDate(iso);
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)} 天前`;
    return formatDate(iso);
  }

  function formatSize(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function findWorkspace(workspaceId) {
    return state.data ? state.data.workspaces.find((item) => item.id === workspaceId) : undefined;
  }

  function workspaceSessions(workspace) {
    return workspace ? workspace.sessions : [];
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (error) {
      /* 继续使用兜底方案 */
    }
    try {
      const area = el('textarea', { style: 'position:fixed;opacity:0;top:0;left:0' });
      area.value = text;
      document.body.append(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      return ok;
    } catch (error) {
      return false;
    }
  }

  /* ----------------------------------------------------------------- api */

  async function fetchState({ deep = false } = {}) {
    const response = await fetch(`/api/state${deep ? '?deep=1' : ''}`, { cache: 'no-store' });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error((payload && payload.message) || `HTTP ${response.status}`);
    }
    return payload;
  }

  function applyState(payload) {
    state.data = payload;
    const workspaceIds = new Set(payload.workspaces.map((workspace) => workspace.id));
    const sessionIds = new Set(
      payload.workspaces.flatMap((workspace) => workspace.sessions.map((session) => session.id)),
    );
    for (const id of [...state.selectedWorkspaces]) {
      if (!workspaceIds.has(id)) state.selectedWorkspaces.delete(id);
    }
    for (const id of [...state.selectedSessions]) {
      if (!sessionIds.has(id)) state.selectedSessions.delete(id);
    }
    for (const id of [...state.collapsed]) {
      if (!workspaceIds.has(id)) state.collapsed.delete(id);
    }
    render();
  }

  /* --------------------------------------------------------------- render */

  function render() {
    renderStatus();
    renderBanner();
    renderFooter();
    renderList();
    updateSelectionUI();
  }

  function renderStatus() {
    const data = state.data;
    if (!data) {
      els.statusPill.className = 'pill pill-muted';
      els.statusText.textContent = '尚未连接';
      return;
    }
    const running = data.dsh.running;
    if (!running) {
      els.statusPill.className = 'pill pill-ok';
      els.statusText.textContent = 'DSH 未运行';
    } else if (data.force) {
      els.statusPill.className = 'pill pill-warn';
      els.statusText.textContent = 'DSH 运行中 · 强制模式';
    } else {
      els.statusPill.className = 'pill pill-bad';
      els.statusText.textContent = 'DSH 正在运行';
    }
  }

  function renderBanner() {
    const data = state.data;
    if (!data || !data.dsh.running) {
      els.banner.hidden = true;
      return;
    }
    els.banner.hidden = false;
    els.banner.classList.toggle('banner-force', Boolean(data.force));
    if (data.force) {
      els.bannerTitle.textContent = '强制模式：DSH 正在运行';
      els.bannerDesc.textContent =
        '你已使用 --force 启动，删除不会被拦截。但运行中的 DSH 可能把旧数据写回，强烈建议先退出 DSH 再操作。';
    } else {
      els.bannerTitle.textContent = '检测到 DSH 正在运行，删除已锁定';
      els.bannerDesc.textContent =
        '请先完全退出 DeepSeek Harness，然后点击右侧「重新检测」；退出后再执行删除才安全。';
    }
    els.bannerTips.hidden = Boolean(data.force);

    els.bannerProcs.replaceChildren();
    const groups = new Map();
    for (const process of data.dsh.processes || []) {
      groups.set(process.name, (groups.get(process.name) || 0) + 1);
    }
    for (const [name, count] of groups) {
      els.bannerProcs.append(
        el('span', {
          class: 'proc-chip',
          text: count > 1 ? `${name} × ${count}` : name,
          title: (data.dsh.processes || [])
            .filter((process) => process.name === name && process.pid)
            .map((process) => `PID ${process.pid}`)
            .join(' · '),
        }),
      );
    }
    els.bannerProcs.hidden = groups.size === 0;
  }

  function renderFooter() {
    const data = state.data;
    if (!data) return;
    els.homePath.textContent = data.home;
    els.homePath.title = data.home;
    els.backupDir.textContent = data.backupDir;
    els.backupDir.title = data.backupDir;

    const stats = data.stats;
    const parts = [
      `共 ${stats.workspaces} 个工作区`,
      `${stats.sessions} 个对话`,
      `日志 ${formatSize(stats.bytes)}`,
    ];
    if (stats.missingLogs > 0) parts.push(`${stats.missingLogs} 个对话缺少日志文件`);
    if (data.backup && data.backup.latest) {
      parts.push(`最近备份 ${data.backup.latest.replace(/^workspace-/, '').replace(/\.json$/, '')}`);
    }
    els.statsText.textContent = parts.join(' · ');
  }

  function matchesWorkspace(workspace, query) {
    if (!query) return true;
    return (
      workspace.title.toLowerCase().includes(query) ||
      (workspace.path || '').toLowerCase().includes(query) ||
      workspace.id.toLowerCase().includes(query)
    );
  }

  function matchesSession(session, query) {
    if (!query) return true;
    return (
      (session.title || '').toLowerCase().includes(query) || session.id.toLowerCase().includes(query)
    );
  }

  function renderList() {
    if (!state.data) return;
    const query = state.filter.trim().toLowerCase();
    const fragment = document.createDocumentFragment();
    let visible = 0;

    for (const workspace of state.data.workspaces) {
      const workspaceMatches = matchesWorkspace(workspace, query);
      const matchedSessions = workspace.sessions.filter((session) => matchesSession(session, query));
      if (query && !workspaceMatches && matchedSessions.length === 0) continue;
      visible += 1;
      const sessionsToShow = workspaceMatches ? workspace.sessions : matchedSessions;
      fragment.append(renderWorkspace(workspace, { query, sessions: sessionsToShow }));
    }

    els.list.replaceChildren();
    if (visible === 0) {
      els.list.append(renderEmptySearch(query));
    } else {
      els.list.append(fragment);
    }
    updateSelectionUI();
  }

  function renderEmptySearch(query) {
    return el(
      'div',
      { class: 'empty' },
      el('h2', { text: query ? '没有匹配的结果' : '还没有工作区' }),
      el('p', {
        text: query
          ? '试试更短的关键词，或搜索工作区 ID / 对话 ID。'
          : '在 DSH 中打开过项目后，这里会显示对应的工作区与对话。',
      }),
    );
  }

  function renderWorkspace(workspace, options) {
    const { query, sessions } = options;
    const locked = isLocked();
    const selected = state.selectedWorkspaces.has(workspace.id);
    const collapsed = state.collapsed.has(workspace.id) && !query;

    const card = el('article', { class: 'ws', dataset: { wsId: workspace.id } });
    if (selected) card.classList.add('is-selected');
    if (collapsed) card.classList.add('collapsed');

    const checkbox = el('input', { type: 'checkbox', disabled: locked });
    checkbox.dataset.wsCheck = workspace.id;
    checkbox.checked = selected;
    checkbox.indeterminate = !selected && workspace.sessions.some((session) => state.selectedSessions.has(session.id));
    checkbox.addEventListener('change', () => toggleWorkspace(workspace, checkbox.checked));

    const head = el('div', { class: 'ws-head' });
    head.append(el('label', { class: 'check', title: locked ? '请先退出 DSH' : '删除整个工作区及其全部对话' }, checkbox));

    const info = el('div', { class: 'ws-info' });
    info.append(el('h2', { class: 'ws-title', text: workspace.title || '（未命名工作区）' }));
    if (workspace.path) {
      info.append(el('div', { class: 'ws-path', text: workspace.path, title: workspace.path }));
    }

    const meta = el('div', { class: 'ws-meta' });
    const idCode = el('code', { text: workspace.id, title: '点击复制工作区 ID' });
    idCode.dataset.act = 'copy-ws-id';
    idCode.dataset.id = workspace.id;
    idCode.classList.add('copyable');
    meta.append(
      metaItem('ID', idCode),
      metaItem('创建', formatDate(workspace.createdAt)),
      metaItem('更新', formatDate(workspace.updatedAt)),
      metaItem('对话', `${workspace.sessions.length} 个`),
    );
    if (!workspace.exists && workspace.path) {
      meta.append(el('span', { class: 'badge badge-warn', text: '文件夹不存在' }));
    }
    info.append(meta);
    head.append(info);

    const actions = el('div', { class: 'ws-actions' });
    const toggleBtn = el('button', {
      class: 'ws-toggle',
      type: 'button',
      text: collapsed ? '▸' : '▾',
      title: collapsed ? '展开对话列表' : '收起对话列表',
      'aria-label': collapsed ? '展开对话列表' : '收起对话列表',
      'aria-expanded': String(!collapsed),
    });
    toggleBtn.dataset.act = 'toggle-ws';
    toggleBtn.dataset.id = workspace.id;
    const copyBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: '复制 ID' });
    copyBtn.dataset.act = 'copy-ws-id';
    copyBtn.dataset.id = workspace.id;
    const delBtn = el('button', {
      class: 'btn btn-danger-outline btn-sm',
      type: 'button',
      text: '删除工作区',
      title: '删除工作区及其全部对话（磁盘上的项目文件夹保留）',
      disabled: locked,
    });
    delBtn.dataset.act = 'delete-ws';
    delBtn.dataset.id = workspace.id;
    actions.append(toggleBtn, copyBtn, delBtn);
    head.append(actions);
    card.append(head);

    const list = el('ul', { class: 'sessions' });
    if (sessions.length === 0) {
      list.append(
        el('li', {
          class: 'empty-sessions',
          text: query ? '没有匹配的对话。' : '该工作区下没有对话记录。',
        }),
      );
    } else {
      for (const session of sessions) {
        list.append(renderSession(workspace, session, locked));
      }
    }
    card.append(list);
    return card;
  }

  function metaItem(label, value) {
    const item = el('span', { class: 'meta-item' });
    if (label) item.append(el('span', { class: 'meta-label', text: label }));
    if (value instanceof Node) item.append(value);
    else item.append(el('span', { text: value }));
    return item;
  }

  function renderSession(workspace, session, locked) {
    const workspaceSelected = state.selectedWorkspaces.has(workspace.id);
    const selected = workspaceSelected || state.selectedSessions.has(session.id);

    const row = el('li', { class: 'session', dataset: { sessionId: session.id, wsId: workspace.id } });
    if (selected) row.classList.add('is-target');

    const checkbox = el('input', {
      type: 'checkbox',
      checked: selected,
      disabled: locked || workspaceSelected,
      title: workspaceSelected ? '该对话会随工作区一起删除' : '',
    });
    checkbox.dataset.sessionCheck = session.id;
    checkbox.dataset.wsId = workspace.id;
    checkbox.addEventListener('change', () => toggleSession(workspace, session, checkbox.checked));
    row.append(el('label', { class: 'check' }, checkbox));

    const main = el('div', { class: 's-main' });
    const title = el('div', { class: `s-title${session.title ? '' : ' untitled'}` });
    title.append(el('span', { text: session.title || '（无标题）' }));
    if (session.archived) title.append(el('span', { class: 'badge badge-info', text: '已归档' }));
    if (!session.hasLog) title.append(el('span', { class: 'badge badge-warn', text: '日志缺失' }));
    main.append(title);

    const meta = el('div', { class: 's-meta' });
    const idCode = el('code', { text: session.id, title: '点击复制对话 ID' });
    idCode.dataset.act = 'copy-session-id';
    idCode.dataset.id = session.id;
    idCode.classList.add('copyable');
    meta.append(idCode);
    meta.append(el('span', { text: session.hasLog ? formatSize(session.logSize) : '无日志文件' }));
    if (session.updatedAt) {
      meta.append(
        el('span', {
          text: formatRelative(session.updatedAt),
          title: `最后更新：${formatDate(session.updatedAt)}`,
        }),
      );
    }
    if (session.archived) meta.append(el('span', { text: '已归档' }));
    main.append(meta);
    row.append(main);

    const actions = el('div', { class: 'session-actions' });
    const delBtn = el('button', {
      class: 'btn btn-icon',
      type: 'button',
      text: '删除',
      title: '删除这个对话（不可恢复）',
      disabled: locked || workspaceSelected,
      'aria-label': `删除对话 ${session.title || session.id}`,
    });
    delBtn.dataset.act = 'delete-session';
    delBtn.dataset.id = session.id;
    delBtn.dataset.wsId = workspace.id;
    actions.append(delBtn);
    row.append(actions);
    return row;
  }

  /* ------------------------------------------------------------ selection */

  function toggleWorkspace(workspace, checked) {
    if (checked) {
      state.selectedWorkspaces.add(workspace.id);
      for (const session of workspace.sessions) state.selectedSessions.delete(session.id);
    } else {
      state.selectedWorkspaces.delete(workspace.id);
    }
    renderList();
  }

  function toggleSession(workspace, session, checked) {
    if (checked) state.selectedSessions.add(session.id);
    else state.selectedSessions.delete(session.id);
    updateSelectionUI();
    const row = els.list.querySelector(`[data-session-id="${cssEscape(session.id)}"]`);
    if (row) row.classList.toggle('is-target', checked);
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
    return value.replace(/["\\]/g, '\\$&');
  }

  function effectiveSelection() {
    if (!state.data) return { sessions: new Set(), workspaces: new Set() };
    const sessions = new Set(state.selectedSessions);
    const workspaces = new Set(state.selectedWorkspaces);
    for (const workspace of state.data.workspaces) {
      if (!workspaces.has(workspace.id)) continue;
      for (const session of workspace.sessions) sessions.add(session.id);
    }
    return { sessions, workspaces };
  }

  function updateSelectionUI() {
    if (!state.data) return;
    const { sessions, workspaces } = effectiveSelection();
    const count = sessions.size;
    const locked = isLocked();
    const busy = state.busy;

    if (count === 0 && workspaces.size === 0) {
      els.selectionText.textContent = '勾选对话或工作区后，点击右侧按钮删除';
    } else if (workspaces.size === 0) {
      els.selectionText.innerHTML = `已选 <strong>${count}</strong> 个对话`;
    } else if (count === 0) {
      els.selectionText.innerHTML = `已选 <strong>${workspaces.size}</strong> 个工作区`;
    } else {
      els.selectionText.innerHTML = `已选 <strong>${count}</strong> 个对话 · <strong>${workspaces.size}</strong> 个工作区`;
    }
    els.actionbar.classList.toggle('has-selection', count > 0 || workspaces.size > 0);

    els.deleteSelected.disabled = busy || locked || (count === 0 && workspaces.size === 0);
    els.deleteSelected.title = locked
      ? 'DSH 正在运行，请先退出 DSH'
      : '删除所有已选内容（不可恢复）';

    els.lockHint.hidden = !locked;
    els.selectAll.disabled = busy || locked;
    els.clearSelection.disabled = busy || (count === 0 && workspaces.size === 0);
    els.expandAll.disabled = busy;
    els.collapseAll.disabled = busy;
    els.refresh.disabled = busy;
    els.recheck.disabled = busy;

    for (const input of els.list.querySelectorAll('[data-session-check]')) {
      const sessionId = input.dataset.sessionCheck;
      const workspaceId = input.dataset.wsId;
      const workspaceSelected = state.selectedWorkspaces.has(workspaceId);
      input.checked = workspaceSelected || state.selectedSessions.has(sessionId);
      input.disabled = locked || workspaceSelected || busy;
      const row = input.closest('.session');
      if (row) row.classList.toggle('is-target', input.checked);
    }
    for (const input of els.list.querySelectorAll('[data-ws-check]')) {
      const workspaceId = input.dataset.wsCheck;
      const workspace = findWorkspace(workspaceId);
      const selected = state.selectedWorkspaces.has(workspaceId);
      input.checked = selected;
      input.disabled = locked || busy;
      input.indeterminate =
        !selected && Boolean(workspace) && workspace.sessions.some((session) => state.selectedSessions.has(session.id));
      const card = input.closest('.ws');
      if (card) card.classList.toggle('is-selected', selected);
    }
    for (const button of els.list.querySelectorAll('[data-act="delete-session"]')) {
      const workspaceSelected = state.selectedWorkspaces.has(button.dataset.wsId);
      button.disabled = locked || busy || workspaceSelected;
    }
    for (const button of els.list.querySelectorAll('[data-act="delete-ws"]')) {
      button.disabled = locked || busy;
    }
  }

  /* -------------------------------------------------------------- actions */

  async function refreshState({ deep = false, quiet = false } = {}) {
    if (state.busy) return;
    if (!state.data) {
      els.list.replaceChildren(
        el('div', { class: 'loading' }, el('div', { class: 'spinner' }), el('p', { text: '正在读取 DSH 数据…' })),
      );
    }
    els.refresh.disabled = true;
    els.refresh.classList.add('is-loading');
    try {
      const payload = await fetchState({ deep });
      applyState(payload);
      if (deep && !quiet) {
        /* 首次加载或手动重新检测，不额外提示 */
      }
    } catch (error) {
      renderFatalError(error.message);
    } finally {
      els.refresh.disabled = false;
      els.refresh.classList.remove('is-loading');
    }
  }

  function renderFatalError(message) {
    els.banner.hidden = true;
    els.statusPill.className = 'pill pill-bad';
    els.statusText.textContent = '读取失败';
    els.list.replaceChildren(
      el(
        'div',
        { class: 'empty' },
        el('h2', { text: '无法读取 DSH 数据' }),
        el('p', { text: message }),
        el(
          'button',
          { class: 'btn btn-warn', type: 'button', text: '重试', onclick: () => refreshState({ deep: true }) },
        ),
      ),
    );
  }

  function buildDialog() {
    const payload = state.dialogPayload;
    const data = state.data;
    if (!payload || !data) return;

    const selectedWorkspaces = data.workspaces.filter((workspace) => payload.workspaceIds.includes(workspace.id));
    const covered = new Set(
      selectedWorkspaces.flatMap((workspace) => workspace.sessions.map((session) => session.id)),
    );
    const allSessions = new Map();
    for (const workspace of data.workspaces) {
      for (const session of workspace.sessions) allSessions.set(session.id, session);
    }
    const explicitSessions = payload.sessionIds
      .filter((id) => !covered.has(id) && allSessions.has(id))
      .map((id) => allSessions.get(id));

    els.dialogBody.replaceChildren();

    if (selectedWorkspaces.length > 0) {
      const section = el('section', { class: 'dialog-section' });
      section.append(el('h3', { text: `工作区（${selectedWorkspaces.length}）` }));
      const list = el('ul', { class: 'dialog-list' });
      for (const workspace of selectedWorkspaces.slice(0, 8)) {
        list.append(
          el(
            'li',
            {},
            el('span', { class: 'item-name', text: workspace.title }),
            el('span', { class: 'item-id', text: `${workspace.sessions.length} 个对话` }),
          ),
        );
      }
      if (selectedWorkspaces.length > 8) {
        list.append(el('li', {}, el('span', { class: 'item-name', text: `…还有 ${selectedWorkspaces.length - 8} 个工作区` })));
      }
      section.append(list);
      els.dialogBody.append(section);
    }

    if (explicitSessions.length > 0) {
      const section = el('section', { class: 'dialog-section' });
      section.append(el('h3', { text: `对话（${explicitSessions.length}）` }));
      const list = el('ul', { class: 'dialog-list' });
      for (const session of explicitSessions.slice(0, 10)) {
        list.append(
          el(
            'li',
            {},
            el('span', { class: 'item-name', text: session.title || '（无标题对话）' }),
            el('span', { class: 'item-id', text: session.id }),
          ),
        );
      }
      if (explicitSessions.length > 10) {
        list.append(el('li', {}, el('span', { class: 'item-name', text: `…还有 ${explicitSessions.length - 10} 个对话` })));
      }
      section.append(list);
      els.dialogBody.append(section);
    }

    const totalSessions = covered.size + explicitSessions.length;
    const totalWorkspaces = selectedWorkspaces.length;
    const totalBytes = [...covered, ...explicitSessions.map((session) => session.id)]
      .map((id) => allSessions.get(id))
      .filter(Boolean)
      .reduce((sum, session) => sum + (session.logSize || 0), 0);
    els.dialogBody.append(
      el('div', {
        class: 'dialog-total',
        text: `合计：${totalSessions} 个对话、${totalWorkspaces} 个工作区，日志约 ${formatSize(totalBytes)}`,
      }),
    );

    els.dialogSubtitle.textContent = totalWorkspaces
      ? '工作区记录及其名下的全部对话都会被删除'
      : '所选对话会被永久删除';
    els.dialogWarning.textContent = isForce()
      ? '删除后无法恢复。强制模式下 DSH 仍在运行，删除结果可能被覆盖。工作区所指向的磁盘文件夹不会被删除。'
      : '删除后无法恢复。工作区所指向的磁盘文件夹不会被删除，但其中的对话记录会全部删除。';
  }

  function openConfirm({ sessionIds = [], workspaceIds = [] }) {
    if (!state.data || state.busy || isLocked()) return;
    if (sessionIds.length === 0 && workspaceIds.length === 0) return;
    state.dialogPayload = { sessionIds, workspaceIds };
    buildDialog();
    els.dialogConfirm.disabled = false;
    if (typeof els.dialog.showModal === 'function') els.dialog.showModal();
  }

  async function performDelete() {
    const payload = state.dialogPayload;
    if (!payload || state.busy) return;
    state.dialogPayload = null;
    state.busy = true;
    updateSelectionUI();
    els.dialogConfirm.disabled = true;
    try {
      const response = await fetch('/api/delete', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'dsh-chat-manager',
        },
        body: JSON.stringify({
          sessionIds: payload.sessionIds,
          workspaceIds: payload.workspaceIds,
        }),
      });
      const result = await response.json().catch(() => null);
      if (!response.ok || !result || !result.ok) {
        const message = (result && result.message) || `删除失败（HTTP ${response.status}）`;
        toast(message, 'error');
        if (result && result.error === 'dsh-running') {
          await refreshState({ deep: true, quiet: true });
        }
        return;
      }
      const report = result.report;
      const parts = [`已删除 ${report.deletedSessions.length} 个对话`];
      if (report.deletedWorkspaces.length) parts.push(`${report.deletedWorkspaces.length} 个工作区`);
      const freed = report.bytesFreed ? `，释放 ${formatSize(report.bytesFreed)}` : '';
      if (report.errors.length) {
        toast(`${parts.join('、')}${freed}；${report.errors.length} 个文件删除失败`, 'error');
      } else {
        toast(`${parts.join('、')}${freed}`, 'ok');
      }
      state.selectedSessions.clear();
      state.selectedWorkspaces.clear();
      applyState(result.state);
      if (result.state && result.state.dsh && result.state.dsh.running) {
        renderBanner();
      }
    } catch (error) {
      toast(`请求失败：${error.message}`, 'error');
    } finally {
      state.busy = false;
      updateSelectionUI();
    }
  }

  /* ---------------------------------------------------------------- toast */

  let toastTimer = null;
  let toastHideTimer = null;
  function toast(message, kind = 'ok') {
    if (toastHideTimer) {
      clearTimeout(toastHideTimer);
      toastHideTimer = null;
    }
    els.toast.hidden = false;
    els.toast.classList.toggle('is-error', kind === 'error');
    els.toastIcon.textContent = kind === 'error' ? '!' : '✓';
    els.toastText.textContent = message;
    requestAnimationFrame(() => els.toast.classList.add('visible'));
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      els.toast.classList.remove('visible');
      toastHideTimer = setTimeout(() => {
        els.toast.hidden = true;
        toastHideTimer = null;
      }, 220);
    }, 4200);
  }

  /* --------------------------------------------------------------- events */

  els.refresh.addEventListener('click', () => refreshState({ deep: false }));

  els.recheck.addEventListener('click', () => refreshState({ deep: true, quiet: true }));

  els.search.addEventListener('input', () => {
    state.filter = els.search.value || '';
    if (state.data) renderList();
  });

  els.selectAll.addEventListener('click', () => {
    if (!state.data) return;
    state.selectedWorkspaces.clear();
    state.selectedSessions.clear();
    for (const workspace of state.data.workspaces) {
      for (const session of workspace.sessions) state.selectedSessions.add(session.id);
    }
    updateSelectionUI();
  });

  els.clearSelection.addEventListener('click', () => {
    state.selectedSessions.clear();
    state.selectedWorkspaces.clear();
    updateSelectionUI();
  });

  els.expandAll.addEventListener('click', () => {
    state.collapsed.clear();
    if (state.data) renderList();
  });

  els.collapseAll.addEventListener('click', () => {
    if (!state.data) return;
    state.collapsed = new Set(state.data.workspaces.map((workspace) => workspace.id));
    renderList();
  });

  els.deleteSelected.addEventListener('click', () => {
    const { sessions, workspaces } = effectiveSelection();
    openConfirm({ sessionIds: [...sessions], workspaceIds: [...workspaces] });
  });

  els.list.addEventListener('click', async (event) => {
    const target = event.target.closest('[data-act]');
    if (!target) return;
    const action = target.dataset.act;
    if (action === 'copy-ws-id' || action === 'copy-session-id') {
      const ok = await copyText(target.dataset.id || '');
      toast(ok ? 'ID 已复制' : '复制失败，请手动选择文本', ok ? 'ok' : 'error');
      return;
    }
    if (action === 'delete-ws') {
      openConfirm({ workspaceIds: [target.dataset.id] });
      return;
    }
    if (action === 'delete-session') {
      openConfirm({ sessionIds: [target.dataset.id] });
      return;
    }
    if (action === 'toggle-ws') {
      const workspaceId = target.dataset.id;
      if (state.collapsed.has(workspaceId)) state.collapsed.delete(workspaceId);
      else state.collapsed.add(workspaceId);
      renderList();
    }
  });

  els.dialog.addEventListener('close', () => {
    if (els.dialog.returnValue === 'confirm') {
      performDelete();
    } else {
      state.dialogPayload = null;
    }
  });

  let initialized = false;
  function init() {
    if (initialized) return;
    initialized = true;
    refreshState({ deep: true });
  }

  document.addEventListener('DOMContentLoaded', init);
  if (document.readyState !== 'loading') init();
})();
