import csv
import fcntl
from html import escape
from http.server import BaseHTTPRequestHandler
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
from socketserver import UnixStreamServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import usage

from store import BASE, ORIGIN, WEB_GID, InputError, canonical_uuid, canonical_token, user_id, find_user, folder_for, publish, read, subscription, validate_template
from store import canonical_expiry, canonical_forward, display_expiry, expiry_time, item_state, read_forwards, find_subscription

SOCKET = Path(os.environ.get("PANEL_SOCKET", "/run/panel/panel.sock"))
CSRF = secrets.token_urlsafe(32)
MAX_FORM = 2 * 1024 * 1024
STYLE = Path(__file__).with_name("style.css").read_text()
SCRIPT = """
const toast = document.getElementById('toast');
let toastTimer;
function showToast(message, error = false) {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.classList.toggle('failure', error);
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
}
const notices = {
  saved: '订阅保存成功',
  template: '模板保存成功，全部订阅已更新',
  revoked: '订阅已停用',
  restored: '订阅已恢复',
  deleted: '订阅已删除，链接已失效',
  deleted_history_retained: '订阅已删除，链接已失效，但 UA 记录清理失败，请联系管理员处理',
  forward_saved: '特殊转发保存成功',
  forward_deleted: '特殊转发已删除'
};
const query = new URLSearchParams(location.search);
const initialNotice = notices[query.get('notice')];
if (initialNotice) {
  showToast(initialNotice, query.get('notice') === 'deleted_history_retained');
  query.delete('notice');
  const suffix = query.toString();
  history.replaceState(null, '', location.pathname + (suffix ? '?' + suffix : ''));
}
const agentDialog = document.getElementById('ua-dialog');
const agentList = document.getElementById('ua-list');
const agentStatus = document.getElementById('ua-status');
const agentSearch = document.getElementById('ua-search');
const agentPrev = document.getElementById('ua-prev');
const agentNext = document.getElementById('ua-next');
let agentId = '', agentPage = 1, agentRequest, agentSource, agentVersion, agentCsrf;
const agentFeedback = document.getElementById('ua-feedback');

async function loadAgents(page = 1) {
  agentRequest?.abort();
  const request = new AbortController();
  agentRequest = request;
  agentStatus.textContent = '正在加载…';
  agentList.replaceChildren();
  agentPrev.disabled = agentNext.disabled = true;
  const params = new URLSearchParams({id: agentId, q: agentSearch.value.trim(), page});
  try {
    const response = await fetch('/panel/user-agents?' + params, {signal: request.signal});
    if (!response.ok) throw new Error('load failed');
    const result = await response.json();
    if (request.signal.aborted) return;
    agentPage = result.page;
    agentVersion = result.version;
    agentCsrf = result.csrf;
    if (agentSource?.isConnected) {
      agentSource.textContent = '查看 ' + result.summary.count + ' 条 UA';
      agentSource.closest('tr').querySelector('.last-request').textContent = result.summary.last_request;
    }
    for (const item of result.items) {
      const entry = document.createElement('li');
      const value = document.createElement('code');
      value.className = 'agent-value';
      value.textContent = item.user_agent;
      const time = document.createElement('small');
      time.textContent = '最近请求 ' + item.last_seen + '（北京时间）';
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'delete-agent';
      remove.dataset.deleteAgent = item.value;
      remove.textContent = '删除';
      entry.append(value, time, remove);
      agentList.append(entry);
    }
    agentStatus.textContent = '第 ' + result.page + ' / ' + result.pages + ' 页 · 共 ' + result.total + ' 条 · 每页最多 50 条';
    if (!result.items.length) agentStatus.textContent = '未找到匹配的 User-Agent';
    agentPrev.disabled = result.page <= 1;
    agentNext.disabled = result.page >= result.pages;
  } catch (error) {
    if (error.name !== 'AbortError') agentStatus.textContent = '加载失败，请关闭弹窗后重试';
  }
}
document.getElementById('ua-close').addEventListener('click', () => agentDialog.close());
agentDialog.addEventListener('close', () => {
  agentRequest?.abort();
  document.body.classList.remove('modal-open');
});
document.getElementById('ua-search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  loadAgents(1);
});
agentPrev.addEventListener('click', () => loadAgents(agentPage - 1));
agentNext.addEventListener('click', () => loadAgents(agentPage + 1));
document.addEventListener('click', async (event) => {
  const removeAgent = event.target.closest('button[data-delete-agent]');
  if (removeAgent) {
    if (!confirm('删除这条 UA 记录？同一 UA 后续请求时会重新记录。')) return;
    removeAgent.disabled = true;
    agentFeedback.textContent = '';
    try {
      const body = new URLSearchParams({
        csrf: agentCsrf, version: agentVersion, id: agentId, user_agent: removeAgent.dataset.deleteAgent
      });
      const response = await fetch('/panel/user-agents/delete', {method: 'POST', body});
      if (!response.ok) throw new Error('delete failed');
      await loadAgents(agentPage);
      agentFeedback.textContent = 'UA 记录已删除';
    } catch {
      agentFeedback.textContent = '删除失败，请刷新列表后重试';
      removeAgent.disabled = false;
    }
    return;
  }
  if (event.target.closest('button[data-generate-token]')) {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    document.getElementById('subscriber-token').value = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    return;
  }
  const agentButton = event.target.closest('button[data-agents]');
  if (agentButton) {
    agentId = agentButton.dataset.agents;
    agentSource = agentButton;
    agentFeedback.textContent = '';
    agentSearch.value = '';
    document.getElementById('ua-account').textContent = agentButton.dataset.email;
    document.body.classList.add('modal-open');
    agentDialog.showModal();
    loadAgents();
    return;
  }
  const button = event.target.closest('button[data-copy]');
  if (!button) return;
  try { await navigator.clipboard.writeText(button.dataset.copy); showToast('复制成功'); }
  catch { showToast('复制失败，请允许浏览器访问剪贴板', true); }
});
document.addEventListener('submit', (event) => {
  const message = event.submitter?.dataset.confirm;
  if (message && !confirm(message)) event.preventDefault();
});
"""


