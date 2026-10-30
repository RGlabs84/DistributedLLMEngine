/**
 * The request side of DLNE Swarm in VS Code's model picker: what the Chat view hands a language model, turned into what
 * DlneClient sends the swarm, and a cheap token count for the Chat view's own budgeting. No vscode import, so tests
 * load it straight from dist/.
 *
 * VS Code gives a provider "parts" (text, tool calls, tool results, data such as an image). They are told apart by
 * their fields and not by class: the classes belong to the editor, and a part made by another editor build (or by a
 * test) is not an instance of the ones this extension sees.
 */
import type { ChatMessage, ToolDefinition } from './dlneClient';
import { PictureAttachment, pictureFromBytes, withoutOldPictures } from './pictures';

/** A message as VS Code gives it to a provider (vscode.LanguageModelChatRequestMessage, without the import). */
export interface RequestMessage {
  readonly role: number;
  readonly content: ReadonlyArray<unknown>;
}

/** A tool VS Code offers the model for this request (vscode.LanguageModelChatTool). */
export interface RequestTool {
  readonly name: string;
  readonly description?: string;
  readonly inputSchema?: object;
}

const ROLE_ASSISTANT = 2;
/**
 * LanguageModelChatMessageRole.System. It is only in the proposed typings, but the Chat view's agent sends its system
 * prompt with it, and a provider receives the number whether or not it has the proposal.
 */
const ROLE_SYSTEM = 3;

/** What the swarm counts for each image (the coordinator's engine_image_max_tokens). */
export const IMAGE_TOKENS = 1024;
/** Each message's own framing (role markers and the like), in characters: the figure the agent's estimate uses too. */
const MESSAGE_FRAMING_CHARS = 16;

export const IMAGE_REFUSED =
  'DLNE Swarm cannot read images right now (the swarm\'s engine has no way to see them). Take the image out of the request, or pick a model that can.';

/**
 * A request the swarm cannot take as it is, with the reason in plain words (an image today). Nothing was sent. It has
 * no stack, because the Chat view would print the stack under the sentence (see sentence() in modelProvider.ts).
 */
export class UnsupportedRequest extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'UnsupportedRequest';
    this.stack = undefined;
  }
}

type Loose = Record<string, any>;

const asPart = (raw: unknown): Loose | null => (raw !== null && typeof raw === 'object' ? (raw as Loose) : null);
const isToolResult = (p: Loose): boolean => typeof p.callId === 'string' && Array.isArray(p.content);
const isToolCall = (p: Loose): boolean => typeof p.callId === 'string' && typeof p.name === 'string' && 'input' in p;
const isData = (p: Loose): boolean => typeof p.mimeType === 'string' && ArrayBuffer.isView(p.data);

const isImage = (p: Loose): boolean => String(p.mimeType).toLowerCase().startsWith('image/');
/** Data that is plain text in another wrapper (a JSON or text part); anything else (a cache marker, say) says nothing to the model. */
const isTextData = (p: Loose): boolean => {
  const mime = String(p.mimeType).toLowerCase();
  return mime.startsWith('text/') || mime === 'application/json' || mime.endsWith('+json');
};
const decode = (data: ArrayBufferView): string => Buffer.from(data.buffer, data.byteOffset, data.byteLength).toString('utf8');

/**
 * The text in a list of parts, joined as written (parts are pieces of one text, not separate lines). An image is
 * refused. Data that is text counts as text. A part holding a structured value (prompt-tsx) is read as its JSON.
 * Anything else, thinking and cache markers included, is left out.
 */
function textOfParts(parts: ReadonlyArray<unknown>, onImage?: (part: Loose) => void): string {
  let text = '';
  for (const raw of parts) {
    const p = asPart(raw);
    if (!p) continue;
    if (typeof p.value === 'string') {
      text += p.value;
    } else if (isData(p)) {
      if (isImage(p)) {
        if (!onImage) throw new UnsupportedRequest(IMAGE_REFUSED);
        onImage(p);
      } else if (isTextData(p)) {
        text += decode(p.data);
      }
    } else if (p.value !== null && typeof p.value === 'object' && !Array.isArray(p.value)) {
      try {
        text += JSON.stringify(p.value);
      } catch {
        // a value that cannot be written out says nothing to the model
      }
    }
  }
  return text;
}

