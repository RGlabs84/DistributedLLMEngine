/**
 * The Auto switch inside the Chat view. The agent asks before it edits a file or runs a command unless Auto is on
 * (dlne.autoApproveAll: the status bar's DLNE: Auto / DLNE: Ask item), and the question is a pop-up, so the first one
 * of a run is also where the response offers a button to turn Auto on.
 */
import { ApprovalRequest, ToolRegistry } from '../agent/tools';

/**
 * The button. The command takes `true` to mean "on", so a second click, or a click after the status bar item was
 * used, leaves Auto on instead of switching it back off under a label that says "Turn on".
 */
export const AUTO_BUTTON = {
  command: 'dlne.toggleAutoApprove',
  arguments: [true],
  title: 'Turn on Auto (stop asking)',
  tooltip: 'Every edit and command then runs without asking, wherever it is. Click DLNE: Auto in the status bar to have it ask again.',
};

/**
 * `dlne.toggleAutoApprove`: with no argument (the status bar item, the palette) it flips the Auto switch; with `true` or
 * `false` (the chat's button) it sets it. Any other argument, from a keybinding say, flips it.
 */
export function toggleAuto(on?: unknown): Promise<void> {
  return ToolRegistry.setAutoApproveAll(typeof on === 'boolean' ? on : !ToolRegistry.autoApproveAllOn());
}

type Approver = typeof ToolRegistry.approver;

const listeners = new Set<(request: ApprovalRequest) => void>();
let installed: { wrapper: Approver; inner: Approver } | undefined;

function install(): void {
  const inner = ToolRegistry.approver;
  const wrapper: Approver = (...args) => {
    // Told before the question is asked, so what the notice adds is on screen while the pop-up is up. A listener
    // that fails (a chat response that was closed) must never stop the question being asked.
    for (const listener of [...listeners]) {
      try {
        listener(args[0]);
      } catch {
        // not the approval's concern
      }
    }
    return inner(...args);
  };
  ToolRegistry.approver = wrapper;
  installed = { wrapper, inner };
}

/**
 * Calls `listener` each time the agent is about to ask the user to approve something, which only happens while Auto is
 * off. It watches the approver ToolRegistry already has (tools.ts), without changing what the approver does, and puts
 * it back when the last listener goes. Dispose it when the run ends.
 */
export function observeApprovals(listener: (request: ApprovalRequest) => void): { dispose(): void } {
  listeners.add(listener);
  if (!installed || ToolRegistry.approver !== installed.wrapper) install();
  return {
    dispose() {
      listeners.delete(listener);
      if (listeners.size > 0 || !installed) return;
      if (ToolRegistry.approver === installed.wrapper) ToolRegistry.approver = installed.inner;
      installed = undefined;
    },
  };
}
