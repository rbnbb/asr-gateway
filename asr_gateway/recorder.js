const el = id => document.getElementById(id);
let recorder, stream, chunks = [], upload = null, busy = false;
let savedId = readSaved('asr-job'), retryToken = null;
let authenticated = false;

function readSaved(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writeSaved(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch { /* Current-page recovery still works if storage is unavailable. */ }
}
function remember(id) {
  savedId = id;
  writeSaved('asr-job', id);
  el('job').textContent = id ? 'Job: ' + id : '';
}
function status(message) { el('status').textContent = message; }
function setBusy(value) {
  busy = value;
  el('record').disabled = value || !authenticated;
  el('resume').disabled = value;
  el('retry').disabled = value;
  el('retry-job').disabled = value;
}
async function request(url, options = {}) {
  const response = await fetch(url, {
    ...options, headers: { ...options.headers }
  });
  if (!response.ok) {
    const error = Error(response.status === 401 ? 'Please sign in again.' :
      'Request failed (' + response.status + ').');
    error.status = response.status;
    throw error;
  }
  return response;
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function showLogin(value) {
  authenticated = !value;
  el('login').hidden = !value;
  el('signed-in').hidden = value;
  el('model').disabled = value;
  setBusy(busy);
}
async function loadModels() {
  const data = await (await request('/v1/models')).json();
  el('model').replaceChildren();
  for (const model of data.data) {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.id;
    el('model').append(option);
  }
  const previous = readSaved('asr-model');
  if (data.data.some(model => model.id === previous)) el('model').value = previous;
  if (!data.data.length) status('No models are configured on this gateway.');
}
async function initialize() {
  setBusy(true);
  try {
    await request('/session');
    showLogin(false);
    await loadModels();
    status(savedId ? 'Saved job available. Check it to retrieve the result.' : 'Ready to record.');
  } catch (error) {
    showLogin(true);
    status(error.status === 401 ? (new URLSearchParams(location.search).get('login') === 'failed' ? 'Username or password was rejected.' : 'Sign in to record.') : 'Cannot connect to the gateway.');
  } finally { setBusy(false); }
}
el('logout').onclick = async () => {
  if (busy) return;
  try {
    await request('/session', { method: 'DELETE' });
    showLogin(true);
    status('Signed out.');
  } catch (error) { status(error.message); }
};
el('model').onchange = () => writeSaved('asr-model', el('model').value);

async function poll(id) {
  remember(id);
  el('retry-job').hidden = true;
  let failures = 0;
  for (;;) {
    try {
      const job = await (await request('/jobs/' + id)).json();
      const attempt = job.attempts ? ' (attempt ' + job.attempts + ')' : '';
      const labels = {
        queued: job.error ? 'Queued for retry: ' + job.error : 'Queued',
        waking: 'Waiting for transcription server',
        transcribing: 'Transcribing'
      };
      status((labels[job.state] || job.state) + attempt);
      if (job.state === 'failed') {
        el('retry-job').hidden = !job.retryable;
        status('Transcription failed: ' + job.error + '. ' +
          (job.retryable ? 'Recording retained; you can retry.' :
            'No retained recording is available. Please record again.'));
        return;
      }
      if (job.state === 'succeeded') {
        const response = await request('/jobs/' + id + '/result');
        const type = response.headers.get('content-type') || '';
        el('result').value = type.includes('json') ?
          (await response.json()).text : await response.text();
        // Keep the ID visible/retrievable until another job replaces it or it expires.
        status('Ready to copy.');
        return;
      }
      failures = 0;
    } catch (error) {
      if (error.status === 401) { showLogin(true); status('Sign in again, then check the saved job.'); return; }
      if (error.status === 404) { status('Job not found or expired. Please record again.'); return; }
      failures += 1;
      if (failures >= 3) {
        status('Connection interrupted. Job ID saved; use Check saved job to reconnect.');
        return;
      }
      status('Connection interrupted. Reconnecting to saved job…');
    }
    await delay(1500);
  }
}

async function submit() {
  if (busy || !upload) return;
  setBusy(true);
  el('retry').style.display = 'none';
  try {
    status('Uploading…');
    const job = await (await request('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': upload.type, 'Idempotency-Key': upload.key },
      body: upload.body
    })).json();
    remember(job.id);
    upload = null;
    await poll(job.id);
  } catch (error) {
    status(error.message);
    if (upload) el('retry').style.display = 'inline-block';
  } finally { setBusy(false); }
}

async function resume() {
  if (busy) return;
  if (!savedId) { status('No saved job.'); return; }
  setBusy(true);
  try { await poll(savedId); }
  finally { setBusy(false); }
}

async function retryJob() {
  if (busy || !savedId) return;
  setBusy(true);
  try {
    const previous = savedId;
    const storageKey = 'asr-retry-' + previous;
    retryToken = retryToken && retryToken.previous === previous ? retryToken : {
      previous, key: readSaved(storageKey) || crypto.randomUUID()
    };
    writeSaved(storageKey, retryToken.key);
    const job = await (await request('/jobs/' + previous + '/retry', {
      method: 'POST', headers: { 'Idempotency-Key': retryToken.key }
    })).json();
    remember(job.id);
    writeSaved(storageKey, null);
    retryToken = null;
    await poll(job.id);
  } catch (error) {
    status(error.status === 409 ? 'This job has no retryable recording.' :
      error.message + ' Job ID saved; retry again when connected.');
  } finally { setBusy(false); }
}

el('retry').onclick = submit;
el('resume').onclick = resume;
el('retry-job').onclick = retryJob;
el('record').onclick = async () => {
  if (busy) return;
  try {
    if (recorder && recorder.state === 'recording') {
      setBusy(true); // Prevent another click during asynchronous stop serialization.
      recorder.stop();
      return;
    }
    if (!el('model').value.trim()) throw Error('Select a configured model first.');
    setBusy(true);
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = async () => {
      try {
        stream.getTracks().forEach(track => track.stop());
        el('record').textContent = 'Record';
        const type = recorder.mimeType, form = new FormData();
        const extension = type.includes('mp4') ? 'mp4' : type.includes('ogg') ? 'ogg' : 'webm';
        form.append('file', new Blob(chunks, { type }), 'recording.' + extension);
        form.append('model', el('model').value.trim());
        form.append('response_format', 'json');
        const serialized = new Request(location.origin + '/jobs', { method: 'POST', body: form });
        upload = { body: await serialized.arrayBuffer(), type: serialized.headers.get('Content-Type'), key: crypto.randomUUID() };
        setBusy(false);
        await submit();
      } catch (error) { status(error.message); setBusy(false); }
    };
    recorder.start();
    setBusy(false);
    el('resume').disabled = true;
    el('retry-job').disabled = true;
    el('retry').disabled = true;
    el('record').textContent = 'Stop';
    el('retry-job').hidden = true;
    status('Recording…');
  } catch (error) {
    if (stream) stream.getTracks().forEach(track => track.stop());
    status(error.message);
    setBusy(false);
  }
};
el('copy').onclick = async () => {
  try { await navigator.clipboard.writeText(el('result').value); status('Copied.'); }
  catch { status('Select the text and copy it manually.'); }
};
window.addEventListener('beforeunload', event => {
  if (upload || (recorder && recorder.state === 'recording')) {
    event.preventDefault(); event.returnValue = '';
  }
});
remember(savedId);
if (savedId) status('Saved job available. Sign in and check it.');
initialize();
