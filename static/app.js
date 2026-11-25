/* Shared data and rendering helpers. Messages and filenames are always data. */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KTV = api;
}(typeof window !== 'undefined' ? window : globalThis, function (root) {
  'use strict';

  function parseData(value) {
    if (typeof value !== 'string') return value;
    if (!value) return null;
    try { return JSON.parse(value); } catch (_) { return null; }
  }

  function dispatchMessage(raw, handlers) {
    var message = parseData(raw);
    if (!message || Array.isArray(message) || typeof message !== 'object') return false;
    if (!['nowplaying', 'queue', 'notification'].includes(message.type)) return false;
    if (typeof handlers[message.type] !== 'function') return false;
    handlers[message.type](message.data);
    return true;
  }

  function showRequestError(xhr) {
    var error = xhr && xhr.responseJSON && xhr.responseJSON.error;
    if (typeof error === 'string' && typeof root.showNotification === 'function') {
      root.showNotification(error, 'is-danger', 6000);
    }
  }

  function normalizeUserName(value) {
    if (typeof value !== 'string') return '';
    // Match Python str.strip() before NFC normalization, including Unicode spaces.
    return value.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, '').normalize('NFC');
  }

  function isValidUserName(value) {
    var name = normalizeUserName(value);
    var length = Array.from(name).length;
    return length >= 1 && length <= 10 && !/[\u0000-\u001f\u007f-\u009f]/.test(name);
  }

  function userNameMessages() {
    var language = (root.Cookies && root.Cookies.get('lang')) || (root.navigator && root.navigator.language) || 'en';
    return /^zh(?:[-_]|$)/i.test(language) ? {
      prompt: '请输入姓名（1–10 个字符），姓名会显示在你点的歌曲旁边。',
      invalid: '姓名必须为 1–10 个字符，不能包含换行或控制字符。请重新输入。',
      unset: '请输入姓名'
    } : {
      prompt: 'Please enter your name (1–10 characters). It will appear beside your songs.',
      invalid: 'Use 1–10 characters, without line breaks or control characters. Please try again.',
      unset: 'Set name'
    };
  }

  function displayUserName(name) {
    root.currentUser = name || null;
    var link = root.document && root.document.querySelector && root.document.querySelector('#current-user');
    if (link) {
      link.classList.remove('is-hidden');
      var label = link.querySelector('span');
      if (label) label.textContent = name || userNameMessages().unset;
    }
  }

  function getSavedUserName() {
    if (!root.Cookies) return null;
    var raw = root.Cookies.get('user');
    var name = normalizeUserName(raw);
    // Older versions silently assigned user-N; require one explicit confirmation.
    var generated = /^user-\d+$/i.test(name) && root.Cookies.get('user_confirmed') !== '1';
    if (!isValidUserName(name) || generated) {
      root.Cookies.remove('user');
      root.Cookies.remove('user_confirmed');
      return null;
    }
    return name;
  }

  function rememberUserName(value) {
    var name = normalizeUserName(value);
    if (!isValidUserName(name)) return null;
    if (root.Cookies) {
      root.Cookies.set('user', name, {expires: 3650});
      root.Cookies.set('user_confirmed', '1', {expires: 3650});
    }
    displayUserName(name);
    return name;
  }

  function promptUserName(initialName) {
    var messages = userNameMessages();
    var message = messages.prompt;
    var candidate = normalizeUserName(initialName);
    while (true) {
      var answer = root.prompt(message, candidate);
      if (answer === null) return null;
      candidate = normalizeUserName(answer);
      if (isValidUserName(candidate)) return candidate;
      message = messages.invalid + '\n\n' + messages.prompt;
    }
  }

  function initializeUserName() {
    var name = getSavedUserName();
    displayUserName(name);
    return name;
  }

  function ensureUserName() {
    var name = getSavedUserName();
    if (!name) name = promptUserName('');
    return name === null ? null : rememberUserName(name);
  }

  function formatTime(value) {
    var seconds = Number(value);
    seconds = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
      .map(function (part) { return String(part).padStart(2, '0'); }).join(':');
  }

  function textElement(tag, text, className) {
    var element = root.document.createElement(tag);
    if (className) element.className = className;
    element.textContent = text == null ? '' : String(text);
    return element;
  }

  function renderSong(target, title, singer, emptyMessage) {
    if (!target) return;
    target.textContent = '';
    if (!title) {
      target.textContent = emptyMessage || '';
      return;
    }
    target.appendChild(textElement('span', title, 'song-title'));
    if (singer) {
      var user = textElement('span', null, 'song-singer has-text-success');
      var icon = textElement('i', null, 'icon icon-adult');
      icon.setAttribute('aria-hidden', 'true');
      user.appendChild(icon);
      user.appendChild(root.document.createTextNode(String(singer)));
      target.appendChild(user);
    }
  }

  function renderQueue(table, songs, labels) {
    var body = root.document.createElement('tbody');
    labels = labels || {};
    if (!songs.length) {
      var empty = root.document.createElement('tr');
      var cell = textElement('td', labels.empty || '', 'has-text-warning');
      cell.colSpan = 4;
      empty.appendChild(cell);
      body.appendChild(empty);
    }
    songs.forEach(function (song, index) {
      var row = root.document.createElement('tr');
      row.dataset.file = song.file;
      row.appendChild(textElement('td', index + 1, 'queue-number'));
      var move = textElement('td', null, 'queue-move-cell');
      var actions = textElement('div', null, 'queue-actions');
      function button(action, icon, label, disabled) {
        var control = textElement('button', null, 'queue-action has-text-success');
        control.type = 'button';
        control.dataset.action = action;
        control.dataset.file = song.file;
        control.dataset.title = song.title;
        control.title = label || action;
        control.setAttribute('aria-label', label || action);
        control.disabled = !!disabled;
        control.appendChild(textElement('i', null, 'icon ' + icon));
        return control;
      }
      actions.appendChild(button('up', 'icon-up-circled', labels.up, index === 0));
      actions.appendChild(button('down', 'icon-down-circled', labels.down, index === songs.length - 1));
      move.appendChild(actions);
      row.appendChild(move);
      var songCell = textElement('td', null, 'queue-song');
      if (song.user) {
        songCell.appendChild(textElement('span', String(song.user) + ': ', 'queue-singer has-text-success'));
      }
      songCell.appendChild(textElement('span', song.title, 'queue-title'));
      row.appendChild(songCell);
      var remove = textElement('td', null, 'queue-delete-cell');
      var deleteButton = button('delete', 'icon-trash-empty', labels.remove);
      deleteButton.classList.remove('has-text-success');
      deleteButton.classList.add('has-text-danger');
      remove.appendChild(deleteButton);
      row.appendChild(remove);
      body.appendChild(row);
    });
    table.textContent = '';
    table.appendChild(body);
    return body;
  }

  function startQueue(options) {
    var $ = root.jQuery;
    var table = root.document.querySelector(options.table || '#auto-refresh');
    var songs = [], stopped = false, pending = null, dragging = null, lastPayload = null;
    if (!table) return { update: function () {}, refresh: function () {}, stop: function () {} };
    function active() { return !stopped && (!options.isActive || options.isActive()); }
    function update(data) {
      if (!active() || dragging !== null) return;
      var parsed = parseData(data);
      if (!Array.isArray(parsed)) return;
      parsed = parsed.filter(function (song) {
        return song && typeof song.file === 'string' && typeof song.title === 'string';
      });
      var payload = JSON.stringify(parsed);
      if (payload === lastPayload) return;
      songs = parsed;
      lastPayload = payload;
      var body = renderQueue(table, songs, options.labels);
      if ($.fn.sortable && songs.length) {
        $(body).sortable({
          items: '> tr[data-file]', handle: '.queue-number', cancel: 'button,a',
          tolerance: 'pointer', revert: true,
          start: function (_event, ui) { dragging = ui.item.index(); ui.item.data('queueStart', dragging); },
          stop: function () { dragging = null; },
          update: function (_event, ui) {
            $.post('/queue/edit', {
              action: 'move', from: ui.item.data('queueStart'), to: ui.item.index(), size: songs.length,
              order: JSON.stringify(songs.map(function (song) { return song.file; }))
            })
              .fail(showRequestError).always(function () { lastPayload = null; refresh(); });
          }
        });
      }
    }
    function refresh() {
      if (!active() || dragging !== null || pending) return;
      pending = $.get('/get_queue').done(function (data) { if (active()) update(data); })
        .always(function () { pending = null; });
    }
    function action(event) {
      var button = event.target.closest('button[data-action]');
      if (!button || !table.contains(button) || button.disabled || !active()) return;
      event.preventDefault();
      if (button.dataset.action === 'delete' && options.confirmDelete &&
          !root.confirm(options.confirmDelete + ' "' + button.dataset.title + '"?')) return;
      button.disabled = true;
      $.get('/queue/edit', {action: button.dataset.action, song: button.dataset.file})
        .fail(showRequestError).always(function () { button.disabled = false; refresh(); });
    }
    table.addEventListener('click', action);
    var timer = root.setInterval(refresh, 1500);
    refresh();
    return {
      update: update, refresh: refresh,
      stop: function () {
        stopped = true;
        root.clearInterval(timer);
        if (pending) pending.abort();
        table.removeEventListener('click', action);
      }
    };
  }

  return {
    parseData: parseData, dispatchMessage: dispatchMessage, formatTime: formatTime, showRequestError: showRequestError,
    normalizeUserName: normalizeUserName, isValidUserName: isValidUserName, getSavedUserName: getSavedUserName,
    rememberUserName: rememberUserName, promptUserName: promptUserName, initializeUserName: initializeUserName,
    ensureUserName: ensureUserName,
    renderSong: renderSong, renderQueue: renderQueue, startQueue: startQueue
  };
}));
