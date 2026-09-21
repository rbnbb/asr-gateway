// Controller tests with DOM/fetch stubs, not microphone/browser integration tests.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../asr_gateway/recorder.js'), 'utf8');

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status,
    headers: { get: () => 'application/json' }, json: async () => data,
    text: async () => JSON.stringify(data) };
}
async function app(initial = {}) {
  const storage = new Map(Object.entries(initial)), elements = new Map(), calls = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', hidden: false, disabled: false, textContent: '', style: {}, options: [],
      replaceChildren() { this.options = []; this.value = ''; },
      append(option) { this.options.push(option); if (!this.value) this.value = option.value; }
    });
    return elements.get(id);
  }
  const state = { handler: async url => {
    if (url === '/session') return response({ authenticated: true });
    if (url === '/v1/models') return response({ data: [{ id: 'configured-model' }, { id: 'second-model' }] });
    throw Error('Unexpected request: ' + url);
  }};
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => ({ value: '', textContent: '' }) },
    localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    fetch: async (url, options) => { calls.push({ url, options }); return state.handler(url, options); },
    crypto: { randomUUID: () => 'test-retry-key' },
    setTimeout: callback => { queueMicrotask(callback); return 1; },
    window: { addEventListener() {} }, navigator: {}, location: { origin: 'https://asr.example.test', search: '' }, URLSearchParams, console
  });
  vm.runInContext(source, context);
  await new Promise(setImmediate); // complete initialize()
  return { context, storage, element, calls, state };
}

test('models load automatically, first is default, session hides key entry', async () => {
  const a = await app();
  assert.equal(a.element('model').value, 'configured-model');
  assert.equal(a.element('model').options.length, 2);
  assert.equal(a.element('login').hidden, true);
  assert.equal(a.element('record').disabled, false);
  assert.equal(a.calls[1].options.headers.Authorization, undefined);
});

test('remembered model selection is restored when still available', async () => {
  const a = await app({ 'asr-model': 'second-model' });
  assert.equal(a.element('model').value, 'second-model');
});

test('failed job remains visible and retries retained audio without reupload', async () => {
  const a = await app({ 'asr-job': 'old-job' });
  a.state.handler = async (url, options) => {
    if (url === '/jobs/old-job') return response({ state: 'failed', attempts: 2, error: 'backend_connection_error', retryable: true });
    if (url === '/jobs/old-job/retry') {
      assert.equal(options.headers['Idempotency-Key'], 'test-retry-key');
      assert.equal(options.body, undefined);
      return response({ id: 'new-job' }, 202);
    }
    if (url === '/jobs/new-job') return response({ state: 'succeeded', attempts: 1 });
    if (url === '/jobs/new-job/result') return response({ text: 'recovered' });
    throw Error('Unexpected request');
  };
  await a.context.resume();
  assert.equal(a.element('retry-job').hidden, false);
  assert.match(a.element('status').textContent, /Recording retained/);
  await a.context.retryJob();
  assert.equal(a.element('result').value, 'recovered');
  assert.equal(a.storage.get('asr-job'), 'new-job');
  assert.equal(a.element('job').textContent, 'Job: new-job');
});

test('network interruption preserves job ID and allows later reconnection', async () => {
  const a = await app({ 'asr-job': 'saved-job' });
  a.state.handler = async () => { throw Error('network unavailable'); };
  await a.context.resume();
  assert.match(a.element('status').textContent, /Connection interrupted/);
  assert.equal(a.storage.get('asr-job'), 'saved-job');
  assert.equal(a.element('resume').disabled, false);
  a.state.handler = async url => url.endsWith('/result') ? response({ text: 'done' }) : response({ state: 'succeeded' });
  await a.context.resume();
  assert.equal(a.element('result').value, 'done');
});

test('legacy failed jobs explain that their recordings are unavailable', async () => {
  const a = await app({ 'asr-job': 'old-job' });
  a.state.handler = async () => response({ state: 'failed', error: 'backend_connection_error', retryable: false });
  await a.context.resume();
  assert.equal(a.element('retry-job').hidden, true);
  assert.match(a.element('status').textContent, /Please record again/);
});

test('expired sessions show login without discarding the saved job', async () => {
  const a = await app({ 'asr-job': 'saved-job' });
  a.state.handler = async () => response({}, 401);
  await a.context.resume();
  assert.equal(a.element('login').hidden, false);
  assert.equal(a.storage.get('asr-job'), 'saved-job');
  assert.equal(a.element('record').disabled, true);
});

test('retry request reuses its idempotency key after an ambiguous network failure', async () => {
  const a = await app({ 'asr-job': 'old-job' });
  const keys = [];
  a.state.handler = async (url, options) => {
    if (url.endsWith('/retry')) {
      keys.push(options.headers['Idempotency-Key']);
      if (keys.length === 1) throw Error('Response lost');
      return response({ id: 'new-job' }, 202);
    }
    if (url.endsWith('/result')) return response({ text: 'success' });
    return response({ state: 'succeeded' });
  };
  await a.context.retryJob();
  assert.equal(a.storage.get('asr-job'), 'old-job');
  await a.context.retryJob();
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
  assert.equal(a.element('result').value, 'success');
});
