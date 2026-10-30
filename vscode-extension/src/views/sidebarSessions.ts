/**
 * The sidebar's sessions (1.5.3, the owner's ask of 2026-10-30): like VS Code's Chat view, a New chat keeps the one
 * before it, and the back arrow lists them all to go back to. Each session is its own conversation: its own agent
 * history and saved task (agentState.openSidebarSessionStore) and its own visible chat. This file is the bookkeeping
 * only, kept apart from `vscode` so the tests can run it under plain Node: the index in the workspace state, the
 * titles, the order, and which sessions housekeeping lets go.
 */
import { isThreadId, newThreadId } from '../agent/agentState';

/** One session as the index keeps it. `updated` (epoch ms) orders the list; `title` is the first words asked. */
export interface SessionMeta {
  id: string;
  title: string;
  created: number;
  updated: number;
}

/** The workspace state: which sessions there are, and the one the sidebar shows. */
export interface SessionIndex {
  active: string;
  sessions: SessionMeta[];
}

/** A row of the page's list. */
export interface SessionRow {
  id: string;
  title: string;
  updated: number;
  running: boolean;
  active: boolean;
}

export const SESSIONS_KEY = 'dlne.sidebarSessions';
/** The visible chat of a session: `dlne.chatHistory.<id>`. Before 1.5.3 there was one, under `dlne.chatHistory`. */
export const LEGACY_HISTORY_KEY = 'dlne.chatHistory';
export const historyKey = (id: string) => `${LEGACY_HISTORY_KEY}.${id}`;

/** The most sessions kept: the oldest that is neither shown nor running goes when a new one would pass it. */
export const MAX_SESSIONS = 20;
export const NEW_SESSION_TITLE = 'New chat';
const TITLE_CHARS = 60;

/** A session's title from the first thing asked in it: one line, at most TITLE_CHARS characters. */
export function sessionTitle(text: unknown): string {
  const line = typeof text === 'string' ? text.replace(/\s+/g, ' ').trim() : '';
  if (!line) return NEW_SESSION_TITLE;
  return line.length > TITLE_CHARS ? `${line.slice(0, TITLE_CHARS - 1).trimEnd()}…` : line;
}

export function newSession(now: number): SessionMeta {
  return { id: newThreadId(), title: NEW_SESSION_TITLE, created: now, updated: now };
}

/** The index as saved, checked entry by entry (it comes back from disk), or null when there is none worth using. */
export function readIndex(raw: unknown): SessionIndex | null {
  if (!raw || typeof raw !== 'object') return null;
  const listed = Array.isArray((raw as any).sessions) ? (raw as any).sessions : [];
  const seen = new Set<string>();
  const sessions: SessionMeta[] = [];
  for (const entry of listed) {
    if (!entry || !isThreadId(entry.id) || seen.has(entry.id)) continue;
    const created = Number.isFinite(entry.created) ? entry.created : 0;
    const updated = Number.isFinite(entry.updated) ? entry.updated : created;
    seen.add(entry.id);
    sessions.push({ id: entry.id, title: sessionTitle(entry.title), created, updated });
  }
  if (sessions.length === 0) return null;
  const active = seen.has((raw as any).active) ? (raw as any).active : newest(sessions).id;
  return { active, sessions };
}

/** Newest first, by when each was last used. */
export function byRecent(sessions: readonly SessionMeta[]): SessionMeta[] {
  return [...sessions].sort((a, b) => b.updated - a.updated || b.created - a.created);
}

function newest(sessions: readonly SessionMeta[]): SessionMeta {
  return byRecent(sessions)[0];
}

/** What the page lists, newest first. */
export function sessionRows(index: SessionIndex, running: ReadonlySet<string>): SessionRow[] {
  return byRecent(index.sessions).map((s) => ({
    id: s.id, title: s.title, updated: s.updated, running: running.has(s.id), active: s.id === index.active,
  }));
}

/**
 * The sessions housekeeping lets go so that at most `max` are kept: the oldest first, never the one shown and never
 * one that is running (those stay even past the limit, until they are neither).
 */
export function sessionsToDrop(index: SessionIndex, running: ReadonlySet<string>, max = MAX_SESSIONS): string[] {
  const extra = index.sessions.length - max;
  if (extra <= 0) return [];
  return byRecent(index.sessions)
    .reverse()
    .filter((s) => s.id !== index.active && !running.has(s.id))
    .slice(0, extra)
    .map((s) => s.id);
}
