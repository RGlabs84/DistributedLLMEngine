'use strict';
/**
 * Stand-ins for the two sides of the DLNE sidebar, so a task can be run end to end the way VS Code runs it:
 *
 * - the page: the sidebar's own script, run under `vm` against a small DOM that keeps a real tree (an element that is
 *   removed is gone from its parent, text and HTML set on it are kept), with timers the test runs by hand;
 * - the view: what VS Code hands the provider. It follows what the typings promise: a hidden view is delivered nothing
 *   (postMessage resolves false), a view that does not retain its context loses its page when hidden and gets a new
 *   one, with the saved state, when shown, and messages and state cross as JSON, as they do between processes (so an
 *   Error posted to the page arrives as `{}`).
 *
 * Nothing here talks to a real swarm or a real editor. The caller requires ./vscode-stub first.
 */
const vm = require('node:vm');

// ---- a DOM with a real tree --------------------------------------------------------------------------------------

const escapeText = (text) => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

class FakeElement {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.parent = null;
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.classes = new Set();
    this.ownText = '';
    this.ownHtml = '';
    this.value = '';
    this.disabled = false;
    this.open = false;
    this.title = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
  }

  get className() { return [...this.classes].join(' '); }
  set className(value) { this.classes = new Set(String(value).split(/\s+/).filter(Boolean)); }
  get classList() {
    const classes = this.classes;
    return {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      toggle: (name, force) => { if (force ?? !classes.has(name)) classes.add(name); else classes.delete(name); },
      contains: (name) => classes.has(name),
    };
  }

  get parentNode() { return this.parent; }
  get parentElement() { return this.parent; }

  appendChild(child) {
    child.remove();
    child.parent = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  prepend(...nodes) { nodes.reverse().forEach((n) => { n.remove(); n.parent = this; this.children.unshift(n); }); }
  insertBefore(child, ref) {
    child.remove();
    child.parent = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(child); else this.children.splice(at, 0, child);
    return child;
  }
  replaceChildren(...nodes) { this.clear(); nodes.forEach((n) => this.appendChild(n)); }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  clear() {
    this.children.forEach((c) => { c.parent = null; });
    this.children = [];
  }

  get textContent() { return this.ownText + this.children.map((c) => c.textContent).join(''); }
  set textContent(value) { this.clear(); this.ownHtml = ''; this.ownText = String(value); }
  get innerHTML() {
    const inner = this.ownHtml || escapeText(this.ownText);
    return inner + this.children.map((c) => c.outerHTML).join('');
  }
  set innerHTML(value) { this.clear(); this.ownText = ''; this.ownHtml = String(value); }
  get outerHTML() {
    const cls = this.className ? ` class="${this.className}"` : '';
    return `<${this.tag}${cls}>${this.innerHTML}</${this.tag}>`;
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }

  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  /** As in a browser, an event reaches the listeners of the element it happened on, then of each parent up to the page. */
  dispatch(type, event = {}) {
    let stopped = false;
    const e = { target: this, preventDefault() {}, stopPropagation() { stopped = true; }, ...event };
    for (let node = this; node && !stopped; node = node.parent) (node.listeners[type] || []).forEach((fn) => fn(e));
  }
  /** As in a browser, click() fires the listeners whether or not the element is displayed. */
  click() { if (!this.disabled) this.dispatch('click'); }
  focus() {}
  scrollIntoView() {}
  // A canvas that draws nothing and answers a data URI of the type asked (the panel's picture code, tests of 4d).
  getContext() { return { fillStyle: '', fillRect() {}, drawImage() {} }; }
  toDataURL(type = 'image/png') { return `data:${type};base64,${'QUJD'.repeat(4)}`; }

  matches(selector) {
    const m = /^([a-z0-9]*)((?:\.[\w-]+)*)$/i.exec(selector);
    if (!m) return false;
    if (m[1] && m[1].toLowerCase() !== this.tag) return false;
    return m[2].split('.').filter(Boolean).every((c) => this.classes.has(c));
  }
  descendants() { return this.children.flatMap((c) => [c, ...c.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter((d) => d.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let node = this; node; node = node.parent) if (node.matches(selector)) return node;
    return null;
  }
}

/** The tree below `root` as indented lines, so two pages (and a failing assertion) can be compared as text. */
function outline(root, depth = 0) {
  return root.children.map((child) => {
    const label = [child.tag, ...child.classes].join('.');
    const text = child.ownHtml ? ` html:${child.ownHtml}` : child.ownText ? ` ${JSON.stringify(child.ownText)}` : '';
    const state = [child.open ? 'open' : '', child.disabled ? 'disabled' : ''].filter(Boolean).join(',');
    return `${'  '.repeat(depth)}${label}${state ? ` [${state}]` : ''}${text}\n${outline(child, depth + 1)}`;
  }).join('');
}

// ---- the page ----------------------------------------------------------------------------------------------------

/**
 * Runs the sidebar's script as a page. `state` is what VS Code hands a page it makes anew (what the old page kept with
 * setState); `toHost` receives what the page posts, as JSON; `keepState` is told what setState keeps.
 */
function createPage(html, { state, toHost, keepState = () => {}, globals = {} }) {
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const byId = {};
  const timers = new Map();
  const errors = [];
  const listeners = {};
  let timerId = 0;
  const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
  const sandbox = {
    document: {
      activeElement: null,
      getElementById: (id) => (byId[id] ??= new FakeElement('div')),
      createElement: (tag) => new FakeElement(tag),
      querySelector: () => new FakeElement('div'),
      querySelectorAll: () => [],
      body: new FakeElement('body'),
      addEventListener() {},
    },
    acquireVsCodeApi: () => ({
      postMessage: (message) => toHost(clone(message)),
      getState: () => clone(state),
      setState: (value) => { state = clone(value); keepState(state); },
    }),
    window: { addEventListener: (type, fn) => { listeners[type] = fn; } },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: (id) => { timers.delete(id); },
    setInterval: () => 0,
    clearInterval() {},
    console: { error: (...args) => errors.push(args.map(String).join(' ')), log() {}, warn() {} },
    navigator: {},
  };
  // What the HTML itself hides at the start (the Stop button, the busy hint, the queue banner) is hidden here too.
  for (const [, id] of html.matchAll(/<[a-z]+[^>]*\bid="([^"]+)"[^>]*\bstyle="display:\s*none;?"/g)) {
    byId[id] = new FakeElement('div');
    byId[id].style.display = 'none';
  }
  Object.assign(sandbox, globals);       // what a browser has that the fake page does not (FileReader, Image...), when a test needs it
  vm.createContext(sandbox);
  for (const script of scripts) vm.runInContext(script, sandbox);

  const el = (id) => byId[id];
  // (A page without the element, such as one from before it existed, answers 'absent' instead of throwing.)
  const shown = (id) => (!el(id) ? 'absent' : el(id).style.display === 'none' ? 'hidden' : 'shown');
  const page = {
    errors,
    el,
    /** Delivers a message to the page. */
    receive(message) { listeners.message({ data: clone(message) }); },
    /** Runs the timers the page set (the answer is drawn at most every 50 ms). */
    flushTimers() {
      while (timers.size) {
        const [id, fn] = timers.entries().next().value;
        timers.delete(id);
        fn();
      }
    },
    /** What the chat shows. */
    chat() { page.flushTimers(); return outline(el('chat-container')); },
    /** The elements in the chat with this class. */
    find(className) { return el('chat-container').descendants().filter((e) => e.classes.has(className)); },
    controls() {
      return {
        send: shown('send-btn'),
        stop: shown('stop-btn'),
        stopLabel: el('stop-btn').textContent,
        stopDisabled: el('stop-btn').disabled,
        hint: shown('busy-hint'),
        input: el('prompt-input').value,
        queue: shown('queue-banner'),
      };
    },
    type(text) { el('prompt-input').value = text; },
    pressEnter() { el('prompt-input').dispatch('keydown', { key: 'Enter', shiftKey: false }); },
    clickSend() { el('send-btn').click(); },
    clickStop() { el('stop-btn').click(); },
    clickClear() { el('clear-btn').click(); },
    savedState: () => clone(state),
  };
  return page;
}

// ---- the view ----------------------------------------------------------------------------------------------------

/**
 * A WebviewView for `provider`. `retain`: the view keeps its page while hidden (registerWebviewViewProvider's
 * retainContextWhenHidden); without it a hidden view's page is thrown away.
 */
function openView(provider, { retain = true, globals = {} } = {}) {
  const sent = [];          // everything the host posted: { message, delivered }
  const fromPage = [];      // everything the page posted
  const pending = [];       // what the host is still doing for those messages
  const visibility = [];
  const disposal = [];
  let handler;
  let pageState;            // what the page's setState kept: VS Code hands it to a page it makes anew
  let loading = null;       // messages sent while a page is loading: delivered, in order, once it has run

  const view = {
    visible: true,
    disposed: false,
    failPosts: false,
    page: null,
    sent,
    fromPage,
    webview: {
      options: {},
      html: '',
      cspSource: 'test-csp',
      asWebviewUri: (u) => u,
      onDidReceiveMessage: (fn) => { handler = fn; return { dispose() {} }; },
      postMessage(message) {
        if (view.disposed || view.failPosts) throw new Error('Webview is disposed');
        const copy = JSON.parse(JSON.stringify(message));
        if (!view.visible) {
          sent.push({ message: copy, delivered: false });
          return Promise.resolve(false);
        }
        sent.push({ message: copy, delivered: true });
        if (loading) loading.push(copy); else view.page.receive(copy);
        return Promise.resolve(true);
      },
    },
    onDidChangeVisibility: (fn) => { visibility.push(fn); return { dispose() {} }; },
    onDidDispose: (fn) => { disposal.push(fn); return { dispose() {} }; },

    /** VS Code resolves the view: the provider sets the page's HTML and the page loads. */
    mount() {
      loading = [];
      provider.resolveWebviewView(view, {}, {});
      makePage();
    },
    /** The view is hidden: another side bar tab, Ctrl+B. */
    hide() {
      view.visible = false;
      if (!retain) view.page = null;
      visibility.forEach((fn) => fn());
    },
    /** The view is shown again (a page is made anew first, as VS Code does, when it did not keep the old one). */
    show() {
      view.visible = true;
      if (!view.page) { loading = []; makePage(); }
      visibility.forEach((fn) => fn());
    },
    /** The webviews are reloaded (Developer: Reload Webviews): a new page, with no word to the extension. */
    reloadPage() {
      loading = [];
      makePage();
    },
    dispose() {
      view.disposed = true;
      disposal.forEach((fn) => fn());
    },
    /** Waits for everything the host is doing for the messages the page posted (a whole task, for a sent message). */
    async idle() {
      while (pending.length) await pending.shift();
    },
    /** A message to the host as a page would post it, without the page's own checks (a page that was behind). */
    toHost(message) {
      fromPage.push(message);
      const done = handler(JSON.parse(JSON.stringify(message)));
      pending.push(done);
      return done;
    },
    /** The messages the host posted that were this type. */
    posted(type) { return sent.map((s) => s.message).filter((m) => m.type === type); },
  };

  function makePage() {
    const queued = loading || [];
    loading = queued;
    view.page = createPage(view.webview.html, {
      globals,
      state: pageState,
      keepState: (s) => { pageState = s; },
      toHost: (message) => {
        fromPage.push(message);
        const done = handler(message);
        pending.push(done);
        return done;
      },
    });
    loading = null;
    queued.forEach((m) => view.page.receive(m));
  }

  return view;
}

// ---- the swarm and the host --------------------------------------------------------------------------------------

function deferred() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}

