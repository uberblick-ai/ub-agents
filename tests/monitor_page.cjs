// Run the shipped page script with fake browser APIs and inspect its rendered output.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) {
    this.attributes[key] = value;
    if (key.startsWith("data-")) this.dataset[key.slice(5)] = value;
  }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  focus() { this.focused = true; }
  get textContent() { return this.children.map((child) => child.textContent ?? String(child)).join(""); }
  set textContent(value) { this.children = [value]; }
  descendants() { return this.children.filter((child) => child instanceof Element).flatMap((child) => [child, ...child.descendants()]); }
  querySelector(tag) { return this.descendants().find((child) => child.tag === tag); }
}

const script = fs.readFileSync(0, "utf8").split("<script>")[1].split("</script>")[0];
const stamp = "2026-11-06T06:00:00Z";
const unread = (repository, error = "network unavailable") => ({ repository, folder: `/checkout/${repository}`, error });
const successful = (repository, extra = {}) => ({
  ...unread(repository, null), generated_at: stamp, stop_labels: ["needs-human"], stop_outcomes: [],
  needs_human: [], running: [], expired: [], recent: [],
  eligible: { count: 0, rows: [], interval_seconds: 300, generated_at: stamp }, ...extra,
});

async function browser(repositories, storage = new Map()) {
  const nodes = Object.fromEntries(["app", "repository", "notify-hint", "poll-spinner", "freshness", "keys", "picker"]
    .map((id) => [id, new Element("div")]));
  const document = {
    title: "ub-agents", hidden: false, listeners: {},
    createElement: (tag) => new Element(tag),
    getElementById: (id) => nodes[id] ?? Object.values(nodes).flatMap((node) => node.descendants()).find((node) => node.attributes.id === id),
    querySelectorAll: () => [],
    querySelector: (selector) => Object.values(nodes).flatMap((node) => node.descendants())
      .find((node) => node.className?.split(" ").includes(selector.split(" ")[0].slice(1)))?.querySelector("select"),
    addEventListener(event, callback) { this.listeners[event] = callback; },
  };
  const notifications = [];
  class Notification {
    static permission = "granted";
    constructor(title, options) { notifications.push({ title, ...options }); }
  }
  let response = repositories;
  const requests = [];
  const context = vm.createContext({
    document, window: { Notification }, Notification, URLSearchParams,
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    navigator: { userAgent: "test" }, matchMedia: () => ({ matches: false }), setInterval: () => {},
    fetch: async (...args) => {
      requests.push(args);
      if (response instanceof Error) throw response;
      return { ok: true, json: async () => ({ generated_at: stamp, local_host: "laptop", rates: {}, repositories: response }) };
    },
  });
  await vm.runInContext(script, context);
  return {
    nodes, document, notifications, storage, requests,
    select(label) {
      return Object.values(nodes).flatMap((node) => node.descendants())
        .find((node) => node.attributes["aria-label"] === label);
    },
    change(label, value) { this.select(label).listeners.change({ target: { value } }); },
    act(repository, number, action) {
      const row = needsPanel(this).descendants().find((node) => node.className === "row" &&
        node.descendants().some((child) => child.attributes.href === item(repository, number).url));
      const button = row?.descendants().find((node) => node.tag === "button" && node.textContent === action);
      assert.ok(button, `${action} is available on ${repository} #${number}`);
      button.listeners.click();
    },
    reload() { return browser(response, storage); },
    async poll(repositories) {
      response = repositories;
      await document.listeners.keydown({ key: "r" });
      // The key handler starts the asynchronous poll without returning its promise.
      await new Promise(setImmediate);
    },
    picker() {
      nodes.keys.descendants().find((node) => node.attributes.id === "notify-when").listeners.click();
      return nodes.picker.textContent;
    },
    enableNotifications() { storage.set("ub-agents-monitor.notify", "true"); },
  };
}

const item = (repository, number) => ({ number, kind: "issue", title: `Task ${number}`,
  url: `https://github.com/${repository}/issues/${number}` });
const run = (repository, number, host) => ({ item: item(repository, number), id: `run-${number}`, host,
  started: stamp, expires: stamp, ended: stamp, renewed: stamp, agent: "worker", attempt: 1,
  runtime: "codex", actor: "loop", state: "running", status: "success", result: "success", cost: 1 });
