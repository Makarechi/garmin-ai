"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const names = {
    heart_rate_bpm: "Пульс",
    stress_score: "Стресс",
    body_battery: "Body Battery",
    respiration_rpm: "Дыхание",
    spo2_pct: "SpO₂",
    hrv_nightly_avg: "HRV за ночь",
    sleep_score: "Сон",
    training_readiness_score: "Готовность к тренировке",
    caffeine: "Кофе",
    medication: "Лекарство",
    migraine: "Мигрень",
    wellbeing_observation: "Самочувствие",
    activity_effort: "Усилие после активности",
    note: "Заметка",
    alcohol: "Алкоголь",
    symptom_observation: "Симптом",
    headache_observation: "Наблюдение головной боли",
    meal: "Еда",
    hydration: "Вода",
    illness: "Болезнь",
    nap: "Дневной сон",
    stressor: "Стрессовый фактор",
    travel: "Поездка",
    mood: "Настроение",
    context: "Контекст",
    caffeine_absence: "Отсутствие кофеина",
    caffeine_log_complete: "Учёт кофеина завершён",
  };
  const reasons = {
    recent_observations: ["Актуально", "Последние измерения доступны", "good"],
    recent_daily_summary: [
      "Дневная сводка",
      "Сводка за календарную дату; не текущее состояние",
      "good",
    ],
    partial: ["Есть пробелы", "Неполное покрытие измерениями", "gap"],
    stale_observation: ["Данные устарели", "Нет свежих наблюдений", "gap"],
    source_empty: ["Пустой ответ", "Источник не вернул новых данных", "gap"],
    parser_error: [
      "Ошибка обработки",
      "Последний ответ не удалось обработать",
      "gap",
    ],
    fetch_error: ["Ошибка получения", "Последняя загрузка не удалась", "gap"],
    not_synced: ["Нет наблюдений", "Синхронизация ещё не дала данных", ""],
    unknown: ["Неизвестно", "Недостаточно данных для оценки свежести", ""],
  };
  const sources = {
    telegram_text: "Текст",
    telegram_voice: "Голос",
    telegram_button: "Кнопка",
    manual: "Вручную",
    wearable: "Устройство",
    inferred: "Предположение",
  };
  const statuses = {
    confirmed: "подтверждено",
    needs_confirmation: "требует подтверждения",
    inferred: "предположение",
  };
  let token = "",
    generation = 0,
    controller,
    demo = true,
    canWriteDiary = false,
    exporting = false,
    trackerPreview,
    trackerDraftVersion = 0,
    currentForm,
    builderLocale = "ru";
  const today = new Date().toISOString().slice(0, 10);
  const samples = [
    {
      kind: "caffeine",
      start: `${today}T09:00:00Z`,
      source: "manual",
      status: "confirmed",
      payload: {
        beverage: "Чёрный кофе",
        caffeine_mg_estimate: 60,
        dose_basis: "total",
        dose_provenance: "estimated",
      },
    },
    {
      kind: "wellbeing_observation",
      start: `${today}T08:15:00Z`,
      source: "manual",
      status: "confirmed",
      payload: { notes: "Небольшая усталость", energy: 6 },
    },
    {
      kind: "note",
      start: `${today}T07:30:00Z`,
      source: "manual",
      status: "confirmed",
      payload: { description: "Прогулка перед завтраком" },
    },
  ];
  const demoStarter = {
    key: "focus_session",
    name: "Концентрация",
    topology: "point",
    fields: [
      { key: "focus", label: "Концентрация", kind: "scale", required: true, minimum: 1, maximum: 5 },
      { key: "note", label: "Заметка", kind: "text", required: false },
    ],
  };
  function newDemoState() {
    return {
      trackers: [structuredClone(demoStarter)],
      events: [
        ...structuredClone(samples),
        { id: "demo-focus-1", kind: "user.focus_session", start: `${today}T10:00:00Z`,
          source: "manual", status: "confirmed", topology: "point",
          payload: { focus: 2, note: "После прогулки" } },
        { id: "demo-focus-2", kind: "user.focus_session", start: `${today}T14:00:00Z`,
          source: "manual", status: "confirmed", topology: "point",
          payload: { focus: 4, note: "После перерыва" } },
      ],
      nextId: 3,
      preview: null,
      operations: new Map(),
    };
  }
  let demoState = newDemoState();
  function demoActions() {
    return demoState.trackers.map((tracker) => ({
      id: `create:user.${tracker.key}`,
      definition_key: `user.${tracker.key}`,
      label: tracker.shortcut || tracker.name,
    }));
  }
  function demoFormField(field) {
    return {
      name: field.key,
      label: field.label,
      input: field.kind === "scale" ? "integer" : field.kind,
      required: field.required,
      unit: demoUnit(field),
      minimum: field.minimum ?? null,
      maximum: field.maximum ?? null,
      max_length: field.kind === "text" ? field.max_length || 500 : null,
      options: field.options || [],
    };
  }
  function demoUnit(field) {
    if (field.kind === "scale") return `score_${field.minimum}-${field.maximum}`;
    if (field.kind === "integer") return field.unit || "count";
    return field.unit || null;
  }
  function demoForm(action) {
    const event = action.event_id
      ? demoState.events.find((item) => item.id === action.event_id)
      : null;
    const tracker = demoState.trackers.find((item) => `user.${item.key}` === action.definition_key);
    if (!tracker || (action.event_id && !event)) throw Error("Демо-запись не найдена.");
    return {
      id: action.id, action, title: tracker.name, topology: tracker.topology,
      schema_hash: `demo:${tracker.key}`, fields: tracker.fields.map(demoFormField),
      initial_values: event ? { ...event.payload } : {},
      initial_timezone: "UTC", initial_start: event?.start || new Date().toISOString(),
      initial_end: event?.end || null,
    };
  }
  function demoOverlaps(event, interval) {
    const start = Date.parse(event.start);
    const left = Date.parse(interval.start);
    const right = Date.parse(interval.end);
    if (!(start < right)) return false;
    const end = event.end == null ? null : Date.parse(event.end);
    if (end !== null && end > start) return end > left;
    if (end === null && event.topology === "open_interval") return true;
    return (event.topology === undefined || ["point", "flexible"].includes(event.topology)) &&
      start >= left;
  }
  function demoRequest(url, body) {
    if (url === "/tracker-profile") return { locale: "ru", timezone: "UTC" };
    if (url === "/tracker-setups/preview" && body) {
      if (demoState.trackers.some((item) => item.key === body.key))
        throw Error("Трекер с таким ключом уже есть в демо.");
      if (!body.name.trim() || body.fields.some((field) => !field.label.trim()))
        throw Error("Названия трекера и полей не могут состоять из пробелов.");
      if (!body.fields.length || body.fields.length > 32 ||
          new Set(body.fields.map((item) => item.key)).size !== body.fields.length)
        throw Error("Добавьте поля с разными ключами.");
      if (body.fields.some((field) => `user.${body.key}.${field.key}`.length > 128))
        throw Error("Сократите ключ трекера или поля: общий идентификатор слишком длинный.");
      if (body.fields.some((field) => field.key === "type"))
        throw Error("Ключ поля type зарезервирован. Выберите другой ключ.");
      if (body.derived_duration && (body.topology === "point" ||
          body.fields.some((field) => field.key === "elapsed_minutes")))
        throw Error("Длительность доступна только для интервала и резервирует ключ elapsed_minutes.");
      for (const field of body.fields) {
        if (field.kind === "choice" &&
            (!field.options?.length || field.options.length > 50 ||
             new Set(field.options).size !== field.options.length ||
             field.options.some((option) => !option || option.length > 120)))
          throw Error(`Добавьте разные варианты для поля «${field.label}».`);
        if (!["number", "integer", "scale"].includes(field.kind)) continue;
        if (field.kind === "number" && !field.unit)
          throw Error(`Укажите единицу для поля «${field.label}».`);
        if (["number", "integer"].includes(field.kind) && field.unit &&
            ![...$("tracker-units").options].some((option) => option.value === field.unit))
          throw Error(`Единица поля «${field.label}» не поддерживается.`);
        if (field.metric_semantics === "interval_total" && body.topology !== "bounded_interval")
          throw Error(`Итог за интервал требует завершённого интервала: «${field.label}».`);
        if (field.metric_semantics === "event_count" &&
            (field.kind !== "integer" || (field.unit && !["count", "steps"].includes(field.unit))))
          throw Error(`Число случаев требует целого значения: «${field.label}».`);
        if (field.metric_semantics && field.metric_semantics !== "gauge" && field.minimum < 0)
          throw Error(`Итоги и счётчики не могут быть отрицательными: «${field.label}».`);
        if (!Number.isFinite(field.minimum) || !Number.isFinite(field.maximum) ||
            field.minimum > field.maximum ||
            (["integer", "scale"].includes(field.kind) &&
             (!Number.isSafeInteger(field.minimum) || !Number.isSafeInteger(field.maximum))) ||
            (field.kind === "scale" && field.maximum - field.minimum > 20))
          throw Error(`Проверьте границы поля «${field.label}».`);
      }
      demoState.preview = structuredClone(body);
      return {
        confirmation_token: "demo-preview",
        definition: { labels: { [body.locale]: body.name } },
        form: { topology: body.topology, fields: body.fields.map(demoFormField) },
      };
    }
    if (url === "/tracker-setups" && body) {
      if (body.confirmation_token !== "demo-preview" ||
          JSON.stringify(body.draft) !== JSON.stringify(demoState.preview))
        throw Error("Предпросмотр устарел. Проверьте форму ещё раз.");
      demoState.trackers.push(structuredClone(body.draft));
      demoState.preview = null;
      return { synthetic: true };
    }
    if (url === "/actions") return { actions: demoActions() };
    if (url.startsWith("/actions/events/")) {
      const id = decodeURIComponent(url.slice("/actions/events/".length));
      const event = demoState.events.find((item) => item.id === id);
      if (!event || !event.kind.startsWith("user.")) throw Error("Демо-запись не найдена.");
      return { id: `edit:${id}`, kind: "edit_entry", event_id: id, definition_key: event.kind };
    }
    if (url.startsWith("/forms/")) {
      const submitting = url.endsWith("/submit");
      const tail = decodeURIComponent(url.slice("/forms/".length, submitting ? -7 : undefined));
      const action = tail.startsWith("create:")
        ? demoActions().find((item) => item.id === tail)
        : tail.startsWith("edit:")
          ? demoRequest("/actions/events/" + encodeURIComponent(tail.slice(5)))
          : null;
      if (!action) throw Error("Демо-форма не найдена.");
      if (!body) return demoForm(action);
      if (!submitting || body.action_id !== action.id ||
          body.schema_hash !== `demo:${action.definition_key.slice(5)}`)
        throw Error("Демо-форма изменилась. Откройте её заново.");
      const tracker = demoState.trackers.find((item) => `user.${item.key}` === action.definition_key);
      const start = Date.parse(body.start);
      const end = body.end == null ? null : Date.parse(body.end);
      if (!Number.isFinite(start) || (end !== null && !Number.isFinite(end)))
        throw Error("Проверьте время записи.");
      if (end !== null && end < start)
        throw Error("Окончание не может быть раньше начала.");
      if (tracker.topology === "bounded_interval" && (end === null || end <= start))
        throw Error("Укажите окончание позже начала.");
      if (tracker.topology === "point" && end !== null && end !== start)
        throw Error("Для записи-момента окончание не нужно.");
      if (demoState.operations.has(body.operation_id))
        return { ...demoState.operations.get(body.operation_id), synthetic: true };
      for (const field of tracker.fields) {
        const value = body.values[field.key];
        if (field.required && (value === undefined || value === ""))
          throw Error(`Заполните поле «${field.label}».`);
        if (value !== undefined && ["number", "integer", "scale"].includes(field.kind) &&
            (!Number.isFinite(value) || (field.minimum !== undefined && value < field.minimum) ||
             (field.maximum !== undefined && value > field.maximum)))
          throw Error(`Проверьте значение поля «${field.label}».`);
        if (value !== undefined && field.kind === "text" &&
            (typeof value !== "string" || value.length > (field.max_length || 500)))
          throw Error(`Текст поля «${field.label}» слишком длинный.`);
      }
      const entry = action.event_id
        ? demoState.events.find((item) => item.id === action.event_id)
        : { id: `demo-focus-${demoState.nextId++}`, kind: action.definition_key,
            source: "manual", status: "confirmed" };
      entry.start = body.start;
      entry.topology = tracker.topology === "bounded_interval" || (end !== null && end > start)
        ? "bounded_interval"
        : tracker.topology === "open_interval" && end === null ? "open_interval" : "point";
      entry.end = entry.topology === "bounded_interval" ? body.end : null;
      entry.missing_end = entry.topology === "open_interval" && entry.end === null;
      entry.payload = { ...body.values };
      if (!action.event_id) demoState.events.push(entry);
      demoState.operations.set(body.operation_id, structuredClone(entry));
      return { ...entry, synthetic: true };
    }
    throw Error("Это действие недоступно в демо.");
  }
  const syntheticChannels = {
    heart_rate_bpm: {
      quality_reason: "recent_observations",
      newest_observed_at: `${today}T08:45:00Z`,
    },
    sleep_score: {
      quality_reason: "unknown",
      source_calendar_date: new Date(Date.parse(today) - 2 * 86400000)
        .toISOString()
        .slice(0, 10),
    },
    stress_score: {
      quality_reason: "recent_observations",
      newest_observed_at: `${today}T08:30:00Z`,
    },
  };
  $("end").value = today;
  $("start").value = new Date(Date.parse(today) - 6 * 86400000)
    .toISOString()
    .slice(0, 10);
  function range() {
    const a = $("start").value,
      b = $("end").value;
    const left = Date.parse(a + "T00:00:00Z"),
      right = Date.parse(b + "T00:00:00Z");
    if (
      !a ||
      !b ||
      !Number.isFinite(left + right) ||
      right < left ||
      right - left > 30 * 86400000
    )
      throw Error("Выберите период от 1 до 31 дня.");
    return {
      start: new Date(left).toISOString(),
      end: new Date(right + 86400000).toISOString(),
    };
  }
  function stamp(value) {
    if (!value) return "Нет данных";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Дата неизвестна"
      : new Intl.DateTimeFormat("ru", {
          timeZone: "UTC",
          day: "numeric",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        }).format(date);
  }
  function cell(row, text) {
    const td = document.createElement("td");
    td.textContent = String(text);
    row.append(td);
    return td;
  }
  function placeholder(id, text, columns = 4) {
    const tr = document.createElement("tr");
    cell(tr, text).colSpan = columns;
    $(id).replaceChildren(tr);
  }
  function notice(title, text) {
    $("mode").textContent = title;
    $("message").textContent = text;
  }
  function renderChannels(channels) {
    $("source-rows").replaceChildren();
    for (const [key, value] of Object.entries(channels)) {
      const [label, explanation, style] =
        reasons[value.quality_reason] || reasons.unknown;
      const tr = document.createElement("tr");
      cell(tr, names[key] || key);
      const status = cell(tr, label);
      status.className = style;
      const dot = document.createElement("span");
      dot.className = "dot";
      dot.setAttribute("aria-hidden", "true");
      status.prepend(dot);
      cell(
        tr,
        value.source_calendar_date
          ? value.source_calendar_date + " · дневная сводка"
          : stamp(value.newest_observed_at),
      );
      cell(tr, explanation);
      $("source-rows").append(tr);
    }
    if (!Object.keys(channels).length)
      placeholder("source-rows", "Источники пока не дали наблюдений.");
  }
  function description(event) {
    const payload = event.payload || {};
    const fields = {
      beverage: "Напиток",
      name: "Название",
      description: "",
      notes: "",
      dose: "Доза",
      unit: "Единица",
      caffeine_mg_estimate: "Оценка кофеина, мг",
      caffeine_mg_min: "Кофеин от, мг",
      caffeine_mg_max: "Кофеин до, мг",
      energy: "Энергия",
      restedness: "Отдых",
      pain: "Боль",
      functional_impact: "Влияние на дела",
      perceived_exertion: "Усилие",
      activity_id: "Активность",
      servings: "Порции",
      dose_basis: "Доза",
      dose_provenance: "Источник дозы",
      severity: "Интенсивность",
      impact: "Влияние на дела",
      aura: "Аура",
      headache: "Головная боль",
      migraine: "Мигрень",
      amount: "Количество",
      symptoms: "Симптомы",
    };
    const known = Object.entries(fields)
        .filter(([key]) => payload[key] !== null && payload[key] !== undefined)
        .map(
          ([key, label]) =>
            (label ? label + ": " : "") +
            (key === "dose_basis"
              ? {
                  total: "всего",
                  per_serving: "на порцию",
                  unknown: "неизвестно",
                }[payload[key]] || payload[key]
              : key === "dose_provenance"
                ? {
                    estimated: "оценка",
                    reported_label: "этикетка",
                    unknown: "неизвестно",
                  }[payload[key]] || payload[key]
                : ["headache", "migraine"].includes(key)
                  ? { yes: "да", no: "нет", unknown: "неизвестно" }[
                      payload[key]
                    ] || String(payload[key])
                  : String(payload[key])),
        )
        .join(" · ");
    const custom = Object.entries(payload)
      .filter(([key, value]) => !(key in fields) && key !== "type" && value !== null && value !== undefined)
      .map(([key, value]) => key + ": " + (typeof value === "object" ? JSON.stringify(value) : String(value)))
      .join(" · ");
    return [known, custom].filter(Boolean).join(" · ") || "Подробности не указаны";
  }
  function renderDiary(result) {
    $("diary-rows").replaceChildren();
    for (const event of result.rows) {
      const tr = document.createElement("tr");
      cell(
        tr,
        stamp(event.start) +
          (event.missing_end
            ? " — конец не указан"
            : event.topology === "bounded_interval"
              ? " — " + stamp(event.end)
              : ""),
      );
      cell(tr, names[event.kind] || event.kind);
      cell(tr, description(event));
      cell(
        tr,
        (sources[event.source] || event.source) +
          " · " +
          (statuses[event.status] || event.status),
      );
      const actionCell = cell(tr, "");
      if (event.kind.startsWith("user.") && (demo || (canWriteDiary && event.can_update === true))) {
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "outline";
        edit.textContent = "Исправить";
        edit.addEventListener("click", async () => {
          try {
            const action = await request("/actions/events/" + encodeURIComponent(event.id));
            await openAction(action);
          } catch (error) {
            notice("Форма недоступна", error.message);
          }
        });
        actionCell.append(edit);
      }
      $("diary-rows").append(tr);
    }
    if (!result.rows.length)
      placeholder("diary-rows", "В выбранном периоде записей нет.", 5);
    $("diary-status").textContent = result.truncated
      ? "Показаны первые 500 записей. Сузьте период для просмотра остальных."
      : "Записей: " +
        result.rows.length +
        ". Экспорт включает все типы записей за выбранный период.";
  }
  function renderActions(actions) {
    $("tracker-actions").replaceChildren();
    for (const action of actions) {
      names[action.definition_key] = action.label;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "outline";
      button.textContent = action.label;
      button.addEventListener("click", () =>
        openAction(action).catch((error) => notice("Форма недоступна", error.message)),
      );
      $("tracker-actions").append(button);
    }
    if (!actions.length)
      $("tracker-actions").textContent = "Пользовательских трекеров пока нет.";
  }
  function renderDemoAnalysis(interval) {
    const visible = demoState.events.filter((event) =>
      event.kind.startsWith("user.") && event.status === "confirmed" &&
      demoOverlaps(event, interval));
    const parts = [];
    for (const tracker of demoState.trackers) {
      const entries = visible.filter((event) => event.kind === `user.${tracker.key}`);
      for (const field of tracker.fields.filter((item) => item.kind === "choice" || item.kind === "boolean")) {
        const values = entries.map((event) => event.payload[field.key])
          .filter((value) => value !== undefined && value !== null);
        if (!values.length) continue;
        if (field.kind === "boolean") {
          const yes = values.filter((value) => value === true).length;
          parts.push(`${tracker.name} · ${field.label}: да ${yes} из ${values.length}, ` +
            `доля ${((yes / values.length) * 100).toFixed(0)}%`);
        } else {
          const counts = field.options.map((option) =>
            `${option} ${values.filter((value) => value === option).length}`);
          parts.push(`${tracker.name} · ${field.label}: ${counts.join(", ")}`);
        }
      }
      for (const field of tracker.fields.filter((item) =>
        ["number", "integer", "scale"].includes(item.kind))) {
        const values = entries.map((event) => event.payload[field.key])
          .filter((value) => typeof value === "number" && Number.isFinite(value));
        if (values.length) {
          const total = values.reduce((sum, value) => sum + value, 0);
          const semantics = field.metric_semantics || "gauge";
          if (field.kind === "scale") {
            const ordered = [...values].sort((a, b) => a - b);
            const middle = Math.floor(ordered.length / 2);
            const median = ordered.length % 2 ? ordered[middle] :
              (ordered[middle - 1] + ordered[middle]) / 2;
            parts.push(`${tracker.name} · ${field.label}: ${values.length} знач., ` +
              `медиана ${median.toFixed(1)} ${demoUnit(field)}`);
          } else if (semantics === "cumulative_counter") {
            parts.push(`${tracker.name} · ${field.label}: накопительный счётчик не пересчитывается в демо`);
          } else {
            const summed = ["event_total", "event_count", "interval_total"].includes(semantics);
            parts.push(`${tracker.name} · ${field.label}: ${values.length} знач., ` +
              `${summed ? "сумма" : "среднее"} ${(summed ? total : total / values.length).toFixed(1)}` +
              (demoUnit(field) ? ` ${demoUnit(field)}` : ""));
          }
        }
      }
      if (tracker.derived_duration) {
        const minutes = entries.map((event) => event.end
          ? (Date.parse(event.end) - Date.parse(event.start)) / 60000 : null)
          .filter((value) => value !== null && Number.isFinite(value) && value >= 0);
        if (minutes.length)
          parts.push(`${tracker.name} · Длительность: ${minutes.length} знач., ` +
            `сумма ${minutes.reduce((sum, value) => sum + value, 0).toFixed(1)} мин`);
      }
    }
    $("demo-analysis-text").textContent = parts.join("; ") ||
      "За выбранный период нет числовых записей для расчёта.";
  }
  function browserTimezone() {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  }
  function zoneParts(value, timezone) {
    return Object.fromEntries(
      new Intl.DateTimeFormat("en-CA", {
        timeZone: timezone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(value)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
  }
  function localDateTime(value, timezone) {
    const parts = zoneParts(value ? new Date(value) : new Date(), timezone);
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`;
  }
  function zonedISOString(value, timezone, originalValue = null) {
    const normalized = value.length === 16 ? `${value}:00` : value;
    if (originalValue && localDateTime(originalValue, timezone) === normalized)
      return new Date(originalValue).toISOString();
    const [date, time] = value.split("T");
    const [year, month, day] = date.split("-").map(Number);
    const [hour, minute, second = 0] = time.split(":").map(Number);
    const target = Date.UTC(year, month - 1, day, hour, minute, second);
    const offsets = new Set();
    for (let delta = -36; delta <= 36; delta += 1) {
      const sample = target + delta * 60 * 60 * 1000;
      const parts = zoneParts(new Date(sample), timezone);
      const rendered = Date.UTC(
        Number(parts.year),
        Number(parts.month) - 1,
        Number(parts.day),
        Number(parts.hour),
        Number(parts.minute),
        Number(parts.second),
      );
      offsets.add(rendered - sample);
    }
    const matches = new Set();
    for (const offset of offsets) {
      const candidate = target - offset;
      if (localDateTime(new Date(candidate), timezone) === normalized)
        matches.add(new Date(candidate).toISOString());
    }
    if (!matches.size)
      throw Error("Выбранное местное время не существует из-за перевода часов.");
    if (matches.size > 1)
      throw Error(
        "Выбранное местное время встречается дважды из-за перевода часов. " +
          "Укажите другое время или не изменяйте исходную отметку.",
      );
    return matches.values().next().value;
  }
  async function openAction(action) {
    currentForm = await request("/forms/" + encodeURIComponent(action.id));
    currentForm.operation_id = crypto.randomUUID();
    $("entry-title").textContent = currentForm.title;
    $("entry-fields").replaceChildren();
    for (const field of currentForm.fields) {
      const label = document.createElement("label");
      label.textContent = field.label + (field.unit ? " (" + field.unit + ")" : "");
      let input;
      const initial = currentForm.initial_values[field.name];
      if (field.input === "choice" || field.input === "boolean") {
        input = document.createElement("select");
        if (!field.required || initial === undefined || initial === null) {
          const empty = document.createElement("option");
          empty.value = "";
          empty.textContent = field.required ? "Выберите значение" : "Не указано";
          empty.disabled = field.required;
          empty.selected = true;
          input.append(empty);
        }
        const options =
          field.input === "boolean"
            ? [
                ["true", "Да"],
                ["false", "Нет"],
              ]
            : field.options.map((value) => [String(value), String(value)]);
        for (const [value, text] of options) {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = text;
          input.append(option);
        }
      } else {
        input = document.createElement("input");
        input.type = ["number", "integer"].includes(field.input) ? "number" : "text";
        if (field.input === "integer") input.step = "1";
        if (field.input === "number") input.step = "any";
        if (field.minimum !== null) input.min = field.minimum;
        if (field.maximum !== null) input.max = field.maximum;
        if (field.max_length) input.maxLength = field.max_length;
      }
      input.required = field.required;
      input.dataset.name = field.name;
      input.dataset.kind = field.input;
      input.dataset.unit = field.unit || "";
      if (initial !== undefined && initial !== null)
        input.value = field.input === "json" ? JSON.stringify(initial) : String(initial);
      label.append(input);
      $("entry-fields").append(label);
    }
    const timezone = currentForm.initial_timezone || browserTimezone();
    $("entry-start").value = localDateTime(currentForm.initial_start, timezone);
    $("entry-end").value = currentForm.initial_end
      ? localDateTime(currentForm.initial_end, timezone)
      : "";
    $("entry-end-label").hidden = currentForm.topology === "point";
    $("entry-end").required = currentForm.topology === "bounded_interval";
    $("entry-status").textContent = "";
    $("entry-dialog").showModal();
  }
  function clearData() {
    placeholder("source-rows", "Данные не загружены.");
    placeholder("diary-rows", "Данные не загружены.", 5);
    $("source-status").textContent = "";
    $("diary-status").textContent = "";
    $("tracker-actions").textContent = "Действия не загружены.";
  }
  function invalidate() {
    generation++;
    controller?.abort();
    controller = new AbortController();
    canWriteDiary = false;
    clearData();
    $("demo-analysis-text").textContent = "";
    return generation;
  }
  function orderedDemoEvents(events, interval) {
    const left = Date.parse(interval.start);
    return [...events].sort((a, b) =>
      Number(Date.parse(b.start) >= left) - Number(Date.parse(a.start) >= left) ||
      Date.parse(a.start) - Date.parse(b.start) ||
      String(a.id || "").localeCompare(String(b.id || "")));
  }
  async function request(url, body) {
    if (demo) return demoRequest(url, body);
    const response = await fetch(url, {
      method: body ? "POST" : "GET",
      headers: {
        Authorization: "Bearer " + token,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
    });
    if (!response.ok) {
      let details = {};
      try {
        details = await response.json();
      } catch (_) {
        details = {};
      }
      const error = Error(
        response.status === 401
          ? "Токен не принят. Подключитесь заново."
          : response.status === 403
            ? "Недостаточно прав для этих данных."
            : details.errors?.length
              ? details.errors.map((item) => item.field + ": " + item.message).join("; ")
            : "Не удалось загрузить данные. Проверьте период и доступность экземпляра.",
      );
      error.status = response.status;
      error.details = details;
      throw error;
    }
    return response.json();
  }
  function authFailed(error) {
    if (error.status !== 401) return false;
    token = "";
    demo = false;
    invalidate();
    resetForms();
    $("connect").textContent = "Подключить";
    notice("Подключение не выполнено", error.message);
    return true;
  }
  async function load() {
    const version = invalidate();
    let interval;
    try {
      interval = range();
    } catch (error) {
      notice("Проверьте период", error.message);
      return;
    }
    if (demo) {
      canWriteDiary = false;
      $("demo-controls").hidden = false;
      $("demo-analysis").hidden = false;
      renderActions(demoActions());
      renderChannels(syntheticChannels);
      renderDiary({
        rows: orderedDemoEvents(demoState.events.filter(
          (e) =>
            demoOverlaps(e, interval) &&
            (!$("kind").value || e.kind === $("kind").value),
        ), interval),
        truncated: false,
      });
      renderDemoAnalysis(interval);
      notice(
        "Демонстрационные данные",
        "Пример вымышленный. Вводите только вымышленные записи; личные данные не загружаются.",
      );
      $("source-status").textContent =
        "Синтетический пример на " +
        today +
        ". Период выше относится к дневнику.";
      return;
    }
    $("demo-controls").hidden = true;
    $("demo-analysis").hidden = true;
    notice("Загрузка", "Получаем доступные данные этого экземпляра…");
    try {
      const [tools, capabilities] = await Promise.all([
        request("/tools"),
        request("/capabilities"),
      ]);
      if (version !== generation) return;
      canWriteDiary = capabilities.write_diary === true;
      const allowed = new Set(tools.map((t) => t.name));
      const jobs = [];
      if (allowed.has("data_freshness"))
        jobs.push(
          request("/tools/data_freshness", { arguments: {} }).then((value) => {
            if (version !== generation) return;
            renderChannels(value.channels);
            $("source-status").textContent =
              "Проверено: " +
              stamp(value.checked_at) +
              " UTC. Успешная загрузка не доказывает непрерывное ношение часов.";
          }),
        );
      else
        placeholder(
          "source-rows",
          "Для источников требуется право чтения данных Garmin.",
        );
      if (allowed.has("events"))
        jobs.push(
          request("/tools/events", {
            arguments: {
              ...interval,
              ...($("kind").value ? { kind: $("kind").value } : {}),
            },
          }).then((value) => {
            if (version === generation) renderDiary(value);
          }),
        );
      else
        placeholder(
          "diary-rows",
          "Для дневника требуется право чтения дневника.",
        );
      if (capabilities.read_diary)
        jobs.push(
          request("/actions").then((value) => {
            if (version === generation) renderActions(value.actions);
          }),
        );
      else renderActions([]);
      if (capabilities.manage_definitions)
        jobs.push(
          request("/tracker-profile").then((profile) => {
            if (version === generation) {
              builderLocale = profile.locale;
              localizeBuilder();
            }
          }),
        );
      const outcomes = await Promise.allSettled(jobs);
      if (version !== generation) return;
      const authError = outcomes.find(
        (r) => r.status === "rejected" && r.reason.status === 401,
      );
      if (authError && authFailed(authError.reason)) return;
      const failed = outcomes.find((r) => r.status === "rejected");
      notice(
        failed ? "Часть данных недоступна" : "Подключено",
        failed
          ? failed.reason.message
          : "Записи загружены из вашего экземпляра. Токен остаётся только в памяти вкладки.",
      );
    } catch (error) {
      if (
        version === generation &&
        error.name !== "AbortError" &&
        !authFailed(error)
      )
        notice("Подключение не выполнено", error.message);
    }
  }
  $("range").addEventListener("submit", (event) => {
    event.preventDefault();
    load();
  });
  $("kind").addEventListener("change", load);
  $("connect").addEventListener("click", () => {
    if (token) {
      token = "";
      demo = false;
      invalidate();
      resetForms();
      $("connect").textContent = "Подключить";
      notice(
        "Отключено",
        "Токен и загруженные записи удалены из памяти страницы.",
      );
    } else $("auth").showModal();
  });
  $("demo-switch").addEventListener("click", () => {
    token = "";
    demo = true;
    builderLocale = "ru";
    resetForms();
    localizeBuilder();
    $("connect").textContent = "Подключить";
    load();
  });
  $("demo-reset").addEventListener("click", () => {
    if (!demo) return;
    demoState = newDemoState();
    resetForms();
    load();
  });
  $("cancel-auth").addEventListener("click", () => $("auth").close());
  $("auth").addEventListener("close", () => {
    $("token").value = "";
  });
  $("auth-form").addEventListener("submit", (event) => {
    event.preventDefault();
    token = $("token").value.trim();
    $("token").value = "";
    demo = false;
    resetForms();
    $("auth").close();
    $("connect").textContent = "Отключить";
    load();
  });
  function fieldControl(row, name) {
    return row.querySelector(`[data-field="${name}"]`);
  }
  function builderEnglish() {
    return builderLocale.split("-", 1)[0] === "en";
  }
  function invalidateTrackerPreview() {
    trackerDraftVersion += 1;
    trackerPreview = undefined;
    $("tracker-preview").hidden = true;
  }
  function setControlLabel(control, value) {
    const label = control.closest("label");
    const text = [...label.childNodes].find((node) => node.nodeType === Node.TEXT_NODE);
    if (text) {
      label.dataset.ruLabel ??= text.textContent;
      text.textContent = builderEnglish() ? value : label.dataset.ruLabel;
    }
  }
  function setTranslatedText(element, english) {
    element.dataset.ruText ??= element.textContent;
    element.textContent = builderEnglish() ? english : element.dataset.ruText;
  }
  function localizeFieldRow(row) {
    const labels = {
      label: "Field name",
      key: "Field key",
      kind: "Field type",
      "metric-semantics": "Numeric meaning",
      unit: "Unit",
      min: "Minimum",
      max: "Maximum",
      options: "Comma-separated choices",
      required: "Required field",
    };
    for (const [name, value] of Object.entries(labels)) {
      setControlLabel(fieldControl(row, name), value);
    }
    setTranslatedText(row.querySelector('[data-field-action="remove"]'), "Remove");
    for (const [action, english] of [["up", "Move field up"], ["down", "Move field down"]]) {
      const button = row.querySelector(`[data-field-action="${action}"]`);
      button.dataset.ruAria ??= button.getAttribute("aria-label");
      button.setAttribute("aria-label", builderEnglish() ? english : button.dataset.ruAria);
    }
    const kinds = ["Text", "Scale", "Number with unit", "Integer", "Yes / no", "Choice"];
    fieldControl(row, "kind").querySelectorAll("option").forEach((option, index) => {
      setTranslatedText(option, kinds[index]);
    });
    const meanings = ["Gauge: average", "Event total: sum", "Event count: sum", "Interval total: sum", "Cumulative counter: change"];
    fieldControl(row, "metric-semantics").querySelectorAll("option").forEach((option, index) => {
      setTranslatedText(option, meanings[index]);
    });
  }
  function localizeBuilder() {
    const labels = {
      "tracker-name": "Name",
      "tracker-key": "Key",
      "tracker-topology": "Entry time",
      "tracker-derived-duration": "Calculate duration from start and end",
      "tracker-shortcut": "Quick action",
      "tracker-reminder": "Reminder",
      "tracker-privacy": "Data access",
    };
    for (const [id, value] of Object.entries(labels)) setControlLabel($(id), value);
    setTranslatedText($("tracker-setup").closest("details").querySelector("summary"), "Create tracker");
    setTranslatedText($("add-tracker-field"), "Add field");
    setTranslatedText($("tracker-setup").querySelector('button[type="submit"]'), "Preview");
    setTranslatedText($("tracker-preview").querySelector("strong"), "Review before enabling");
    setTranslatedText($("confirm-tracker"), "Enable tracker");
    const topologies = ["Point", "Closed interval", "Open interval", "Point or interval"];
    $("tracker-topology").querySelectorAll("option").forEach((option, index) => {
      setTranslatedText(option, topologies[index]);
    });
    setTranslatedText($("tracker-privacy").querySelectorAll("option")[0], "Private tracker");
    setTranslatedText(
      $("tracker-privacy").querySelectorAll("option")[1],
      "Sensitive: separate consent for channels and model",
    );
    for (const row of $("tracker-fields").children) localizeFieldRow(row);
    updateFieldPositions();
  }
  function syncFieldControls(row) {
    const kind = fieldControl(row, "kind").value;
    const supported = ["number", "integer"].includes(kind);
    const numeric = ["number", "integer", "scale"].includes(kind);
    const selector = fieldControl(row, "metric-semantics");
    row.querySelector('[data-field-row="metric-semantics"]').hidden = !supported;
    selector.disabled = !supported;
    selector.querySelector('[value="event_count"]').disabled = kind !== "integer";
    selector.querySelector('[value="interval_total"]').disabled =
      $("tracker-topology").value !== "bounded_interval";
    if (!supported || selector.selectedOptions[0].disabled) selector.value = "gauge";
    for (const name of ["min", "max"]) {
      const control = fieldControl(row, name);
      row.querySelector(`[data-field-row="${name === "min" ? "minimum" : "maximum"}"]`).hidden = !numeric;
      control.required = numeric;
      control.step = ["integer", "scale"].includes(kind) ? "1" : "any";
    }
    row.querySelector('[data-field-row="unit"]').hidden = !supported;
    fieldControl(row, "unit").required = kind === "number";
    row.querySelector('[data-field-row="options"]').hidden = kind !== "choice";
    fieldControl(row, "options").required = kind === "choice";
  }
  function updateFieldPositions() {
    const rows = [...$("tracker-fields").querySelectorAll(".tracker-field")];
    rows.forEach((row, index) => {
      row.querySelector("legend").textContent =
        `${builderEnglish() ? "Field" : "Поле"} ${index + 1}`;
      row.querySelector('[data-field-action="up"]').disabled = index === 0;
      row.querySelector('[data-field-action="down"]').disabled = index === rows.length - 1;
      row.querySelector('[data-field-action="remove"]').disabled = rows.length === 1;
    });
    $("add-tracker-field").disabled = rows.length >= 32;
  }
  function addTrackerField() {
    if ($("tracker-fields").children.length >= 32) return;
    const row = $("tracker-field-template").content.firstElementChild.cloneNode(true);
    row.querySelector('[data-field="kind"]').addEventListener("change", () => syncFieldControls(row));
    row.querySelector('[data-field-action="up"]').addEventListener("click", () => {
      row.previousElementSibling?.before(row);
      updateFieldPositions();
      invalidateTrackerPreview();
    });
    row.querySelector('[data-field-action="down"]').addEventListener("click", () => {
      row.nextElementSibling?.after(row);
      updateFieldPositions();
      invalidateTrackerPreview();
    });
    row.querySelector('[data-field-action="remove"]').addEventListener("click", () => {
      row.remove();
      updateFieldPositions();
      invalidateTrackerPreview();
    });
    $("tracker-fields").append(row);
    localizeFieldRow(row);
    syncFieldControls(row);
    updateFieldPositions();
    invalidateTrackerPreview();
    return row;
  }
  function resetForms() {
    $("entry-dialog").close();
    $("entry-fields").replaceChildren();
    currentForm = undefined;
    $("tracker-setup").reset();
    $("tracker-fields").replaceChildren();
    addTrackerField();
    syncDerivedDuration();
    $("tracker-status").textContent = "";
  }
  $("add-tracker-field").addEventListener("click", addTrackerField);
  $("tracker-topology").addEventListener("change", () => {
    for (const row of $("tracker-fields").children) syncFieldControls(row);
  });
  function syncDerivedDuration() {
    const point = $("tracker-topology").value === "point";
    $("tracker-derived-duration").disabled = point;
    if (point) $("tracker-derived-duration").checked = false;
  }
  $("tracker-topology").addEventListener("change", syncDerivedDuration);
  syncDerivedDuration();
  addTrackerField();
  $("tracker-setup").addEventListener("input", invalidateTrackerPreview);
  $("tracker-setup").addEventListener("change", invalidateTrackerPreview);
  $("tracker-setup").addEventListener("submit", async (event) => {
    event.preventDefault();
    const draftVersion = trackerDraftVersion;
    if (!demo && !token) {
      $("tracker-status").textContent = "Сначала подключитесь к своему экземпляру.";
      return;
    }
    const reminder = $("tracker-reminder").value;
    const fields = [...$("tracker-fields").querySelectorAll(".tracker-field")].map((row) => {
      const kind = fieldControl(row, "kind").value;
      const numeric = ["number", "integer", "scale"].includes(kind);
      return {
        key: fieldControl(row, "key").value,
        label: fieldControl(row, "label").value,
        kind,
        required: fieldControl(row, "required").checked,
        ...(["number", "integer"].includes(kind)
          ? { metric_semantics: fieldControl(row, "metric-semantics").value }
          : {}),
        ...(numeric
          ? {
              minimum: fieldControl(row, "min").value === ""
                ? null : Number(fieldControl(row, "min").value),
              maximum: fieldControl(row, "max").value === ""
                ? null : Number(fieldControl(row, "max").value),
            }
          : {}),
        ...(["number", "integer"].includes(kind) && fieldControl(row, "unit").value
          ? { unit: fieldControl(row, "unit").value }
          : {}),
        ...(kind === "choice"
          ? {
              options: fieldControl(row, "options").value
                .split(",")
                .map((option) => option.trim())
                .filter(Boolean),
            }
          : {}),
      };
    });
    try {
      const profile = await request("/tracker-profile");
      const draft = {
        key: $("tracker-key").value,
        name: $("tracker-name").value,
        locale: profile.locale,
        topology: $("tracker-topology").value,
        derived_duration: $("tracker-derived-duration").checked,
        fields,
        shortcut: $("tracker-shortcut").value || null,
        reminder_enabled: !demo && Boolean(reminder),
        reminder_time: !demo ? reminder || null : null,
        reminder_timezone: profile.timezone,
        privacy: $("tracker-privacy").value,
      };
      const preview = await request("/tracker-setups/preview", draft);
      if (draftVersion !== trackerDraftVersion) return;
      trackerPreview = { draft, token: preview.confirmation_token };
      const english = draft.locale.split("-", 1)[0] === "en";
      const topology = english
        ? {
            point: "point",
            bounded_interval: "closed interval",
            open_interval: "open interval",
            flexible: "point or interval",
          }
        : {
            point: "момент",
            bounded_interval: "интервал с окончанием",
            open_interval: "эпизод",
            flexible: "момент или интервал",
          };
      $("tracker-preview-text").textContent =
        (preview.definition.labels[draft.locale] || draft.name) +
        ": " +
        preview.form.fields.map((item) => item.label).join(", ") +
        (english ? ". Time: " : ". Время: ") +
        (topology[preview.form.topology] || preview.form.topology) +
        ".";
      $("tracker-preview-privacy").textContent = draft.privacy === "sensitive"
        ? english
          ? "Sensitive data: channels and the model need separate consent for each destination. Enabling this tracker grants none."
          : "Чувствительные данные: каналам и модели нужно отдельное согласие для каждого подключения. Включение трекера его не выдаёт."
        : english
          ? "Private data: enabled channels and the model may use it under your current integration settings. Enabling this tracker does not change those settings."
          : "Личные данные: включённые каналы и модель могут использовать их согласно текущим настройкам подключений. Включение трекера не меняет эти настройки.";
      if (demo) $("tracker-preview-privacy").textContent =
        "Демо хранит записи только в памяти вкладки. Каналы, модель и напоминания не включаются.";
      $("tracker-preview").hidden = false;
      $("tracker-status").textContent = english
        ? "Preview ready. No data has been saved."
        : "Предпросмотр готов. Данные ещё не записаны.";
    } catch (error) {
      $("tracker-status").textContent = error.message;
    }
  });
  $("confirm-tracker").addEventListener("click", async () => {
    if (!trackerPreview) return;
    try {
      const locale = trackerPreview.draft.locale;
      await request("/tracker-setups", {
        draft: trackerPreview.draft,
        confirmation_token: trackerPreview.token,
      });
      trackerPreview = undefined;
      $("tracker-preview").hidden = true;
      $("tracker-setup").reset();
      $("tracker-fields").replaceChildren();
      addTrackerField();
      syncDerivedDuration();
      $("tracker-status").textContent =
        locale === "en"
          ? "Tracker enabled and added to actions."
          : "Трекер включён и появился в действиях.";
      const actions = await request("/actions");
      renderActions(actions.actions);
    } catch (error) {
      $("tracker-status").textContent = error.message;
    }
  });
  $("cancel-entry").addEventListener("click", () => $("entry-dialog").close());
  $("entry-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!currentForm) return;
    const values = {};
    const units = {};
    try {
      for (const input of $("entry-fields").querySelectorAll("input, select")) {
        if (input.value === "") continue;
        let value = input.value;
        if (input.dataset.kind === "boolean") value = value === "true";
        else if (input.dataset.kind === "integer") value = Number(value);
        else if (input.dataset.kind === "number") value = Number(value);
        else if (input.dataset.kind === "json") value = JSON.parse(value);
        values[input.dataset.name] = value;
        if (input.dataset.unit) units[input.dataset.name] = input.dataset.unit;
      }
      const timezone = currentForm.initial_timezone || browserTimezone();
      const body = {
        action_id: currentForm.action.id,
        operation_id: currentForm.operation_id,
        schema_hash: currentForm.schema_hash,
        start: zonedISOString(
          $("entry-start").value,
          timezone,
          currentForm.initial_start,
        ),
        end: $("entry-end").value
          ? zonedISOString(
              $("entry-end").value,
              timezone,
              currentForm.initial_end,
            )
          : null,
        timezone,
        values,
        units,
      };
      await request(
        "/forms/" + encodeURIComponent(currentForm.action.id) + "/submit",
        body,
      );
      $("entry-dialog").close();
      currentForm = undefined;
      await load();
    } catch (error) {
      $("entry-status").textContent = error.message;
    }
  });
  $("export").addEventListener("click", async () => {
    if (exporting) return;
    const version = generation;
    exporting = true;
    $("export").disabled = true;
    try {
      const interval = range();
      if (!demo && !token)
        throw Error("Сначала подключитесь к своему экземпляру.");
      const data = demo
        ? {
            synthetic: true,
            rows: orderedDemoEvents(demoState.events.filter(
              (e) => demoOverlaps(e, interval),
            ), interval),
          }
        : await request(
            "/exports/diary?" +
              new URLSearchParams({
                ...interval,
                timezone: "UTC",
                format: "json",
              }),
          );
      if (version !== generation) return;
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = demo ? "garmin-ai-demo.json" : "garmin-ai-diary.json";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (
        version === generation &&
        error.name !== "AbortError" &&
        !authFailed(error)
      )
        notice("Экспорт не выполнен", error.message);
    } finally {
      exporting = false;
      $("export").disabled = false;
    }
  });
  window.addEventListener("pagehide", () => {
    token = "";
    invalidate();
  });
  load();
})();
