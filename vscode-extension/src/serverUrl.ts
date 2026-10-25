// The coordinator address, in one place.

/** The public swarm, over HTTPS (Caddy in front of the coordinator, Let's Encrypt). */
export const DEFAULT_SERVER_URL = 'https://73-215-216-195.sslip.io';

/**
 * The plain-HTTP address every release before 1.2.0 used as its default. Some users have it
 * saved in their settings (copied, or written by an old version); it still works, but sends
 * prompts and code unencrypted, so it is read as the HTTPS default instead.
 */
export const LEGACY_SERVER_URL = 'http://73.215.216.195:11435';

interface ConfigReader {
  get<T>(section: string): T | undefined;
}

/** dlne.serverUrl without a trailing slash, the legacy public address upgraded to HTTPS. */
export function resolveServerUrl(config: ConfigReader): string {
  const raw = (config.get<string>('serverUrl') || '').trim().replace(/\/+$/, '');
  if (!raw || raw === LEGACY_SERVER_URL) {
    return DEFAULT_SERVER_URL;
  }
  return raw;
}