const waiting = (repository, number) => ({ item: item(repository, number), labelled: true, labels: ["needs-human"] });
const stopped = (repository, number) => ({ ...waiting(repository, number),
  ask: { agent: "worker", outcome: "help", created: stamp, actions: ["Decide what to do."], options: [],
    summary: "Needs a decision", url: `${item(repository, number).url}#issuecomment-1` },
  notice: `${item(repository, number).url}#issuecomment-2`,
});
const busy = (repository, number, host) => successful(repository, {
  needs_human: [waiting(repository, number)], running: [run(repository, number + 1, host)],
  expired: [run(repository, number + 2, host)], recent: [run(repository, number + 3, host)],
  eligible: { count: 1, interval_seconds: 300, generated_at: stamp,
    rows: [{ item: item(repository, number + 4), glyph: "●", state: "next", pauses: {}, agents: [{ name: "worker" }] }] },
});
const options = (select) => select.children.map((node) => node.textContent);
const selected = (select) => select.children.find((node) => "selected" in node.attributes).attributes.value;
const headings = (page) => page.nodes.app.descendants().filter((node) => node.tag === "h2").map((node) => node.textContent);
const itemLinks = (page) => page.nodes.app.descendants().filter((node) => node.className === "cell item").map((node) => node.textContent);
const needsPanel = (page) => page.nodes.app.descendants().find((node) => node.tag === "section" && node.className.split(" ").includes("needs"));
const needsItems = (page) => needsPanel(page).descendants().filter((node) => node.className === "cell item").map((node) => node.textContent);
const dismissals = (page) => JSON.parse(page.storage.get("ub-agents-monitor.dismissed-stops") ?? "[]");

async function dismissalControlsAndMemory() {
  const backend = successful("org/backend", { needs_human: [stopped("org/backend", 1), waiting("org/backend", 2)],
    running: [run("org/backend", 3, "server")] });
  let page = await browser([backend]);
  page.enableNotifications();
  assert.equal(page.select("Needs human view"), undefined);
  page.act("org/backend", 1, "dismiss");
  assert.deepEqual(needsItems(page), ["#2Task 2"]);
  assert.equal(headings(page)[0], "Needs human · 1");
  assert.equal(page.document.title, "(1) needs human · backend");
  assert.deepEqual(options(page.select("Needs human view")), ["Open (1)", "Dismissed (1)"]);
  assert.equal(selected(page.select("Needs human view")), "open");
  assert.equal(needsPanel(page).children.at(-1).className, "needs-picker muted");
  assert.equal(dismissals(page).length, 1);
  page.act("org/backend", 2, "dismiss");
  assert.equal(headings(page)[0], "Needs human · 0");
  assert.match(needsPanel(page).textContent, /✓ Nothing waits for a person/);
  assert.equal(page.document.title, "1 running · backend");
  assert.deepEqual(options(page.select("Needs human view")), ["Open (0)", "Dismissed (2)"]);

  page.change("Needs human view", "dismissed");
  assert.equal(headings(page)[0], "Needs human · 2 dismissed");
  assert.deepEqual(needsItems(page), ["#1Task 1", "#2Task 2"]);
  assert.match(needsPanel(page).textContent, /Decide what to do\./);
  assert.ok(needsPanel(page).descendants().some((node) => node.attributes.href === backend.needs_human[0].notice));
  assert.equal(page.document.title, "1 running · backend");
  assert.equal(page.select("Needs human view").focused, true);
  assert.equal(page.requests.length, 1, "Dismiss and dropdown clicks make no request");
  page = await page.reload();
  assert.equal(selected(page.select("Needs human view")), "dismissed");
  assert.equal(headings(page)[0], "Needs human · 2 dismissed");
  page.act("org/backend", 1, "restore");
  assert.deepEqual(needsItems(page), ["#2Task 2"]);
  assert.equal(headings(page)[0], "Needs human · 1 dismissed");
  assert.equal(page.document.title, "(1) needs human · backend");
  assert.deepEqual(options(page.select("Needs human view")), ["Open (1)", "Dismissed (1)"]);
  page.act("org/backend", 2, "restore");
  assert.equal(page.select("Needs human view"), undefined);
  assert.equal(headings(page)[0], "Needs human · 2");
  assert.deepEqual(needsItems(page), ["#1Task 1", "#2Task 2"]);
  assert.equal(page.requests.length, 1, "Restore makes no request");
  assert.equal(page.notifications.length, 0);
  await page.poll([backend]);
  assert.equal(page.notifications.length, 0, "A restored stop stays quiet at the next poll");
  assert.equal((await page.reload()).select("Needs human view"), undefined);
  assert.deepEqual(dismissals(page), []);

  const idle = await browser([successful("org/backend", { needs_human: [waiting("org/backend", 1)] })]);
  idle.act("org/backend", 1, "dismiss");
  assert.equal(idle.document.title, "backend");
  const partial = await browser([backend, unread("org/studio")]);
  partial.act("org/backend", 1, "dismiss");
  partial.act("org/backend", 2, "dismiss");
  assert.match(needsPanel(partial).textContent, /Some repositories have not been read yet\./);
  assert.doesNotMatch(needsPanel(partial).textContent, /Nothing waits/);
  assert.equal(headings(partial)[0], "Needs human · 0");
}

