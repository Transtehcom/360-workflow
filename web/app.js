"use strict";

// Одна очередь на анкету: быстрые повторные нажатия не меняют порядок записей при медленной сети.
class DraftSaver {
  constructor(save, changed, delay = 600) {
    this.save = save;
    this.changed = changed;
    this.delay = delay;
    this.dirty = new Map();
    this.version = 0;
    this.running = null;
    this.timer = null;
    this.error = null;
    this.closed = null;
  }
  change(key, value, immediate = false) {
    // Анкета закрыта для изменений: запись всё равно не будет принята, очередь не копим.
    if (this.closed) return;
    this.dirty.set(key, { value, version: ++this.version });
    this.error = null;
    clearTimeout(this.timer);
    this.changed();
    if (immediate) this.flush();
    else this.timer = setTimeout(() => this.flush(), this.delay);
  }
  // true — ждать больше нечего (всё сохранено или анкета закрыта); false — остались несохранённые записи.
  async flush() {
    clearTimeout(this.timer);
    this.timer = null;
    if (this.running) return this.running;
    if (this.closed || !this.dirty.size) return true;
    this.error = null;
    // Запуск в микрозадаче, чтобы running был установлен до уведомления интерфейса.
    this.running = Promise.resolve().then(async () => {
      this.changed();
      try {
        while (this.dirty.size) {
          const [key, entry] = this.dirty.entries().next().value;
          await this.save(key, entry.value);
          if (this.dirty.get(key)?.version === entry.version) this.dirty.delete(key);
          this.changed();
        }
        return true;
      } catch (error) {
        // 404/409: анкету убрали или закрыли. Повтор бессмыслен, поэтому ждать больше нечего.
        if (error.status === 404 || error.status === 409) {
          this.closed = error;
          this.error = null;
          this.dirty.clear();
          return true;
        }
        this.error = error;
        return false;
      } finally {
        this.running = null;
        this.changed();
      }
    });
    return this.running;
  }
  get pending() { return Boolean(this.running || this.dirty.size); }
}

// Пароль, который администратор видит, диктует и переписывает: без похожих 0/O/o и 1/l/I.
const PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
function generatePassword(length = 10, random = n => crypto.getRandomValues(new Uint32Array(n))) {
  return Array.from(random(length), value => PASSWORD_ALPHABET[value % PASSWORD_ALPHABET.length]).join("");
}

// Запрос к API. POST — с keepalive: сохранение ответа при закрытии вкладки (pagehide) доходит до сервера.
// Предел keepalive — 64 КБ тела, ответ ограничен 5000 символами.
function fetchOptions(body, signal) {
  const post = body !== undefined;
  return {
    method: post ? "POST" : "GET",
    headers: post ? { "Content-Type": "application/json" } : {},
    body: post ? JSON.stringify(body) : undefined,
    keepalive: post, credentials: "same-origin", cache: "no-store", signal,
  };
}

// «Повторить» у оценивающего. После завершения или отмены оценки новую задачу сервер не поставит,
// поэтому кнопка остаётся только для задачи, которая есть, но не обновилась.
function canRetry(taskStatus, active, b24Enabled) {
  return b24Enabled && /ошибка|не поставлена|не обновилась/.test(taskStatus) && (active || !/не поставлена/.test(taskStatus));
}

// Проверка очереди средствами Node не требует браузера и внешних библиотек.
if (typeof module !== "undefined" && module.exports) module.exports = { DraftSaver, generatePassword, PASSWORD_ALPHABET, fetchOptions, canRetry };