/** A tool call's input as the swarm wants it: JSON text. */
function argumentsOf(input: unknown): string {
  try {
    return JSON.stringify(input ?? {}) ?? '{}';
  } catch {
    return '{}';
  }
}

/**
 * The conversation VS Code sent, as messages for the swarm. Every `content` is a plain string, and that is on purpose:
 * the request pipeline hides secrets in message strings only, so a content array would go out unchecked.
 *
 * - The system prompt (role 3) becomes a real `system` message. (The old picker entry sent it as the assistant's.) Chat
 *   templates expect one system message and expect it first, so any others are joined into it.
 * - An assistant turn's tool calls become `tool_calls`, their input written as JSON.
 * - Each tool result becomes a `tool` message, in the order given and before the text of the same user turn, because
 *   VS Code sends the results of a step together with whatever the user added after it.
 * - An image is refused (UnsupportedRequest): the model does not read them, and leaving it out without a word would
 *   have the model answer as if it had seen it.
 */
export function toOpenAiMessages(messages: ReadonlyArray<RequestMessage>, options: { vision?: boolean } = {}): ChatMessage[] {
  const system: string[] = [];
  let userTurns = 0;
  const out: ChatMessage[] = [];
  const toolNames = new Map<string, string>(); // call id -> tool name, so its result can say which tool answered

  for (const message of messages) {
    const parts = Array.isArray(message?.content) ? message.content : [];
    if (message?.role === ROLE_SYSTEM) {
      const text = textOfParts(parts);
      if (text) system.push(text);
    } else if (message?.role === ROLE_ASSISTANT) {
      const calls: NonNullable<ChatMessage['tool_calls']> = [];
      const rest: unknown[] = [];
      for (const raw of parts) {
        const p = asPart(raw);
        if (p && isToolCall(p)) {
          calls.push({ id: p.callId, type: 'function', function: { name: p.name, arguments: argumentsOf(p.input) } });
          toolNames.set(p.callId, p.name);
        } else {
          rest.push(raw);
        }
      }
      const content = textOfParts(rest);
      if (content || calls.length > 0) {
        out.push(calls.length > 0 ? { role: 'assistant', content, tool_calls: calls } : { role: 'assistant', content });
      }
    } else {
      const rest: unknown[] = [];
      for (const raw of parts) {
        const p = asPart(raw);
        if (p && isToolResult(p)) {
          const name = toolNames.get(p.callId);
          out.push({ role: 'tool', tool_call_id: p.callId, ...(name ? { name } : {}), content: textOfParts(p.content) });
        } else {
          rest.push(raw);
        }
      }
      // A picture in a user message goes with it while the swarm's engine can read pictures (4d); a tool result's, or
      // an assistant's, never does.
      const images: PictureAttachment[] = [];
      const content = textOfParts(rest, options.vision ? (part) => {
        const checked = pictureFromBytes(`picture ${images.length + 1}`, new Uint8Array(part.data.buffer, part.data.byteOffset, part.data.byteLength));
        if (!checked.ok) throw new UnsupportedRequest(`DLNE Swarm could not take picture ${images.length + 1}: ${checked.error}.`);
        images.push(checked.picture);
      } : undefined);
      if (content || images.length > 0) {
        userTurns++;
        out.push(images.length > 0 ? { role: 'user', content, images, pictureTurn: userTurns } : { role: 'user', content });
      }
    }
  }
  if (system.length > 0) out.unshift({ role: 'system', content: system.join('\n\n') });
  // The server takes MAX_PICTURES a request, all messages together, and a picture of an old turn is a note.
  return out.some((m) => m.images && m.images.length > 0) ? withoutOldPictures(out, userTurns) : out;
}

