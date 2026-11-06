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
  const context = vm.createContext({
    document, window: { Notification }, Notification, URLSearchParams,
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    navigator: { userAgent: "test" }, matchMedia: () => ({ matches: false }), setInterval: () => {},
    fetch: async () => {
      if (response instanceof Error) throw response;
      return { ok: true, json: async () => ({ generated_at: stamp, local_host: "laptop", rates: {}, repositories: response }) };
    },
  });
  await vm.runInContext(script, context);
  return {
    nodes, document, notifications, storage,
    select(label) {
      return Object.values(nodes).flatMap((node) => node.descendants())
        .find((node) => node.attributes["aria-label"] === label);
    },
    change(label, value) { this.select(label).listeners.change({ target: { value } }); },
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
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
