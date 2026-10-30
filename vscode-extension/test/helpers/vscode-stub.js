'use strict';
/**
 * A recording stand-in for the `vscode` module, so the compiled extension code (dist/) can run
 * under plain Node. Only the surface the tested modules touch is implemented. Everything the
 * code shows or changes is recorded on `state` so tests can assert on what a user would see.
 */
const Module = require('module');

const state = {
  settings: {},          // 'dlne.serverUrl' -> value, read by workspace.getConfiguration
  updates: [],           // [key, value] pairs written through config.update()
  infoMessages: [],
  errorMessages: [],
  warningMessages: [],
  errorChoice: undefined, // what the user "clicks" on an error toast, e.g. 'Show Log'
  infoChoice: undefined,
  channels: [],          // every output channel created, with its recorded lines
  statusBarItems: [],
  commands: [],          // [id, ...args] for every vscode.commands.executeCommand()
  clipboard: undefined,  // the last text written through vscode.env.clipboard
  configListeners: [],   // what was handed to workspace.onDidChangeConfiguration
};

function reset() {
  // The DLNE log keeps its output channel between calls; a new case starts with none (the module is only asked if a test loaded it).
  try {
    const log = require.cache[require.resolve('../../dist/log.js')];
    if (log && typeof log.exports.forgetLogChannel === 'function') log.exports.forgetLogChannel();
  } catch {
    // dist is not built yet: nothing to forget
  }
  state.settings = {};
  state.updates = [];
  state.infoMessages = [];
  state.errorMessages = [];
  state.warningMessages = [];
  state.warningChoice = undefined;
  state.errorChoice = undefined;
  state.infoChoice = undefined;
  state.channels = [];
  state.statusBarItems = [];
  state.commands = [];
  state.clipboard = undefined;
  state.opened = [];
  state.configListeners = [];
}

/** Tells the listeners of workspace.onDidChangeConfiguration that these settings (full names, "dlne.ownGpu") changed. */
function fireConfigChange(...keys) {
  const event = { affectsConfiguration: (key) => keys.some((k) => k === key || k.startsWith(`${key}.`)) };
  [...state.configListeners].forEach((fn) => fn(event));
}

class EventEmitter {
  constructor() {
    this.listeners = [];
    this.event = (fn) => {
      this.listeners.push(fn);
      return { dispose() {} };
    };
  }
  fire(value) {
    this.listeners.forEach((fn) => fn(value));
  }
  dispose() {
    this.listeners = [];
  }
}

class Disposable {
  constructor(callOnDispose) {
    this.callOnDispose = callOnDispose;
  }
  dispose() {
    const call = this.callOnDispose;
    this.callOnDispose = undefined;
    if (call) call();
  }
}

class Position {
  constructor(line, character) {
    this.line = line;
    this.character = character;
  }
}

class Range {
  constructor(start, end) {
    this.start = start;
    this.end = end;
  }
}

class InlineCompletionItem {
  constructor(insertText, range) {
    this.insertText = insertText;
    this.range = range;
  }
}

const vscode = {
  EventEmitter,
  Disposable,
  Position,
  Range,
  InlineCompletionItem,
  ProgressLocation: { Notification: 15, Window: 10, SourceControl: 1 },
  StatusBarAlignment: { Left: 1, Right: 2 },
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  // Just enough of Uri for media files: a base ({ fsPath }) plus path segments, as a string.
  Uri: {
    parse(text) {
      return { toString: () => text };
    },
    joinPath(base, ...parts) {
      const text = [base.fsPath, ...parts].join('/');
      return { fsPath: text, toString: () => text };
    },
    file(fsPath) {
      return { scheme: 'file', fsPath, path: fsPath, toString: () => `file://${fsPath}` };
    },
  },
  env: {
    async openExternal(uri) {
      (state.opened = state.opened || []).push(String(uri));
      return true;
    },
    clipboard: {
      async writeText(text) {
        state.clipboard = text;
      },
    },
  },
  extensions: { getExtension: () => undefined },
  commands: {
    async executeCommand(id, ...args) {
      state.commands.push([id, ...args]);
    },
  },
  workspace: {
    workspaceFolders: undefined,
    onDidChangeConfiguration(listener) {
      state.configListeners.push(listener);
      return new Disposable(() => { state.configListeners = state.configListeners.filter((fn) => fn !== listener); });
    },
    getConfiguration(section) {
      return {
        get(key, fallback) {
          const full = `${section}.${key}`;
          return Object.prototype.hasOwnProperty.call(state.settings, full) ? state.settings[full] : fallback;
        },
        async update(key, value) {
          state.updates.push([`${section}.${key}`, value]);
          // As in the editor, updating a setting to undefined removes it, so it reads as its default again.
          if (value === undefined) delete state.settings[`${section}.${key}`];
          else state.settings[`${section}.${key}`] = value;
        },
        // A setting the test put in state.settings counts as one the user set (its global value).
        inspect(key) {
          const full = `${section}.${key}`;
          return Object.prototype.hasOwnProperty.call(state.settings, full) ? { key: full, globalValue: state.settings[full] } : { key: full };
        },
      };
    },
  },
  window: {
    createOutputChannel(name) {
      const channel = {
        name,
        lines: [],
        shown: 0,
        appendLine(line) { this.lines.push(line); },
        append(text) { this.lines.push(text); },
        show() { this.shown++; },
        dispose() {},
        text() { return this.lines.join('\n'); },
      };
      state.channels.push(channel);
      return channel;
    },
    async showInformationMessage(message, ...items) {
      state.infoMessages.push(message);
      return state.infoChoice;
    },
    async showErrorMessage(message, ...items) {
      state.errorMessages.push({ message, items });
      return state.errorChoice;
    },
    async showWarningMessage(message, ...items) {
      state.warningMessages.push({ message, items });
      return state.warningChoice;
    },
    async withProgress(_options, task) {
      return task({ report() {} });
    },
    createStatusBarItem() {
      const item = { text: '', tooltip: '', command: undefined, show() {}, hide() {}, dispose() {} };
      state.statusBarItems.push(item);
      return item;
    },
  },
};

const originalLoad = Module._load;
Module._load = function patched(request, parent, isMain) {
  if (request === 'vscode') return vscode;
  return originalLoad.call(this, request, parent, isMain);
};

module.exports = { vscode, state, reset, fireConfigChange };
