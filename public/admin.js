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

async function refresh() {
  const data = await request('admin/dashboard');
  $('contestName').textContent = data.contest.name;
  $('contestTime').textContent = `${timestamp(data.contest.start_at)} – ${timestamp(data.contest.end_at)}`;
  $('stats').replaceChildren();
  for (const [label, value] of [
    ['Thí sinh', data.participants], ['Chờ chấm', data.counts.QUEUED || 0],
    ['Đang chấm', data.counts.JUDGING || 0], ['Đã chấm', data.counts.DONE || 0]
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
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const form = new FormData(event.currentTarget);
    const data = await request('admin/login', { method: 'POST', body: JSON.stringify({
      username: form.get('username'), password: form.get('password')
    }) });
    token = data.token;
    $('identity').textContent = data.user.username;
    await refresh();
    $('loginPanel').hidden = true;
    $('dashboard').hidden = false;
    event.currentTarget.reset();
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
