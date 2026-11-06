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

async function browser(repositories) {
  const nodes = Object.fromEntries(["app", "repository", "notify-hint", "poll-spinner", "freshness", "keys", "picker"]
    .map((id) => [id, new Element("div")]));
  const document = {
    title: "ub-agents", hidden: false, listeners: {},
    createElement: (tag) => new Element(tag),
    getElementById: (id) => nodes[id] ?? Object.values(nodes).flatMap((node) => node.descendants()).find((node) => node.attributes.id === id),
    querySelectorAll: () => [],
    addEventListener(event, callback) { this.listeners[event] = callback; },
  };
  const storage = new Map();
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
    fetch: async () => ({ ok: true, json: async () => ({ generated_at: stamp, local_host: "laptop", rates: {}, repositories: response }) }),
  });
  await vm.runInContext(script, context);
  return {
    nodes, document, notifications,
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
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
