const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const page = fs.readFileSync(process.argv[2], 'utf8').replaceAll('__BASE__', '/plugins/freetier2api');
const nodes = new Map();
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.events = {}; this.style = {}; this.value = ''; this.hidden = false; this.disabled = false; this._text = ''; this.className = ''; }
  set textContent(text) { this._text = String(text); this.children = []; if (this.tagName === 'SELECT') this._value = null; }
  set value(value) { this._value = this.tagName === 'SELECT' && !this.children.some(option => option.value === String(value)) ? '' : String(value); }
  get value() { return this._value === null && this.tagName === 'SELECT' ? (this.children[0]?.value || '') : this._value; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(text) { assert.equal(text, '', 'dynamic content must use textContent'); this.textContent = ''; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'hidden') this.hidden = true; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; if (key === 'hidden') this.hidden = false; }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  dispatch(type, extra = {}) { const ev = {target: this, preventDefault() {}, stopPropagation() {}, ...extra}; for (const fn of this.events[type] || []) fn.call(this, ev); }
  click() { if (!this.disabled) this.dispatch('click'); }
  focus() { document.activeElement = this; }
  setSelectionRange() {}
  get childElementCount() { return this.children.length; }
  get firstChild() { return this.children[0]; }
  querySelectorAll(selector) { return descendants(this).filter(node => matches(node, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
function matches(node, selector) {
  const tag = selector.match(/^[a-z]+/i);
  if (tag && node.tagName !== tag[0].toUpperCase()) return false;
  const cls = selector.match(/\.([\w-]+)/);
  if (cls && !node.className.split(' ').includes(cls[1])) return false;
  for (const [,key,value] of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
    const actual = node.getAttribute(key) ?? node[key];
    if (actual == null || (value && String(actual) !== value)) return false;
  }
  return true;
}
const document = global.document = {activeElement: null, events: {}, createElement: tag => new Element(tag), getElementById: id => nodes.get(id) || null,
  addEventListener: Element.prototype.addEventListener,
  querySelectorAll(selector) { const match = selector.match(/^#([\w-]+)\s+(.+)$/); return match ? nodes.get(match[1]).querySelectorAll(match[2]) : [...nodes.values()].flatMap(descendants).filter(node => matches(node, selector)); }
};
for (const match of page.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  const node = new Element(match[1]); node.id = match[3]; node.hidden = /\bhidden\b/.test(match[2]); node.disabled = /\bdisabled\b/.test(match[2]); nodes.set(node.id, node);
}
global.window = {confirm: () => true}; global.location = {host: 'fixture.local'};
Object.defineProperty(global, 'navigator', {value: {userAgent: 'fixture'}, configurable: true});
const store = new Map([['cli-proxy-auth', JSON.stringify({state: {managementKey: 'fixture-key'}})]]);
global.localStorage = {getItem: key => store.get(key) || null, setItem: (key,value) => store.set(key,value)};
global.sessionStorage = {getItem: () => null, setItem() {}};
const timers = new Map(); let timerID = 0;
global.setTimeout = (fn, ms) => { timers.set(++timerID, {fn, ms}); return timerID; };
global.setInterval = () => 1; global.clearTimeout = id => timers.delete(id); global.clearInterval = () => {};
const accounts = [
  {auth_id: 'runtime-a', auth_index: 'index-a', file_id: 'file-a', vendor_id: 'minimaxcodecn', alias: '中文别名', native_label: 'native-a'},
  {auth_id: 'runtime-b', auth_index: 'index-b', file_id: 'file-b', vendor_id: 'minimaxcodecn', native_label: '原始名称'},
  {auth_index: 'index-disabled', vendor_id: 'minimaxcodecn', disabled: true, alias: '禁用账号'},
  {auth_index: 'index-global', vendor_id: 'minimaxcodeglobal', alias: '其它区域'}
];
const models = [{id: 'minimax-m3', alias: 'my-model', scope_id: 'minimaxcodecn:minimax-m3', vendor_id: 'minimaxcodecn'},
  {id: 'disabled-model', vendor_id: 'minimaxcodecn', disabled: true},
  {id: 'minimax-m4', alias: 'second-model', vendor_id: 'minimaxcodecn'},
  {id: 'global-only', vendor_id: 'minimaxcodeglobal'}];
const calls = []; let probeResolve; let statusFailure = null;
global.fetch = async (url, options) => {
  const path = new URL(url, 'https://fixture.local').pathname.replace('/v0/management/plugins/freetier2api', '');
  const body = options.body ? JSON.parse(options.body) : null; calls.push({path, method: options.method, body});
  let data;
  if (path === '/vendors') data = {vendors: [{id: 'minimaxcodecn', name: 'MiniMax CN'}, {id: 'minimaxcodeglobal', name: 'MiniMax Global'}]};
  else if (path === '/status') { if (statusFailure) throw new Error(statusFailure); data = {accounts, settings: {}}; }
  else if (path === '/models') data = {models};
  else if (path === '/accounts/alias') { accounts.find(account => account.file_id === body.file_id).alias = body.alias; data = {ok: true}; }
  else if (path === '/quotas') data = {results: []};
  else if (path === '/logs') data = {entries: []};
  else if (path === '/models/probe') return new Promise((resolve, reject) => { if (options.signal) options.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), {name: "AbortError"})), {once: true}); probeResolve = data => resolve({status: 200, ok: true, json: async () => data}); });
  else throw new Error('unexpected request ' + path);
  return {status: 200, ok: true, json: async () => JSON.parse(JSON.stringify(data))};
};
const flush = () => new Promise(resolve => setImmediate(resolve));
const probes = () => calls.filter(c => c.path === '/models/probe');
(async () => {
  const script = page.match(/<script>([\s\S]*?)<\/script>/)[1]; vm.runInThisContext(script);
  await flush();
  assert.equal(probes().length, 0, 'page load must not execute a model');
  const table = nodes.get('models').children[0];
  const row = table.children.find(row => row.textContent.includes('my-model'));
  assert.ok(row, 'model row rendered');
  const checkbox = row.querySelector('input[type=checkbox]'); checkbox.checked = true;
  const button = row.children.at(-1).querySelector('button');
  assert.ok(button && button.textContent === '测速', 'model list needs a rightmost probe button');
  const disabledRow = table.children.find(row => row.textContent.includes('disabled-model'));
  assert.ok(disabledRow.children.at(-1).querySelector('button').disabled, 'disabled model has no executable action');
  accounts[0].alias = '改名后的账号';
  const statusBefore = calls.filter(c => c.path === '/status').length;
  button.click(); await flush();
  assert.equal(nodes.get('probeModal').hidden, false);
  assert.ok(calls.filter(c => c.path === '/status').length > statusBefore, 'opening refreshes account names');
  const options = nodes.get('probeAccount').children;
  assert.deepEqual(options.map(o => [o.value,o.textContent]), [['index-a','改名后的账号'],['index-b','原始名称']]);
  assert.equal(probes().length, 0, 'opening the selector never executes a model');
  assert.ok(checkbox.checked, 'opening probe preserves model selection');
  const select = nodes.get('probeAccount');
  select.value = 'index-b'; select.dispatch('change');
  const run = nodes.get('btnProbeRun');
  run.click(); run.dispatch('click'); await flush();
  assert.deepEqual(probes(), [{path: '/models/probe', method: 'POST', body: {auth_index: 'index-b', vendor_id: 'minimaxcodecn', model: 'my-model'}}], 'one exact pinned request even on double click');
  assert.ok(run.disabled && select.disabled, 'pending request locks inputs');
  nodes.get('btnProbeClose').click();
  assert.equal(nodes.get('probeModal').hidden, false, 'pending request remains visible');
  const answer = '<img src=x onerror=alert(1)> ok';
  probeResolve({ok: true, status: 200, latency_ms: 42, model: 'my-model', auth_index: 'index-b', text: answer}); await flush();
  assert.equal(nodes.get('probeOutput').textContent, answer);
  assert.equal(nodes.get('probeOutput').children.length, 0, 'upstream response remains literal text');
  assert.match(nodes.get('probeSummary').textContent, /通过.*200.*42 ms/);
  assert.match(nodes.get('probeIdentity').textContent, /原始名称/);
  assert.equal(run.disabled, false);
  assert.equal(select.value, 'index-b', 'selected credential stays pinned after completion');
  const accountRow = nodes.get('accounts').children[0].children.find(row => row.textContent.includes('原始名称'));
  const nameCell = accountRow.children[3]; nameCell.children[0].click();
  const rename = nameCell.querySelector('input'); rename.value = '刚修改的别名'; rename.dispatch('keydown', {key: 'Enter'}); await flush();
  assert.equal(select.children.find(option => option.value === 'index-b').textContent, '刚修改的别名', 'selector follows alias edit immediately');
  assert.match(nodes.get('probeIdentity').textContent, /刚修改的别名/, 'result identity follows alias edit immediately');
  const upstreamError = '{"code":11158,"msg":"model is not allowed for user type [personal_free_user]"}';
  run.click(); await flush();
  probeResolve({ok: false, status: 400, latency_ms: 81, error: upstreamError}); await flush();
  assert.match(nodes.get('probeSummary').textContent, /失败.*400/);
  assert.equal(nodes.get('probeOutput').textContent, upstreamError, 'code and message stay unchanged');
  run.click(); await flush();
  probeResolve({ok: false, status: 200, latency_ms: 0, text: '', error: 'empty model response'}); await flush();
  assert.match(nodes.get('probeSummary').textContent, /失败.*200.*0 ms/);
  assert.equal(nodes.get('probeOutput').textContent, 'empty model response');
  run.click(); await flush();
  probeResolve({ok: true, status: 200, latency_ms: 0, text: '  '}); await flush();
  assert.match(nodes.get('probeSummary').textContent, /失败/,'empty content never counts as a passed probe');
  run.click(); await flush();
  probeResolve({ok: true, status: 503, latency_ms: 11, text: 'upstream unavailable'}); await flush();
  assert.match(nodes.get('probeSummary').textContent, /失败.*503/, 'non-2xx is never a passed probe');
  run.click(); await flush();
  const timer = [...timers.values()].find(timer => timer.ms === 90000);
  assert.ok(timer, 'probe must enforce its advertised 90 second browser limit');
  timer.fn(); await flush();
  assert.equal(run.disabled, false, 'aborted request unlocks execution');
  assert.equal(nodes.get('btnProbeClose').disabled, false, 'aborted request unlocks close');
  assert.match(nodes.get('probeOutput').textContent, /90 秒/);
  const abortedSummary = nodes.get('probeSummary').textContent;
  probeResolve({ok: true, status: 200, text: 'late response'}); await flush();
  assert.equal(nodes.get('probeSummary').textContent, abortedSummary, 'late result never overwrites cancellation');
  nodes.get('btnProbeClose').click();
  assert.equal(nodes.get('probeModal').hidden, true);
  const refreshedAccountRow = nodes.get('accounts').children[0].children.find(row => row.textContent.includes('刚修改的别名'));
  const accountButton = refreshedAccountRow.children.at(-1).querySelector('button');
  assert.ok(accountButton && accountButton.textContent === '测速', 'account row retains the approved shortcut');
  const beforeAccount = probes().length; accountButton.click(); await flush();
  assert.equal(select.value, 'index-b', 'account shortcut selects that exact account, not the first');
  const modelSelect = nodes.get('probeModelSelect');
  assert.equal(modelSelect.hidden, false);
  assert.deepEqual(modelSelect.children.map(option => option.textContent), ['my-model','second-model'], 'model choices exclude disabled and other vendors');
  assert.equal(probes().length, beforeAccount, 'account shortcut only opens selection');
  modelSelect.value = modelSelect.children[1].value; modelSelect.dispatch('change');
  run.click(); await flush();
  assert.deepEqual(probes().at(-1).body, {auth_index: 'index-b', vendor_id: 'minimaxcodecn', model: 'second-model'});
  assert.ok(modelSelect.disabled, 'model is locked during execution');
  probeResolve({ok: true, status: 200, latency_ms: 8, text: 'ok'}); await flush();
  assert.match(nodes.get('probeIdentity').textContent, /second-model/);
  select.value = 'index-a'; select.dispatch('change');
  run.click(); await flush();
  assert.equal(probes().at(-1).body.auth_index, 'index-a', 'switching to another same-vendor credential is explicit');
  probeResolve({ok: true, status: 200, latency_ms: 9, text: 'ok'}); await flush();
  nodes.get('btnProbeClose').click();
  accounts[1].disabled = true;
  accountButton.click(); await flush();
  assert.equal(select.value, '', 'disabled originating account is not replaced by the first account');
  assert.ok(run.disabled);
  nodes.get('btnProbeClose').click(); accounts[1].disabled = false;
  models.forEach(model => { if (model.vendor_id === 'minimaxcodecn') model.disabled = true; });
  accountButton.click(); await flush();
  assert.equal(modelSelect.children.length, 0);
  assert.ok(run.disabled, 'no enabled models means no execution');
  assert.match(nodes.get('probeHint').textContent, /没有已启用的模型/);
  nodes.get('btnProbeClose').click(); models[0].disabled = false;
  accounts[0].disabled = true; accounts[1].disabled = true;
  button.click(); await flush();
  assert.equal(select.children.length, 0, 'no disabled or other-vendor credential leaks into selector');
  assert.ok(run.disabled);
  assert.match(nodes.get('probeHint').textContent, /没有已启用/);
  const count = probes().length; run.dispatch('click'); await flush();
  assert.equal(probes().length, count, 'no selectable account means no request');
  nodes.get('btnProbeClose').click(); accounts[0].disabled = false;
  statusFailure = 'status request failed'; button.click(); await flush();
  assert.ok(run.disabled, 'failed refresh must not execute with stale credentials');
  assert.equal(nodes.get('probeHint').textContent, 'status request failed');
  run.dispatch('click'); await flush(); assert.equal(probes().length, count);

  console.log('model and account probe entries, pinned execution, alias edits and disabled filtering passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
