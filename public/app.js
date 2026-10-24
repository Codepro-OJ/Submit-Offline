'use strict';

let token = '';
let contest = null;
let currentProblem = '';
let username = '';
let resultTimer = null;
const $ = (id) => document.getElementById(id);

function message(value) {
  $('message').textContent = value;
  if (value) setTimeout(() => { if ($('message').textContent === value) $('message').textContent = ''; }, 6000);
}

async function request(route, options = {}) {
  const response = await fetch(`/api/${route}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers
    }
  });
  const body = await response.json();
  if (response.status === 401 && route !== 'login') showLogin();
  if (!response.ok) throw new Error(body.error || 'Yêu cầu thất bại.');
  return body;
}

function showLogin() {
  if (resultTimer) clearInterval(resultTimer);
  resultTimer = null;
  token = '';
  contest = null;
  currentProblem = '';
  $('identity').textContent = '';
  $('source').value = '';
  $('loginPanel').hidden = false;
  $('examPanel').hidden = true;
  $('backupFolder').value = '';
  $('backupProgress').hidden = true;
}

function showView(view) {
  const backup = view === 'backup';
  $('examWorkspace').hidden = backup;
  $('backupPanel').hidden = !backup;
  $('examViewButton').classList.toggle('active-view', !backup);
  $('backupViewButton').classList.toggle('active-view', backup);
  if (backup) loadBackups().catch((error) => message(error.message));
}

function renderStatement(markdown) {
  const root = $('statement');
  root.replaceChildren();
  let code = null;
  for (const line of (markdown || '').split(/\r?\n/)) {
    if (line.startsWith('```')) {
      if (code) { code = null; } else {
        const block = document.createElement('pre');
        code = document.createElement('code');
        block.append(code);
        root.append(block);
      }
      continue;
    }
    if (code) { code.textContent += `${line}\n`; continue; }
    if (!line.trim()) continue;
    const match = /^(#{1,3})\s+(.*)$/.exec(line);
    const element = document.createElement(match ? `h${match[1].length + 1}` : 'p');
    element.textContent = match ? match[2] : line;
    root.append(element);
  }
}

async function selectProblem(code) {
  if (currentProblem) localStorage.setItem(`draft:${username}:${contest.key}:${currentProblem}`, $('source').value);
  const { problem } = await request(`problem/${encodeURIComponent(code)}`);
  currentProblem = code;
  $('problemCode').textContent = code;
  $('problemName').textContent = problem.name;
  renderStatement(problem.description || 'Chưa có nội dung đề bài.');
  $('source').value = localStorage.getItem(`draft:${username}:${contest.key}:${code}`) || '';
  for (const button of document.querySelectorAll('.problem-button')) button.classList.toggle('active', button.dataset.code === code);
}

async function loadSubmissions() {
  const { submissions } = await request('submissions');
  const list = $('submissionList');
  list.replaceChildren();
  if (!submissions.length) { list.textContent = 'Chưa có bài nộp.'; return; }
  for (const item of submissions) {
    const row = document.createElement('div');
    row.className = 'submission';
    const title = document.createElement('strong');
    title.textContent = `#${item.id} · ${item.problem} · ${item.result || item.status || 'Đang chấm'}`;
    const detail = document.createElement('span');
    detail.textContent = new Date(item.date).toLocaleString('vi-VN');
    row.append(title, detail);
    list.append(row);
  }
}

async function loadBackups() {
  const { backups } = await request('backups');
  const list = $('backupList');
  list.replaceChildren();
  if (!backups.length) { list.textContent = 'Chưa có bản sao lưu.'; return; }
  for (const item of backups) {
    const row = document.createElement('div');
    row.className = 'backup-row';
    const label = document.createElement('strong');
    label.textContent = new Date(item.created_at).toLocaleString('vi-VN');
    const detail = document.createElement('span');
    detail.textContent = `${item.file_count} file · ${(item.bytes / 1024 / 1024).toFixed(2)} MB · ${item.id.slice(0, 8)}`;
    row.append(label, detail);
    list.append(row);
  }
}

async function enterExam() {
  const data = await request('contest');
  contest = data.contest;
  username = data.username;
  $('identity').textContent = username;
  $('contestName').textContent = contest.name;
  $('contestTime').textContent = `${new Date(contest.start_time).toLocaleString('vi-VN')} – ${new Date(contest.end_time).toLocaleString('vi-VN')}`;
  const list = $('problemList');
  list.replaceChildren();
  for (const problem of contest.problems || []) {
    const button = document.createElement('button');
    button.className = 'problem-button';
    button.dataset.code = problem.code;
    button.textContent = `${problem.output_prefix || problem.order}. ${problem.name}`;
    button.addEventListener('click', () => selectProblem(problem.code).catch((error) => message(error.message)));
    list.append(button);
  }
  const { languages } = await request('languages');
  $('language').replaceChildren();
  for (const lang of languages) {
    const option = document.createElement('option');
    option.value = lang.key;
    option.textContent = lang.name;
    $('language').append(option);
  }
  $('loginPanel').hidden = true;
  $('examPanel').hidden = false;
  showView('exam');
  if (contest.problems?.length) await selectProblem(contest.problems[0].code);
  await loadSubmissions();
  resultTimer = setInterval(() => loadSubmissions().catch(() => {}), 5000);
}