async function dismissalIdentityAndNotifications() {
  // Exercise stops explained by labels, declared outcomes and blocked runs.
  for (const cause of ["label", "outcome", "blocked"]) {
    const source = successful("org/backend", { stop_outcomes: cause === "outcome" ? [{ name: "help", agents: ["worker"] }] : [] });
    let entry = stopped("org/backend", 1);
    if (cause === "blocked") entry = { ...entry, labelled: false, labels: ["blocked"], notice: null };
    const snapshot = () => [{ ...source, needs_human: [entry] }];
    let page = await browser([source]);
    page.enableNotifications();
    await page.poll(snapshot());
    assert.equal(page.notifications.length, 1);
    page.act("org/backend", 1, "dismiss");
    await page.poll(snapshot());
    assert.equal(page.notifications.length, 1, "A dismissed stop never notifies again");
    page = await page.reload();
    assert.equal(page.notifications.length, 0);
    assert.equal(headings(page)[0], "Needs human · 0");
    entry = { ...entry, item: { ...entry.item, title: "Renamed task" } };
    await page.poll(snapshot());
    assert.equal(headings(page)[0], "Needs human · 0", "Title changes keep the same stop dismissed");
    assert.equal(page.notifications.length, 0);

    const replacements = [
      () => ({ ...entry, ask: { ...entry.ask, url: `${entry.ask.url}-new-record` } }),
      () => ({ ...entry, notice: `${entry.item.url}#issuecomment-new-notice` }),
      () => ({ ...entry, labels: [...entry.labels, "review-needed"] }),
    ];
    for (const replace of replacements) {
      entry = replace();
      const previous = page.notifications.length;
      await page.poll(snapshot());
      assert.equal(headings(page)[0], "Needs human · 1", `${cause}: a new stop reopens the row`);
      assert.equal(page.notifications.length, previous + 1, `${cause}: a new stop notifies`);
      assert.equal(page.select("Needs human view"), undefined, "Only the current stop belongs to either list");
      page.act("org/backend", 1, "dismiss");
    }
    entry = { ...entry, labels: [...entry.labels].reverse() };
    const previous = page.notifications.length;
    await page.poll(snapshot());
    assert.equal(page.notifications.length, previous, "Label order is not a new stop");
    assert.equal(headings(page)[0], "Needs human · 0");
    page.change("Needs human view", "dismissed");
    page.act("org/backend", 1, "restore");
    await page.poll(snapshot());
    assert.equal(page.notifications.length, previous, "Restore does not notify");
  }
}

async function dismissalPruning() {
  const backend = successful("org/backend", { needs_human: [waiting("org/backend", 1)] });
  const studio = successful("org/studio", { needs_human: [waiting("org/studio", 1)] });
  const page = await browser([backend, studio]);
  page.act("org/backend", 1, "dismiss");
  page.act("org/studio", 1, "dismiss");
  const kept = dismissals(page);
  const partial = await browser([successful("org/backend"), unread("org/studio")], page.storage);
  assert.deepEqual(dismissals(partial), kept, "No pruning before every watched repository has been read");
  partial.change("Repository", "org/backend");
  await partial.poll([successful("org/backend"), { ...studio, error: "refresh failed" }]);
  assert.deepEqual(dismissals(partial), [kept[1]], "Prune across repositories even while one is selected");
  partial.change("Repository", "org/studio");
  assert.deepEqual(options(partial.select("Needs human view")), ["Open (0)", "Dismissed (1)"]);
  assert.match(partial.nodes.app.textContent, /Showing the last result/);
  partial.change("Needs human view", "dismissed");
  assert.deepEqual(needsItems(partial), ["#1Task 1"], "A failed read preserves its last dismissed rows");
  await partial.poll(new Error("server unavailable"));
  assert.deepEqual(dismissals(partial), [kept[1]], "A failed poll cannot prune anything");
  await partial.poll([successful("org/backend"), successful("org/studio")]);
  assert.deepEqual(dismissals(partial), []);
  assert.equal(partial.select("Needs human view"), undefined);
  assert.equal(headings(partial)[0], "Needs human · 0");
  assert.match(needsPanel(partial).textContent, /Nothing waits for a person/);
  assert.equal(partial.storage.get("ub-agents-monitor.needs-view"), '"open"');
}