/** The tools VS Code offers, as the swarm's request names them. A tool with no schema takes no arguments. */
export function toOpenAiTools(tools: ReadonlyArray<RequestTool> | undefined): ToolDefinition[] {
  const out: ToolDefinition[] = [];
  for (const tool of tools ?? []) {
    if (!tool || typeof tool.name !== 'string' || !tool.name) continue;
    out.push({
      type: 'function',
      function: {
        name: tool.name,
        description: typeof tool.description === 'string' ? tool.description : '',
        parameters: (tool.inputSchema as Record<string, any> | undefined) ?? { type: 'object', properties: {} },
      },
    });
  }
  return out;
}

/** The characters a request puts into the prompt: message text, tool-call names and arguments, the tool list. */
export function chatChars(messages: ReadonlyArray<ChatMessage>, tools: ReadonlyArray<ToolDefinition>): number {
  let chars = tools.length > 0 ? JSON.stringify(tools).length : 0;
  for (const m of messages) {
    chars += (m.content?.length ?? 0) + MESSAGE_FRAMING_CHARS + (m.images?.length ?? 0) * IMAGE_TOKENS * 3;
    for (const tc of m.tool_calls ?? []) {
      chars += (tc.function?.name?.length ?? 0) + (tc.function?.arguments?.length ?? 0) + MESSAGE_FRAMING_CHARS;
    }
  }
  return chars;
}

/** What one part of a request costs the prompt: characters, and images (which are counted in tokens, not characters). */
function addPartCost(raw: unknown, cost: { chars: number; images: number }): void {
  const p = asPart(raw);
  if (!p) return;
  if (isToolResult(p)) {
    for (const inner of p.content) addPartCost(inner, cost);
  } else if (isToolCall(p)) {
    cost.chars += p.name.length + argumentsOf(p.input).length;
  } else if (typeof p.value === 'string') {
    cost.chars += p.value.length;
  } else if (isData(p)) {
    if (isImage(p)) cost.images++;
    else if (isTextData(p)) cost.chars += p.data.byteLength;
  } else if (p.value !== null && typeof p.value === 'object' && !Array.isArray(p.value)) {
    cost.chars += argumentsOf(p.value).length;
  }
}

const MIN_CHARS_PER_TOKEN = 2;
const MAX_CHARS_PER_TOKEN = 5;
/** After an overflow, texts are counted this much higher, so the Chat view trims its prompt sooner. */
const OVERFLOW_COUNT_FACTOR = 0.85;

/**
 * Counts tokens for the Chat view, which asks once per string (hundreds of times for one request's tool list), so it is
 * only characters divided by a figure. The figure starts as the coordinator's own ratio and is moved toward what the
 * swarm reports about each prompt it really read, the way the agent loop does (agentLoop.ts calibrate).
 */
export class TokenEstimator {
  constructor(private cpt: number) {}

  get charsPerToken(): number {
    return this.cpt;
  }

  /** Tokens in a text, or in a message: its text, its tool calls and results (an image counts IMAGE_TOKENS). */
  tokens(input: string | RequestMessage): number {
    if (typeof input === 'string') return Math.ceil(input.length / this.cpt);
    const cost = { chars: MESSAGE_FRAMING_CHARS, images: 0 };
    for (const part of Array.isArray(input?.content) ? input.content : []) addPartCost(part, cost);
    return Math.ceil(cost.chars / this.cpt) + cost.images * IMAGE_TOKENS;
  }

  /** A reply said how many tokens its prompt was: move the figure toward what that shows. */
  calibrate(sentChars: number, promptTokens: number | undefined): void {
    if (!promptTokens || promptTokens < 200) return;
    const measured = sentChars / promptTokens;
    // A figure far from the current one is not believed: the coordinator may have rewritten the prompt it was given.
    if (measured < this.cpt / 3 || measured > this.cpt * 3) return;
    this.cpt = Math.min(MAX_CHARS_PER_TOKEN, Math.max(MIN_CHARS_PER_TOKEN, 0.7 * this.cpt + 0.3 * measured));
  }

  /** The swarm said a prompt did not fit: count higher from now on (the next real prompt size corrects it again). */
  noteOverflow(): void {
    this.cpt = Math.max(MIN_CHARS_PER_TOKEN, this.cpt * OVERFLOW_COUNT_FACTOR);
  }
}
