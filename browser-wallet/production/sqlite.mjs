// node:sqlite says once per process that Node 24 still marks it experimental. It is used here on purpose, so only that
// notice is left out; every other warning still shows.
const original = process.emitWarning;
process.emitWarning = (warning, ...rest) => {
  const text = typeof warning === "string" ? warning : warning.message;
  if (!text.startsWith("SQLite is an experimental feature")) Reflect.apply(original, process, [warning, ...rest]);
};
const sqlite = await import("node:sqlite");
process.emitWarning = original;

export const { DatabaseSync } = sqlite;