/** Polls until `check()` is true (the host is asynchronous: a task takes a few ticks to reach its next step). */
async function until(check, what = 'the condition', ms = 3000) {
  const end = Date.now() + ms;
  while (!check()) {
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}

let callCounter = 0;
/** A tool call as the client hands it to the agent loop. */
const toolCall = (name, args) => ({ id: `call_${++callCounter}`, name, arguments: JSON.stringify(args) });

/**
 * A client whose answers are scripted: step n answers the n-th request (the last repeats). A step is a function of the
 * request ({ messages, tools, signal, onDelta, onLoadingProgress, index }) returning the reply, and may take its time.
 */
function scriptedClient(steps) {
  const client = {
    calls: [],
    lastKnownClusterStatus: () => null,
    currentQueue: () => null,
    onClusterStatus: () => () => {},
    async streamChat(messages, tools, onDelta, _onToolCallStart, signal, onLoadingProgress) {
      const call = { messages, tools, signal };
      client.calls.push(call);
      const index = client.calls.length - 1;
      return steps[Math.min(index, steps.length - 1)]({ ...call, onDelta, onLoadingProgress, index });
    },
  };
  return client;
}

/** A step that says `text` as it streams, then asks for these tool calls (none: it is the answer). */
const say = (text, toolCalls = []) => async ({ onDelta }) => {
  if (text) onDelta(text);
  return { content: text, toolCalls, finishReason: toolCalls.length ? 'tool_calls' : 'stop' };
};

/**
 * A step that waits for `gate` (the model is thinking), then does `then`. A Stop ends it at once, as the real client's
 * request ends when its signal fires. `before` runs first (a status line, some words).
 */
const waiting = (gate, then, before = () => {}) => (ctx) => new Promise((resolve, reject) => {
  before(ctx);
  ctx.signal.addEventListener('abort', () => resolve({ content: '', toolCalls: [] }));
  gate.promise.then(() => Promise.resolve(then(ctx)).then(resolve, reject));
});

module.exports = { FakeElement, outline, createPage, openView, deferred, until, toolCall, scriptedClient, say, waiting };
