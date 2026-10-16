// agentctl's opencode plugin. agentctl loads it (via OPENCODE_CONFIG) only into the opencode
// TUIs it launches. It forwards status and permission events to agentctld's Unix socket, which
// tells agentctl which tmux pane shows which session. It never blocks opencode and never throws.
import { createConnection } from "node:net";
import { homedir } from "node:os";

const SOCK = `${homedir()}/.agentctl/agentctl.sock`;
const FORWARD = new Set([
  "session.status",
  "session.idle",
  "permission.asked",
  "permission.replied",
]);

export const Agentctl = async () => ({
  event: async ({ event }) => {
    if (!FORWARD.has(event.type)) return;
    try {
      const params = {
        source: "opencode",
        event: event.type,
        payload: event.properties,
        pid: process.pid,
        pane: process.env.TMUX_PANE ?? null,
        tmux: process.env.TMUX ?? null,
      };
      const c = createConnection(SOCK);
      c.on("error", () => {}); // daemon down: drop it, like the Claude hooks do
      c.on("data", () => c.destroy());
      c.write(JSON.stringify({ id: 1, method: "hook", params }) + "\n");
      setTimeout(() => c.destroy(), 2000).unref?.();
    } catch {}
  },
});
