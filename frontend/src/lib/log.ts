/**
 * The review app's log: one JSON line per event in the browser console, in the services' shape (time, level,
 * service, message, ids). A failed call's line carries the gateway's X-Request-ID, which finds the same call in
 * the gateway's and the services' logs. A line holds ids, paths without their query string, status codes and
 * error names and messages, never a token or a request body.
 */

type Fields = Record<string, string | number | null | undefined>;

function write(level: "WARNING" | "ERROR", message: string, fields: Fields): void {
  const line = JSON.stringify({ time: new Date().toISOString(), level, service: "frontend", message, ...fields });
  if (level === "ERROR") console.error(line);
  else console.warn(line);
}

export const log = {
  warning: (message: string, fields: Fields = {}) => write("WARNING", message, fields),
  error: (message: string, fields: Fields = {}) => write("ERROR", message, fields),
};
