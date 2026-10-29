/**
 * What the model picker path does about private files, and what it cannot.
 *
 * In `@dlne`, DLNE's own tools read the files, so they can refuse the ones the privacy rules exclude (a credentials file, anything in
 * .gitignore or .dlneignore) and nothing of them is ever read. In the model picker it is VS Code's tools that read, and what
 * reaches DLNE is text. Two things are still possible, and done here, on the converted messages just before they leave:
 *
 *  - a tool result is swapped for the notice `read_file` gives when the call that produced it named an excluded file (the
 *    messages hold every call's arguments, so the file is known: tool_call_id -> the assistant's tool call), and an attached file
 *    (`<attachment ... filePath="...">` in the user's text) is emptied the same way;
 *  - the tool list (descriptions and schemas; an MCP server's can say anything) is redacted like the messages.
 *
 * What stays out of reach: a file read without being named (a search that lists matching lines, `cat .env` in a terminal, text VS
 * Code puts in the prompt by itself). There the secret patterns are all that is left, and the first-use notice says so.
 */
import { fileURLToPath } from 'url';
import type { ChatMessage, ToolDefinition } from './dlneClient';
import { excludedNotice, excludedReason, redactText } from '../privacy';
import { resolvePath } from '../agent/tools';

/** The arguments of a tool that name a file, as VS Code's own tools and MCP servers spell them. */
const FILE_ARGUMENTS = ['filePath', 'filePaths', 'file_path', 'path', 'paths', 'uri', 'uris', 'file', 'files'];
/** Most files looked at in one call's arguments (a call that names more than this is not an ordinary one). */
const MAX_FILES_PER_CALL = 100;

/** The files a tool call's arguments (JSON text) name, as written. Anything that is not JSON, or names none, gives []. */
export function filesNamedBy(argumentsJson: string | undefined): string[] {
  if (!argumentsJson) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(argumentsJson);
  } catch {
    return [];
  }
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return [];
  const named: string[] = [];
  for (const key of FILE_ARGUMENTS) {
    const value = (parsed as Record<string, unknown>)[key];
    if (typeof value === 'string' && value.trim()) {
      named.push(value);
    } else if (Array.isArray(value)) {
      for (const item of value) if (typeof item === 'string' && item.trim()) named.push(item);
    }
  }
  return named.slice(0, MAX_FILES_PER_CALL);
}

/** The path on this machine that `named` points at: a file: URI, another scheme's path, `~/x`, or a path relative to the workspace. */
function localPath(named: string, workspace: string | undefined): string {
  let text = named.trim();
  if (/^file:/i.test(text)) {
    try {
      text = fileURLToPath(text);
    } catch {
      // not a well-formed file: URI; judged as written
    }
  } else if (/^[a-z][a-z0-9+.-]+:\//i.test(text)) {
    // git:, vscode-notebook-cell:, vscode-remote:: the file behind it is what the rules are about, whatever the scheme
    try {
      text = decodeURIComponent(new URL(text).pathname);
    } catch {
      // judged as written
    }
  }
  return resolvePath(workspace, text);
}

/** Why `named` is excluded, or null. Fails closed, as read_file does: a check that cannot be made counts as excluded. */
function exclusionOf(named: string, workspace: string | undefined): string | null {
  try {
    return excludedReason(localPath(named, workspace));
  } catch {
    return 'the privacy check could not be completed';
  }
}

/** The notice for the first excluded file a call names, or null when none is. */
function refusalFor(argumentsJson: string | undefined, workspace: string | undefined): string | null {
  for (const named of filesNamedBy(argumentsJson)) {
    const reason = exclusionOf(named, workspace);
    if (reason) return excludedNotice(named, reason);
  }
  return null;
}

/** The value of attribute `name` in the text of a tag's attributes, double or single quoted. */
function attributeOf(attributes: string, name: string): string | null {
  const found = new RegExp(`(?:^|\\s)${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`, 'i').exec(attributes);
  return found ? (found[1] ?? found[2] ?? null) : null;
}

/** Where an attachment's text ends when its closing tag is missing: at the next thing VS Code puts after it, or the end. */
const AFTER_ATTACHMENT = /<\/attachment>|<\/attachments>|<userRequest>|<user_query>/i;

/**
 * `text` with the contents of every attached file the privacy rules exclude replaced by the notice. VS Code writes an attached
 * file as `<attachment id="..." filePath="...">` and its text and `</attachment>`; a tag that names the file and holds nothing
 * (`<attachment ... />`) is left as it is.
 */
export function withoutExcludedAttachments(text: string, workspace: string | undefined): string {
  if (!/<attachment\b/i.test(text)) return text;
  const opening = /<attachment\b([^>]*?)(\/?)>/gi;
  let out = '';
  let from = 0;
  for (let tag = opening.exec(text); tag; tag = opening.exec(text)) {
    if (tag.index < from) continue; // inside text already replaced
    const named = attributeOf(tag[1], 'filePath');
    if (!named || tag[2] === '/') continue;
    const reason = exclusionOf(named, workspace);
    if (!reason) continue;
    const bodyStart = tag.index + tag[0].length;
    const rest = AFTER_ATTACHMENT.exec(text.slice(bodyStart));
    const bodyEnd = rest ? bodyStart + rest.index : text.length;
    out += `${text.slice(from, bodyStart)}\n${excludedNotice(named, reason)}\n`;
    from = bodyEnd;
    opening.lastIndex = bodyEnd;
  }
  return out + text.slice(from);
}

/**
 * The conversation with the contents of excluded files taken out: a tool result whose call named one becomes the notice
 * `read_file` gives, and an excluded attachment in a user's text is emptied. `workspace` is the folder a relative path is
 * read against. The messages given are not changed.
 */
export function withoutExcludedFiles(messages: ChatMessage[], workspace: string | undefined): ChatMessage[] {
  const argumentsOf = new Map<string, string>(); // tool call id -> its arguments
  return messages.map((message) => {
    if (message.role === 'assistant') {
      for (const call of message.tool_calls ?? []) {
        if (call.id) argumentsOf.set(call.id, call.function?.arguments ?? '');
      }
      return message;
    }
    if (message.role === 'tool' && message.tool_call_id) {
      const refusal = refusalFor(argumentsOf.get(message.tool_call_id), workspace);
      return refusal ? { ...message, content: refusal } : message;
    }
    if (message.role === 'user' && typeof message.content === 'string') {
      const content = withoutExcludedAttachments(message.content, workspace);
      return content === message.content ? message : { ...message, content };
    }
    return message;
  });
}

/** Every string inside `value` (an object, an array, or a string) through the redaction; everything else as it was. */
function redactedValue(value: unknown): any {
  if (typeof value === 'string') return redactText(value).text;
  if (Array.isArray(value)) return value.map(redactedValue);
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, inner]) => [key, redactedValue(inner)]));
  }
  return value;
}

/**
 * The tool list with secrets hidden in its descriptions and schemas (a default, an example, an enum: an MCP tool's schema can
 * carry anything). Names are left alone, because the model must call a tool by its name. The same text always redacts to
 * the same text, so the engine's cached prompt still matches.
 */
export function redactedTools(tools: ToolDefinition[]): ToolDefinition[] {
  return tools.map((tool) => ({
    ...tool,
    function: {
      ...tool.function,
      description: redactText(tool.function.description ?? '').text,
      parameters: redactedValue(tool.function.parameters),
    },
  }));
}
