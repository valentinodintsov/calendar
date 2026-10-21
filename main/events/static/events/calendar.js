/* © 2026 Одинцов Валентин Николаевич. Все права защищены. */
(() => {
  "use strict";

  const API = "/api/events/";
  const MAX_CHIPS = 3;

  const MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"];
  const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
  const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  const WEEKDAYS = ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"];

  // ---------- helpers ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const parseYmd = (s) => {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  };
  // "YYYY-MM-DDTHH:MM" -> local Date
  const parseDT = (s) => {
    const [date, time = "00:00"] = s.split("T");
    const d = parseYmd(date);
    const [h, m] = time.split(":").map(Number);
    d.setHours(h, m, 0, 0);
    return d;
  };
  const fmtDT = (d) => `${ymd(d)}T${hm(d)}`;
  const addDays = (d, n) => {
    const x = new Date(d);
    x.setDate(x.getDate() + n);
    return x;
  };
  const daysBetween = (a, b) => Math.round((parseYmd(b) - parseYmd(a)) / 86400000);
  const shiftDT = (s, n) => ymd(addDays(parseYmd(s.slice(0, 10)), n)) + s.slice(10);
  const todayStr = () => ymd(new Date());
  const escapeHtml = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function getCookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  async function api(url, { method = "GET", body } = {}) {
    const isForm = body instanceof FormData;
    const headers = { "X-CSRFToken": getCookie("csrftoken") };
    if (!isForm) headers["Content-Type"] = "application/json";
    const res = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {
      /* пустой или не-JSON ответ */
    }
    if (!res.ok) {
      const err = new Error((data && data.errors && Object.values(data.errors).flat()[0]) || `Ошибка ${res.status}`);
      err.errors = (data && data.errors) || {};
      throw err;
    }
    return data;
  }

  // ---------- current user & permissions ----------
  const ME = JSON.parse(($("#me-data") || {}).textContent || "{}");
  // права приходят с сервером в ev.permissions; у нового (ещё не сохранённого) события их нет
  const canRead = (ev) => !ev.permissions || !!ev.permissions.read;
  const canWrite = (ev) => !ev.permissions || !!ev.permissions.change; // право «Изменение»

  // ---------- state ----------
  const VIEWS = ["month", "week", "day"];
  // Личные настройки пользователя (у каждого свои, хранятся на сервере): «Вид при открытии»,
  // а при «Последний» — вид, который этот пользователь выбрал в прошлый раз
  const MY_SETTINGS = Object.assign({ theme: "system", start_view: "last", last_view: "month" }, ME.settings || {});
  const savedView = MY_SETTINGS.start_view === "last" ? MY_SETTINGS.last_view : MY_SETTINGS.start_view;
  async function saveMySettings(changes) {
    Object.assign(MY_SETTINGS, changes);
    try {
      await api("/api/me/settings/", { method: "PATCH", body: changes });
    } catch (err) {
      toast(`Настройка не сохранена: ${err.message}`, true);
    }
  }
  try {
    // настройки раньше хранились в браузере (общие для всех, кто входил с него) — убираем
    ["calendar.view", "calendar.startView", "calendar.theme"].forEach((k) => localStorage.removeItem(k));
  } catch (_) {
    /* хранилище недоступно */
  }
  const now = new Date();
  const state = {
    view: VIEWS.includes(savedView) ? savedView : "month",
    month: new Date(now.getFullYear(), now.getMonth(), 1), // якорь для вида «Месяц»
    selected: todayStr(), // выбранный день; якорь для видов «Неделя» и «День»
    events: [],
    byDay: new Map(),
    loadSeq: 0,
    loadedKey: null,
    scrollKey: null,
  };

  // Дни, которые занимает событие (с учётом многодневных)
  function eventSpan(ev) {
    const s = ev.start.slice(0, 10);
    let e = (ev.end || ev.start).slice(0, 10);
    // событие до 00:00 следующего дня не должно появляться в этом дне
    if (!ev.all_day && e > s && ev.end.slice(11) === "00:00") e = ymd(addDays(parseYmd(e), -1));
    if (e < s) e = s;
    return [s, e];
  }
  const isMultiDay = (ev) => {
    const [s, e] = eventSpan(ev);
    return s !== e;
  };

  function indexByDay() {
    const map = new Map();
    for (const ev of state.events) {
      const [s, e] = eventSpan(ev);
      for (let d = parseYmd(s), end = parseYmd(e); d <= end; d = addDays(d, 1)) {
        const key = ymd(d);
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(ev);
      }
    }
    const rank = (ev) => (ev.all_day ? 0 : isMultiDay(ev) ? 1 : 2);
    for (const list of map.values()) {
      list.sort((a, b) => rank(a) - rank(b) || a.start.localeCompare(b.start) || a.id - b.id);
    }
    state.byDay = map;
  }

  const mondayOf = (d) => addDays(d, -((d.getDay() + 6) % 7)); // неделя начинается с понедельника

  // Диапазон дат, который показывает текущий вид: [start, end)
  function viewRange() {
    if (state.view === "week") {
      const start = mondayOf(parseYmd(state.selected));
      return { start, end: addDays(start, 7) };
    }
    if (state.view === "day") {
      const start = parseYmd(state.selected);
      return { start, end: addDays(start, 1) };
    }
    const start = mondayOf(state.month);
    return { start, end: addDays(start, 42) };
  }
  const rangeKey = () => {
    const { start, end } = viewRange();
    return `${ymd(start)}/${ymd(end)}`;
  };

  async function load() {
    const seq = ++state.loadSeq;
    const key = rangeKey();
    const { start, end } = viewRange();
    try {
      const data = await api(`${API}?start=${ymd(start)}&end=${ymd(end)}`);
      if (seq !== state.loadSeq) return; // пришёл устаревший ответ
      state.events = data.events;
      state.loadedKey = key;
      indexByDay();
      render();
    } catch (err) {
      toast("Не удалось загрузить события: " + err.message, true);
    }
  }

  // Перерисовать сразу, а события догрузить, если изменился диапазон
  function refresh() {
    render();
    return rangeKey() === state.loadedKey ? Promise.resolve() : load();
  }

  // ---------- rendering ----------
  const grid = $("#grid");
  const cal = $("#cal");
  const timeView = $("#time-view");
  const viewSelect = $("#view-select");

  function timeLabel(ev, day) {
    if (ev.all_day) return "Весь день";
    const s = parseDT(ev.start);
    const e = parseDT(ev.end);
    if (!isMultiDay(ev)) return ev.start === ev.end ? hm(s) : `${hm(s)} – ${hm(e)}`;
    const short = (d) => `${d.getDate()} ${MONTHS_SHORT[d.getMonth()]} ${hm(d)}`;
    return `${short(s)} – ${short(e)}`;
  }

  function chipHtml(ev, day) {
    const banner = ev.all_day || isMultiDay(ev);
    const tip = escapeHtml(`${ev.title} · ${timeLabel(ev, day)}${ev.location ? " · " + ev.location : ""}`);
    if (banner) {
      return `<div class="chip allday c-${ev.color}" draggable="${canWrite(ev)}" data-id="${ev.id}" title="${tip}">
        <span class="label">${escapeHtml(ev.title)}</span>${clipHtml(ev)}</div>`;
    }
    return `<div class="chip" draggable="${canWrite(ev)}" data-id="${ev.id}" title="${tip}">
      <span class="bar c-${ev.color}"></span><span class="t">${ev.start.slice(11)}</span>
      <span class="label">${escapeHtml(ev.title)}</span>${clipHtml(ev)}</div>`;
  }

  const clipHtml = (ev) => (ev.attachments && ev.attachments.length ? `<span class="clip" aria-label="Есть вложения">📎</span>` : "");

  // Только http(s)-ссылки, чтобы не допустить javascript: и т.п.
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");
  const hostOf = (u) => {
    try {
      return new URL(u).host.replace(/^www\./, "");
    } catch (_) {
      return u;
    }
  };
  const fmtSize = (n) =>
    n < 1024 ? `${n} Б` : n < 1048576 ? `${(n / 1024).toFixed(0)} КБ` : `${(n / 1048576).toFixed(1).replace(".", ",")} МБ`;

  const WD_SHORT = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
  const HOUR_PX = 48; // высота часа в видах «Неделя» и «День» (совпадает с --hour в CSS)
  const toMin = (dt) => Number(dt.slice(11, 13)) * 60 + Number(dt.slice(14, 16));
  const minToHM = (m) => `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
  const nowMinutes = () => {
    const d = new Date();
    return d.getHours() * 60 + d.getMinutes();
  };

  function renderTitle() {
    let title;
    let sub;
    if (state.view === "month") {
      title = MONTHS[state.month.getMonth()];
      sub = String(state.month.getFullYear());
    } else if (state.view === "week") {
      const s = mondayOf(parseYmd(state.selected));
      const e = addDays(s, 6);
      title =
        s.getMonth() === e.getMonth()
          ? `${s.getDate()}–${e.getDate()} ${MONTHS_GEN[e.getMonth()]}`
          : `${s.getDate()} ${MONTHS_SHORT[s.getMonth()]} – ${e.getDate()} ${MONTHS_SHORT[e.getMonth()]}`;
      sub = s.getFullYear() === e.getFullYear() ? String(e.getFullYear()) : `${s.getFullYear()}–${e.getFullYear()}`;
    } else {
      const d = parseYmd(state.selected);
      title = `${d.getDate()} ${MONTHS_GEN[d.getMonth()]}`;
      sub = `${WEEKDAYS[d.getDay()].toLowerCase()}, ${d.getFullYear()}`;
    }
    $("#month-title").textContent = title;
    $("#year-title").textContent = sub;
    document.title = `${title} ${sub} — Календарь`;

    const names = {
      month: ["Предыдущий месяц", "Следующий месяц"],
      week: ["Предыдущая неделя", "Следующая неделя"],
      day: ["Предыдущий день", "Следующий день"],
    }[state.view];
    $("#prev").title = `${names[0]} (←)`;
    $("#prev").setAttribute("aria-label", names[0]);
    $("#next").title = `${names[1]} (→)`;
    $("#next").setAttribute("aria-label", names[1]);
  }

  function render() {
    renderTitle();
    const isMonth = state.view === "month";
    $("#month-view").hidden = !isMonth;
    timeView.hidden = isMonth;
    viewSelect.value = state.view;
    if (isMonth) renderMonth();
    else renderTimeGrid();
    renderDayPanel();
  }

  function renderMonth() {
    const m = state.month;
    const { start } = viewRange();
    const today = todayStr();
    let html = "";
    for (let i = 0; i < 42; i++) {
      const d = addDays(start, i);
      const key = ymd(d);
      const evs = state.byDay.get(key) || [];
      const cls = ["cell"];
      if (d.getMonth() !== m.getMonth()) cls.push("other");
      if (d.getDay() === 0 || d.getDay() === 6) cls.push("weekend");
      if (key === today) cls.push("today");
      if (key === state.selected) cls.push("selected");
      const label = `${d.getDate()} ${MONTHS_GEN[d.getMonth()]}, событий: ${evs.length}`;
      html += `<div class="${cls.join(" ")}" data-date="${key}" role="gridcell" tabindex="-1" aria-label="${label}">
        <span class="num">${d.getDate()}</span>
        ${evs.slice(0, MAX_CHIPS).map((ev) => chipHtml(ev, key)).join("")}
        ${evs.length > MAX_CHIPS ? `<span class="more">+ ещё ${evs.length - MAX_CHIPS}</span>` : ""}
        ${evs.length ? `<span class="dots">${evs.slice(0, 4).map((ev) => `<span class="c-${ev.color}"></span>`).join("")}</span>` : ""}
      </div>`;
    }
    grid.innerHTML = html;
  }

  // Раскладка событий одного дня по колонкам, чтобы пересекающиеся не накрывали друг друга
  function layoutDay(evs) {
    const items = evs
      .filter((ev) => !ev.all_day && !isMultiDay(ev))
      .map((ev) => {
        const s = toMin(ev.start);
        // окончание в 00:00 следующего дня — это конец текущего дня
        const e = ev.end.slice(0, 10) > ev.start.slice(0, 10) ? 1440 : toMin(ev.end);
        return { ev, s, e, vis: Math.min(1440, Math.max(e, s + 20)) };
      })
      .sort((a, b) => a.s - b.s || b.e - a.e);

    const out = [];
    let cluster = [];
    let lanes = [];
    let clusterEnd = -1;
    const flush = () => {
      for (const it of cluster) it.cols = lanes.length;
      out.push(...cluster);
      cluster = [];
      lanes = [];
      clusterEnd = -1;
    };
    for (const it of items) {
      if (cluster.length && it.s >= clusterEnd) flush();
      let lane = lanes.findIndex((end) => end <= it.s);
      if (lane === -1) {
        lane = lanes.length;
        lanes.push(it.vis);
      } else {
        lanes[lane] = it.vis;
      }
      it.lane = lane;
      cluster.push(it);
      clusterEnd = Math.max(clusterEnd, it.vis);
    }
    if (cluster.length) flush();
    return out;
  }

  function renderTimeGrid() {
    const { start } = viewRange();
    const n = state.view === "week" ? 7 : 1;
    const days = Array.from({ length: n }, (_, i) => addDays(start, i));
    const today = todayStr();
    const nowMin = nowMinutes();

    const head = days
      .map((d) => {
        const key = ymd(d);
        const cls = ["tg-dayhead"];
        if (key === today) cls.push("today");
        if (key === state.selected) cls.push("selected");
        if (d.getDay() === 0 || d.getDay() === 6) cls.push("weekend");
        const wd = n === 1 ? WEEKDAYS[d.getDay()] : WD_SHORT[d.getDay()];
        const tip = n === 1 ? "" : ` title="Открыть день"`;
        return `<button type="button" class="${cls.join(" ")}" data-date="${key}"${tip}>
          <span class="wd">${wd}</span><span class="dn">${d.getDate()}</span></button>`;
      })
      .join("");

    const allday = days
      .map((d) => {
        const key = ymd(d);
        const evs = (state.byDay.get(key) || []).filter((ev) => ev.all_day || isMultiDay(ev));
        return `<div class="tg-adcell" data-date="${key}">${evs.map((ev) => chipHtml(ev, key)).join("")}</div>`;
      })
      .join("");

    const hours = Array.from({ length: 24 }, (_, h) => `<div class="tg-hour"><span>${h ? `${pad(h)}:00` : ""}</span></div>`).join("");

    const cols = days
      .map((d) => {
        const key = ymd(d);
        const blocks = layoutDay(state.byDay.get(key) || [])
          .map((it) => {
            const ev = it.ev;
            const top = (it.s * HOUR_PX) / 60;
            const height = Math.max(((it.vis - it.s) * HOUR_PX) / 60 - 2, 16);
            const w = 100 / it.cols;
            const time = ev.start === ev.end ? ev.start.slice(11) : `${ev.start.slice(11)} – ${ev.end.slice(11)}`;
            const tip = escapeHtml(`${ev.title} · ${time}${ev.location ? " · " + ev.location : ""}`);
            return `<div class="tg-ev c-${ev.color}${height < 34 ? " short" : ""}" draggable="${canWrite(ev)}" data-id="${ev.id}" title="${tip}"
              style="top:${top}px;height:${height}px;left:calc(${it.lane * w}% + 2px);width:calc(${w}% - 4px)">
              <div class="tg-ev-title">${escapeHtml(ev.title)}${clipHtml(ev)}</div>
              <div class="tg-ev-time">${time}${ev.location ? " · " + escapeHtml(ev.location) : ""}</div>
            </div>`;
          })
          .join("");
        const nowLine = key === today ? `<div class="tg-now" style="top:${(nowMin * HOUR_PX) / 60}px"></div>` : "";
        const cls = ["tg-col"];
        if (key === today) cls.push("today");
        if (key === state.selected) cls.push("selected");
        return `<div class="${cls.join(" ")}" data-date="${key}">${blocks}${nowLine}</div>`;
      })
      .join("");

    const prevBody = timeView.querySelector(".tg-body");
    const prevScroll = prevBody ? prevBody.scrollTop : 0;

    // Заголовки дней и «весь день» лежат в той же прокручиваемой области, что и часы
    // (закреплены сверху через sticky), поэтому полоса прокрутки не сдвигает колонки.
    timeView.innerHTML = `<div class="tg" style="--days:${n}">
      <div class="tg-body">
        <div class="tg-sticky">
          <div class="tg-head"><div class="tg-gutter"></div>${head}</div>
          <div class="tg-allday"><div class="tg-gutter">весь<br>день</div>${allday}</div>
        </div>
        <div class="tg-inner"><div class="tg-times">${hours}</div>${cols}</div>
      </div>
    </div>`;

    // При открытии новой недели/дня прокручиваем к текущему времени или к 8:00
    const body = timeView.querySelector(".tg-body");
    const key = `${state.view}:${ymd(start)}`;
    if (state.scrollKey !== key) {
      state.scrollKey = key;
      const hasToday = days.some((d) => ymd(d) === today);
      body.scrollTop = ((hasToday ? Math.max(nowMin - 90, 0) : 8 * 60) * HOUR_PX) / 60;
    } else {
      body.scrollTop = prevScroll;
    }
  }

  function renderDayPanel() {
    const d = parseYmd(state.selected);
    $("#day-weekday").textContent = WEEKDAYS[d.getDay()];
    $("#day-date").textContent = `${d.getDate()} ${MONTHS_GEN[d.getMonth()]} ${d.getFullYear()}`;
    const evs = state.byDay.get(state.selected) || [];
    const list = $("#day-events");
    if (!evs.length) {
      list.innerHTML = `<li class="none">Нет событий</li>`;
      return;
    }
    list.innerHTML = evs
      .map(
        (ev) => `<li class="c-${ev.color}" data-id="${ev.id}" tabindex="0">
          <div>
            <div class="time">${escapeHtml(timeLabel(ev, state.selected))}</div>
            <div class="ttl">${escapeHtml(ev.title)}</div>
            ${ev.restricted ? `<div class="locked">🔒 Подробности скрыты</div>` : ""}
            ${ev.owner && ev.owner.id !== ME.id ? `<div class="meta-line">👤 ${escapeHtml(ev.owner.name)}</div>` : ""}
            ${ev.location ? `<div class="loc">📍 ${escapeHtml(ev.location)}</div>` : ""}
            ${safeUrl(ev.url) ? `<div class="meta-line">🔗 <a href="${escapeHtml(ev.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(hostOf(ev.url))}</a></div>` : ""}
            ${ev.invitees && ev.invitees.length ? `<div class="meta-line" title="${escapeHtml(ev.invitees.map((u) => u.name).join(", "))}">👥 ${escapeHtml(ev.invitees.map((u) => u.name).join(", "))}</div>` : ""}
            ${(ev.attachments || []).map((a) => `<div class="meta-line">📎 <a href="${escapeHtml(a.url)}">${escapeHtml(a.name)}</a> <span>${fmtSize(a.size)}</span></div>`).join("")}
            ${ev.description ? `<div class="desc">${escapeHtml(ev.description)}</div>` : ""}
          </div></li>`
      )
      .join("");
  }

  // ---------- navigation ----------
  function syncMonthToSelected() {
    const d = parseYmd(state.selected);
    state.month = new Date(d.getFullYear(), d.getMonth(), 1);
  }

  function navigate(delta) {
    if (state.view === "month") {
      const m = new Date(state.month.getFullYear(), state.month.getMonth() + delta, 1);
      const daysInMonth = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
      state.month = m;
      // выбранный день переносим в новый месяц, чтобы при смене вида остаться в нём
      state.selected = ymd(new Date(m.getFullYear(), m.getMonth(), Math.min(parseYmd(state.selected).getDate(), daysInMonth)));
    } else {
      state.selected = ymd(addDays(parseYmd(state.selected), delta * (state.view === "week" ? 7 : 1)));
      syncMonthToSelected();
    }
    return refresh();
  }

  function goToDate(dateStr) {
    state.selected = dateStr;
    syncMonthToSelected();
    return refresh();
  }

  function setView(view) {
    if (!VIEWS.includes(view)) return;
    if (view !== state.view && view !== MY_SETTINGS.last_view) saveMySettings({ last_view: view });
    state.view = view;
    syncMonthToSelected();
    refresh();
  }

  function select(dateStr) {
    state.selected = dateStr;
    cal.querySelectorAll(".cell.selected, .tg-col.selected, .tg-dayhead.selected").forEach((c) => c.classList.remove("selected"));
    cal.querySelectorAll(`.cell[data-date="${dateStr}"], .tg-col[data-date="${dateStr}"], .tg-dayhead[data-date="${dateStr}"]`)
      .forEach((c) => c.classList.add("selected"));
    renderDayPanel();
  }

  // ---------- dialog ----------
  const dialog = $("#event-dialog");
  const form = $("#event-form");
  const f = form.elements;
  let durationMs = 3600000;

  function clearErrors() {
    form.querySelectorAll(".error").forEach((e) => (e.textContent = ""));
  }

  function showErrors(errors) {
    let shown = false;
    for (const [field, msgs] of Object.entries(errors || {})) {
      const target = form.querySelector(`.error[data-for="${field}"]`) || form.querySelector('.error[data-for="__all__"]');
      target.textContent = [].concat(msgs).join(" ");
      shown = true;
    }
    return shown;
  }

  function syncAllDay() {
    const on = f.all_day.checked;
    for (const el of [f.start_time, f.end_time]) {
      el.hidden = on;
      el.disabled = on;
    }
  }

  function readStart() {
    return parseDT(`${f.start_date.value}T${f.all_day.checked ? "00:00" : f.start_time.value || "00:00"}`);
  }
  function readEnd() {
    return parseDT(`${f.end_date.value}T${f.all_day.checked ? "00:00" : f.end_time.value || "00:00"}`);
  }

  // ---------- участники (выбор из адресной книги AD) ----------
  const USERS = JSON.parse(($("#users-data") || {}).textContent || "[]");
  const usersById = new Map(USERS.map((u) => [u.id, u]));
  const usersByEmail = new Map(USERS.filter((u) => u.email).map((u) => [u.email.toLowerCase(), u]));
  const AVATAR_COLORS = ["blue", "green", "purple", "orange", "red", "gray"];
  const initials = (name) =>
    name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  // [{email, name, dir}] — dir: выбран из адресной книги (событие само попадёт в его календарь)
  const parts = { list: [] };

  // ---------- attachments ----------
  const ATT_API = "/api/attachments/";
  const MAX_MB = window.CALENDAR_MAX_UPLOAD_MB || 20;
  const files = { existing: [], removed: new Set(), pending: [] };
  const fileInput = $("#file-input");
  const fileList = $("#file-list");
  const dropzone = $("#dropzone");

  function setFiles(attachments) {
    files.existing = (attachments || []).slice();
    files.removed = new Set();
    files.pending = [];
    renderFiles();
  }

  function addFiles(list) {
    form.querySelector('.error[data-for="files"]').textContent = "";
    const tooBig = [];
    for (const file of list) {
      if (file.size > MAX_MB * 1048576) {
        tooBig.push(file.name);
        continue;
      }
      if (files.pending.some((p) => p.name === file.name && p.size === file.size)) continue;
      files.pending.push(file);
    }
    if (tooBig.length) showErrors({ files: `Файлы больше ${MAX_MB} МБ не прикреплены: ${tooBig.join(", ")}` });
    renderFiles();
  }

  function renderFiles() {
    fileList.innerHTML = [
      ...files.existing.map((a) => {
        const rm = files.removed.has(a.id);
        return `<li class="${rm ? "removed" : ""}">
          <span aria-hidden="true">📎</span>
          <a class="fname" href="${escapeHtml(a.url)}">${escapeHtml(a.name)}</a>
          <span class="fsize">${fmtSize(a.size)}</span>
          <button type="button" data-toggle-existing="${a.id}">${rm ? "Вернуть" : "Удалить"}</button>
        </li>`;
      }),
      ...files.pending.map(
        (p, i) => `<li>
          <span aria-hidden="true">📄</span>
          <span class="fname">${escapeHtml(p.name)}</span>
          <span class="badge">новый</span>
          <span class="fsize">${fmtSize(p.size)}</span>
          <button type="button" data-remove-pending="${i}" aria-label="Убрать: ${escapeHtml(p.name)}">✕</button>
        </li>`
      ),
    ].join("");
  }

  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });
  fileList.addEventListener("click", (e) => {
    const ex = e.target.closest("button[data-toggle-existing]");
    const pend = e.target.closest("button[data-remove-pending]");
    if (ex) {
      const id = Number(ex.dataset.toggleExisting);
      files.removed.has(id) ? files.removed.delete(id) : files.removed.add(id);
      renderFiles();
    } else if (pend) {
      files.pending.splice(Number(pend.dataset.removePending), 1);
      renderFiles();
    }
  });
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  ["dragenter", "dragover"].forEach((t) =>
    dropzone.addEventListener(t, (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dropzone.classList.add("over");
    })
  );
  ["dragleave", "drop"].forEach((t) => dropzone.addEventListener(t, () => dropzone.classList.remove("over")));
  dropzone.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    addFiles(e.dataTransfer.files);
  });
  // файл, брошенный мимо зоны, не должен открываться браузером вместо календаря
  document.addEventListener("dragover", (e) => hasFiles(e) && e.preventDefault());
  document.addEventListener("drop", (e) => hasFiles(e) && e.preventDefault());

  // ---------- fill / open ----------
  function fillForm(ev) {
    form.reset();
    clearErrors();
    f.id.value = ev.id || "";
    f.title.value = ev.title || "";
    f.all_day.checked = !!ev.all_day;
    f.start_date.value = ev.start.slice(0, 10);
    f.start_time.value = ev.start.slice(11, 16);
    f.end_date.value = ev.end.slice(0, 10);
    f.end_time.value = ev.end.slice(11, 16);
    f.location.value = ev.location || "";
    f.url.value = ev.url || ""; // поля «Ссылка» в форме нет, но ссылку, пришедшую из других календарей, не теряем
    f.reminder_minutes.value = ev.reminder_minutes === null || ev.reminder_minutes === undefined ? "" : String(ev.reminder_minutes);
    setParticipants(ev);
    f.notify.checked = false;
    f.description.value = ev.description || "";
    const color = form.querySelector(`input[name="color"][value="${ev.color}"]`) || form.querySelector('input[name="color"]');
    color.checked = true;
    if (ev.all_day) {
      // в форме у событий на весь день показываем «разумное» время, если пользователь снимет флажок
      f.start_time.value = "09:00";
      f.end_time.value = "10:00";
    }
    setFiles(ev.attachments || []);
    syncAllDay();
    durationMs = Math.max(0, readEnd() - readStart());
    // режим «только чтение», если нет права на запись
    const readOnly = !!ev.id && !canWrite(ev);
    $("#form-fields").disabled = readOnly;
    $("#readonly-note").hidden = !readOnly;
    $("#save").hidden = readOnly;
    $("#cancel").textContent = readOnly ? "Закрыть" : "Отмена";
    $("#dialog-title").textContent = !ev.id ? "Новое событие" : readOnly ? "Событие" : "Редактировать событие";
    $("#delete-event").hidden = !ev.id || readOnly;

    // в чей календарь создаётся событие
    const owners = ME.creatable_owners || [];
    const ownerField = $("#owner-field");
    const ownerLine = $("#owner-line");
    const ownerSelect = $("#owner-select");
    ownerField.hidden = !!ev.id || owners.length < 2;
    if (!ownerField.hidden) {
      ownerSelect.innerHTML = owners
        .map((o) => `<option value="${o.id}">${escapeHtml(o.name)}${o.id === ME.id ? " (мой календарь)" : ""}</option>`)
        .join("");
      ownerSelect.value = String(owners.some((o) => o.id === ME.id) ? ME.id : owners[0].id);
    }
    const shownOwner = ev.id ? ev.owner : owners.length === 1 ? owners[0] : null;
    ownerLine.hidden = !shownOwner || shownOwner.id === ME.id;
    if (!ownerLine.hidden) ownerLine.textContent = `Календарь: ${shownOwner.name}`;
  }

  function openNew(dateStr = state.selected, timeStr = null) {
    if (!ME.can_create) {
      toast("У вас нет права создавать события.", true);
      return;
    }
    let start;
    if (timeStr) {
      start = parseDT(`${dateStr}T${timeStr}`);
    } else if (dateStr === todayStr()) {
      start = new Date();
      start.setHours(start.getHours() + 1, 0, 0, 0);
      if (ymd(start) !== dateStr) start = parseDT(`${dateStr}T23:00`);
    } else {
      start = parseDT(`${dateStr}T09:00`);
    }
    const end = new Date(start.getTime() + 3600000);
    fillForm({ title: "", start: fmtDT(start), end: fmtDT(end), all_day: false, color: "blue", reminder_minutes: 15 });
    dialog.showModal();
    f.title.focus();
  }

  async function openEdit(id) {
    let ev = state.events.find((e) => e.id === Number(id));
    if (!ev) {
      try {
        ev = await api(`${API}${id}/`);
      } catch (err) {
        return toast(err.message, true);
      }
    }
    if (!canRead(ev)) {
      toast("Подробности скрыты: нет права на чтение этого события.", true);
      return;
    }
    fillForm(ev);
    dialog.showModal();
    (canWrite(ev) ? f.title : $("#cancel")).focus();
  }

  // При изменении начала сдвигаем окончание, сохраняя длительность
  function onStartChange() {
    if (!f.start_date.value) return;
    const end = new Date(readStart().getTime() + durationMs);
    f.end_date.value = ymd(end);
    if (!f.all_day.checked) f.end_time.value = hm(end);
  }
  function onEndChange() {
    if (!f.start_date.value || !f.end_date.value) return;
    durationMs = Math.max(0, readEnd() - readStart());
  }

  f.start_date.addEventListener("change", onStartChange);
  f.start_time.addEventListener("change", onStartChange);
  f.end_date.addEventListener("change", onEndChange);
  f.end_time.addEventListener("change", onEndChange);
  f.all_day.addEventListener("change", () => {
    syncAllDay();
    onEndChange();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("#form-fields").disabled) return; // режим только чтения
    clearErrors();

    const errors = {};
    if (!f.title.value.trim()) errors.title = "Введите название.";
    if (!f.start_date.value) errors.start = "Укажите дату начала.";
    if (!f.end_date.value) errors.end = "Укажите дату окончания.";
    if (!errors.start && !errors.end && readEnd() < readStart()) errors.end = "Окончание не может быть раньше начала.";
    const url = f.url.value.trim();
    if (url && /^[a-z][a-z0-9+.-]*:/i.test(url) && !/^https?:\/\//i.test(url)) errors.url = "Ссылка должна начинаться с http:// или https://";
    if (showErrors(errors)) return;

    const allDay = f.all_day.checked;
    const payload = {
      title: f.title.value.trim(),
      all_day: allDay,
      start: allDay ? `${f.start_date.value}T00:00` : `${f.start_date.value}T${f.start_time.value || "00:00"}`,
      end: allDay ? `${f.end_date.value}T23:59` : `${f.end_date.value}T${f.end_time.value || "00:00"}`,
      location: f.location.value.trim(),
      url,
      description: f.description.value.trim(),
      color: form.querySelector('input[name="color"]:checked').value,
      guest_emails: parts.list.map((p) => p.email), // сотрудники из списка станут приглашёнными на сервере
      directory_guests: parts.list.filter((p) => p.dir).map((p) => p.email),
      participant_names: Object.fromEntries(parts.list.filter((p) => p.name).map((p) => [p.email.toLowerCase(), p.name])),
      reminder_minutes: f.reminder_minutes.value === "" ? null : Number(f.reminder_minutes.value),
    };

    // следом будут операции с файлами — приглашения участникам уйдут после них, уже с файлами
    const fileOps = files.removed.size + (files.pending.length ? 1 : 0);
    payload.files_pending = fileOps > 0;

    const id = f.id.value;
    if (!id) {
      const owners = ME.creatable_owners || [];
      payload.owner = !$("#owner-field").hidden ? Number($("#owner-select").value) : owners.length ? owners[0].id : ME.id;
    }
    const saveBtn = $("#save");
    saveBtn.disabled = true;
    try {
      const saved = await api(id ? `${API}${id}/` : API, { method: id ? "PUT" : "POST", body: payload });
      // если с файлами что-то пойдёт не так, повторное «Сохранить» обновит уже созданное событие
      f.id.value = saved.id;
      $("#delete-event").hidden = false;

      const problems = [];
      let opsLeft = fileOps;
      // приглашения отправляет последняя операция с файлами; у остальных — ?deliver=0
      const deliverFlag = () => (--opsLeft > 0 ? "?deliver=0" : "");
      for (const aid of [...files.removed]) {
        try {
          const res = await api(`${ATT_API}${aid}/${deliverFlag()}`, { method: "DELETE" });
          if (res && res.delivery) saved.delivery = res.delivery;
          files.removed.delete(aid);
          files.existing = files.existing.filter((a) => a.id !== aid);
        } catch (err) {
          problems.push(err.message);
        }
      }
      if (files.pending.length) {
        saveBtn.textContent = "Загрузка файлов…";
        const fd = new FormData();
        files.pending.forEach((file) => fd.append("files", file, file.name));
        try {
          const res = await api(`${API}${saved.id}/attachments/${deliverFlag()}`, { method: "POST", body: fd });
          if (res.delivery) saved.delivery = res.delivery;
          files.existing.push(...res.attachments);
          files.pending = [];
        } catch (err) {
          problems.push((err.errors && err.errors.files && err.errors.files.join(" ")) || err.message);
        }
      }

      const day = saved.start.slice(0, 10);
      state.selected = day;
      const target = parseYmd(day);
      state.month = new Date(target.getFullYear(), target.getMonth(), 1);

      if (problems.length) {
        renderFiles();
        showErrors({ files: "Событие сохранено, но с файлами возникла проблема: " + problems.join(" ") });
        load();
        return;
      }
      // приглашения по e-mail (.ics) участникам и гостям
      let note = "";
      if (f.notify.checked) {
        try {
          const r = await api(`${API}${saved.id}/invite/`, { method: "POST", body: {} });
          note = r.sent && r.sent.length ? ` Приглашения отправлены: ${r.sent.length}.` : ` ${r.message || ""}`;
        } catch (err) {
          note = ` Приглашения не отправлены: ${err.message}`;
        }
      }
      if (saved.reminder_minutes !== null && saved.reminder_minutes !== undefined) {
        askNotificationPermission();
        checkReminders();
      }
      const dn = describeDelivery(saved.delivery);
      dialog.close();
      toast(
        (id ? "Событие обновлено." : "Событие создано.") + note + (dn.text ? " " + dn.text : ""),
        note.includes("не отправлены") || dn.error,
        dn.text ? 9000 : 0
      );
      await load();
    } catch (err) {
      if (!showErrors(err.errors)) showErrors({ __all__: err.message });
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Сохранить";
    }
  });

  $("#delete-event").addEventListener("click", async () => {
    const id = f.id.value;
    if (!id || !confirm(`Удалить событие «${f.title.value}»?`)) return;
    try {
      // отмена для участников, если отмечено «Отправить приглашения»
      if (f.notify.checked) {
        try {
          await api(`${API}${id}/invite/`, { method: "POST", body: { cancel: true } });
        } catch (err) {
          if (!confirm(`Не удалось отправить отмену участникам (${err.message}). Всё равно удалить?`)) return;
        }
      }
      await api(`${API}${id}/`, { method: "DELETE" });
      dialog.close();
      toast("Событие удалено");
      load();
    } catch (err) {
      showErrors({ __all__: err.message });
    }
  });

  $("#cancel").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close(); // клик по подложке
  });

  // ---------- calendar interactions (все виды) ----------
  const snapMinutes = (clientY, col, step) => {
    const y = clientY - col.getBoundingClientRect().top;
    return Math.min(Math.max(Math.floor((y * 60) / HOUR_PX / step) * step, 0), 1440 - step);
  };

  cal.addEventListener("click", (e) => {
    const head = e.target.closest(".tg-dayhead");
    if (head) {
      // клик по заголовку дня в «Неделе» открывает этот день
      state.selected = head.dataset.date;
      return setView("day");
    }
    const day = e.target.closest("[data-date]");
    if (!day) return;
    select(day.dataset.date);
    const item = e.target.closest(".chip, .tg-ev");
    if (item) openEdit(item.dataset.id);
  });

  cal.addEventListener("dblclick", (e) => {
    if (e.target.closest(".chip, .tg-ev, .tg-dayhead")) return;
    const col = e.target.closest(".tg-col");
    if (col) return openNew(col.dataset.date, minToHM(snapMinutes(e.clientY, col, 30)));
    const cell = e.target.closest(".cell, .tg-adcell");
    if (cell) openNew(cell.dataset.date);
  });

  $("#day-events").addEventListener("click", (e) => {
    if (e.target.closest("a")) return; // ссылки и файлы открываются сами по себе
    const li = e.target.closest("li[data-id]");
    if (li) openEdit(li.dataset.id);
  });
  $("#day-events").addEventListener("keydown", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      openEdit(li.dataset.id);
    }
  });

  // drag & drop: перенос на другой день (месяц, «весь день») или на другое время (неделя, день)
  const DROP_TARGETS = ".cell, .tg-adcell, .tg-col";
  let drag = null;
  const clearDrop = () => cal.querySelectorAll(".drop").forEach((c) => c.classList.remove("drop"));

  cal.addEventListener("dragstart", (e) => {
    const el = e.target.closest(".chip, .tg-ev");
    if (!el) return;
    const host = el.closest("[data-date]");
    drag = {
      id: Number(el.dataset.id),
      from: host.dataset.date,
      // где именно схватили блок — чтобы при переносе он не «прыгал»
      grabMin: el.classList.contains("tg-ev") ? ((e.clientY - el.getBoundingClientRect().top) * 60) / HOUR_PX : 0,
    };
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(drag.id));
  });
  cal.addEventListener("dragend", (e) => {
    e.target.closest?.(".chip, .tg-ev")?.classList.remove("dragging");
    clearDrop();
    drag = null;
  });
  cal.addEventListener("dragover", (e) => {
    const target = e.target.closest(DROP_TARGETS);
    if (!drag || !target) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!target.classList.contains("drop")) {
      clearDrop();
      target.classList.add("drop");
    }
  });
  cal.addEventListener("drop", async (e) => {
    const target = e.target.closest(DROP_TARGETS);
    if (!drag || !target) return;
    e.preventDefault();
    clearDrop();
    const { id, from, grabMin } = drag;
    const ev = state.events.find((x) => x.id === id);
    if (!ev) return;
    if (!canWrite(ev)) {
      toast("Нет права изменять это событие.", true);
      return;
    }

    let patch;
    if (target.classList.contains("tg-col") && !ev.all_day && !isMultiDay(ev)) {
      // новое время с шагом 15 минут, длительность сохраняется
      const y = e.clientY - target.getBoundingClientRect().top;
      const min = Math.min(Math.max(Math.round(((y * 60) / HOUR_PX - grabMin) / 15) * 15, 0), 1440 - 15);
      const oldStart = parseDT(ev.start);
      const newStart = parseDT(`${target.dataset.date}T${minToHM(min)}`);
      const newEnd = new Date(parseDT(ev.end).getTime() + (newStart - oldStart));
      patch = { start: fmtDT(newStart), end: fmtDT(newEnd) };
    } else {
      const delta = daysBetween(from, target.dataset.date);
      patch = { start: shiftDT(ev.start, delta), end: shiftDT(ev.end, delta) };
    }
    if (patch.start === ev.start && patch.end === ev.end) return;

    // оптимистичное обновление
    Object.assign(ev, patch);
    indexByDay();
    state.selected = target.dataset.date;
    render();
    try {
      await api(`${API}${id}/`, { method: "PATCH", body: patch });
      toast("Событие перенесено");
    } catch (err) {
      toast("Не удалось перенести: " + err.message, true);
    }
    load();
  });

  // ---------- toolbar ----------
  $("#prev").addEventListener("click", () => navigate(-1));
  $("#next").addEventListener("click", () => navigate(1));
  viewSelect.addEventListener("change", () => setView(viewSelect.value));
  $("#new-event").addEventListener("click", () => openNew());
  $("#add-to-day").addEventListener("click", () => openNew(state.selected));

  // ---------- search ----------
  // Поиск событий: подсказки по мере ввода, выбор стрелками ↑/↓, Enter — открыть, Esc — закрыть.
  const search = $("#search");
  const results = $("#search-results");
  const found = { items: [], active: -1 };
  let searchTimer = null;
  let searchSeq = 0;

  function closeResults() {
    results.hidden = true;
    search.setAttribute("aria-expanded", "false");
    search.removeAttribute("aria-activedescendant");
  }

  function setActive(i) {
    found.active = i;
    results.querySelectorAll("[role=option]").forEach((el, j) => {
      el.classList.toggle("active", j === i);
      el.setAttribute("aria-selected", String(j === i));
    });
    const act = results.querySelector(`#search-opt-${i}`);
    if (act) {
      search.setAttribute("aria-activedescendant", act.id);
      act.scrollIntoView({ block: "nearest" });
    } else {
      search.removeAttribute("aria-activedescendant");
    }
  }

  function renderResults() {
    results.innerHTML = found.items.length
      ? found.items
          .map((ev, i) => {
            const d = parseDT(ev.start);
            const when = `${d.getDate()} ${MONTHS_GEN[d.getMonth()]} ${d.getFullYear()}${ev.all_day ? "" : ", " + hm(d)}`;
            return `<button type="button" tabindex="-1" role="option" id="search-opt-${i}" data-i="${i}" aria-selected="false">
              <span class="dots" style="display:block"><span class="c-${ev.color}" style="display:block;width:8px;height:8px;border-radius:50%;margin-top:6px"></span></span>
              <span><div>${escapeHtml(ev.title)}</div><div class="meta">${when}</div></span></button>`;
          })
          .join("")
      : `<div class="empty">Ничего не найдено</div>`;
    results.hidden = false;
    search.setAttribute("aria-expanded", "true");
    // первый результат сразу выделен — Enter откроет его
    setActive(found.items.length ? 0 : -1);
  }

  async function runSearch() {
    clearTimeout(searchTimer);
    searchTimer = null; // отложенный поиск больше не ждёт
    const q = search.value.trim();
    if (!q) {
      found.items = [];
      closeResults();
      return;
    }
    const seq = ++searchSeq;
    try {
      const data = await api(`${API}?q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq) return; // ответ на устаревший запрос
      found.items = data.events.slice(0, 30);
      renderResults();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function openFound(i) {
    const ev = found.items[i];
    if (!ev) return;
    closeResults();
    search.blur();
    await goToDate(ev.start.slice(0, 10));
    openEdit(ev.id);
  }

  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = null;
    if (!search.value.trim()) {
      found.items = [];
      closeResults();
      return;
    }
    searchTimer = setTimeout(runSearch, 250);
  });

  search.addEventListener("focus", () => {
    if (search.value.trim() && found.items.length) {
      results.hidden = false;
      search.setAttribute("aria-expanded", "true");
    }
  });

  search.addEventListener("keydown", async (e) => {
    const n = found.items.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (results.hidden) {
        // список закрыт — открываем (или сначала ищем, если ещё не искали)
        if (n) renderResults();
        else await runSearch();
        return;
      }
      if (!n) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(found.active < 0 ? (step > 0 ? 0 : n - 1) : (found.active + step + n) % n);
    } else if (e.key === "Home" && !results.hidden && n) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === "End" && !results.hidden && n) {
      e.preventDefault();
      setActive(n - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (searchTimer) await runSearch(); // ввод ещё не обработан — ищем сразу
      if (!results.hidden && found.active >= 0) openFound(found.active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (!results.hidden) {
        closeResults(); // первый Esc закрывает список
      } else {
        search.value = ""; // второй — очищает поле
        found.items = [];
        search.blur();
      }
    } else if (e.key === "Tab") {
      closeResults();
    }
  });

  results.addEventListener("mousedown", (e) => e.preventDefault()); // фокус остаётся в поле
  results.addEventListener("mousemove", (e) => {
    const opt = e.target.closest("[role=option]");
    if (opt && Number(opt.dataset.i) !== found.active) setActive(Number(opt.dataset.i));
  });
  results.addEventListener("click", (e) => {
    const opt = e.target.closest("[role=option]");
    if (opt) openFound(Number(opt.dataset.i));
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".search")) closeResults();
  });

  // ---------- keyboard ----------
  document.addEventListener("keydown", (e) => {
    if (dialog.open || accessDialog.open || intDialog.open || cDialog.open || settingsDialog.open || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    const k = e.key.toLowerCase();
    if (e.key === "ArrowLeft") navigate(-1);
    else if (e.key === "ArrowRight") navigate(1);
    else if (k === "t" || k === "е") goToDate(todayStr());
    else if (k === "m" || k === "ь") setView("month");
    else if (k === "w" || k === "ц") setView("week");
    else if (k === "d" || k === "в") setView("day");
    else if (k === "n" || k === "т") {
      e.preventDefault();
      openNew();
    } else if (k === "/") {
      e.preventDefault();
      search.focus();
    }
  });

  // ---------- access: делегирование прав менеджерам ----------
  const DELEG_API = "/api/delegations/";
  const ACTIONS = { create: "Запись", read: "Чтение", change: "Изменение" };
  const accessDialog = $("#access-dialog");
  const accessRows = $("#access-rows");
  const accessError = accessDialog.querySelector('.error[data-for="access"]');
  const accessSearch = $("#access-search");
  const accessSuggest = $("#access-suggest");
  let accessState = []; // строки таблицы: {id, name, email, create, read, change, orig, isNew}
  const flagsOf = (m) => JSON.stringify({ create: !!m.create, read: !!m.read, change: !!m.change });
  const NO_RIGHTS = flagsOf({});
  const isRevoked = (m) => !m.isNew && m.orig !== NO_RIGHTS && flagsOf(m) === NO_RIGHTS;

  // ---- таблица прав ----
  function renderAccess() {
    accessRows.innerHTML = accessState.length
      ? accessState
          .map((m, i) => {
            const cls = [m.isNew ? "new" : "", isRevoked(m) ? "revoked" : ""].join(" ").trim();
            return `<tr data-id="${m.id}"${cls ? ` class="${cls}"` : ""}>
              <td>${escapeHtml(m.name)}${m.email ? `<span class="email">${escapeHtml(m.email)}</span>` : ""}</td>
              ${Object.entries(ACTIONS)
                .map(
                  ([k, label]) =>
                    `<td><input type="checkbox" data-i="${i}" data-k="${k}"${m[k] ? " checked" : ""} aria-label="${label}: ${escapeHtml(m.name)}"></td>`
                )
                .join("")}
              <td><button type="button" class="remove" data-remove="${i}" title="Отозвать все права" aria-label="Отозвать права: ${escapeHtml(m.name)}">×</button></td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="5" class="empty">Права пока никому не выданы. Найдите сотрудника через поиск выше.</td></tr>`;
  }

  async function openAccess() {
    try {
      const data = await api(DELEG_API);
      accessState = data.delegations.map((d) => {
        const row = { ...d.user, create: d.can_create, read: d.can_read, change: d.can_change };
        row.orig = flagsOf(row);
        return row;
      });
      accessError.textContent = "";
      accessSearch.value = "";
      suggest.items = [];
      closeSuggest();
      renderAccess();
      accessDialog.showModal();
      accessSearch.focus();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function addToAccess(user) {
    if (!accessState.some((m) => m.id === user.id)) {
      // новый сотрудник: по умолчанию отмечено «Чтение», остальное — вручную
      accessState.push({ ...user, create: false, read: true, change: false, orig: NO_RIGHTS, isNew: true });
    }
    renderAccess();
    const row = accessRows.querySelector(`tr[data-id="${user.id}"]`);
    if (row) {
      row.classList.remove("flash");
      void row.offsetWidth; // перезапуск анимации подсветки
      row.classList.add("flash");
      row.scrollIntoView({ block: "nearest" });
    }
    accessSearch.value = "";
    suggest.items = [];
    closeSuggest();
    accessSearch.focus();
  }

  accessRows.addEventListener("change", (e) => {
    const cb = e.target.closest("input[data-k]");
    if (!cb) return;
    const i = Number(cb.dataset.i);
    const m = accessState[i];
    const k = cb.dataset.k;
    m[k] = cb.checked;
    // изменять можно только то, что можно прочитать: изменение ⇒ чтение
    if (cb.checked && k === "change") m.read = true;
    if (!cb.checked && k === "read") m.change = false;
    accessRows.querySelectorAll(`input[data-i="${i}"]`).forEach((x) => (x.checked = m[x.dataset.k]));
    cb.closest("tr").classList.toggle("revoked", isRevoked(m));
  });

  accessRows.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-remove]");
    if (!btn) return;
    const i = Number(btn.dataset.remove);
    const m = accessState[i];
    if (m.isNew) accessState.splice(i, 1); // ещё не сохранён — просто убираем строку
    else Object.assign(m, { create: false, read: false, change: false }); // права отзовутся при сохранении
    renderAccess();
  });

  // ---- поиск сотрудника с подсказками ----
  const suggest = { items: [], active: 0, seq: 0, timer: null };

  // Подсветка совпавших частей: без учёта регистра, «ё» = «е»; приоритет — совпадению с начала слова
  const foldText = (s) => Array.from(String(s || ""), (c) => c.toLowerCase().replace("ё", "е")).join("");
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  function highlight(text, query) {
    const src = Array.from(String(text || ""));
    const norm = foldText(text);
    const marks = new Array(src.length).fill(false);
    for (const t of foldText(query).split(/\s+/).filter(Boolean)) {
      const m = new RegExp(`(^|[\\s@._-])${escapeRe(t)}`).exec(norm);
      const idx = m ? m.index + m[1].length : norm.indexOf(t);
      if (idx >= 0) for (let j = idx; j < idx + t.length && j < marks.length; j++) marks[j] = true;
    }
    let html = "";
    let open = false;
    src.forEach((ch, j) => {
      if (marks[j] !== open) {
        html += marks[j] ? "<mark>" : "</mark>";
        open = marks[j];
      }
      html += escapeHtml(ch);
    });
    return open ? html + "</mark>" : html;
  }

  function closeSuggest() {
    accessSuggest.hidden = true;
    accessSearch.setAttribute("aria-expanded", "false");
    accessSearch.removeAttribute("aria-activedescendant");
  }

  function renderSuggest(query) {
    accessSuggest.innerHTML = suggest.items.length
      ? suggest.items
          .map((u, i) => {
            const already = accessState.some((m) => m.id === u.id);
            const tag = u.exact
              ? `<span class="tag exact">точное совпадение</span>`
              : already
                ? `<span class="tag">уже в списке</span>`
                : "";
            return `<li role="option" id="acc-opt-${u.id}" data-i="${i}" aria-selected="${i === suggest.active}" class="${i === suggest.active ? "active" : ""}">
              <span class="avatar c-${AVATAR_COLORS[u.id % AVATAR_COLORS.length]}">${escapeHtml(initials(u.name))}</span>
              <span><div>${highlight(u.name, query)}</div><div class="email">${highlight(u.email || u.username, query)}</div></span>
              ${tag}
            </li>`;
          })
          .join("")
      : `<li class="empty">Никого не найдено</li>`;
    accessSuggest.hidden = false;
    accessSearch.setAttribute("aria-expanded", "true");
    const act = accessSuggest.querySelector("li.active");
    if (act) {
      accessSearch.setAttribute("aria-activedescendant", act.id);
      act.scrollIntoView({ block: "nearest" });
    } else {
      accessSearch.removeAttribute("aria-activedescendant");
    }
  }

  async function runSuggest() {
    const q = accessSearch.value;
    if (!q.trim()) {
      suggest.items = [];
      closeSuggest();
      return;
    }
    const seq = ++suggest.seq;
    try {
      const data = await api(`${DELEG_API}candidates/?q=${encodeURIComponent(q)}`);
      if (seq !== suggest.seq) return; // пришёл ответ на устаревший запрос
      suggest.items = data.results;
      suggest.active = 0; // лучшее совпадение (в т.ч. точное) — первым
      renderSuggest(q);
    } catch (err) {
      accessError.textContent = err.message;
    }
  }

  accessSearch.addEventListener("input", () => {
    clearTimeout(suggest.timer);
    suggest.timer = setTimeout(runSuggest, 150);
  });
  accessSearch.addEventListener("focus", () => {
    if (accessSearch.value.trim() && suggest.items.length) renderSuggest(accessSearch.value);
  });
  accessSearch.addEventListener("blur", () => setTimeout(closeSuggest, 120));
  accessSearch.addEventListener("keydown", (e) => {
    const n = suggest.items.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (accessSuggest.hidden) {
        runSuggest();
        return;
      }
      if (n) suggest.active = (suggest.active + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      renderSuggest(accessSearch.value);
    } else if (e.key === "Enter") {
      e.preventDefault(); // не отправлять форму
      if (!accessSuggest.hidden && suggest.items[suggest.active]) addToAccess(suggest.items[suggest.active]);
    } else if (e.key === "Escape" && !accessSuggest.hidden) {
      e.preventDefault(); // закрыть подсказки, а не окно
      e.stopPropagation();
      closeSuggest();
    }
  });
  accessSuggest.addEventListener("mousedown", (e) => {
    e.preventDefault(); // фокус остаётся в поле поиска
    const li = e.target.closest("li[data-i]");
    if (li) addToAccess(suggest.items[Number(li.dataset.i)]);
  });

  // ---- сохранение ----
  $("#access-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const changed = accessState.filter((m) => flagsOf(m) !== m.orig);
    const btn = $("#access-save");
    btn.disabled = true;
    accessError.textContent = "";
    try {
      for (const m of changed) {
        await api(DELEG_API, {
          method: "POST",
          body: { manager: m.id, can_create: m.create, can_read: m.read, can_change: m.change },
        });
        m.orig = flagsOf(m);
        m.isNew = false;
      }
      accessDialog.close();
      toast(changed.length ? "Права сохранены" : "Изменений нет");
    } catch (err) {
      accessError.textContent = err.message;
      renderAccess();
    } finally {
      btn.disabled = false;
    }
  });
  $("#access-cancel").addEventListener("click", () => accessDialog.close());
  const accessBtn = $("#access-btn");
  if (accessBtn) accessBtn.addEventListener("click", openAccess);

  // ---------- интеграции: внешние календари ----------
  const INT_API = "/api/integrations/";
  const intDialog = $("#integrations-dialog");
  const intList = $("#int-list");
  const intForm = $("#int-form");
  const intError = intForm.querySelector('.error[data-for="int"]');
  const intProvider = $("#int-provider");
  const intState = { providers: [], directions: [], colors: [], found: [], googlePending: false };

  const fmtSyncTime = (iso) => {
    if (!iso) return "ещё не синхронизирован";
    const d = new Date(iso);
    return `синхронизирован ${d.getDate()} ${MONTHS_GEN[d.getMonth()]} в ${hm(d)}`;
  };
  const optionsHtml = (items, selected) =>
    items.map((o) => `<option value="${o.code}"${o.code === selected ? " selected" : ""}>${escapeHtml(o.label)}</option>`).join("");

  function renderIntList(calendars) {
    intList.innerHTML = calendars
      .map(
        (c) => `<li class="int-item c-${c.color}${c.enabled ? "" : " off"}" data-id="${c.id}">
          <div>
            <div class="int-title">${escapeHtml(c.calendar_name)}</div>
            <div class="int-sub">${escapeHtml(c.provider_label)}${c.username ? " · " + escapeHtml(c.username) : ""}${c.remote_read_only ? " · только чтение" : ""}</div>
            <div class="int-status${c.last_error ? " error" : ""}">${
              c.last_error ? "⚠ " + escapeHtml(c.last_error) : `${fmtSyncTime(c.last_sync_at)}${c.last_summary ? " · " + escapeHtml(c.last_summary) : ""}`
            }</div>
          </div>
          <div></div>
          <div class="int-controls">
            <select data-act="direction" aria-label="Направление синхронизации"${c.remote_read_only ? " disabled" : ""}>${optionsHtml(intState.directions, c.direction)}</select>
            <select data-act="color" aria-label="Цвет событий">${optionsHtml(intState.colors, c.color)}</select>
            <label class="check"><input type="checkbox" data-act="enabled"${c.enabled ? " checked" : ""}> Синхронизировать</label>
            <span class="spacer"></span>
            ${c.last_error && c.provider !== "google" ? `<button type="button" class="btn small" data-act="password">Новый пароль</button>` : ""}
            <button type="button" class="btn small" data-act="sync">Синхронизировать</button>
            <button type="button" class="btn small danger" data-act="remove">Отключить</button>
          </div>
        </li>`
      )
      .join("");
  }

  async function loadIntegrations() {
    const data = await api(INT_API);
    intState.providers = data.providers;
    intState.directions = data.directions;
    intState.colors = data.colors;
    intState.googlePending = data.google_pending;
    intProvider.innerHTML = optionsHtml(data.providers, intProvider.value || "yandex");
    $("#int-direction").innerHTML = optionsHtml(data.directions, "both");
    $("#int-color").innerHTML = optionsHtml(data.colors, "gray");
    renderIntList(data.calendars);
    return data;
  }

  async function openIntegrations(opts = {}) {
    try {
      await loadIntegrations();
    } catch (err) {
      toast(err.message, true);
      return;
    }
    intForm.hidden = true;
    if (!intDialog.open) intDialog.showModal();
    if (opts.provider) showIntForm(opts.provider);
  }

  function syncFormToProvider() {
    const p = intState.providers.find((x) => x.code === intProvider.value);
    if (!p) return;
    $("#int-help").textContent = p.help;
    const isGoogle = p.auth === "oauth";
    $("#int-google").hidden = !isGoogle;
    $("#int-caldav").hidden = isGoogle;
    $("#int-google-btn").hidden = !p.available || (isGoogle && intState.googlePending);
    $("#int-google-na").hidden = p.available;
    if (!isGoogle) $("#int-server").value = p.server_url;
    // для Exchange — доменный пароль, для Яндекса/Mail.ru — пароль приложения
    $("#int-password").previousElementSibling.textContent = p.code === "exchange" ? "Пароль (доменный)" : "Пароль приложения";
    $("#int-username").placeholder = p.code === "exchange" ? "DOMAIN\\пользователь или адрес почты" : "";
    $("#int-found").hidden = true;
    $("#int-connect").disabled = true;
    intError.textContent = "";
    // уже вошли через Google — сразу показываем его календари
    if (isGoogle && intState.googlePending) discoverCalendars();
  }

  function showIntForm(provider) {
    intForm.hidden = false;
    intProvider.value = provider || intProvider.value || "yandex";
    $("#int-username").value = "";
    $("#int-password").value = "";
    syncFormToProvider();
    intForm.scrollIntoView({ block: "nearest" });
    (intProvider.value === "google" ? intProvider : $("#int-username")).focus();
  }

  function caldavPayload() {
    return {
      provider: intProvider.value,
      server_url: $("#int-server").value.trim(),
      username: $("#int-username").value.trim(),
      password: $("#int-password").value,
    };
  }

  async function discoverCalendars() {
    intError.textContent = "";
    const btn = $("#int-discover");
    btn.classList.add("busy");
    btn.textContent = "Ищу календари…";
    try {
      const body = intProvider.value === "google" ? { provider: "google" } : caldavPayload();
      const data = await api(`${INT_API}discover/`, { method: "POST", body });
      intState.found = data.calendars;
      const list = $("#int-cal-list");
      list.innerHTML = data.calendars
        .map(
          (c, i) => `<li><label class="${c.connected ? "disabled" : ""}">
            <input type="radio" name="int-cal" value="${i}"${c.connected ? " disabled" : ""}>
            <span class="swatch-dot" style="${c.color ? `background:${escapeHtml(c.color)}` : ""}"></span>
            ${escapeHtml(c.name)}
            <span class="meta">${c.connected ? "уже подключён" : c.read_only ? "только чтение" : ""}</span>
          </label></li>`
        )
        .join("");
      const first = list.querySelector("input:not(:disabled)");
      if (first) first.checked = true;
      $("#int-found").hidden = false;
      $("#int-connect").disabled = !first;
      if (!first) intError.textContent = "Все найденные календари уже подключены.";
    } catch (err) {
      intError.textContent = err.message;
    } finally {
      btn.classList.remove("busy");
      btn.textContent = "Найти календари";
    }
  }

  intProvider.addEventListener("change", syncFormToProvider);
  $("#int-discover").addEventListener("click", discoverCalendars);
  $("#int-add").addEventListener("click", () => showIntForm());
  $("#int-cancel").addEventListener("click", () => (intForm.hidden = true));
  $("#int-close").addEventListener("click", () => intDialog.close());
  $("#int-cal-list").addEventListener("change", () => {
    const chosen = intState.found[Number(($("#int-cal-list input:checked") || {}).value)];
    if (chosen && chosen.read_only) $("#int-direction").value = "import"; // в календарь только для чтения не записать
  });

  intForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const picked = $("#int-cal-list input:checked");
    if (!picked) return;
    const cal = intState.found[Number(picked.value)];
    const body = {
      ...(intProvider.value === "google" ? { provider: "google" } : caldavPayload()),
      calendar_id: cal.id,
      calendar_name: cal.name,
      read_only: cal.read_only,
      direction: $("#int-direction").value,
      color: $("#int-color").value,
    };
    const btn = $("#int-connect");
    btn.disabled = true;
    btn.textContent = "Подключаю и синхронизирую…";
    intError.textContent = "";
    try {
      const res = await api(INT_API, { method: "POST", body });
      intForm.hidden = true;
      toast(`Подключено: ${res.calendar.calendar_name}. ${res.sync.summary}`, res.sync.errors.length > 0);
      await loadIntegrations();
      load();
    } catch (err) {
      intError.textContent = err.message;
      btn.disabled = false;
    } finally {
      btn.textContent = "Подключить";
    }
  });

  intList.addEventListener("change", async (e) => {
    const el = e.target.closest("[data-act]");
    const item = e.target.closest(".int-item");
    if (!el || !item) return;
    const field = el.dataset.act;
    const value = field === "enabled" ? el.checked : el.value;
    try {
      await api(`${INT_API}${item.dataset.id}/`, { method: "PATCH", body: { [field]: value } });
      await loadIntegrations();
    } catch (err) {
      toast(err.message, true);
      await loadIntegrations();
    }
  });

  intList.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    const item = e.target.closest(".int-item");
    if (!btn || !item) return;
    const id = item.dataset.id;
    const title = item.querySelector(".int-title").textContent;
    if (btn.dataset.act === "sync") {
      btn.classList.add("busy");
      btn.textContent = "Синхронизирую…";
      try {
        const res = await api(`${INT_API}${id}/sync/`, { method: "POST", body: {} });
        toast(`${title}: ${res.sync.summary}`, res.sync.errors.length > 0);
        load();
      } catch (err) {
        toast(err.message, true);
      }
      await loadIntegrations();
    } else if (btn.dataset.act === "password") {
      const password = prompt(`Новый пароль приложения для «${title}»:`);
      if (!password) return;
      try {
        await api(`${INT_API}${id}/`, { method: "PATCH", body: { password } });
        const res = await api(`${INT_API}${id}/sync/`, { method: "POST", body: {} });
        toast(`${title}: ${res.sync.summary}`, res.sync.errors.length > 0);
        load();
      } catch (err) {
        toast(err.message, true);
      }
      await loadIntegrations();
    } else if (btn.dataset.act === "remove") {
      if (!confirm(`Отключить календарь «${title}»? Синхронизация прекратится.`)) return;
      const removeEvents = confirm("Удалить из нашего календаря события, которые были импортированы из него?\n\nОК — удалить, Отмена — оставить.");
      try {
        await api(`${INT_API}${id}/${removeEvents ? "?remove_events=1" : ""}`, { method: "DELETE" });
        toast("Календарь отключён");
        await loadIntegrations();
        if (removeEvents) load();
      } catch (err) {
        toast(err.message, true);
      }
    }
  });

  $("#int-sync-all").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.classList.add("busy");
    btn.textContent = "Синхронизирую…";
    try {
      const res = await api(`${INT_API}sync/`, { method: "POST", body: {} });
      const failed = res.results.filter((r) => r.sync.errors.length).length;
      toast(res.results.length ? `Синхронизировано календарей: ${res.results.length}${failed ? `, с ошибками: ${failed}` : ""}` : "Нет подключённых календарей", failed > 0);
      load();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.classList.remove("busy");
      btn.textContent = "Синхронизировать всё";
      loadIntegrations().catch(() => {});
    }
  });

  const intBtn = $("#integrations-btn");
  if (intBtn) intBtn.addEventListener("click", () => openIntegrations());

  // возврат со страницы согласия Google: ?integrations=google | google-denied | google-error
  (() => {
    const params = new URLSearchParams(location.search);
    const flag = params.get("integrations");
    if (!flag) return;
    params.delete("integrations");
    history.replaceState(null, "", location.pathname + (params.toString() ? `?${params}` : ""));
    if (flag === "google") openIntegrations({ provider: "google" });
    else if (flag === "google-denied") toast("Доступ к Google Календарю не предоставлен.", true);
    else toast("Не удалось подключить Google Календарь. Попробуйте ещё раз.", true);
  })();

  // ---------- адресная книга (Active Directory / LDAP) — доступна всем; поиск идёт на сервере каталога ----------
  const CONTACTS_API = "/api/contacts/";
  const cDialog = $("#contacts-dialog");
  const cSearch = $("#contacts-search");
  const cList = $("#contacts-list");
  const cMeta = $("#contacts-meta");
  const cCard = $("#contact-card");
  const cs = { items: [], active: -1, query: "", seq: 0, timer: null, minQuery: 2, configured: true };

  const contactSub = (c) => [c.title, c.department].filter(Boolean).join(" · ") || c.company || "";
  const telHref = (p) => "tel:" + String(p).replace(/[^\d+]/g, "");
  // цвет аватара по идентификатору (у контактов каталога он строковый)
  const colorFor = (id) => {
    let h = 0;
    for (const ch of String(id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return AVATAR_COLORS[h % AVATAR_COLORS.length];
  };

  function renderContactsMeta(data, loading = false) {
    cs.minQuery = data.min_query || 2;
    cs.configured = data.configured;
    $("#contacts-admin").hidden = !data.can_manage;
    if (!data.configured) {
      cMeta.textContent = data.can_manage
        ? "Адресная книга не подключена. Добавьте каталог Active Directory или LDAP в настройках."
        : "Адресная книга не подключена. Обратитесь к администратору.";
      return;
    }
    const names = data.sources.filter((s) => s.enabled).map((s) => s.name).join(", ");
    const parts = [];
    if (loading) parts.push(`Поиск в каталоге ${names}…`);
    else if (cs.query.length >= cs.minQuery) parts.push(`Найдено: ${data.total}`);
    else parts.push(`Каталог: ${names}. Введите от ${cs.minQuery} символов — поиск выполняется на сервере каталога.`);
    for (const e of data.errors || []) parts.push(`⚠ ${e.source}: ${e.message}`);
    cMeta.textContent = parts.join(" · ");
  }

  function setContactActive(i) {
    cs.active = i;
    cList.querySelectorAll("li[role=option]").forEach((el, j) => {
      el.classList.toggle("active", j === i);
      el.setAttribute("aria-selected", String(j === i));
    });
    const act = cList.querySelector(`#contact-opt-${i}`);
    if (act) {
      cSearch.setAttribute("aria-activedescendant", act.id);
      act.scrollIntoView({ block: "nearest" });
    } else {
      cSearch.removeAttribute("aria-activedescendant");
    }
  }

  function renderContacts() {
    const q = cs.query;
    if (q.length < cs.minQuery) {
      cList.innerHTML = "";
      setContactActive(-1);
      return;
    }
    cList.innerHTML = cs.items.length
      ? cs.items
          .map(
            (c, i) => `<li role="option" id="contact-opt-${i}" data-i="${i}" aria-selected="false">
              <span class="avatar c-${colorFor(c.id)}">${escapeHtml(initials(c.name))}</span>
              <span class="c-main">
                <div class="c-name">${highlight(c.name, q)}${c.user_id ? ' <span class="badge-cal">в календаре</span>' : ""}</div>
                <div class="c-sub">${escapeHtml(contactSub(c))}</div>
              </span>
              <span class="c-side">${c.email ? highlight(c.email, q) : ""}${c.phone ? `<br>${escapeHtml(c.phone)}` : ""}</span>
            </li>`
          )
          .join("")
      : `<li class="empty">Никого не найдено</li>`;
    setContactActive(cs.items.length ? 0 : -1);
  }

  async function loadContacts() {
    const seq = ++cs.seq;
    const q = cSearch.value.trim();
    cs.query = q;
    try {
      if (q.length < cs.minQuery) {
        // без запроса — только сведения о каталогах; на сервер каталога не обращаемся
        const data = await api(CONTACTS_API);
        if (seq !== cs.seq) return;
        cs.items = [];
        renderContactsMeta(data);
        renderContacts();
        return;
      }
      cMeta.textContent = "Поиск в каталоге…";
      const data = await api(`${CONTACTS_API}?q=${encodeURIComponent(q)}&limit=50`);
      if (seq !== cs.seq) return; // устаревший ответ
      cs.items = data.results;
      renderContactsMeta(data);
      renderContacts();
    } catch (err) {
      if (seq === cs.seq) cMeta.textContent = err.message;
    }
  }

  function showCard(c) {
    if (!c) return;
    const rows = [
      ["E-mail", c.email && `<a href="mailto:${escapeHtml(c.email)}">${escapeHtml(c.email)}</a>`],
      ["Телефон", c.phone && `<a href="${escapeHtml(telHref(c.phone))}">${escapeHtml(c.phone)}</a>`],
      ["Мобильный", c.mobile && `<a href="${escapeHtml(telHref(c.mobile))}">${escapeHtml(c.mobile)}</a>`],
      ["Должность", c.title && escapeHtml(c.title)],
      ["Подразделение", c.department && escapeHtml(c.department)],
      ["Организация", c.company && escapeHtml(c.company)],
      ["Кабинет", c.office && escapeHtml(c.office)],
      ["Каталог", c.source && escapeHtml(c.source)],
    ].filter((r) => r[1]);
    cCard.innerHTML = `
      <h3>${escapeHtml(c.name)} ${c.user_id ? '<span class="badge-cal">в календаре</span>' : ""}</h3>
      ${contactSub(c) ? `<div class="cc-sub">${escapeHtml(contactSub(c))}</div>` : ""}
      <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      <div class="cc-actions">
        ${ME.can_create && (c.email || c.user_id) ? `<button type="button" class="btn small primary" data-cc="invite">Пригласить на событие</button>` : ""}
        ${c.email ? `<a class="btn small" href="mailto:${escapeHtml(c.email)}">Написать письмо</a>` : ""}
        ${c.email ? `<button type="button" class="btn small" data-cc="copy">Скопировать e-mail</button>` : ""}
        <button type="button" class="btn small" data-cc="close">Скрыть</button>
      </div>`;
    cCard.dataset.i = String(cs.items.indexOf(c));
    cCard.hidden = false;
    cCard.scrollIntoView({ block: "nearest" });
  }

  // Добавить контакт в событие: пользователь календаря → участник, иначе e-mail → гости
  function addContactToEvent(c) {
    const email = c.email || (c.user_id && usersById.has(c.user_id) ? usersById.get(c.user_id).email : "");
    if (!email) return "";
    addParticipant({ email, name: c.name, dir: true });
    return "участник";
  }

  cCard.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-cc]");
    if (!btn) return;
    const c = cs.items[Number(cCard.dataset.i)];
    if (btn.dataset.cc === "close") {
      cCard.hidden = true;
      cSearch.focus();
    } else if (btn.dataset.cc === "copy" && c) {
      try {
        await navigator.clipboard.writeText(c.email);
        toast("E-mail скопирован");
      } catch (_) {
        toast(c.email);
      }
    } else if (btn.dataset.cc === "invite" && c) {
      cDialog.close();
      openNew();
      if (dialog.open) {
        const as = addContactToEvent(c);
        toast(`${c.name} добавлен(а) в событие как ${as}`);
      }
    }
  });

  cList.addEventListener("mousedown", (e) => e.preventDefault()); // фокус остаётся в поиске
  cList.addEventListener("mousemove", (e) => {
    const li = e.target.closest("li[role=option]");
    if (li && Number(li.dataset.i) !== cs.active) setContactActive(Number(li.dataset.i));
  });
  cList.addEventListener("click", (e) => {
    const li = e.target.closest("li[role=option]");
    if (li) showCard(cs.items[Number(li.dataset.i)]);
  });

  cSearch.addEventListener("input", () => {
    clearTimeout(cs.timer);
    cCard.hidden = true;
    cs.timer = setTimeout(loadContacts, 300); // пауза в наборе — только тогда запрос на сервер каталога
  });
  cSearch.addEventListener("keydown", (e) => {
    const n = cs.items.length;
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && n) {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setContactActive(cs.active < 0 ? 0 : (cs.active + step + n) % n);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (cs.timer && cs.query !== cSearch.value.trim()) {
        clearTimeout(cs.timer);
        loadContacts(); // Enter до паузы — ищем сразу
      } else if (cs.active >= 0) {
        showCard(cs.items[cs.active]);
      }
    } else if (e.key === "Escape" && !cCard.hidden) {
      e.preventDefault(); // сначала закрываем карточку, а не окно
      cCard.hidden = true;
    }
  });
  cDialog.addEventListener("cancel", (e) => {
    if (!cCard.hidden) {
      e.preventDefault();
      cCard.hidden = true;
      cSearch.focus();
    }
  });
  $("#contacts-close").addEventListener("click", () => cDialog.close());

  async function openContacts() {
    cCard.hidden = true;
    cSearch.value = "";
    cs.items = [];
    if (!cDialog.open) cDialog.showModal();
    cSearch.focus();
    await loadContacts();
  }
  $("#contacts-btn").addEventListener("click", openContacts);

  // ---------- подсказки из адресной книги в поле «Гости» ----------
  const partInput = $("#participant-input");
  const partChips = $("#participant-chips");
  const partSuggest = $("#participant-suggest");
  const ps = { items: [], active: 0, seq: 0, timer: null };
  const EMAIL_RE = /^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/;

  function syncParticipantsField() {
    f.guest_emails.value = parts.list.map((p) => p.email).join(", ");
    const n = parts.list.filter((p) => p.dir).length;
    const note = $("#dir-guests-note");
    note.hidden = !n;
    note.textContent = n
      ? `Из адресной книги: ${n} — событие автоматически появится в их календаре и будет обновляться при изменениях.`
      : "";
  }

  function renderParticipants() {
    partChips.innerHTML = parts.list
      .map((p) => {
        const label = escapeHtml(p.name || p.email);
        const title = escapeHtml(p.name ? `${p.name} <${p.email}>` : p.email);
        return `<li class="${p.dir ? "dir" : "manual"}" title="${title}${p.dir ? " — из адресной книги" : ""}">
          <span>${label}</span><button type="button" data-remove="${escapeHtml(p.email)}" aria-label="Убрать: ${label}">×</button></li>`;
      })
      .join("");
    partInput.placeholder = parts.list.length ? "Добавить ещё…" : "Фамилия, имя или e-mail из адресной книги";
    syncParticipantsField();
  }

  function addParticipant({ email, name = "", dir = false }) {
    email = (email || "").trim();
    if (!email) return false;
    const key = email.toLowerCase();
    const known = parts.list.find((p) => p.email.toLowerCase() === key);
    if (known) {
      known.dir = known.dir || dir;
      known.name = known.name || name;
    } else {
      const user = usersByEmail.get(key);
      parts.list.push({ email, name: name || (user ? user.name : ""), dir });
    }
    renderParticipants();
    return true;
  }

  function removeParticipant(email) {
    parts.list = parts.list.filter((p) => p.email.toLowerCase() !== email.toLowerCase());
    renderParticipants();
  }

  // Участники события при открытии: гости и приглашённые сотрудники — одним списком
  function setParticipants(ev) {
    const names = ev.participant_names || {};
    const dir = new Set((ev.directory_guests || []).map((e) => e.toLowerCase()));
    parts.list = [];
    const seen = new Set();
    const push = (email, name) => {
      const key = (email || "").toLowerCase();
      if (!key || seen.has(key)) return;
      seen.add(key);
      parts.list.push({ email, name: name || names[key] || (usersByEmail.get(key) || {}).name || "", dir: dir.has(key) });
    };
    for (const u of ev.invitees || []) push(u.email, u.name);
    for (const e of ev.guest_emails || []) push(e);
    partInput.value = "";
    closeParticipantSuggest();
    renderParticipants();
  }

  function closeParticipantSuggest() {
    partSuggest.hidden = true;
    partInput.setAttribute("aria-expanded", "false");
    partInput.removeAttribute("aria-activedescendant");
  }

  function renderParticipantSuggest(q) {
    if (!ps.items.length) {
      const typed = partInput.value.trim();
      partSuggest.innerHTML = EMAIL_RE.test(typed)
        ? `<li class="empty">В адресной книге не найдено. Enter — добавить ${escapeHtml(typed)} как внешнего участника</li>`
        : `<li class="empty">${ps.searching ? "Поиск в адресной книге…" : "Никого не найдено"}</li>`;
    } else {
      partSuggest.innerHTML = ps.items
        .map((c, i) => {
          const chosen = parts.list.some((p) => p.email.toLowerCase() === (c.email || "").toLowerCase());
          return `<li role="option" id="part-opt-${i}" data-i="${i}" aria-selected="${chosen}" class="${i === ps.active ? "active" : ""}">
            <span class="check-mark">${chosen ? "✓" : ""}</span>
            <span class="avatar c-${colorFor(c.id || c.email)}">${escapeHtml(initials(c.name || c.email))}</span>
            <span><div>${highlight(c.name || c.email, q)}</div><div class="email">${highlight(c.email, q)}${
              contactSub(c) ? " · " + escapeHtml(contactSub(c)) : ""
            }</div></span>
          </li>`;
        })
        .join("");
    }
    partSuggest.hidden = false;
    partInput.setAttribute("aria-expanded", "true");
    const act = partSuggest.querySelector("li.active");
    if (act) {
      partInput.setAttribute("aria-activedescendant", act.id);
      act.scrollIntoView({ block: "nearest" });
    } else {
      partInput.removeAttribute("aria-activedescendant");
    }
  }

  async function runParticipantSearch() {
    const q = partInput.value.trim();
    if (q.length < 2) {
      ps.items = [];
      return closeParticipantSuggest();
    }
    const seq = ++ps.seq;
    ps.searching = true;
    renderParticipantSuggest(q);
    let items = [];
    try {
      const data = await api(`${CONTACTS_API}?q=${encodeURIComponent(q)}&limit=10`);
      items = (data.results || []).filter((c) => c.email);
    } catch (_) {
      /* адресная книга недоступна — ищем среди пользователей календаря */
    }
    if (seq !== ps.seq) return;
    if (!items.length) {
      const ql = q.toLowerCase();
      items = USERS.filter((u) => u.email && (u.name.toLowerCase().includes(ql) || u.email.toLowerCase().includes(ql)))
        .slice(0, 10)
        .map((u) => ({ id: `u${u.id}`, name: u.name, email: u.email, local: true }));
    }
    ps.searching = false;
    ps.items = items;
    ps.active = 0;
    renderParticipantSuggest(q);
  }

  function pickParticipant(c) {
    // из адресной книги — событие автоматически попадёт в календарь участника
    addParticipant({ email: c.email, name: c.name, dir: !c.local });
    partInput.value = "";
    ps.items = [];
    closeParticipantSuggest();
    partInput.focus();
  }

  partInput.addEventListener("input", () => {
    clearTimeout(ps.timer);
    ps.timer = setTimeout(runParticipantSearch, 300); // поиск идёт на сервере каталога — ждём паузу в наборе
  });
  partInput.addEventListener("blur", () => setTimeout(closeParticipantSuggest, 120));
  partInput.addEventListener("keydown", (e) => {
    const open = !partSuggest.hidden && ps.items.length;
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && open) {
      e.preventDefault();
      const n = ps.items.length;
      ps.active = (ps.active + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      renderParticipantSuggest(partInput.value.trim());
    } else if (e.key === "Enter" || (e.key === "Tab" && open)) {
      const typed = partInput.value.trim().replace(/[,;]$/, "");
      if (open) {
        e.preventDefault();
        pickParticipant(ps.items[ps.active]);
      } else if (e.key === "Enter") {
        e.preventDefault(); // не отправлять форму
        if (EMAIL_RE.test(typed)) {
          addParticipant({ email: typed }); // внешний адрес, которого нет в адресной книге
          partInput.value = "";
          closeParticipantSuggest();
        }
      }
    } else if (e.key === "Escape" && !partSuggest.hidden) {
      e.preventDefault(); // закрыть подсказки, а не окно события
      e.stopPropagation();
      closeParticipantSuggest();
    } else if (e.key === "Backspace" && !partInput.value && parts.list.length) {
      parts.list.pop();
      renderParticipants();
    }
  });
  partSuggest.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const li = e.target.closest("li[data-i]");
    if (li) pickParticipant(ps.items[Number(li.dataset.i)]);
  });
  partChips.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-remove]");
    if (!btn) return;
    e.stopPropagation();
    removeParticipant(btn.dataset.remove);
  });
  $("#participants .ms-box").addEventListener("click", () => partInput.focus());

  // ---------- напоминания о событиях ----------
  // Сервер отдаёт напоминания, которые пора показать; страница проверяет раз в минуту.
  // Показываем всплывающее уведомление (если браузер разрешил) и сообщение внизу экрана.
  const REMINDERS_API = "/api/reminders/";
  const reminderTimers = new Map();
  const shownKey = `calendar.reminders.shown.${ME.id || 0}`;
  const shownReminders = (() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(shownKey) || "[]"));
    } catch (_) {
      return new Set();
    }
  })();
  function rememberShown(key) {
    shownReminders.add(key);
    try {
      localStorage.setItem(shownKey, JSON.stringify([...shownReminders].slice(-200)));
    } catch (_) {
      /* хранилище недоступно — в этой вкладке всё равно не повторим */
    }
  }
  function reminderText(r) {
    const when = r.all_day ? `${r.start.slice(8, 10)}.${r.start.slice(5, 7)}, весь день` : `в ${r.start.slice(11, 16)}`;
    const left = Math.round((parseDT(r.start) - Date.now()) / 60000);
    const soon = r.all_day ? "" : left > 0 ? ` (через ${left < 60 ? left + " мин" : Math.round(left / 60) + " ч"})` : " (уже началось)";
    return `${r.title} — ${when}${soon}${r.location ? ", " + r.location : ""}`;
  }
  function showReminder(r) {
    const key = `${r.id}@${r.remind_at}`; // при переносе события напомним снова
    if (shownReminders.has(key)) return;
    rememberShown(key);
    const text = reminderText(r);
    toast(`🔔 Напоминание: ${text}`, false, 15000);
    if ("Notification" in window && Notification.permission === "granted") {
      try {
        const n = new Notification("Напоминание о событии", { body: text, tag: key });
        n.onclick = () => {
          window.focus();
          n.close();
        };
      } catch (_) {
        /* уведомления недоступны — хватит сообщения на странице */
      }
    }
  }
  async function checkReminders() {
    let data;
    try {
      data = await api(REMINDERS_API);
    } catch (_) {
      return;
    }
    for (const r of data.reminders || []) {
      const key = `${r.id}@${r.remind_at}`;
      if (shownReminders.has(key) || reminderTimers.has(key)) continue;
      const wait = new Date(r.remind_at).getTime() - Date.now();
      if (wait <= 0) showReminder(r);
      else
        reminderTimers.set(
          key,
          setTimeout(() => {
            reminderTimers.delete(key);
            showReminder(r);
          }, wait)
        );
    }
  }
  function askNotificationPermission() {
    // спрашиваем только после действия пользователя (сохранение события с напоминанием)
    if ("Notification" in window && Notification.permission === "default") {
      try {
        Notification.requestPermission();
      } catch (_) {
        /* старые браузеры */
      }
    }
  }
  checkReminders();
  setInterval(checkReminders, 60000);

  // ---------- Дополнительные настройки (шестерёнка) ----------
  const settingsDialog = $("#settings-dialog");
  const settingsForm = $("#settings-form");
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  }
  $("#settings-btn").addEventListener("click", () => {
    const theme = MY_SETTINGS.theme;
    const startView = MY_SETTINGS.start_view;
    settingsForm.querySelector(`input[name="theme"][value="${theme}"]`).checked = true;
    (settingsForm.querySelector(`input[name="start_view"][value="${startView}"]`) ||
      settingsForm.querySelector('input[name="start_view"][value="last"]')).checked = true;
    fillReportPeriod();
    settingsDialog.showModal();
  });
  settingsForm.addEventListener("change", (e) => {
    if (e.target.name === "theme") {
      applyTheme(e.target.value);
      saveMySettings({ theme: e.target.value });
    } else if (e.target.name === "start_view") {
      saveMySettings({ start_view: e.target.value });
    }
  });
  settingsDialog.addEventListener("click", (e) => {
    if (e.target === settingsDialog) settingsDialog.close();
  });

  // ---------- Отчёт по событиям (.docx) за выбранный период ----------
  const reportStart = $("#report-start");
  const reportEnd = $("#report-end");
  const reportBtn = $("#report-btn");
  const reportError = $("#report-error");
  function showReportError(text) {
    reportError.textContent = text;
    reportError.hidden = !text;
  }
  // по умолчанию — месяц, открытый в календаре
  function fillReportPeriod() {
    const m = state.month;
    reportStart.value = ymd(new Date(m.getFullYear(), m.getMonth(), 1));
    reportEnd.value = ymd(new Date(m.getFullYear(), m.getMonth() + 1, 0));
    showReportError("");
  }
  // Файл скачивает сам браузер: кнопка отправляет форму на /api/events/report/ (GET). Здесь — только проверка периода.
  reportBtn.addEventListener("click", (e) => {
    const start = reportStart.value;
    const end = reportEnd.value;
    let problem = "";
    if (!start || !end) problem = "Укажите начало и окончание периода.";
    else if (end < start) problem = "Окончание периода не может быть раньше начала.";
    showReportError(problem);
    if (problem) e.preventDefault();
  });
  // «Доступ» и «Интеграции» открываются из настроек: сначала закрываем окно настроек,
  // затем срабатывает обработчик самой кнопки (фаза перехвата идёт раньше)
  settingsDialog.addEventListener(
    "click",
    (e) => {
      if (e.target.closest("#access-btn, #integrations-btn")) settingsDialog.close();
    },
    true
  );

  // ---------- toast ----------
  let toastTimer = null;
  function toast(msg, isError = false, ms = 0) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("error", isError);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), ms || (isError ? 4500 : 2200));
  }

  // Итог доставки события участникам из адресной книги (поле delivery в ответе на сохранение)
  function describeDelivery(d) {
    if (!d) return { text: "", error: false };
    const list = (a) => a.join(", ");
    const parts = [];
    const how = d.channel === "exchange" ? " (через Exchange)" : " (письмом)";
    if (d.invited && d.invited.length) parts.push(`Приглашение отправлено${how}: ${list(d.invited)}.`);
    if (d.updated && d.updated.length) parts.push(`Обновление отправлено: ${list(d.updated)}.`);
    if (d.cancelled && d.cancelled.length) parts.push(`Отмена отправлена: ${list(d.cancelled)}.`);
    (d.notes || []).forEach((n) => parts.push(n + "."));
    const failed = Object.entries(d.failed || {});
    if (failed.length) {
      const byReason = {};
      failed.forEach(([email, reason]) => (byReason[reason] = byReason[reason] || []).push(email));
      Object.entries(byReason).forEach(([reason, emails]) =>
        parts.push(`Не удалось отправить ${list(emails)}: ${reason}.`)
      );
    }
    const sentSomething = parts.length > failed.length;
    if (d.errors && d.errors.length && !failed.length) parts.push(`Exchange: ${d.errors[0]}.`);
    return { text: parts.join(" "), error: failed.length > 0 || (!sentSomething && (d.errors || []).length > 0) };
  }

  // Раз в минуту двигаем линию «сейчас», после полуночи обновляем подсветку «сегодня»
  let lastToday = todayStr();
  setInterval(() => {
    if (todayStr() !== lastToday) {
      lastToday = todayStr();
      render();
      return;
    }
    const top = `${(nowMinutes() * HOUR_PX) / 60}px`;
    timeView.querySelectorAll(".tg-now").forEach((el) => (el.style.top = top));
  }, 60000);

  refresh();
})();
