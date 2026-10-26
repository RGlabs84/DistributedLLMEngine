import { randomUUID } from 'crypto';

/**
 * Session ids (header `X-DLNE-Session`, docs/coding-members.md section 10). They are random, mean nothing
 * and are never derived from the user, the workspace or the conversation's text: the coordinator uses
 * one only to send a conversation back to the member that already read its prompt (sticky routing).
 */
export function newSessionId(): string {
  return randomUUID();
}

let editorSession: string | undefined;

/** One id for the whole editor session: autocomplete, and chat that has no conversation of its own. */
export function editorSessionId(): string {
  if (!editorSession) {
    editorSession = newSessionId();
  }
  return editorSession;
}