if (typeof document !== "undefined") (() => {
  const root = document.querySelector("#app");
  const account = document.querySelector("#account");
  const navigation = document.querySelector("#navigation");
  let me = null;
  let draft = null;
  let returnHash = null;
  let renderedHash = "";
  let generation = 0;
  const assessmentStatus = { active: "Идёт", finished: "Завершена", cancelled: "Отменена" };
  const answerStatus = { draft: "Не отправлена", submitted: "Отправлена", closed: "Закрыта" };
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const date = value => value ? value.slice(0, 10).split("-").reverse().join(".") : "—";
  const closedNotice = "Анкета закрыта для изменений — ответ не сохранён";
  const errorBox = () => '<div class="error" role="alert"></div>';
  const tag = (status, labels) => `<span class="tag ${esc(status)}">${esc(labels[status] || status)}</span>`;
  const userOptions = (users, selected = "") => '<option value="">Выберите участника</option>' + users.filter(u => u.active).map(u => `<option value="${u.id}" ${Number(selected) === u.id ? "selected" : ""}>${esc(u.fio)}${u.position ? " · " + esc(u.position) : ""}</option>`).join("");
  const roleOptions = (selected = "peer") => Object.entries(me.roles).map(([key, title]) => `<option value="${key}" ${key === selected ? "selected" : ""}>${esc(title)}</option>`).join("");

  function loginRequired() {
    if (location.hash && location.hash !== "#login") returnHash = location.hash;
    me = null;
    location.hash = "#login";
  }

  async function request(path, body, attachment = false) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, fetchOptions(body, controller.signal));
      if (response.status === 401 && path !== "/api/login") loginRequired();
      if (attachment && response.ok) return { blob: await response.blob(), header: response.headers.get("Content-Disposition") };
      // Ответ прокси или шлюза может быть не JSON (например, HTML-страница 502).
      const data = await response.json().catch(() => null);
      if (!response.ok || data === null) {
        const error = new Error(data?.error || `Не удалось выполнить запрос (HTTP ${response.status})`);
        if (!response.ok) error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Сервер не ответил. Проверьте связь и повторите.");
      if (error instanceof TypeError) throw new Error("Нет связи с сервером. Проверьте подключение и повторите.");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function chrome() {
    account.innerHTML = me ? `<span>${esc(me.user.fio)}</span><button class="small" id="logout">Выйти</button>` : "";
    navigation.innerHTML = me?.is_admin ? `<a href="#admin" class="${location.hash.startsWith("#admin") ? "active" : ""}">Оценки</a><a href="#users" class="${location.hash === "#users" ? "active" : ""}">Участники</a><a href="#my" class="${location.hash.startsWith("#a/") || location.hash === "#my" ? "active" : ""}">Мои анкеты</a>` : "";
    document.querySelector(".brand").href = me?.is_admin ? "#admin" : "#my";
    document.querySelector("#logout")?.addEventListener("click", async () => {
      if (draft?.saver.pending && !(await draft.saver.flush())) { showError(root, new Error("Есть несохранённые ответы. Повторите сохранение перед выходом.")); return; }
      try {
        await request("/api/logout", {});
        me = null; draft = null; returnHash = null;
        location.hash = "#login";
      } catch (error) { showError(root, error); }
    });
  }

  function showError(container, error) {
    let target = container.querySelector(".error");
    if (!target) { target = document.createElement("div"); target.className = "error"; target.setAttribute("role", "alert"); container.prepend(target); }
    target.textContent = error.message;
  }

  function formAction(form, action) {
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (form.dataset.busy) return;
      form.dataset.busy = "1";
      const buttons = [...form.querySelectorAll('button[type="submit"]')];
      buttons.forEach(b => b.disabled = true);
      form.querySelector(".error").textContent = "";
      try { await action(new FormData(form)); }
      catch (error) { if (form.isConnected) showError(form, error); }
      finally { delete form.dataset.busy; buttons.forEach(b => b.disabled = false); }
    });
  }

  async function download(id, button) {
    button.disabled = true;
    try {
      const result = await request(`/api/admin/assessments/${id}/xlsx`, undefined, true);
      const match = result.header?.match(/filename\*=UTF-8''([^;]+)/i);
      const link = document.createElement("a");
      link.href = URL.createObjectURL(result.blob);
      link.download = match ? decodeURIComponent(match[1]) : "Оценка 360.xlsx";
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 30000);
    } catch (error) { showError(root, error); }
    finally { button.disabled = false; }
  }
  function bindDownloads() { root.querySelectorAll("[data-download]").forEach(button => button.addEventListener("click", () => download(button.dataset.download, button))); }

  function renderLogin() {
    root.innerHTML = `<section class="panel login"><p class="eyebrow">Оценка 360°</p><h1>Добро пожаловать</h1><p class="muted">Войдите под логином и паролем, которые вам выдал администратор.</p><form class="stack" id="login-form"><label>Логин<input name="login" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required autofocus></label><label>Пароль<input name="password" type="password" autocomplete="current-password" required></label>${errorBox()}<button type="submit" class="primary">Войти</button></form></section>`;
    formAction(root.querySelector("form"), async data => {
      await request("/api/login", { login: data.get("login"), password: data.get("password") });
      me = await request("/api/me");
      if (draft && draft.owner !== me.user.id) { draft = null; returnHash = null; }
      if (draft?.saver.pending) await draft.saver.flush();
      const target = returnHash || (me.is_admin ? "#admin" : "#my");
      returnHash = null;
      location.hash = target;
    });
    root.querySelector("input").focus();
  }

  function myCards(assignments) {
    return `<div class="cards">${assignments.map(a => `<article class="panel"><p class="eyebrow">${esc(a.role_title)}</p><h2>${esc(a.subject_fio)}</h2><p class="muted">До ${date(a.deadline)} · заполнено ${a.blocks_done} из ${a.blocks_total} блоков</p><progress value="${a.blocks_done}" max="${a.blocks_total}" aria-label="Заполнено блоков"></progress><a class="button primary" href="#a/${a.assignment_id}">${a.blocks_done ? "Продолжить" : "Начать"}</a></article>`).join("")}</div>`;
  }
  function renderMy(assignments) {
    root.innerHTML = assignments.length ? `<div class="intro"><p class="eyebrow">Обратная связь</p><h1>Мои анкеты</h1><p class="muted">Ответы сохраняются по ходу заполнения. Вы можете продолжить позже.</p></div>${myCards(assignments)}` : '<section class="panel empty"><div class="empty-mark">✓</div><h1>Оценивать больше некого</h1><p class="muted">Спасибо! Все назначенные вам анкеты заполнены.</p></section>';
  }

  function respondentRow(users, selected = "", role = "peer") {
    const row = document.createElement("div"); row.className = "respondent-row";
    row.innerHTML = `<label>Участник<select name="respondent" required>${userOptions(users, selected)}</select></label><label>Роль<select name="role">${roleOptions(role)}</select></label><button type="button" class="danger" aria-label="Убрать оценивающего">×</button>`;
    row.querySelector("button").onclick = () => row.remove();
    return row;
  }

  function renderAdmin(assessments, users, assignments) {
    root.innerHTML = `${assignments.length ? `<section class="intro"><h2>Мои анкеты</h2>${myCards(assignments)}</section>` : ""}<div class="intro"><p class="eyebrow">Администратор</p><h1>Оценки сотрудников</h1><p class="muted">Назначайте оценки и следите за заполнением анкет.</p></div>
      <section class="panel"><h2>Новая оценка</h2>${!users.some(u => u.active) ? '<p class="muted">Сначала добавьте участников.</p>' : ""}<form id="assessment-form" class="stack"><div class="form-grid"><label>Кого оцениваем<select name="subject_id" required>${userOptions(users)}</select></label><label>Срок<input type="date" name="deadline" value="${me.default_deadline}" required></label><label class="check full"><input name="is_manager" type="checkbox">Оценивается как руководитель</label></div><div><h3>Кто оценивает</h3><div id="respondents"></div><button type="button" id="add-respondent">+ Добавить оценивающего</button></div>${errorBox()}<div class="actions"><button class="primary" type="submit">Разослать</button><span class="muted">${me.b24_enabled ? "Задачи появятся в Битрикс24 у связанных участников." : "Битрикс24 не подключён. Сообщите участникам о новых анкетах."}</span></div></form></section>
      <section class="panel"><h2>Все оценки <span class="tag">${assessments.length}</span></h2>${errorBox()}${assessments.length ? `<div class="table-wrap"><table><thead><tr><th>Сотрудник</th><th>Срок</th><th>Заполнили</th><th>Статус</th><th>Действия</th></tr></thead><tbody>${assessments.map(a => `<tr><td><strong>${esc(a.subject_fio)}</strong></td><td data-label="Срок">${date(a.deadline)}</td><td data-label="Заполнили">${a.submitted} из ${a.total}</td><td>${tag(a.status, assessmentStatus)}</td><td><div class="actions"><a class="button small" href="#admin/a/${a.id}">Открыть</a><button class="small" data-download="${a.id}">Итоги .xlsx</button></div></td></tr>`).join("")}</tbody></table></div>` : '<p class="muted">Оценок пока нет. Назначьте первую оценку в форме выше.</p>'}</section>`;
    const form = root.querySelector("#assessment-form");
    const rows = root.querySelector("#respondents");
    root.querySelector("#add-respondent").onclick = () => rows.append(respondentRow(users));
    form.elements.subject_id.onchange = () => {
      rows.querySelectorAll("[data-auto-self]").forEach(r => r.remove());
      const id = Number(form.elements.subject_id.value);
      if (!id) return;
      const existing = [...rows.children].find(r => Number(r.querySelector("select").value) === id);
      if (!existing) { const row = respondentRow(users, id, "self"); row.dataset.autoSelf = "1"; rows.prepend(row); }
    };
    formAction(form, async data => {
      const respondents = [...rows.children].map(r => ({ user_id: Number(r.querySelector('[name="respondent"]').value), role: r.querySelector('[name="role"]').value }));
      if (!respondents.length) throw new Error("Добавьте хотя бы одного оценивающего");
      const result = await request("/api/admin/assessments", { subject_id: Number(data.get("subject_id")), deadline: data.get("deadline"), is_manager: data.has("is_manager"), respondents });
      location.hash = `#admin/a/${result.id}`;
    });
    bindDownloads();
  }

  function renderAssessment(a, users) {
    const active = a.status === "active";
    root.innerHTML = `<a class="back" href="#admin">← Все оценки</a><div class="intro between"><div><p class="eyebrow">${a.is_manager ? "Оценка руководителя" : "Оценка сотрудника"}</p><h1>${esc(a.subject_fio)}</h1><div class="actions">${tag(a.status, assessmentStatus)}<span class="muted">До ${date(a.deadline)} · заполнили ${a.submitted} из ${a.total}</span></div></div><button data-download="${a.id}" class="primary">Итоги .xlsx</button></div>
      <section class="panel"><h2>Оценивающие</h2>${errorBox()}<div class="table-wrap"><table><thead><tr><th>Участник</th><th>Анкета</th><th>Битрикс24</th><th>Действия</th></tr></thead><tbody>${a.respondents.map(r => `<tr><td><strong>${esc(r.fio)}</strong><div class="muted">${esc(r.role_title)}</div></td><td>${tag(r.status, answerStatus)}${r.returned_at && r.status === "draft" ? '<div class="muted">Возвращена на доработку</div>' : ""}</td><td data-label="Битрикс24">${esc(r.task_status)}</td><td><div class="actions"><a class="button small" href="#a/${r.assignment_id}?back=${a.id}">Просмотреть</a>${active && r.status === "submitted" ? `<button class="small" data-op="return" data-id="${r.assignment_id}">Вернуть на доработку</button>` : ""}${active && r.status === "draft" ? `<button class="small danger" data-op="remove" data-id="${r.assignment_id}">Убрать</button>` : ""}${canRetry(r.task_status, active, me.b24_enabled) ? `<button class="small" data-op="retry" data-id="${r.assignment_id}">Повторить</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div></section>
      ${active ? `<section class="panel"><h2>Добавить оценивающего</h2><form id="add-form" class="stack"><div class="form-grid"><label>Участник<select name="user_id" required>${userOptions(users.filter(u => !a.respondents.some(r => r.user_id === u.id)))}</select></label><label>Роль<select name="role">${roleOptions()}</select></label></div>${errorBox()}<div><button type="submit">Добавить</button></div></form></section><section class="panel"><h2>Управление оценкой</h2><form id="deadline-form" class="stack"><label>Срок<input type="date" name="deadline" value="${a.deadline}" required></label>${errorBox()}<div><button type="submit">Изменить срок</button></div></form><hr><p class="muted">При завершении или отмене незаполненные анкеты закрываются. Отправленные ответы сохраняются.</p><div class="actions"><button data-close="finish">Завершить оценку</button><button class="danger" data-close="cancel">Отменить оценку</button></div></section>` : ""}`;
    bindDownloads();
    if (active) {
      formAction(root.querySelector("#add-form"), async data => { await request(`/api/admin/assessments/${a.id}/respondents`, { user_id: Number(data.get("user_id")), role: data.get("role") }); await route(); });
      formAction(root.querySelector("#deadline-form"), async data => { await request(`/api/admin/assessments/${a.id}/deadline`, { deadline: data.get("deadline") }); await route(); });
    }
    root.querySelectorAll("[data-op], [data-close]").forEach(button => button.onclick = async () => {
      const op = button.dataset.op || button.dataset.close;
      const messages = { remove: "Убрать участника? Его черновик будет удалён.", return: "Вернуть анкету на доработку? Ответы сохранятся.", finish: "Завершить оценку? Незаполненные анкеты закроются.", cancel: "Отменить оценку? Незаполненные анкеты закроются." };
      if (messages[op] && !confirm(messages[op])) return;
      button.disabled = true;
      try { await request(button.dataset.op ? `/api/admin/assignments/${button.dataset.id}/${op}` : `/api/admin/assessments/${a.id}/${op}`, {}); await route(); }
      catch (error) { showError(root, error); button.disabled = false; }
    });
  }

  function renderUsers(users, b24, b24Error) {
    root.innerHTML = `<div class="intro"><p class="eyebrow">Администратор</p><h1>Участники</h1><p class="muted">Заведите учётные записи и передайте участникам логины и пароли.</p></div><section class="panel"><div class="between"><h2>Все участники <span class="tag">${users.length}</span></h2><button id="new-user">+ Новый участник</button></div><div class="table-wrap"><table><thead><tr><th>ФИО / должность</th><th>Логин</th><th>Битрикс24</th><th>Доступ</th><th></th></tr></thead><tbody>${users.map(u => `<tr><td><strong>${esc(u.fio)}</strong><div class="muted">${esc(u.position)}</div></td><td data-label="Логин">${esc(u.login)}</td><td data-label="Битрикс24">${u.b24_user_id ? esc(b24.find(b => b.id === u.b24_user_id)?.name || "№" + u.b24_user_id) : "Не связан"}</td><td>${u.is_admin ? "Администратор" : "Участник"}${!u.active ? ' · <span class="error">Отключён</span>' : ""}</td><td><button class="small" data-edit="${u.id}">Изменить</button></td></tr>`).join("")}</tbody></table></div></section><section class="panel" id="user-panel"></section>`;
    function edit(user = {}) {
      const linked = b24.find(b => b.id === user.b24_user_id);
      root.querySelector("#user-panel").innerHTML = `<h2>${user.id ? "Изменить участника" : "Новый участник"}</h2><form id="user-form" class="stack"><div class="form-grid"><label>ФИО<input name="fio" required value="${esc(user.fio)}"></label><label>Должность<input name="position" value="${esc(user.position)}"></label><label>Логин<input name="login" required pattern="[A-Za-z0-9._\\-]{2,40}" maxlength="40" autocomplete="off" value="${esc(user.login)}"><span class="muted">Латиница, цифры, точка, дефис, подчёркивание.</span></label><label>${user.id ? "Новый пароль" : "Пароль"}<span class="password-row"><input name="password" type="text" minlength="6" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" ${user.id ? 'placeholder="Пусто — пароль прежний"' : "required"}><button type="button" id="generate-password">Сгенерировать</button></span><span class="muted">Не менее 6 символов. Пароль виден: передайте его участнику.</span></label><label class="full">Сотрудник в Битрикс24<input name="b24" list="b24-list" autocomplete="off" ${!me.b24_enabled ? "disabled" : ""} value="${esc(linked ? linked.name + " · №" + linked.id : user.b24_user_id ? "№" + user.b24_user_id : "")}" placeholder="Не связан — для внешних участников"><datalist id="b24-list">${b24.map(b => `<option value="${esc(b.name)} · №${b.id}">${esc(b.position)}</option>`).join("")}</datalist></label><div class="full actions"><span class="muted">${me.b24_enabled ? "Выберите сотрудника из списка или оставьте поле пустым." : "Битрикс24 не подключён"}</span>${me.b24_enabled ? '<button type="button" id="refresh-b24">Обновить список</button>' : ""}</div><label class="check"><input type="checkbox" name="is_admin" ${user.is_admin ? "checked" : ""}>Администратор</label>${user.id ? `<label class="check"><input type="checkbox" name="active" ${user.active ? "checked" : ""}>Активен</label>` : ""}</div>${errorBox()}<div class="actions"><button type="submit" class="primary">${user.id ? "Сохранить изменения" : "Создать участника"}</button>${user.id ? '<button type="button" id="cancel-edit">Отмена</button>' : ""}</div></form>`;
      const form = root.querySelector("#user-form");
      if (b24Error) showError(form, b24Error);
      formAction(form, async data => {
        const value = String(data.get("b24") || "").trim();
        const found = b24.find(b => value === `${b.name} · №${b.id}`);
        let b24Id = value ? found?.id : null;
        // Существующая связь остаётся доступной даже во время сбоя портала.
        if (value === "№" + user.b24_user_id) b24Id = user.b24_user_id;
        if (!me.b24_enabled) b24Id = user.b24_user_id ?? null;
        if (value && !b24Id) throw new Error("Выберите сотрудника Битрикс24 из списка или очистите поле");
        const body = { fio: data.get("fio"), position: data.get("position"), login: data.get("login"), password: data.get("password"), is_admin: data.has("is_admin"), b24_user_id: b24Id };
        if (user.id) body.active = data.has("active");
        await request(user.id ? `/api/admin/users/${user.id}` : "/api/admin/users", body);
        me = await request("/api/me"); await route();
        const note = document.createElement("p"); note.className = "success"; note.setAttribute("role", "status"); note.textContent = "Участник сохранён."; root.querySelector("#user-panel").prepend(note);
      });
      root.querySelector("#generate-password").addEventListener("click", () => { form.elements.password.value = generatePassword(); form.elements.password.focus(); });
      root.querySelector("#cancel-edit")?.addEventListener("click", () => edit());
      root.querySelector("#refresh-b24")?.addEventListener("click", async event => {
        event.target.disabled = true;
        try {
          b24 = await request("/api/admin/b24-users?refresh=1"); b24Error = null;
          form.querySelector("#b24-list").innerHTML = b24.map(b => `<option value="${esc(b.name)} · №${b.id}">${esc(b.position)}</option>`).join("");
          form.querySelector(".error").textContent = "";
        } catch (error) { showError(form, error); }
        finally { event.target.disabled = false; }
      });
    }
    root.querySelector("#new-user").onclick = () => { edit(); root.querySelector("#user-panel").scrollIntoView({ behavior: "smooth" }); root.querySelector('[name="fio"]').focus({ preventScroll: true }); };
    root.querySelectorAll("[data-edit]").forEach(button => button.onclick = () => { edit(users.find(u => u.id === Number(button.dataset.edit))); root.querySelector("#user-panel").scrollIntoView({ behavior: "smooth" }); });
    edit();
  }

  function complete(block) { return block.items.every(item => Number(draft.answers[item.key]) >= 1 && Number(draft.answers[item.key]) <= 5); }
  function updateDraftStatus() {
    if (!draft || !root.querySelector("#questionnaire") || Number(root.querySelector("#questionnaire").dataset.id) !== draft.id) return;
    const saver = draft.saver;
    const status = root.querySelector("#save-status");
    status.textContent = saver.closed ? closedNotice : saver.error ? "Не сохранено, повторите" : saver.pending ? "Сохраняю…" : draft.editable ? "Сохранено" : "Только просмотр";
    status.title = saver.error ? saver.error.message : "";
    status.className = "save-status " + (saver.closed || saver.error ? "error" : saver.pending ? "muted" : "success");
    if (saver.closed) root.querySelectorAll("[data-text], [data-score]").forEach(input => input.disabled = true);
    root.querySelector("#retry-save").hidden = !saver.error || !draft.editable;
    const submit = root.querySelector("#submit-answers");
    if (submit) submit.disabled = saver.pending || draft.submitting || Boolean(saver.closed);
    const count = draft.schema.blocks.filter(complete).length;
    root.querySelector("#progress-text").textContent = `Заполнено ${count} из ${draft.schema.blocks.length} блоков`;
    root.querySelectorAll("[data-block]").forEach(button => button.classList.toggle("done", Number(button.dataset.block) < draft.schema.blocks.length && complete(draft.schema.blocks[Number(button.dataset.block)])));
  }

  function renderQuestionnaire(back) {
    const d = draft;
    const blocks = d.schema.blocks;
    const open = d.view === blocks.length;
    const block = blocks[d.view];
    const disabled = d.editable ? "" : "disabled";
    root.innerHTML = `<a class="back" href="${back ? "#admin/a/" + back : me.is_admin ? "#admin" : "#my"}">← ${back ? "К оценке" : "Мои анкеты"}</a><div class="intro"><p class="eyebrow">Вы — ${esc(d.role_title)}</p><h1>${esc(d.subject_fio)}</h1><div class="between"><span class="muted">До ${date(d.deadline)} · <span id="progress-text"></span></span><div class="actions"><span id="save-status" role="status" aria-live="polite"></span><button id="retry-save" class="small" hidden>Повторить сохранение</button></div></div></div><div class="block-nav" aria-label="Блоки анкеты">${[...blocks.map(b => b.key), "Открытые вопросы"].map((key, i) => `<button data-block="${i}" class="${i === d.view ? "current" : ""}" aria-current="${i === d.view ? "step" : "false"}" title="${esc(blocks[i]?.title || key)}">${esc(key)}</button>`).join("")}</div>
      <section class="panel" id="questionnaire" data-id="${d.id}">${!d.editable ? '<p class="read-only">Просмотр анкеты. Изменение ответов недоступно.</p>' : ""}<p class="eyebrow">${open ? "Последний шаг" : `Блок ${esc(block.key)} · ${d.view + 1} из ${blocks.length}`}</p><h2>${esc(open ? "Открытые вопросы" : block.title)}</h2>${errorBox()}
      ${open ? `<p class="muted">Эти вопросы необязательны. Примеры помогут сделать обратную связь полезнее.</p>${[...d.schema.open_questions, { key: "extra", text: d.schema.extra_title }].map(q => `<label class="open-question">${esc(q.text)}<textarea data-text="${q.key}" maxlength="5000" ${disabled}>${esc(d.answers[q.key])}</textarea></label>`).join("")}` : `<p class="muted">${esc(d.schema.instruction)}</p><div class="scale-help">${d.schema.scale.map(([n, title]) => `<div><strong>${n}</strong> — ${esc(title)}</div>`).join("")}</div>${block.items.map(item => `<div class="question"><p><span class="question-num">${item.key}</span>${esc(item.text)}</p><div class="rating" role="group" aria-label="${esc(item.text)}">${d.schema.scale.map(([n, title]) => `<button type="button" data-key="${item.key}" data-score="${n}" class="${Number(d.answers[item.key]) === n ? "selected" : ""}" aria-pressed="${Number(d.answers[item.key]) === n}" aria-label="${n} — ${esc(title)}" ${disabled}>${n}</button>`).join("")}</div></div>`).join("")}<label class="comment">Комментарий / примеры<textarea data-text="c:${block.key}" maxlength="5000" ${disabled}>${esc(d.answers["c:" + block.key])}</textarea></label>`}
      <div class="between" style="margin-top:26px"><button id="previous-block" ${d.view === 0 ? "disabled" : ""}>← Назад</button>${!open ? '<button id="next-block" class="primary">Дальше →</button>' : d.editable ? '<button id="submit-answers" class="primary">Отправить анкету</button>' : ""}</div></section>`;
    function changeView(index) {
      d.view = index;
      try { sessionStorage.setItem(`s360-block-${d.owner}-${d.id}`, String(index)); } catch (_) { /* браузер может запретить хранилище */ }
      renderQuestionnaire(back);
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
    root.querySelectorAll("[data-block]").forEach(button => button.onclick = () => { d.saver.flush(); changeView(Number(button.dataset.block)); });
    root.querySelector("#previous-block").onclick = () => { d.saver.flush(); changeView(d.view - 1); };
    root.querySelector("#next-block")?.addEventListener("click", () => { d.saver.flush(); changeView(d.view + 1); });
    root.querySelector("#retry-save").onclick = () => d.saver.flush();
    root.querySelectorAll("[data-score]").forEach(button => button.onclick = () => {
      const key = button.dataset.key, score = Number(button.dataset.score);
      d.answers[key] = score;
      button.parentElement.querySelectorAll("button").forEach(b => { b.classList.toggle("selected", b === button); b.setAttribute("aria-pressed", String(b === button)); });
      d.saver.change(key, score, true);
    });
    root.querySelectorAll("[data-text]").forEach(field => {
      field.oninput = () => { d.answers[field.dataset.text] = field.value; d.saver.change(field.dataset.text, field.value); };
      field.onblur = () => d.saver.flush();
    });
    root.querySelector("#submit-answers")?.addEventListener("click", async () => {
      if (d.submitting) return;
      d.submitting = true; updateDraftStatus();
      if (!(await d.saver.flush())) { d.submitting = false; updateDraftStatus(); return; }
      if (d.saver.closed) { d.submitting = false; updateDraftStatus(); showError(root.querySelector("#questionnaire"), new Error(closedNotice)); return; }
      const missing = blocks.findIndex(b => !complete(b));
      if (missing >= 0) { d.submitting = false; changeView(missing); showError(root.querySelector("#questionnaire"), new Error("Ответьте на все утверждения в этом блоке перед отправкой.")); return; }
      // После отправки позднее изменение из того же экрана не должно попадать в очередь.
      root.querySelectorAll("[data-text], [data-score]").forEach(input => input.disabled = true);
      try {
        await request(`/api/assignments/${d.id}/submit`, {});
        draft = null;
        location.hash = me?.is_admin ? "#admin" : "#my";
      } catch (error) {
        d.submitting = false;
        if (!me) return;
        if (error.status === 400) {
          // Сервер называет незаполненные блоки; если текст иной — берём первый неполный по своим данным.
          const keys = (error.message.split(":")[1] || "").split(",").map(key => key.trim());
          let index = blocks.findIndex(b => keys.includes(b.key));
          if (index < 0) index = blocks.findIndex(b => !complete(b));
          if (index >= 0) { changeView(index); showError(root.querySelector("#questionnaire"), error); return; }
        }
        renderQuestionnaire(back); showError(root.querySelector("#questionnaire"), error);
      }
    });
    updateDraftStatus();
  }

  async function route() {
    const current = ++generation;
    const hash = location.hash || (me?.is_admin ? "#admin" : "#my");
    // При переходе из анкеты дождаться текстовых ответов. При 401 вход доступен сразу.
    // Возврат на собственную анкету (например, после повторного входа) ничего не теряет и не блокируется.
    const ownDraft = Boolean(draft) && hash.match(/^#a\/(\d+)/)?.[1] === String(draft.id);
    if (me && draft?.saver.pending && hash !== renderedHash && hash !== "#login" && !ownDraft) {
      if (!(await draft.saver.flush())) {
        if (me) { history.replaceState(null, "", renderedHash); showError(root, new Error("Есть несохранённые ответы. Повторите сохранение перед переходом.")); }
        return;
      }
    }
    chrome();
    if (hash === "#login" || !me) { renderedHash = "#login"; renderLogin(); return; }
    root.innerHTML = '<p class="muted" role="status">Загружаю…</p>';
    try {
      if (hash.startsWith("#a/")) {
        const match = hash.match(/^#a\/(\d+)(?:\?back=(\d+))?$/);
        if (!match) throw new Error("Анкета не найдена");
        const a = await request(`/api/assignments/${match[1]}`);
        const schema = await request(`/api/questionnaire?manager=${a.is_manager ? 1 : 0}`);
        if (current !== generation || !me) return;
        if (!draft || draft.id !== a.assignment_id || draft.owner !== me.user.id) {
          draft = { ...a, id: a.assignment_id, owner: me.user.id, schema, view: 0, submitting: false };
          const firstMissing = schema.blocks.findIndex(b => b.items.some(item => !a.answers[item.key]));
          draft.view = firstMissing < 0 ? schema.blocks.length : firstMissing;
          try { const stored = sessionStorage.getItem(`s360-block-${draft.owner}-${draft.id}`); if (stored !== null && Number(stored) >= 0 && Number(stored) <= schema.blocks.length) draft.view = Number(stored); } catch (_) { /* без хранилища продолжаем с первого пропуска */ }
          const d = draft;
          d.saver = new DraftSaver((key, value) => request(`/api/assignments/${d.id}/answer`, { key, value }), updateDraftStatus);
        } else {
          Object.assign(draft, { ...a, answers: { ...a.answers, ...Object.fromEntries([...draft.saver.dirty].map(([key, entry]) => [key, entry.value])) }, schema });
        }
        renderQuestionnaire(match[2]);
      } else if (hash === "#my") {
        const assignments = await request("/api/my");
        if (current !== generation) return;
        renderMy(assignments);
      } else if (me.is_admin && hash === "#admin") {
        const results = await Promise.all([request("/api/admin/assessments"), request("/api/admin/users"), request("/api/my")]);
        if (current !== generation) return;
        renderAdmin(...results);
      } else if (me.is_admin && /^#admin\/a\/\d+$/.test(hash)) {
        const id = hash.split("/")[2];
        const results = await Promise.all([request(`/api/admin/assessments/${id}`), request("/api/admin/users")]);
        if (current !== generation) return;
        renderAssessment(...results);
      } else if (me.is_admin && hash === "#users") {
        const results = await Promise.allSettled([request("/api/admin/users"), request("/api/admin/b24-users")]);
        if (current !== generation || !me) return;
        if (results[0].status === "rejected") throw results[0].reason;
        renderUsers(results[0].value, results[1].status === "fulfilled" ? results[1].value : [], results[1].status === "rejected" ? results[1].reason : null);
      } else { location.hash = me.is_admin ? "#admin" : "#my"; return; }
      renderedHash = hash;
      root.focus({ preventScroll: true });
    } catch (error) {
      if (current !== generation || !me) return;
      root.innerHTML = `<section class="panel"><h1>Не удалось открыть страницу</h1>${errorBox()}<div class="actions"><button id="reload">Повторить</button><a class="button" href="${me.is_admin ? "#admin" : "#my"}">На главную</a></div></section>`;
      showError(root, error); root.querySelector("#reload").onclick = route;
    }
  }
  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", event => { if (draft?.saver.pending) { event.preventDefault(); event.returnValue = ""; } });
  // Сохранить ответы, пока страница ещё жива: связь вернулась, вкладку свернули или закрывают.
  const flushDraft = () => { if (me && draft?.saver.pending && draft.owner === me.user.id) draft.saver.flush(); };
  window.addEventListener("online", flushDraft);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushDraft(); });
  window.addEventListener("pagehide", flushDraft);
  (async () => {
    try { me = await request("/api/me"); }
    catch (error) { if (!error.status) { root.innerHTML = `<section class="panel">${errorBox()}<button id="retry-start">Повторить</button></section>`; showError(root, error); root.querySelector("#retry-start").onclick = () => location.reload(); return; } }
    if (me && (!location.hash || location.hash === "#login")) location.hash = me.is_admin ? "#admin" : "#my";
    else await route();
  })();
})();
