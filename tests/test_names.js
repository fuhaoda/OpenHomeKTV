'use strict';
const assert = require('node:assert/strict');
const KTV = require('../static/app.js');

assert.equal(KTV.normalizeUserName('  Haoda  '), 'Haoda');
assert.equal(KTV.normalizeUserName('\u3000小明\u3000'), '小明');
assert.equal(KTV.normalizeUserName('\u0085Haoda\u001f'), 'Haoda');
assert.equal(KTV.normalizeUserName('e\u0301'), 'é');
assert.equal(KTV.normalizeUserName(null), '');
assert.equal(KTV.isValidUserName(''), false);
assert.equal(KTV.isValidUserName('   '), false);
assert.equal(KTV.isValidUserName('1234567890'), true);
assert.equal(KTV.isValidUserName('12345678901'), false);
assert.equal(KTV.isValidUserName('一二三四五六七八九十'), true);
assert.equal(KTV.isValidUserName('一二三四五六七八九十名'), false);
assert.equal(KTV.isValidUserName('😀'.repeat(10)), true, 'An astral character counts once');
assert.equal(KTV.isValidUserName('😀'.repeat(11)), false);
assert.equal(KTV.isValidUserName('e\u0301'.repeat(10)), true, 'Normalize before counting');
assert.equal(KTV.isValidUserName('Haoda\nKTV'), false);
assert.equal(KTV.isValidUserName('A\u0085B'), false);
assert.equal(KTV.isValidUserName('A\u0000B'), false);
assert.equal(KTV.isValidUserName('A&B / 小明'), true);

const cookies = new Map();
global.Cookies = {
  get: name => cookies.get(name),
  set: (name, value) => cookies.set(name, value),
  remove: name => cookies.delete(name)
};
const label = {textContent: ''};
let hiddenRemoved = false;
global.document = {querySelector: selector => selector === '#current-user' ? {
  classList: {remove: name => { if (name === 'is-hidden') hiddenRemoved = true; }},
  querySelector: selector => selector === 'span' ? label : null
} : null};
let prompts;
function answers(values) {
  prompts = [];
  global.prompt = (message, initial) => {
    prompts.push({message, initial});
    assert.ok(values.length, 'Unexpected extra prompt');
    return values.shift();
  };
}
function reset() {
  cookies.clear();
  label.textContent = '';
  hiddenRemoved = false;
  answers([]);
}

reset();
assert.equal(KTV.initializeUserName(), null);
assert.equal(prompts.length, 0, 'Opening the page must not prompt');
assert.equal(label.textContent, 'Set name');
assert.equal(hiddenRemoved, true, 'Unset name menu remains available');
answers([null]);
assert.equal(KTV.ensureUserName(), null);
assert.equal(cookies.has('user'), false, 'Cancel must not create a singer');

reset();
answers(['12345678901', '   ', '  Haoda  ']);
assert.equal(KTV.ensureUserName(), 'Haoda');
assert.equal(prompts.length, 3);
assert.match(prompts[1].message, /1–10/);
assert.equal(cookies.get('user'), 'Haoda');
assert.equal(cookies.get('user_confirmed'), '1');
assert.equal(label.textContent, 'Haoda');
answers([]);
assert.equal(KTV.ensureUserName(), 'Haoda', 'Valid name is remembered for later songs');
assert.equal(prompts.length, 0);

reset();
cookies.set('user', '小明');
assert.equal(KTV.initializeUserName(), '小明', 'A previously entered real name remains usable');
assert.equal(prompts.length, 0);
assert.equal(label.textContent, '小明');

reset();
cookies.set('user', 'user-12');
assert.equal(KTV.initializeUserName(), null, 'Legacy automatic names need explicit confirmation');
assert.equal(cookies.has('user'), false);
answers(['user-12']);
assert.equal(KTV.ensureUserName(), 'user-12', 'A user may deliberately choose that name');
answers([]);
assert.equal(KTV.ensureUserName(), 'user-12');
assert.equal(prompts.length, 0);

reset();
cookies.set('user', 'TooLongSingerName');
cookies.set('user_confirmed', '1');
assert.equal(KTV.initializeUserName(), null);
assert.equal(cookies.has('user_confirmed'), false);
answers(['😀'.repeat(10)]);
assert.equal(KTV.ensureUserName(), '😀'.repeat(10));

reset();
cookies.set('lang', 'zh_CN');
KTV.initializeUserName();
assert.equal(label.textContent, '请输入姓名');
answers(['十一位名字名字名字名字', '中文名']);
assert.equal(KTV.ensureUserName(), '中文名');
assert.match(prompts[0].message, /请输入姓名/);
assert.match(prompts[1].message, /请重新输入/);

console.log('Name tests passed: Unicode limits, normalization, mandatory first choice, cancel, saved names, legacy generated names, and localized retry prompts.');
