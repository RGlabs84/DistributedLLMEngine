/**
 * Maintenance mode (coordinator 2.8.5). While the owner has it on, the status carries
 * `"maintenance": {"active": true, "message", "until", "since"}`, and a refused request answers 429 with the code
 * `maintenance` and the same object. The panel then shows one card (the web chat's words) until the status stops saying so.
 */

export interface MaintenanceNotice {
  message: string;
  /** The owner's own words for when it ends ("14:00 UTC"), or '' when none was given. */
  until: string;
}

export const MAINTENANCE_TITLE = 'DLNE is being upgraded';
export const MAINTENANCE_DEFAULT_MESSAGE = 'DLNE is down for a short upgrade. Back soon.';
export const MAINTENANCE_FOOTER = 'This page checks every 15 seconds and opens again by itself.';
export const MAINTENANCE_RECHECK_MS = 15000;
/** The status bar item's text while maintenance is on. */
export const MAINTENANCE_STATUS_TEXT = 'DLNE: maintenance';

function noticeFrom(obj: unknown): MaintenanceNotice {
  const o = (obj && typeof obj === 'object' ? obj : {}) as Record<string, unknown>;
  const message = typeof o.message === 'string' ? o.message.trim().slice(0, 600) : '';
  const until = typeof o.until === 'string' ? o.until.trim().slice(0, 120) : '';
  return { message: message || MAINTENANCE_DEFAULT_MESSAGE, until };
}

/** The notice a status answer carries, or null when maintenance is not on. */
export function maintenanceFromStatus(status: { maintenance?: unknown } | null | undefined): MaintenanceNotice | null {
  const m = status?.maintenance as { active?: unknown } | undefined;
  if (!m || typeof m !== 'object' || m.active !== true) return null;
  return noticeFrom(m);
}

/** The notice in the body of a refused request (`code: "maintenance"`), or null for any other refusal. */
export function maintenanceFromBody(body: string): MaintenanceNotice | null {
  try {
    const parsed = JSON.parse(body);
    if (!parsed || parsed.code !== 'maintenance' && parsed.code !== 'voice_paused') return null;
    const m = parsed.maintenance;
    if (m && typeof m === 'object') return noticeFrom(m);
    return noticeFrom({ message: typeof parsed.detail === 'string' ? parsed.detail : '' });
  } catch {
    return null;
  }
}

/** The "Expected back" line, or '' when no time was given. */
export function maintenanceUntilLine(notice: MaintenanceNotice): string {
  return notice.until ? `Expected back: ${notice.until}` : '';
}
