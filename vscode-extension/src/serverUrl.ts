// The coordinator address, in one place.

/** The public swarm, over HTTPS (Caddy in front of the coordinator, Let's Encrypt). Since 1.3.4 the
 * project's own name, which also serves the DLNE website; Porkbun's DNS follows the host's
 * changing home address. */
export const DEFAULT_SERVER_URL = 'https://dlne.rossgilsonlabs.dev';

/** The DuckDNS name releases 1.2.4-1.3.3 used. It still answers (same coordinator); read as the
 * current default so saved settings follow the move. */
export const DUCKDNS_SERVER_URL = 'https://dlne.duckdns.org';

/**
 * The plain-HTTP address every release before 1.2.0 used as its default. Some users have it
 * saved in their settings (copied, or written by an old version); it still works, but sends
 * prompts and code unencrypted, so it is read as the HTTPS default instead.
 */
export const LEGACY_SERVER_URL = 'http://73.215.216.195:11435';

/** The HTTPS address releases 1.2.0-1.2.3 used. It still works but carries the host's IP, which
 * changes; read as the current default. */
export const SSLIP_SERVER_URL = 'https://73-215-216-195.sslip.io';

interface ConfigReader {
  get<T>(section: string): T | undefined;
}

/** dlne.serverUrl without a trailing slash, the earlier public addresses read as today's. */
export function resolveServerUrl(config: ConfigReader): string {
  const raw = (config.get<string>('serverUrl') || '').trim().replace(/\/+$/, '');
  if (!raw || raw === LEGACY_SERVER_URL || raw === SSLIP_SERVER_URL || raw === DUCKDNS_SERVER_URL) {
    return DEFAULT_SERVER_URL;
  }
  return raw;
}

/** The phone chat page the coordinator serves (engine/chat_page.py): DLNE for people without VS Code. */
export function phoneChatUrl(config: ConfigReader): string {
  return `${resolveServerUrl(config)}/chat`;
}
