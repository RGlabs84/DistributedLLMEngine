/**
 * Which model a request names, and which one the sidebar shows. No vscode import, so tests load it
 * straight from dist/.
 *
 * The swarm serves one coding model at a time, the one its coordinator has active (its ladder
 * picks it from the GPUs available). The coordinator reads a chat or completion request's model as
 * `body.model or <the active model>`, so leaving `model` out is how a client says "whatever is
 * active". Naming a model is not harmless: a name the coordinator does not have active makes it try
 * to load that model and swap the engine over to it, for everyone. So by default the extension names
 * none, and `dlne.chatModel` / `dlne.fimModel` stay as an override for a private coordinator.
 */

/** What the VS Code Chat model picker and the sidebar call "whatever the swarm has active". */
export const SWARM_AUTO_LABEL = 'DLNE Swarm (auto)';

/**
 * The files the old model picker could write into `dlne.chatModel` (extension 1.2.4 and earlier
 * offered these eight, which the swarm does not run). A setting still holding one of them is a
 * leftover of that picker, not a choice, so it is read as "auto".
 */
export const RETIRED_MODEL_FILES: readonly string[] = [
  'qwen3-coder-30b-instruct-q4_k_m.gguf',
  'qwen2.5-14b-1m.gguf',
  'qwen2.5-coder-32b-instruct-q4_k_m.gguf',
  'qwen2.5-coder-14b-instruct-q4_k_m.gguf',
  'deepseek-r1-distill-qwen-32b-q4_k_m.gguf',
  'llama-3.3-70b-instruct-iq3_m.gguf',
  'deepseek-r1-distill-llama-70b-q4_k_m.gguf',
  'qwen2.5-72b-instruct-q4_k_m.gguf'
];

function canonicalFile(name: string): string {
  const base = (name.trim().split(/[\\/]/).pop() || '').replace(/:/g, '-').toLowerCase();
  return base.endsWith('.gguf') ? base : `${base}.gguf`;
}

/**
 * The `model` to put in a request, or undefined to leave it out (the coordinator then uses its
 * active model). `configured` is the raw `dlne.chatModel` or `dlne.fimModel` setting.
 */
export function requestedModel(configured: unknown): string | undefined {
  if (typeof configured !== 'string') {
    return undefined;
  }
  const name = configured.trim();
  if (!name || name === 'undefined') {
    return undefined;
  }
  return RETIRED_MODEL_FILES.includes(canonicalFile(name)) ? undefined : name;
}

/** The part of /v1/cluster/status that names the model. `coding_model` is there from server 2.3.5. */
export interface ModelNaming {
  global_active_model?: string | null;
  coding_model?: { name?: unknown } | null;
}

/** The live model's name, or null when the server does not say. */
export function activeModelName(status: ModelNaming | null | undefined): string | null {
  const nice = status?.coding_model?.name;
  if (typeof nice === 'string' && nice.trim()) {
    return nice.trim();
  }
  const file = status?.global_active_model;
  if (typeof file === 'string' && file.trim()) {
    return file.trim().replace(/\.gguf$/i, '');
  }
  return null;
}

/** The read-only line the sidebar shows where the model dropdown used to be. */
export function swarmModelLine(status: ModelNaming | null | undefined): string {
  const name = activeModelName(status);
  return name ? `${SWARM_AUTO_LABEL}: ${name}` : SWARM_AUTO_LABEL;
}
