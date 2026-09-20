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
    exporting = false,
    trackerPreview,
    currentForm;
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
      if (event.kind.startsWith("user.")) {
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
  function localDateTime(value) {
    const date = value ? new Date(value) : new Date();
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }
  async function openAction(action) {
    currentForm = await request("/forms/" + encodeURIComponent(action.id) + "?locale=ru");
    $("entry-title").textContent = currentForm.title;
    $("entry-fields").replaceChildren();
    for (const field of currentForm.fields) {
      const label = document.createElement("label");
      label.textContent = field.label + (field.unit ? " (" + field.unit + ")" : "");
      let input;
      if (field.input === "choice" || field.input === "boolean") {
        input = document.createElement("select");
        if (!field.required) {
          const empty = document.createElement("option");
          empty.value = "";
          empty.textContent = "Не указано";
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
      const initial = currentForm.initial_values[field.name];
      if (initial !== undefined && initial !== null) input.value = String(initial);
      label.append(input);
      $("entry-fields").append(label);
    }
    $("entry-start").value = localDateTime(currentForm.initial_start);
    $("entry-end").value = currentForm.initial_end ? localDateTime(currentForm.initial_end) : "";
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
    clearData();
    return generation;
  }
  async function request(url, body) {
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
      renderActions([]);
      renderChannels(syntheticChannels);
      renderDiary({
        rows: samples.filter(
          (e) =>
            Date.parse(e.start) >= Date.parse(interval.start) &&
            Date.parse(e.start) < Date.parse(interval.end) &&
            (!$("kind").value || e.kind === $("kind").value),
        ),
        truncated: false,
      });
      notice(
        "Демонстрационные данные",
        "Все записи синтетические. Реальные данные не загружены.",
      );
      $("source-status").textContent =
        "Синтетический пример на " +
        today +
        ". Период выше относится к дневнику.";
      return;
    }
    notice("Загрузка", "Получаем доступные данные этого экземпляра…");
    try {
      const tools = await request("/tools");
      if (version !== generation) return;
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
      jobs.push(
        request("/actions?locale=ru").then((value) => {
          if (version === generation) renderActions(value.actions);
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
      $("connect").textContent = "Подключить";
      notice(
        "Отключено",
        "Токен и загруженные записи удалены из памяти страницы.",
      );
    } else $("auth").showModal();
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
    $("auth").close();
    $("connect").textContent = "Отключить";
    load();
  });
  $("tracker-setup").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (demo || !token) {
      $("tracker-status").textContent = "Сначала подключитесь к своему экземпляру.";
      return;
    }
    const kind = $("field-kind").value;
    const numeric = ["number", "integer", "scale"].includes(kind);
    const minimum = $("field-min").value;
    const maximum = $("field-max").value;
    const reminder = $("tracker-reminder").value;
    const field = {
      key: $("field-key").value,
      label: $("field-label").value,
      kind,
      required: $("field-required").checked,
      ...(numeric
        ? {
            minimum: minimum === "" ? null : Number(minimum),
            maximum: maximum === "" ? null : Number(maximum),
          }
        : {}),
      ...(kind === "number" && $("field-unit").value
        ? { unit: $("field-unit").value }
        : {}),
    };
    const draft = {
      key: $("tracker-key").value,
      name: $("tracker-name").value,
      locale: "ru",
      topology: $("tracker-topology").value,
      fields: [field],
      shortcut: $("tracker-shortcut").value || null,
      reminder_enabled: Boolean(reminder),
      reminder_time: reminder || null,
      reminder_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      privacy: "private",
    };
    try {
      const preview = await request("/tracker-setups/preview", draft);
      trackerPreview = { draft, token: preview.confirmation_token };
      $("tracker-preview-text").textContent =
        preview.definition.labels.ru +
        ": " +
        preview.form.fields.map((item) => item.label).join(", ") +
        ". Время: " +
        ({
          point: "момент",
          bounded_interval: "интервал с окончанием",
          open_interval: "эпизод",
          flexible: "момент или интервал",
        }[preview.form.topology] || preview.form.topology) +
        ".";
      $("tracker-preview").hidden = false;
      $("tracker-status").textContent = "Предпросмотр готов. Данные ещё не записаны.";
    } catch (error) {
      $("tracker-status").textContent = error.message;
    }
  });
  $("confirm-tracker").addEventListener("click", async () => {
    if (!trackerPreview) return;
    try {
      await request("/tracker-setups", {
        draft: trackerPreview.draft,
        confirmation_token: trackerPreview.token,
      });
      trackerPreview = undefined;
      $("tracker-preview").hidden = true;
      $("tracker-setup").reset();
      $("tracker-status").textContent = "Трекер включён и появился в действиях.";
      const actions = await request("/actions?locale=ru");
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
        else if (input.dataset.kind === "integer") value = Number.parseInt(value, 10);
        else if (input.dataset.kind === "number") value = Number(value);
        else if (input.dataset.kind === "json") value = JSON.parse(value);
        values[input.dataset.name] = value;
        if (input.dataset.unit) units[input.dataset.name] = input.dataset.unit;
      }
      const body = {
        action_id: currentForm.action.id,
        schema_hash: currentForm.schema_hash,
        start: new Date($("entry-start").value).toISOString(),
        end: $("entry-end").value ? new Date($("entry-end").value).toISOString() : null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
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
            rows: samples.filter(
              (e) =>
                Date.parse(e.start) >= Date.parse(interval.start) &&
                Date.parse(e.start) < Date.parse(interval.end),
            ),
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