AGENT_DIALOG = """
<dialog id="ua-dialog" aria-labelledby="ua-title">
<div class="ua-heading"><h2 id="ua-title">User-Agent 记录</h2><button type="button" id="ua-close" aria-label="关闭">关闭</button></div>
<p id="ua-account"></p><p class="ua-help">按完整 User-Agent 去重，条数代表不同 UA 字符串。</p>
<form id="ua-search-form" class="toolbar" role="search">
<label for="ua-search">搜索 User-Agent</label><input type="search" id="ua-search"><button type="submit">搜索</button>
</form><p id="ua-feedback" role="status" aria-live="polite"></p><p id="ua-status" role="status"></p><ul id="ua-list"></ul>
<div class="ua-navigation"><button type="button" id="ua-prev" disabled>上一页</button><button type="button" id="ua-next" disabled>下一页</button></div>
</dialog>
"""


def fields(version):
    return f'<input type="hidden" name="csrf" value="{CSRF}"><input type="hidden" name="version" value="{version}">'


STATE_LABELS = {"live": "启用", "expired": "已到期", "revoked": "停用"}


def subscriber_list(version, users, query="", page_number=1, sort="", direction="asc"):
    rows = []
    records = usage.summaries()
    states = {user_id(u): item_state(u) for u in users}
    active = sum(state == "live" for state in states.values())
    expired = sum(state == "expired" for state in states.values())
    sort = sort if sort in {"status", "expiry", "recent"} else ""
    direction = "desc" if direction == "desc" else "asc"
    query = query.strip()
    search = query.casefold()
    matches = []
    matching_clients = usage.matching_tokens(query) if search else set()
    for user in users:
        count, latest = records.get(user.get("usage_key", user["token"]), (0, None))
        searchable = " ".join([
            user["email"], user["folder"], STATE_LABELS[states[user_id(user)]], display_expiry(user),
            usage.display_time(latest) if latest is not None else "暂无请求",
        ])
        if not search or search in searchable.casefold() or user.get("usage_key", user["token"]) in matching_clients:
            matches.append(user)
    if sort:
        def sort_value(user):
            if sort == "status":
                return {"live": 0, "expired": 1, "revoked": 2}[states[user_id(user)]]
            if sort == "expiry":
                expiry = expiry_time(user)
                return expiry.timestamp() if expiry else None
            return records.get(user.get("usage_key", user["token"]), (0, None))[1]
        present = [u for u in matches if sort_value(u) is not None]
        absent = [u for u in matches if sort_value(u) is None]
        matches = sorted(present, key=sort_value, reverse=direction == "desc") + absent
    page_count = max(1, (len(matches) + 99) // 100)
    try:
        page_number = int(page_number)
    except (ValueError, TypeError):
        page_number = 1
    page_number = min(max(page_number, 1), page_count)
    visible = matches[(page_number - 1) * 100:page_number * 100]
    context = (
        f'<input type="hidden" name="q" value="{escape(query, quote=True)}">'
        f'<input type="hidden" name="page" value="{page_number}">'
        f'<input type="hidden" name="sort" value="{sort}"><input type="hidden" name="direction" value="{direction}">'
    )
    for user in visible:
        email, folder = escape(user["email"]), escape(user["folder"])
        url = escape(subscription(user), quote=True)
        edit = "/panel/edit?" + urlencode({"id": user_id(user)})
        state = states[user_id(user)]
        label = STATE_LABELS[state]
        action, button = ("revoke", "停用") if user["active"] else ("restore", "恢复")
        confirm = 'data-confirm="停用该订阅链接？"' if action == "revoke" else ""
        count, latest = records.get(user.get("usage_key", user["token"]), (0, None))
        last_request = usage.display_time(latest) if latest is not None else "暂无请求"
        agent_html = (
            f'<button type="button" data-agents="{escape(user_id(user), quote=True)}" data-email="{email}">查看 {count} 条 UA</button>'
            if count else "—"
        )
        rows.append(f"""<tr data-state="{state}">
<td>{email}</td><td><code>{folder}</code></td><td><span class="badge">{label}</span></td>
<td>{display_expiry(user)}</td>
<td class="last-request">{last_request}</td><td>{agent_html}</td>
<td><div class="actions"><a class="button-link" href="{escape(edit, quote=True)}">编辑</a>
<button type="button" data-copy="{url}" {'' if user['active'] else 'disabled'}>复制链接</button>
<form method="post" action="/panel/state">{fields(version)}{context}
<input type="hidden" name="folder" value="{folder}">
<input type="hidden" name="id" value="{escape(user_id(user), quote=True)}">
<button name="action" value="{action}" class="{action}" {confirm}>{button}</button>
</form>
<form method="post" action="/panel/subscriber/delete">{fields(version)}{context}
<input type="hidden" name="id" value="{escape(user_id(user), quote=True)}">
<button type="submit" class="revoke" data-confirm="删除订阅 {email}？链接会立即失效，并清除该订阅的 UA 记录。此操作无法在面板中撤销。">删除</button>
</form></div></td></tr>""")
    def page_link(label, target, enabled):
        if not enabled:
            return f'<span aria-disabled="true">{label}</span>'
        url = "/panel/?" + urlencode({"q": query, "page": target, "sort": sort, "direction": direction})
        return f'<a href="{escape(url, quote=True)}">{label}</a>'

    navigation = "".join([
        page_link("首页", 1, page_number > 1),
        page_link("上一页", page_number - 1, page_number > 1),
        page_link("下一页", page_number + 1, page_number < page_count),
        page_link("末页", page_count, page_number < page_count),
    ])
    def sort_link(label, key):
        selected = sort == key
        next_direction = "asc" if selected and direction == "desc" else "desc" if selected or key == "recent" else "asc"
        url = "/panel/?" + urlencode({"q": query, "sort": key, "direction": next_direction})
        arrow = (" ↑" if direction == "asc" else " ↓") if selected else " ↕"
        return f'<a href="{escape(url, quote=True)}">{label}{arrow}</a>'
    empty = '<tr><td colspan="7">未找到匹配的订阅</td></tr>'
    return f"""<p>共 <strong>{len(users)}</strong> 条订阅 · {active} 条启用 · {expired} 条已到期 · {len(users)-active-expired} 条停用</p>
<form class="toolbar" method="get" action="/panel/" role="search">
<input type="hidden" name="sort" value="{sort}"><input type="hidden" name="direction" value="{direction}">
<label for="search">搜索订阅</label>
<input type="search" id="search" name="q" value="{escape(query, quote=True)}" placeholder="搜索全部订阅">
<button type="submit">搜索</button><a href="/panel/">清除搜索</a>
</form>
<p id="page-status">第 {page_number} / {page_count} 页 · 共 {len(matches)} 条结果 · 每页最多 100 条</p>
<div class="grid"><table><thead><tr><th>邮箱</th><th>目录名</th><th>{sort_link('状态', 'status')}</th><th>{sort_link('到期时间（北京时间）', 'expiry')}</th><th>{sort_link('最近请求（北京时间）', 'recent')}</th><th>User-Agent</th><th>操作</th></tr></thead>
<tbody>{''.join(rows) or empty}</tbody></table></div>
<nav class="pagination" aria-label="订阅分页">{navigation}</nav>"""


def subscriber_form(version, user=None, draft=None):
    values = draft or user or {}
    original = escape(user["folder"] if user else "", quote=True)
    identity = escape(user_id(user) if user else "", quote=True)
    email = escape(values.get("email", ""), quote=True)
    credential = escape(values.get("uuid", ""), quote=True)
    token = escape(values.get("token", ""), quote=True)
    hint = "<p>修改邮箱前缀会改变订阅路径。修改 UUID 会更新当前链接的配置。</p>" if user else ""
    return f"""{hint}<form class="form" method="post" action="/panel/subscriber">
{fields(version)}<input type="hidden" name="folder" value="{original}"><input type="hidden" name="id" value="{identity}">
<label>邮箱 / 名称 <input name="email" value="{email}" maxlength="254" required></label>
<label>UUID <input name="uuid" value="{credential}" maxlength="36" required></label>
<label for="subscriber-token">自定义令牌</label>
<div class="token-input"><input id="subscriber-token" name="token" value="{token}" minlength="32" maxlength="255"
autocomplete="off" spellcheck="false" placeholder="留空自动生成新令牌">
<button type="button" data-generate-token>生成新令牌</button></div>
<p class="form-help">支持 32～255 个英文字母、数字和 . _ ~ -，区分大小写。更换令牌后旧链接失效，留空会自动生成。请求历史继续保留。</p>
<label>到期时间（北京时间）<input type="datetime-local" name="expires_at" value="{escape(values.get('expires_at', '') or '', quote=True) if draft else display_expiry(values, form=True)}"></label>
<p class="form-help">留空表示长期有效。到期后链接停止返回配置，延长时间后可继续使用。</p>
<div><button type="submit">保存订阅</button></div></form>"""


def forward_form(version, rule=None, draft=None):
    values = draft or rule or {}
    return f"""<p>服务器按完整路径匹配并返回目标内容。转发优先于同路径订阅，目标地址不会重定向给客户端。</p>
<form class="form" method="post" action="/panel/forwards/save">{fields(version)}
<input type="hidden" name="id" value="{escape(rule['id'] if rule else '', quote=True)}">
<label>请求路径<input name="path" value="{escape(values.get('path', ''), quote=True)}" placeholder="/owner/token/raw/example.yaml" maxlength="2048" required></label>
<label>目标 HTTPS 地址<input type="url" name="target" value="{escape(values.get('target', ''), quote=True)}" placeholder="https://example.com/example.yaml" maxlength="4096" required></label>
<label>状态<select name="active"><option value="1" {'' if values.get('active') in (False, '0') else 'selected'}>启用</option><option value="0" {'selected' if values.get('active') in (False, '0') else ''}>停用</option></select></label>
<label>到期时间（北京时间）<input type="datetime-local" name="expires_at" value="{escape(values.get('expires_at', '') or '', quote=True) if draft else display_expiry(values, form=True)}"></label>
<p class="form-help">留空表示长期有效。支持 HTTPS 域名和 443 端口，服务器校验证书，不跟随上游重定向。</p>
<div><button type="submit">保存转发</button></div></form>"""


def forward_list(version, rules):
    rows = []
    for rule in rules:
        identity = escape(rule["id"], quote=True)
        state = item_state(rule)
        rows.append(f'''<tr data-state="{state}"><td class="forward-path"><code>{escape(rule['path'])}</code></td>
<td class="forward-target"><code>{escape(rule['target'])}</code></td><td><span class="badge">{STATE_LABELS[state]}</span></td><td>{display_expiry(rule)}</td>
<td><div class="actions"><a class="button-link" href="/panel/forwards/edit?id={identity}">编辑</a>
<button type="button" data-copy="{escape(ORIGIN + rule['path'], quote=True)}">复制链接</button>
<form method="post" action="/panel/forwards/delete">{fields(version)}<input type="hidden" name="id" value="{identity}">
<button type="submit" class="revoke" data-confirm="删除这条特殊转发？原链接将停止转发。">删除</button></form></div></td></tr>''')
    return f'''<p>共 {len(rules)} 条特殊转发。规则保存在实例的数据卷中，更新或重建镜像后继续保留。</p>
<p><a class="button-link" href="/panel/forwards/new">新增特殊转发</a></p>
<div class="grid"><table class="forwards"><thead><tr><th>请求路径</th><th>目标地址</th><th>状态</th><th>到期时间（北京时间）</th><th>操作</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan="5">暂无特殊转发</td></tr>'}</tbody></table></div>'''


def template_form(version, template):
    return f"""<p>用 <code>{{{{UUID}}}}</code> 标记用户 UUID。保存后会更新全部订阅配置。</p>
<form class="form" method="post" action="/panel/template">{fields(version)}
<label>YAML 模板<textarea name="template" spellcheck="false" required>
{escape(template)}</textarea></label>
<div><button type="submit" data-confirm="保存模板并更新全部订阅？">保存模板</button></div></form>"""


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(12)
        super().setup()

    def log_message(self, *_args):
        pass

    def respond(self, status, body=b"", content_type="text/html; charset=utf-8", nonce="", location=None, disposition=None, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", f"default-src 'none'; connect-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if location:
            self.send_header("Location", location)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def page(self, title, content, status=200, error=""):
        nonce = secrets.token_urlsafe(24)
        message = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
        body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>{STYLE}</style>
</head><body><main><h1>{title}</h1><nav aria-label="面板导航">
<a href="/panel/">订阅列表</a><a href="/panel/new">新增订阅</a>
<a href="/panel/template">编辑模板</a><a href="/panel/export">导出 TSV</a>
<a href="/panel/forwards">特殊转发</a>
</nav>{message}{content}</main>{AGENT_DIALOG}<div id="toast" role="status" aria-live="polite" hidden></div><script nonce="{nonce}">{SCRIPT}</script></body></html>"""
        self.respond(status, body.encode("utf-8"), nonce=nonce)

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/health":
            healthy = (BASE / "current/users.json").is_file()
            return self.respond(200 if healthy else 503, json.dumps({
                "healthy": healthy, "version": os.environ.get("APP_VERSION", "dev")
            }).encode(), "application/json")
        if path.path == "/internal/track":
            usage.record(self.headers)
            return self.respond(204)
        if path.path in {"/internal/resolve", "/internal/forward"}:
            version, users, _ = read()
            rules = read_forwards(version)
            if path.path == "/internal/forward":
                rule = next((r for r in rules if r["id"] == self.headers.get("X-Forward-Id")), None)
                if not rule or item_state(rule) != "live":
                    return self.respond(403)
                _, target, host = canonical_forward(rule["path"], rule["target"])
                return self.respond(200, headers={"X-Clash-Target": target, "X-Clash-Host": host})
            request_path = self.headers.get("X-Request-Path", "")
            rule = next((r for r in rules if r["path"] == request_path), None)
            if rule:
                if item_state(rule) != "live":
                    return self.respond(404)
                return self.respond(200, headers={"X-Accel-Redirect": "/_clash_forward/" + rule["id"]})
            user = find_subscription(users, request_path, self.headers.get("X-Legacy-Owner", ""))
            if not user:
                return self.respond(404)
            usage.record({
                "X-Usage-Token": user["token"], "X-Usage-Folder": user["folder"],
                "X-Usage-Method": self.headers.get("X-Request-Method", ""),
                "X-Usage-Time": self.headers.get("X-Request-Time", ""),
                "X-Usage-Agent": self.headers.get("X-Request-Agent", ""),
            })
            if item_state(user) != "live":
                return self.respond(404)
            return self.respond(200, headers={"X-Accel-Redirect": f"/_clash_file/{user['token']}/{quote(user['folder'], safe='+')}"})
        if path.path not in {"/panel/", "/panel/new", "/panel/edit", "/panel/template", "/panel/export", "/panel/user-agents", "/panel/forwards", "/panel/forwards/new", "/panel/forwards/edit"}:
            return self.respond(404)
        version, users, template = read()
        if path.path == "/panel/":
            parameters = parse_qs(path.query)
            return self.page("Clash 订阅管理", subscriber_list(
                version, users, parameters.get("q", [""])[0], parameters.get("page", ["1"])[0],
                parameters.get("sort", [""])[0], parameters.get("direction", ["asc"])[0]
            ))
        if path.path.startswith("/panel/forwards"):
            rules = read_forwards(version)
            if path.path == "/panel/forwards":
                return self.page("特殊转发", forward_list(version, rules))
            if path.path == "/panel/forwards/new":
                return self.page("新增特殊转发", forward_form(version))
            identity = parse_qs(path.query).get("id", [""])[0]
            rule = next((r for r in rules if r["id"] == identity), None)
            if not rule:
                return self.respond(404)
            return self.page("编辑特殊转发", forward_form(version, rule))
        if path.path == "/panel/user-agents":
            parameters = parse_qs(path.query)
            folder = parameters.get("folder", [""])[0]
            user = find_user(users, parameters.get("id", [""])[0], folder)
            if not user:
                return self.respond(404)
            result = usage.page(user.get("usage_key", user["token"]), parameters.get("q", [""])[0], parameters.get("page", ["1"])[0])
            result.update(version=version, csrf=CSRF, summary=usage.summary(user.get("usage_key", user["token"])))
            return self.respond(200, json.dumps(result, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if path.path == "/panel/new":
            return self.page("新增订阅", subscriber_form(version))
        if path.path == "/panel/edit":
            parameters = parse_qs(path.query)
            user = find_user(users, parameters.get("id", [""])[0], parameters.get("folder", [""])[0])
            if not user:
                return self.respond(404)
            return self.page("编辑订阅", subscriber_form(version, user))
        if path.path == "/panel/template":
            return self.page("编辑 YAML 模板", template_form(version, template))
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(["email", "uuid", "folder", "url", "status", "expires_at"])
        for u in users:
            writer.writerow([u["email"], u["uuid"], u["folder"], subscription(u), {"live": "active", "revoked": "revoked", "expired": "expired"}[item_state(u)], u.get("expires_at") or ""])
        self.respond(200, output.getvalue().encode("utf-8"), "text/tab-separated-values; charset=utf-8",
                     disposition='attachment; filename="clash-subscriptions.tsv"')

    def do_POST(self):
        if self.path not in {"/panel/state", "/panel/subscriber", "/panel/subscriber/delete", "/panel/template", "/panel/user-agents/delete", "/panel/forwards/save", "/panel/forwards/delete"}:
            return self.respond(404)
        if self.headers.get("Origin") != ORIGIN:
            return self.respond(403)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_FORM or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                return self.respond(400)
            form = {key: values[0] for key, values in parse_qs(
                self.rfile.read(size).decode("utf-8"), keep_blank_values=True,
                strict_parsing=True, max_num_fields=12).items()}
        except ValueError:
            return self.respond(400)
        token = form.get("csrf", "")
        if not token.isascii() or not secrets.compare_digest(token, CSRF):
            return self.respond(403)
        version, users, template = read()
        if form.get("version") != version:
            return self.page("请重新打开表单", "<p>订阅数据已更新，请从面板重新进入。</p>", 409)
        folder = form.get("folder", "")
        identity = form.get("id", "")
        user = find_user(users, identity, folder)
        rules = read_forwards(version)
        rule = next((r for r in rules if r["id"] == identity), None)
        if self.path == "/panel/user-agents/delete":
            if user is None or "user_agent" not in form:
                return self.respond(400)
            try:
                usage.delete(user.get("usage_key", user["token"]), form["user_agent"])
            except sqlite3.Error:
                return self.respond(500)
            return self.respond(204)
        try:
            if self.path.startswith("/panel/forwards/"):
                if self.path == "/panel/forwards/delete":
                    if not rule:
                        return self.respond(404)
                    rules.remove(rule)
                    notice = "forward_deleted"
                else:
                    if identity and not rule:
                        return self.respond(404)
                    source, target, _ = canonical_forward(form.get("path", ""), form.get("target", ""))
                    if any(r is not rule and r["path"] == source for r in rules):
                        raise InputError("该请求路径已存在，请编辑原规则")
                    if form.get("active") not in {"0", "1"}:
                        raise InputError("请选择转发状态")
                    expires_at = canonical_expiry(form.get("expires_at", ""))
                    if not rule:
                        rule = dict(id=secrets.token_hex(16))
                        rules.append(rule)
                    rule.update(path=source, target=target, active=form["active"] == "1", expires_at=expires_at)
                    notice = "forward_saved"
            elif self.path == "/panel/subscriber/delete":
                if not identity or not user:
                    return self.respond(404)
                users.remove(user)
                notice = "deleted"
            elif self.path == "/panel/state":
                action = form.get("action")
                if not user or action not in {"revoke", "restore"}:
                    return self.respond(400)
                user["active"] = action == "restore"
                notice = "restored" if user["active"] else "revoked"
            elif self.path == "/panel/subscriber":
                email = form.get("email", "").strip()
                credential = canonical_uuid(form.get("uuid", "").strip())
                new_folder = folder_for(email)
                supplied_token = form.get("token", user["token"] if user else "").strip()
                new_token = canonical_token(supplied_token) if supplied_token else secrets.token_hex(32)
                expires_at = canonical_expiry(form.get("expires_at", user.get("expires_at", "") if user else "") or "")
                if (identity or folder) and not user:
                    return self.respond(404)
                if any(u is not user and new_token in {u["token"], u.get("usage_key")} for u in users):
                    raise InputError("该令牌已被其他订阅使用")
                if not user:
                    user = dict(id=new_token, token=new_token, active=True)
                    users.append(user)
                elif new_token != user["token"]:
                    user.setdefault("id", user_id(user))
                    user.setdefault("usage_key", user["token"])
                user.update(email=email, folder=new_folder, uuid=credential, token=new_token, expires_at=expires_at)
                notice = "saved"
            else:
                template = form.get("template", "")
                validate_template(template)
                # Browsers normalize textarea line breaks on form submission.
                template = template.replace("\r\n", "\n")
                notice = "template"
            publish(users, template, rules)
        except InputError as exc:
            if self.path.startswith("/panel/forwards/"):
                return self.page("编辑特殊转发" if rule else "新增特殊转发", forward_form(version, rule, form), 400, str(exc))
            if self.path == "/panel/template":
                return self.page("编辑 YAML 模板", template_form(version, template), 400, str(exc))
            return self.page("编辑订阅" if user else "新增订阅", subscriber_form(version, user, form), 400, str(exc))
        except OSError:
            return self.page("保存失败", "<p>请稍后重试。</p>", 500)
        if self.path == "/panel/subscriber/delete":
            # Revocation commits before analytics cleanup, so a cleanup failure
            # cannot leave the deleted subscription accessible.
            try:
                usage.delete_subscription(user.get("usage_key", user["token"]))
            except sqlite3.Error:
                notice = "deleted_history_retained"
        destination = {"notice": notice}
        if self.path in {"/panel/state", "/panel/subscriber/delete"}:
            destination.update(q=form.get("q", ""), page=form.get("page", "1"), sort=form.get("sort", ""), direction=form.get("direction", "asc"))
        base = "/panel/forwards" if self.path.startswith("/panel/forwards/") else "/panel/"
        self.respond(303, location=base + "?" + urlencode(destination))

    do_HEAD = do_GET


def main():
    os.umask(0o077)
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    with (SOCKET.parent / "panel.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if SOCKET.exists():
            if not SOCKET.is_socket() or SOCKET.is_symlink():
                raise RuntimeError("Panel socket path is occupied")
            SOCKET.unlink()
        usage.initialize()
        with UnixStreamServer(str(SOCKET), Handler) as server:
            os.chown(SOCKET, -1, WEB_GID)
            SOCKET.chmod(0o660)
            server.serve_forever()


if __name__ == "__main__":
    main()
