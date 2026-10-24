'use strict';

let token = '';
let timer = null;
const $ = (id) => document.getElementById(id);

function message(value) { $('message').textContent = value; }

async function request(route, options = {}) {
  const response = await fetch(`/api/${route}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Yêu cầu thất bại.');
  return data;
}

function cell(row, value) {
  const td = document.createElement('td');
  td.textContent = value == null ? '' : String(value);
  row.append(td);
}

function timestamp(value) { return value ? new Date(value).toLocaleString('vi-VN') : ''; }
function shortTimestamp(value) {
  return value ? new Date(value).toLocaleString('vi-VN', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
}
function browserName(value) {
  const match = /(Edg|Chrome|Firefox)\/([\d.]+)/.exec(value || '');
  return match ? `${match[1]} ${match[2]}` : 'Trình duyệt khác';
}

async function refresh() {
  const data = await request('admin/dashboard');
  $('contestName').textContent = data.contest.name;
  $('contestTime').textContent = `${timestamp(data.contest.start_at)} – ${timestamp(data.contest.end_at)}`;
  $('stats').replaceChildren();
  for (const [label, value] of [
    ['Thí sinh', data.participants], ['Chờ chấm', data.counts.QUEUED || 0],
    ['Đang chấm', data.counts.JUDGING || 0], ['Đã chấm', data.counts.DONE || 0],
    ['Máy đăng nhập', data.sessions.length], ['Bản sao lưu', data.backup_count]
  ]) {
    const card = document.createElement('div');
    card.className = 'card stat';
    const number = document.createElement('strong');
    number.textContent = value;
    const name = document.createElement('span');
    name.textContent = label;
    card.append(number, name);
    $('stats').append(card);
  }
  $('sessions').replaceChildren();
  for (const item of data.sessions) {
    const row = document.createElement('tr');
    for (const value of [item.username, item.client_ip, item.device,
      shortTimestamp(item.last_seen_at)]) cell(row, value);
    const browserCell = document.createElement('td');
    browserCell.textContent = browserName(item.user_agent);
    browserCell.title = item.user_agent;
    row.append(browserCell);
    const action = document.createElement('td');
    const button = document.createElement('button');
    button.className = 'secondary small';
    button.textContent = 'Giải phóng';
    button.addEventListener('click', async () => {
      if (!window.confirm(`Cho phép ${item.username} đăng nhập lại từ máy khác?`)) return;
      button.disabled = true;
      try {
        await request('admin/session/release', { method: 'POST', body: JSON.stringify({ username: item.username }) });
        message(`Đã giải phóng tài khoản ${item.username}.`);
        await refresh();
      } catch (error) { message(error.message); button.disabled = false; }
    });
    action.append(button);
    row.append(action);
    $('sessions').append(row);
  }
  $('backups').replaceChildren();
  for (const item of data.recent_backups) {
    const row = document.createElement('tr');
    for (const value of [item.username, shortTimestamp(item.created_at), item.file_count,
      `${(item.bytes / 1024 / 1024).toFixed(2)} MB`]) cell(row, value);
    const action = document.createElement('td');
    const button = document.createElement('button');
    button.className = 'secondary small';
    button.textContent = 'Tải ZIP';
    button.addEventListener('click', () => downloadBackup(item.id).catch((error) => message(error.message)));
    action.append(button);
    row.append(action);
    $('backups').append(row);
  }
  $('submissions').replaceChildren();
  for (const item of data.recent) {
    const row = document.createElement('tr');
    for (const value of [item.id, item.username, item.problem_code, item.language,
      item.result || item.status, item.points, timestamp(item.created_at)]) cell(row, value);
    $('submissions').append(row);
  }
  $('audit').replaceChildren();
  for (const item of data.audit) {
    const row = document.createElement('tr');
    for (const value of [timestamp(item.at), item.actor, item.action]) cell(row, value);
    $('audit').append(row);
  }
}

async function downloadBackup(id) {
  const response = await fetch(`/api/admin/backup/${encodeURIComponent(id)}`, {
    headers: { Authorization: `Bearer ${token}` }
  });
  if (!response.ok) {
    const data = await response.json();
    throw new Error(data.error || 'Không thể tải bản sao lưu.');
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = `exam-backup-${id}.zip`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function showLogin() {
  if (timer) clearInterval(timer);
  timer = null;
  token = '';
  $('identity').textContent = '';
  $('dashboard').hidden = true;
  $('loginPanel').hidden = false;
}

$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const button = formElement.querySelector('button');
  button.disabled = true;
  try {
    const form = new FormData(formElement);
    const data = await request('admin/login', { method: 'POST', body: JSON.stringify({
      username: form.get('username'), password: form.get('password')
    }) });
    token = data.token;
    $('identity').textContent = data.user.username;
    await refresh();
    $('loginPanel').hidden = true;
    $('dashboard').hidden = false;
    formElement.reset();
    timer = setInterval(() => refresh().catch((error) => message(error.message)), 5000);
  } catch (error) { showLogin(); message(error.message); }
  finally { button.disabled = false; }
});

$('refreshButton').addEventListener('click', () => refresh().catch((error) => message(error.message)));
$('logoutButton').addEventListener('click', async () => {
  try { await request('logout', { method: 'POST', body: '{}' }); }
  catch { /* Local session still closes if the network is unavailable. */ }
  finally { showLogin(); }
});

showLogin();
