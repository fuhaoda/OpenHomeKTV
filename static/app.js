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
      cell.colSpan = 5;
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
      songCell.appendChild(textElement('span', song.title, 'queue-title'));
      songCell.appendChild(textElement('span', song.user, 'queue-singer-mobile has-text-success'));
      row.appendChild(songCell);
      row.appendChild(textElement('td', song.user, 'queue-user has-text-success'));
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
            $.get('/queue/edit', {action: 'move', from: ui.item.data('queueStart'), to: ui.item.index(), size: songs.length})
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
    renderSong: renderSong, renderQueue: renderQueue, startQueue: startQueue
  };
}));
