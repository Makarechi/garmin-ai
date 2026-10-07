"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  let token = "";
  let pending;
  let fetching = false;
  let timer;

  async function request(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: "Bearer " + token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store", credentials: "omit", redirect: "error",
    });
    if (!response.ok) throw Error(response.status === 401 ? "Ключ не принят." : response.status === 403 ? "Нужен ключ владельца." : "Не удалось выполнить запрос. Попробуйте ещё раз.");
    return response.json();
  }

  function disconnect() {
    token = "";
    pending = undefined;
    clearInterval(timer);
    $("token").value = "";
    $("message").value = "";
    $("messages").replaceChildren();
    $("chat").hidden = true;
    $("connect-form").hidden = false;
    $("disconnect").hidden = true;
    $("connection-status").textContent = "Отключено. Ключ удалён из памяти вкладки.";
  }

  function render(data) {
    const replies = new Map();
    for (const reply of data.replies) {
      const group = replies.get(reply.inbound_message_id) || [];
      group.push({ ...reply, role: "assistant" });
      replies.set(reply.inbound_message_id, group);
    }
    const items = [];
    for (const message of data.messages) {
      items.push({ ...message, role: "owner" });
      items.push(...(replies.get(message.id) || []));
      replies.delete(message.id);
    }
    for (const group of replies.values()) items.push(...group);
    const list = $("messages");
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Сообщений пока нет. Начните с заметки или откройте дневник.";
      list.append(empty);
      return;
    }
    for (const item of items) {
      const bubble = document.createElement("div");
      bubble.className = "bubble " + item.role;
      bubble.textContent = item.text;
      const stamp = document.createElement("small");
      stamp.textContent = new Date(item.created_at).toLocaleString("ru-RU");
      bubble.append(stamp);
      list.append(bubble);
    }
    list.scrollTop = list.scrollHeight;
  }

  async function refresh() {
    if (!token || fetching || document.visibilityState !== "visible") return;
    fetching = true;
    try {
      const data = await request("/web-chat/messages");
      render(data);
      for (const reply of data.replies) {
        if (reply.state === "queued" && token && document.visibilityState === "visible") {
          await request("/web-chat/messages/" + encodeURIComponent(reply.id) + "/read", {});
        }
      }
      $("connection-status").textContent = "Подключено. Новые ответы появляются, пока вкладка открыта.";
    } catch (error) {
      $("connection-status").textContent = error.message;
      if (error.message === "Ключ не принят.") disconnect();
    } finally {
      fetching = false;
    }
  }

  $("connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    token = $("token").value.trim();
    $("token").value = "";
    try {
      const data = await request("/web-chat/messages");
      $("connect-form").hidden = true;
      $("disconnect").hidden = false;
      $("chat").hidden = false;
      render(data);
      await refresh();
      timer = setInterval(refresh, 3000);
    } catch (error) {
      token = "";
      $("connection-status").textContent = error.message;
    }
  });
  $("disconnect").addEventListener("click", disconnect);
  document.addEventListener("visibilitychange", refresh);
  $("send-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = $("message").value.trim();
    if (!text || !token) return;
    if (!pending || pending.text !== text) pending = { client_message_id: crypto.randomUUID(), text };
    $("send").disabled = true;
    $("send-status").textContent = "Обрабатываем сообщение…";
    try {
      await request("/web-chat/messages", pending);
      pending = undefined;
      $("message").value = "";
      $("send-status").textContent = "";
      await refresh();
    } catch (error) {
      $("send-status").textContent = error.message + " Повторная отправка не создаст дубликат.";
    } finally {
      $("send").disabled = false;
    }
  });
})();
