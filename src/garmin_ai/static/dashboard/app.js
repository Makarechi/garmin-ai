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
    exporting = false;
  const samples = [
    {
      kind: "caffeine",
      start: "2026-09-10T09:00:00Z",
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
      start: "2026-09-10T08:15:00Z",
      source: "manual",
      status: "confirmed",
      payload: { notes: "Небольшая усталость", energy: 6 },
    },
    {
      kind: "note",
      start: "2026-09-10T07:30:00Z",
      source: "manual",
      status: "confirmed",
      payload: { description: "Прогулка перед завтраком" },
    },
  ];
  const syntheticChannels = {
    heart_rate_bpm: {
      quality_reason: "recent_observations",
      newest_observed_at: "2026-09-10T08:45:00Z",
    },
    sleep_score: {
      quality_reason: "unknown",
      source_calendar_date: "2026-09-08",
    },
    stress_score: {
      quality_reason: "recent_observations",
      newest_observed_at: "2026-09-10T08:30:00Z",
    },
  };
  const today = new Date().toISOString().slice(0, 10);
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
  function placeholder(id, text) {
    const tr = document.createElement("tr");
    cell(tr, text).colSpan = 4;
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
      aura: "Аура",
      symptoms: "Симптомы",
    };
    return (
      Object.entries(fields)
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
                : String(payload[key])),
        )
        .join(" · ") || "Подробности не указаны"
    );
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
            : event.end
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
      $("diary-rows").append(tr);
    }
    if (!result.rows.length)
      placeholder("diary-rows", "В выбранном периоде записей нет.");
    $("diary-status").textContent = result.truncated
      ? "Показаны первые 500 записей. Сузьте период для просмотра остальных."
      : "Записей: " +
        result.rows.length +
        ". Экспорт включает все типы записей за выбранный период.";
  }
  function clearData() {
    placeholder("source-rows", "Данные не загружены.");
    placeholder("diary-rows", "Данные не загружены.");
    $("source-status").textContent = "";
    $("diary-status").textContent = "";
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
    if (!response.ok)
      throw Error(
        response.status === 401
          ? "Токен не принят. Подключитесь заново."
          : response.status === 403
            ? "Недостаточно прав для этих данных."
            : "Не удалось загрузить данные. Проверьте период и доступность экземпляра.",
      );
    return response.json();
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
        "Пример состояния на 10 сентября 2026 года. Период выше относится к дневнику.";
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
      const outcomes = await Promise.allSettled(jobs);
      if (version !== generation) return;
      const failed = outcomes.find((r) => r.status === "rejected");
      notice(
        failed ? "Часть данных недоступна" : "Подключено",
        failed
          ? failed.reason.message
          : "Записи загружены из вашего экземпляра. Токен остаётся только в памяти вкладки.",
      );
    } catch (error) {
      if (version === generation && error.name !== "AbortError")
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
      if (version === generation && error.name !== "AbortError")
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
