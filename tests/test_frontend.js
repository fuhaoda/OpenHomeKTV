'use strict';
const assert = require('node:assert/strict');
const KTV = require('../static/app.js');

assert.equal(KTV.parseData('{"title":"Don\'t Stop"}').title, "Don't Stop");
assert.equal(KTV.parseData('not json'), null);
assert.equal(KTV.parseData(''), null);
const original = [{file: '/KTV/song.mp4'}];
assert.equal(KTV.parseData(original), original);
let received;
assert.equal(KTV.dispatchMessage(JSON.stringify({type: 'queue', data: original}), {queue: data => { received = data; }}), true);
assert.deepEqual(received, original);
assert.equal(KTV.dispatchMessage('showNotification("executable")', {}), false);
assert.equal(KTV.dispatchMessage('{"type":"unknown","data":1}', {}), false);
assert.equal(KTV.formatTime(null), '00:00:00');
assert.equal(KTV.formatTime(Infinity), '00:00:00');
assert.equal(KTV.formatTime(-10), '00:00:00');
assert.equal(KTV.formatTime(3661), '01:01:01');
assert.equal(KTV.formatTime(90000), '25:00:00');
let notice;
global.showNotification = (message, category) => { notice = {message, category}; };
KTV.showRequestError({responseJSON: {error: `VLC couldn't start`}});
assert.deepEqual(notice, {message: `VLC couldn't start`, category: 'is-danger'});
KTV.showRequestError({status: 0});


class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this._text = ''; this.className = ''; }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this[name] = value; }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get classList() {
    return {
      add: value => { this.className += ' ' + value; },
      remove: value => { this.className = this.className.split(' ').filter(item => item !== value).join(' '); }
    };
  }
}
global.document = {createElement: tag => new Element(tag), createTextNode: text => ({textContent: text})};
const title = `Don't Stop <img src=x onerror=alert(1)> "双音轨"`;
const user = `A&B / "Singer's name"`;
const path = `/KTV/Artist's folder/${title}.mp4`;
const summary = new Element('div');
KTV.renderSong(summary, title, user, 'empty');
assert.equal(summary.children[0].textContent, title);
assert.equal(summary.children[1].textContent, user);
assert.equal(summary.children[0].children.length, 0, 'Song title must stay a text node');
const table = new Element('table');
const body = KTV.renderQueue(table, [{file: path, title, user}, {file: '/other/copy.mp4', title, user}], {});
assert.equal(body.children.length, 2, 'Same-title files must remain separate rows');
assert.equal(body.children[0].dataset.file, path);
assert.equal(body.children[0].children[2].children[0].textContent, title);
const deleteButton = body.children[0].children[4].children[0];
assert.equal(deleteButton.dataset.file, path);
assert.equal(deleteButton.dataset.action, 'delete');
assert.equal(deleteButton.onclick, undefined, 'File names must not become inline JavaScript');
assert.equal(deleteButton.children.length, 1, 'Title markup must not create DOM elements');
console.log('Frontend tests passed: JSON message dispatch, quote/HTML-safe song and queue rendering, duplicate titles, and time formatting.');