async function dismissedRepositoryFiltering() {
  const backend = successful("org/backend", { needs_human: [waiting("org/backend", 1), waiting("org/backend", 2)] });
  const studio = successful("org/studio", { needs_human: [waiting("org/studio", 1), waiting("org/studio", 2)] });
  const page = await browser([backend, studio]);
  page.enableNotifications();
  page.act("org/backend", 1, "dismiss");
  assert.equal(page.document.title, "(3) needs human · backend, studio");
  page.change("Repository", "org/studio");
  assert.equal(page.select("Needs human view"), undefined);
  assert.deepEqual(needsItems(page), ["#1Task 1", "#2Task 2"]);
  page.act("org/studio", 2, "dismiss");
  assert.equal(page.document.title, "(1) needs human · studio");
  assert.deepEqual(options(page.select("Needs human view")), ["Open (1)", "Dismissed (1)"]);
  page.change("Needs human view", "dismissed");
  assert.deepEqual(needsItems(page), ["#2Task 2"]);
  page.change("Repository", "org/backend");
  assert.equal(headings(page)[0], "Needs human · 1 dismissed");
  assert.deepEqual(needsItems(page), ["#1Task 1"]);
  page.change("Repository", "");
  assert.deepEqual(needsItems(page), ["backend #1Task 1", "studio #2Task 2"]);
  assert.deepEqual(options(page.select("Needs human view")), ["Open (2)", "Dismissed (2)"]);
  page.change("Needs human view", "open");
  assert.deepEqual(needsItems(page), ["backend #2Task 2", "studio #1Task 1"]);
  page.change("Repository", "org/backend");
  await page.poll([backend, { ...studio, needs_human: [...studio.needs_human, waiting("org/studio", 3)] }]);
  assert.equal(page.notifications.length, 1, "Hidden repositories still notify for their open stops");
  assert.match(page.notifications[0].title, /studio #3/);
  assert.deepEqual(options(page.select("Needs human view")), ["Open (1)", "Dismissed (1)"]);
}

async function dismissalStorageFailures() {
  for (const value of ["not json", '"wrong type"', '{"wrong":"type"}', '[null,1]']) {
    const page = await browser([successful("org/backend", { needs_human: [waiting("org/backend", 1)] })],
      new Map([["ub-agents-monitor.dismissed-stops", value], ["ub-agents-monitor.needs-view", '"unknown"']]));
    assert.equal(headings(page)[0], "Needs human · 1");
    assert.equal(page.select("Needs human view"), undefined);
  }
  const unavailable = { get() { throw new Error("Storage blocked"); }, set() { throw new Error("Storage full"); } };
  const page = await browser([successful("org/backend", { needs_human: [waiting("org/backend", 1)] })], unavailable);
  page.act("org/backend", 1, "dismiss");
  page.change("Needs human view", "dismissed");
  assert.equal(headings(page)[0], "Needs human · 1 dismissed");
  page.act("org/backend", 1, "restore");
  assert.equal(headings(page)[0], "Needs human · 1");
}

async function repositoryFiltering() {
  const backend = busy("org/backend", 1, "backend-server");
  const studio = busy("org/studio", 11, "studio-server");
  backend.notices = ["Backend notice"];
  studio.notices = ["Studio notice"];
  studio.error = "studio unavailable";
  studio.eligible_error = "studio planning failed";
  const page = await browser([backend, studio]);
  assert.deepEqual(options(page.select("Repository")), ["All repositories (2)", "backend", "studio"]);
  assert.equal(selected(page.select("Repository")), "");
  assert.ok(itemLinks(page).includes("backend #1Task 1"));
  assert.deepEqual(options(page.select("Running machine")), ["All Machines (3)", "this machine", "backend-server", "studio-server"]);

  page.change("Repository", "org/backend");
  assert.equal(selected(page.select("Repository")), "org/backend");
  assert.equal(page.select("Repository").focused, true);
  assert.equal(page.storage.get("ub-agents-monitor.repository"), '"org/backend"');
  assert.deepEqual(headings(page), ["Needs human · 1", "Running · 1", "Eligible · 1", "Expired without a result · 1", "Recent activity · 1 runs · $1.00 · last 24 h"]);
  assert.deepEqual(itemLinks(page), ["#1Task 1", "#2Task 2", "#5Task 5", "#3Task 3", "#4Task 4"]);
  assert.match(page.nodes.app.textContent, /Backend notice/);
  assert.doesNotMatch(page.nodes.app.textContent, /Studio|studio|backend #|Nothing waits/);
  assert.equal(page.document.title, "(1) needs human · backend");
  const eligible = page.nodes.app.descendants().find((node) => node.className === "panel eligible");
  assert.ok(eligible.querySelector("h2").title.includes("Planned every 5 minutes"));
  assert.doesNotMatch(eligible.textContent, /backend/);
  assert.deepEqual(options(page.select("Running machine")), ["All Machines (2)", "this machine", "backend-server"]);
  assert.equal(selected((await page.reload()).select("Repository")), "org/backend");

  page.change("Repository", "org/studio");
  assert.match(page.nodes.app.textContent, /Couldn't refresh: studio unavailable. Showing the last result./);
  assert.match(page.nodes.app.textContent, /Couldn't refresh: Eligible: studio planning failed/);
  assert.match(page.nodes.app.textContent, /Studio notice/);
  assert.doesNotMatch(page.nodes.app.textContent, /Backend notice|studio:/);
  page.change("Running machine", "studio-server");
  page.change("Repository", "org/backend");
  assert.deepEqual(options(page.select("Running machine")), ["All Machines (3)", "this machine", "backend-server", "studio-server"]);
  assert.equal(selected(page.select("Running machine")), "studio-server");
  assert.match(page.nodes.app.textContent, /Running · 0/);
  assert.equal(page.document.title, "(1) needs human · backend");
  page.change("Repository", "");
  assert.equal(selected(page.select("Running machine")), "studio-server");
  assert.ok(itemLinks(page).includes("studio #11Task 11"));
  assert.match(page.nodes.app.textContent, /Needs human · 2/);
  assert.equal(page.document.title, "(2) needs human · backend, studio");
}

async function repositoryMemoryAndFailures() {
  const sources = [successful("org/backend"), unread("org/studio")];
  const page = await browser(sources);
  page.change("Repository", "org/backend");
  assert.match(page.nodes.app.textContent, /✓ Nothing waits for a person/);
  assert.doesNotMatch(page.nodes.app.textContent, /Couldn't|studio/);
  assert.equal(page.document.title, "backend");
  page.change("Repository", "org/studio");
  assert.match(page.nodes.app.textContent, /Couldn't read GitHub: network unavailable/);
  assert.doesNotMatch(page.nodes.app.textContent, /Nothing waits|Idle|Planning unavailable/);
  await page.poll([successful("org/backend"), successful("org/studio", { running: [run("org/studio", 1, "studio-server")] })]);
  assert.equal(page.document.title, "1 running · studio");
  await page.poll([successful("org/backend"), successful("org/studio")]);
  assert.equal(page.document.title, "studio");
  await page.poll([successful("org/backend"), successful("org/third")]);
  assert.equal(selected(page.select("Repository")), "");
  assert.equal(page.storage.get("ub-agents-monitor.repository"), "null");
  assert.equal(page.document.title, "backend, third");

  const removed = await browser([successful("org/backend"), successful("org/studio")],
    new Map([["ub-agents-monitor.repository", '"old/repository"']]));
  assert.equal(selected(removed.select("Repository")), "");
  const collisions = await browser([successful("org/repo"), successful("other/repo")]);
  assert.deepEqual(options(collisions.select("Repository")), ["All repositories (2)", "org/repo", "other/repo"]);
  collisions.change("Repository", "other/repo");
  assert.equal(selected((await collisions.reload()).select("Repository")), "other/repo");

  const single = await browser([successful("org/backend")],
    new Map([["ub-agents-monitor.repository", '"org/backend"']]));
  assert.equal(single.select("Repository"), undefined);
  assert.equal(single.nodes.repository.textContent, "· org/backend");
  const failed = await browser([unread("org/backend"), unread("org/studio", "login required")]);
  failed.change("Repository", "org/backend");
  assert.match(failed.nodes.app.textContent, /Couldn't read GitHub: network unavailable/);
  assert.doesNotMatch(failed.nodes.app.textContent, /login required|Nothing waits/);
  await failed.poll(new Error("monitor disconnected"));
  assert.match(failed.nodes.app.textContent, /Couldn't read GitHub: monitor disconnected/);
}

async function notificationsIgnoreRepositoryChoice() {
  const backend = successful("org/backend");
  const studio = successful("org/studio", { stop_labels: ["review-needed"],
    stop_outcomes: [{ name: "maintainer-merge", agents: ["integrator"] }] });
  const page = await browser([backend, studio]);
  page.enableNotifications();
  const picker = page.picker();
  page.change("Repository", "org/backend");
  assert.equal(page.nodes.picker.textContent, picker);
  assert.match(picker, /maintainer-merge \(integrator · studio only\)/);
  assert.match(picker, /needs-human or review-needed/);
  await page.poll([backend, { ...studio, needs_human: [waiting("org/studio", 12)] }]);
  assert.equal(page.document.title, "backend");
  assert.match(page.nodes.app.textContent, /Nothing waits for a person/);
  assert.doesNotMatch(page.nodes.app.textContent, /review-needed/);
  assert.equal(page.notifications.length, 1);
  assert.equal(page.notifications[0].title, "needs-human · studio #12 Task 12");
  page.change("Repository", "org/studio");
  page.change("Repository", "org/backend");
  await page.poll([backend, { ...studio, needs_human: [waiting("org/studio", 12)] }]);
  assert.equal(page.notifications.length, 1, "Changing the view leaves notification keys and baselines intact");
  assert.equal(page.storage.get("ub-agents-monitor.notify-events-v2"), undefined);
}

async function main() {
  for (const repositories of [[unread("org/backend")], [unread("org/backend"), unread("org/studio", "login required")]]) {
    const page = await browser(repositories);
    assert.match(page.nodes.app.textContent, /Couldn't read GitHub:/);
    for (const source of repositories) assert.ok(page.nodes.app.textContent.includes(source.error));
    if (repositories.length > 1) {
      assert.match(page.nodes.app.textContent, /backend:/);
      assert.match(page.nodes.app.textContent, /studio:/);
    }
    assert.equal(page.nodes.app.descendants().filter((node) => node.tag === "section").length, 0);
    assert.equal(page.document.title, "ub-agents");
    assert.match(page.picker(), /needs-human is added another way/);
    assert.doesNotMatch(page.nodes.app.textContent, /Nothing waits|Idle|Planning unavailable|No runs ended/);
    await page.poll(repositories.map((source) => successful(source.repository)));
    assert.match(page.nodes.app.textContent, /✓ Nothing waits for a person · stop labels: needs-human/);
    assert.doesNotMatch(page.nodes.app.textContent, /Couldn't/);
  }

  const page = await browser([successful("org/backend"), unread("org/studio")]);
  assert.match(page.nodes.app.textContent, /studio: Couldn't refresh: network unavailable. No successful read yet./);
  assert.doesNotMatch(page.nodes.app.textContent, /✓|Nothing waits for a person/);
  assert.match(page.nodes.app.textContent, /Needs human/);
  assert.match(page.picker(), /needs-human is added another way/);
  page.enableNotifications();
  const waiting = { item: { number: 12, kind: "issue", title: "Help", url: "https://github.com/org/studio/issues/12" },
    labelled: true, labels: ["needs-human"] };
  await page.poll([successful("org/backend"), successful("org/studio", { needs_human: [waiting] })]);
  assert.match(page.nodes.app.textContent, /studio #12Help/);
  assert.equal(page.document.title, "(1) needs human · studio");
  assert.equal(page.notifications.length, 0, "A repository's first good read sets its notification baseline");
  await page.poll([successful("org/backend"), successful("org/studio")]);
  assert.match(page.nodes.app.textContent, /✓ Nothing waits for a person/);
  assert.equal(page.document.title, "backend, studio");
  await page.poll([successful("org/backend"), successful("org/studio", { needs_human: [waiting] })]);
  assert.equal(page.notifications.length, 1);
  assert.match(page.notifications[0].title, /needs-human · studio #12/);

  const stale = await browser([successful("org/backend", { error: "refresh failed" })]);
  assert.match(stale.nodes.app.textContent, /Couldn't refresh: refresh failed. Showing the last result./);
  assert.match(stale.nodes.app.textContent, /Needs human/);
  assert.equal(stale.document.title, "backend");

  await repositoryFiltering();
  await repositoryMemoryAndFailures();
  await notificationsIgnoreRepositoryChoice();
  await dismissalControlsAndMemory();
  await dismissalIdentityAndNotifications();
  await dismissalPruning();
  await dismissedRepositoryFiltering();
  await dismissalStorageFailures();
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
