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
  if (contest.problems?.length) await selectProblem(contest.problems[0].code);
  await loadSubmissions();
  resultTimer = setInterval(() => loadSubmissions().catch(() => {}), 5000);
}

$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const form = new FormData(event.currentTarget);
    const data = await request('login', { method: 'POST', body: JSON.stringify({ username: form.get('username'), password: form.get('password') }) });
    token = data.token;
    await enterExam();
    $('loginForm').reset();
  } catch (error) { showLogin(); message(error.message); }
  finally { button.disabled = false; }
});

$('logoutButton').addEventListener('click', async () => {
  try { await request('logout', { method: 'POST', body: '{}' }); }
  catch { /* Clear the local session even if the network drops. */ }
  finally { showLogin(); }
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
