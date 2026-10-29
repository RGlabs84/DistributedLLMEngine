'use strict';
/**
 * The language-model corner of VS Code's API, on top of the shared vscode stub, for the tests of DLNE Swarm in the
 * Chat model picker. Require it right after ./vscode-stub and before anything from dist/: the compiled modules copy the
 * `vscode` module's members when they are loaded.
 *
 * `makeLm` mimics what VS Code 1.143 does with `vscode.lm.registerLanguageModelChatProvider` (read from its source and
 * seen in a probe on 1.143.1): the call returns a disposable at once and never throws in the extension, and core
 * checks the vendor later, on the main thread, where a vendor that package.json does not declare is only an error line
 * in VS Code's log ("Chat model provider uses UNKNOWN vendor X."). `errors` holds those lines, so a test can see what
 * the log would say.
 */
const { vscode } = require('./vscode-stub');

class LanguageModelTextPart {
  constructor(value) {
    this.value = value;
  }
}

class LanguageModelToolCallPart {
  constructor(callId, name, input) {
    this.callId = callId;
    this.name = name;
    this.input = input;
  }
}

class LanguageModelToolResultPart {
  constructor(callId, content) {
    this.callId = callId;
    this.content = content;
  }
}

class LanguageModelDataPart {
  constructor(data, mimeType) {
    this.data = data;
    this.mimeType = mimeType;
  }
  static image(data, mime) {
    return new LanguageModelDataPart(data, mime);
  }
  static text(value, mime = 'text/plain') {
    return new LanguageModelDataPart(new TextEncoder().encode(value), mime);
  }
  static json(value, mime = 'application/json') {
    return new LanguageModelDataPart(new TextEncoder().encode(JSON.stringify(value)), mime);
  }
}

// What VS Code sends a provider: roles are the numbers 1 (user), 2 (assistant), and 3 (system, a proposed value
// that arrives all the same).
const Role = { User: 1, Assistant: 2, System: 3 };
const message = (role, ...parts) => ({ role, content: parts.map((p) => (typeof p === 'string' ? new LanguageModelTextPart(p) : p)), name: undefined });

Object.assign(vscode, { LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart, LanguageModelDataPart });

/** A cancellation token: `cancel()` is what pressing Stop does. */
function makeToken() {
  let cancelled = false;
  let listeners = [];
  return {
    get isCancellationRequested() {
      return cancelled;
    },
    onCancellationRequested(fn) {
      listeners.push(fn);
      return { dispose: () => { listeners = listeners.filter((l) => l !== fn); } };
    },
    cancel() {
      cancelled = true;
      listeners.slice().forEach((fn) => fn());
    },
    get listenerCount() {
      return listeners.length;
    },
  };
}

function makeLm(declaredVendors) {
  const lm = {
    registrations: [],
    errors: [],
    registerLanguageModelChatProvider(vendor, provider) {
      if (!declaredVendors.includes(vendor)) {
        lm.errors.push(`Chat model provider uses UNKNOWN vendor ${vendor}.`);
      } else if (lm.registrations.some((r) => r.vendor === vendor && !r.disposed)) {
        lm.errors.push(`Chat model provider for vendor ${vendor} is already registered.`);
      }
      const registration = { vendor, provider, disposed: false };
      lm.registrations.push(registration);
      return { dispose() { registration.disposed = true; } };
    },
  };
  return lm;
}

module.exports = { LanguageModelTextPart, LanguageModelToolCallPart, LanguageModelToolResultPart, LanguageModelDataPart, Role, message, makeToken, makeLm };
