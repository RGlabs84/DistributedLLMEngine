/**
 * The `view_image` agent tool (docs/release-2.7-contract.md 4d): the agent looks at a picture file in the workspace (a
 * screenshot, a diagram, an icon it made). The coordinator's own engine reads it (TheWall's vision projector); a coding
 * member cannot, so the request that carries it is never sealed to one (client/pipeline.ts). Like generate_image the tool
 * is offered to the model only while /v1/cluster/status says `capabilities.vision` is true, and its prompt line comes
 * and goes with it; engine/canary.py and scripts/cache_probe.py carry the same text, word for word.
 */
import { ToolDefinition } from '../client/dlneClient';

export const VIEW_IMAGE_TOOL_NAME = 'view_image';

export function viewImageToolDefinition(): ToolDefinition {
  return {
    type: 'function',
    function: {
      name: VIEW_IMAGE_TOOL_NAME,
      description: 'Look at a picture file (PNG or JPEG, at most 4 MiB) in the workspace, such as a screenshot, a diagram or an icon you made. The picture is shown to you with your next step. Use it only when seeing the picture matters.',
      parameters: {
        type: 'object',
        properties: {
          path: { type: 'string', description: 'Relative path in workspace or absolute path of the picture file (e.g. docs/screenshot.png)' }
        },
        required: ['path']
      }
    }
  };
}

/** The line the system prompt gains while the tool is offered (the static prompt is pinned by the canary parity test). */
export const VIEW_IMAGE_PROMPT_LINE = '- view_image(path): look at a picture file (PNG or JPEG) in the workspace, such as a screenshot or a diagram; it is shown to you with your next step.';

/** The agent system prompt with the view_image line in the tool list, before run_terminal_command's (after generate_image's). */
export function withViewImageLine(systemPrompt: string): string {
  const marker = '- run_terminal_command(';
  return systemPrompt.includes(marker)
    ? systemPrompt.replace(marker, `${VIEW_IMAGE_PROMPT_LINE}\n${marker}`)
    : `${systemPrompt}\n\n${VIEW_IMAGE_PROMPT_LINE}`;
}
