const bridge = window.AstrBotPluginPage;

const groupsEl = document.getElementById("groups");
const statusEl = document.getElementById("sync-status");
const toastEl = document.getElementById("toast");
const searchEl = document.getElementById("search");
const syncBtn = document.getElementById("sync-btn");

let state = { groups: [], sync: {} };
let toastTimer = null;

function toast(msg, isErr = false) {
  toastEl.textContent = msg;
  toastEl.className = isErr ? "toast err" : "toast";
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2200);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderStatus() {
  const s = state.sync || {};
  statusEl.textContent = "";
  const parts = [];
  const onoff = el("span", s.enabled ? "ok" : "off", s.enabled ? "自动同步：开" : "自动同步：关");
  parts.push(onoff);
  parts.push(document.createTextNode(" \u00b7 "));
  parts.push(el("b", "", "场景：" + (s.scopes || []).join("/")));
  parts.push(document.createTextNode(" \u00b7 面板前缀：" + JSON.stringify(state.prefix ?? "/")));
  if ((s.synced || []).length) {
    parts.push(document.createTextNode(" \u00b7 已同步 " + s.synced.length + " 个目标"));
  }
  if (s.last_result) {
    parts.push(document.createTextNode(" \u00b7 最近：" + s.last_result));
  }
  if (!s.enabled) {
    parts.push(document.createTextNode("（总开关已关，请到插件配置开启 command_panel_sync）"));
  }
  if (state.menu_only) {
    parts.push(document.createTextNode(" · 菜单模式：面板仅注册「菜单」入口，全部指令在菜单卡片中展示"));
  }
  if (state.prefix_dead) {
    parts.push(el("span", "off",
      " ⚠ 唤醒前缀仅含 /，QQ 会剥离开头的 /，面板指令将无法触发；请在 AstrBot 设置中把唤醒前缀加上 # 等符号"));
  }
  for (const node of parts) statusEl.appendChild(node);
}

function buildSwitch(cmd, groupOn) {
  const label = el("label", "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = !!cmd.enabled;
  input.disabled = !cmd.panel_ok || !groupOn;
  if (!cmd.panel_ok) input.title = "名称超宽，无法注册到面板";
  else if (!groupOn) input.title = "插件总开关已关闭";
  const slider = el("span", "slider");
  label.appendChild(input);
  label.appendChild(slider);
  input.addEventListener("change", async () => {
    input.disabled = true;
    try {
      await bridge.apiPost("cmdpanel/toggle", {
        module: cmd.module,
        name: cmd.name,
        enabled: input.checked,
      });
      cmd.enabled = input.checked;
      toast((input.checked ? "已开启 /" : "已关闭 /") + cmd.name + "，将自动同步");
    } catch (err) {
      input.checked = !input.checked;
      toast("保存失败：" + err.message, true);
    } finally {
      if (cmd.panel_ok) input.disabled = false;
    }
  });
  return label;
}

function buildRow(cmd, groupOn) {
  const row = el("div", "cmd-row" + (cmd.enabled && groupOn ? "" : " dim"));
  const info = el("div", "cmd-info");
  const head = el("div", "cmd-head");
  head.appendChild(el("code", "cmd-name", (state.prefix ?? "/") + cmd.name));
  if (cmd.is_alias) head.appendChild(el("span", "tag", "别名"));
  if (cmd.only_admin) head.appendChild(el("span", "tag tag-admin", "管理员"));
  if (!cmd.panel_ok) head.appendChild(el("span", "tag tag-wide", "超宽不注册"));
  else if (!state.menu_only && cmd.enabled && !cmd.selected) head.appendChild(el("span", "tag tag-wide", "容量外"));
  info.appendChild(head);
  info.appendChild(el("div", "cmd-desc", cmd.desc || "（无描述）"));
  row.appendChild(info);
  row.appendChild(buildSwitch(cmd, groupOn));
  return row;
}

function buildGroupSwitch(group, target, text, desc) {
  const wrap = el("span", "group-switch");
  wrap.title = desc;
  wrap.appendChild(el("span", "group-switch-label", text));
  const label = el("label", "switch");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = target === "card" ? !!group.card_enabled : !!group.panel_enabled;
  const slider = el("span", "slider");
  label.appendChild(input);
  label.appendChild(slider);
  wrap.appendChild(label);
  input.addEventListener("change", async () => {
    input.disabled = true;
    try {
      await bridge.apiPost("cmdpanel/toggle", {
        module: group.module,
        name: "",
        target,
        enabled: input.checked,
      });
      toast((input.checked ? "已开启「" : "已关闭「") + group.plugin + "」的" + text);
      await load();   // 指令开关是 插件开∧指令开 的合成态，以服务端重算为准
    } catch (err) {
      input.checked = !input.checked;
      toast("保存失败：" + err.message, true);
    } finally {
      input.disabled = false;
    }
  });
  return wrap;
}

function matches(cmd, kw) {
  return ("/" + cmd.name).toLowerCase().includes(kw)
    || (cmd.desc || "").toLowerCase().includes(kw);
}

function render() {
  renderStatus();
  groupsEl.textContent = "";
  const kw = searchEl.value.trim().toLowerCase();
  let totalShown = 0;
  for (const group of state.groups || []) {
    const cmds = (group.commands || []).filter(
      (cmd) => !kw || matches(cmd, kw) || group.plugin.toLowerCase().includes(kw)
    );
    if (!cmds.length) continue;
    totalShown += cmds.length;
    const section = el("section", "group");
    const header = el("div", "group-header");
    header.appendChild(el("span", "group-line"));
    header.appendChild(el("span", "group-name", group.plugin));
    const onCount = cmds.filter((c) => c.selected).length;
    header.appendChild(el("span", "group-count",
      group.panel_enabled ? onCount + "/" + cmds.length + " 上面板" : "面板已关闭"));
    header.appendChild(el("span", "group-line"));
    header.appendChild(buildGroupSwitch(group, "panel", "面板",
      "面板注册开关：关闭后该插件的指令不注册到 QQ 指令面板（菜单卡片不受影响）"));
    header.appendChild(buildGroupSwitch(group, "card", "卡片",
      "卡片显示开关：关闭后该插件不出现在「菜单」卡片消息中（面板注册不受影响）"));
    section.appendChild(header);
    const list = el("div", "cmd-list");
    for (const cmd of cmds) list.appendChild(buildRow(cmd, !!group.panel_enabled));
    section.appendChild(list);
    groupsEl.appendChild(section);
  }
  if (!totalShown) {
    groupsEl.appendChild(el("div", "empty", kw ? "没有匹配的指令" : "暂无可注册的指令"));
  }
}

async function load() {
  try {
    state = await bridge.apiGet("cmdpanel/overview");
    render();
  } catch (err) {
    toast("加载失败：" + err.message, true);
  }
}

searchEl.addEventListener("input", render);
syncBtn.addEventListener("click", async () => {
  syncBtn.disabled = true;
  try {
    await bridge.apiPost("cmdpanel/sync", {});
    toast("已触发全量同步");
    setTimeout(load, 1500);
  } catch (err) {
    toast("触发失败：" + err.message, true);
  } finally {
    syncBtn.disabled = false;
  }
});

await bridge.ready();
await load();
// 后台同步结果（last_result/synced）周期性回显
setInterval(load, 20000);
