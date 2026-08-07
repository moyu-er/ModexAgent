/**
 * ModexAgent opencode plugin — injects OPENCODE_SESSION_ID into shell subprocess env.
 *
 * Only loaded when OPENCODE_CONFIG_CONTENT env var points to this file
 * (set by ExternalEnvBuilder on the spawned opencode process). Does NOT
 * affect other opencode instances — the env var is per-process.
 *
 * The shell.env hook receives { cwd, sessionID, callID } as input and
 * writes to output.env. We inject the current opencode session ID so
 * modexctl can look up the correct per-session env snapshot file.
 */
export default {
  id: "modex-shell-env",
  server: async () => ({
    "shell.env": async (input, output) => {
      if (input.sessionID) {
        output.env["OPENCODE_SESSION_ID"] = input.sessionID;
      }
    },
  }),
};