$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const button = formElement.querySelector('button');
  button.disabled = true;
  try {
    const form = new FormData(formElement);
    const data = await request('login', { method: 'POST', body: JSON.stringify({ username: form.get('username'), password: form.get('password') }) });
    token = data.token;
    await enterExam();
    formElement.reset();
  } catch (error) { showLogin(); message(error.message); }
  finally { button.disabled = false; }
});

$('logoutButton').addEventListener('click', async () => {
  try { await request('logout', { method: 'POST', body: '{}' }); }
  catch { /* Clear the local session even if the network drops. */ }
  finally { showLogin(); }
});
$('examViewButton').addEventListener('click', () => showView('exam'));
$('backupViewButton').addEventListener('click', () => showView('backup'));
$('backupFolder').addEventListener('change', () => {
  const files = Array.from($('backupFolder').files);
  if (!files.length) { $('folderSummary').textContent = 'Chưa chọn thư mục.'; return; }
  const total = files.reduce((sum, file) => sum + file.size, 0);
  $('folderSummary').textContent = `${files[0].webkitRelativePath.split('/')[0]} · ${files.length} file · ${(total / 1024 / 1024).toFixed(2)} MB`;
});
$('backupForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const files = Array.from($('backupFolder').files);
  const total = files.reduce((sum, file) => sum + file.size, 0);
  if (!files.length || files.length > 200 || total > 32 * 1024 * 1024 ||
      files.some((file) => file.size > 8 * 1024 * 1024 || !file.webkitRelativePath)) {
    message('Thư mục không hợp lệ hoặc vượt giới hạn sao lưu.');
    return;
  }
  const button = $('backupButton');
  const progress = $('backupProgress');
  const form = new FormData();
  for (const file of files) form.append('files', file, file.webkitRelativePath);
  button.disabled = true;
  progress.value = 0;
  progress.hidden = false;
  try {
    const result = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/backup');
      xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      xhr.upload.onprogress = (progressEvent) => {
        if (progressEvent.lengthComputable) progress.value = Math.round(100 * progressEvent.loaded / progressEvent.total);
      };
      xhr.onload = () => {
        let data;
        try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
        if (xhr.status === 401) showLogin();
        if (xhr.status === 201) resolve(data);
        else reject(new Error(data.error || 'Không thể gửi bản sao lưu.'));
      };
      xhr.onerror = () => reject(new Error('Mất kết nối khi gửi bản sao lưu.'));
      xhr.send(form);
    });
    message(`Máy chủ đã nhận ${result.file_count} file. Mã sao lưu: ${result.id.slice(0, 8)}.`);
    $('backupFolder').value = '';
    $('folderSummary').textContent = 'Chưa chọn thư mục.';
    await loadBackups();
  } catch (error) { message(error.message); }
  finally { button.disabled = false; progress.hidden = true; }
});
$('refreshButton').addEventListener('click', () => loadSubmissions().catch((error) => message(error.message)));
$('source').addEventListener('input', () => {
  if (contest && currentProblem) localStorage.setItem(`draft:${username}:${contest.key}:${currentProblem}`, $('source').value);
});
$('sourceFile').addEventListener('change', async (event) => {
  const file = event.currentTarget.files[0];
  if (!file) return;
  if (file.size > 131072) { message('File mã nguồn vượt quá 128 KB.'); return; }
  if (!/\.(cpp|cc|cxx|py)$/i.test(file.name)) { message('Chỉ hỗ trợ file C++ hoặc Python.'); return; }
  $('source').value = await file.text();
  if (/\.py$/i.test(file.name)) $('language').value = 'PY3';
  else if ($('language').value === 'PY3') $('language').value = 'CPP17';
  $('source').dispatchEvent(new Event('input'));
  event.currentTarget.value = '';
});
$('submitForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!currentProblem) return;
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    await request('submit', { method: 'POST', body: JSON.stringify({ problem: currentProblem, language: $('language').value, source: $('source').value }) });
    message('Đã nhận bài nộp. Đang chấm...');
    await loadSubmissions();
  } catch (error) { message(error.message); }
  finally { button.disabled = false; }
});

showLogin();
