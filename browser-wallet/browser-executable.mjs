import { existsSync } from "node:fs";
import { join } from "node:path";

const BROWSERS = {
  chrome: {
    env: "CHROME",
    windows: ["Google", "Chrome", "Application", "chrome.exe"],
    command: "google-chrome",
  },
  edge: {
    env: "EDGE",
    windows: ["Microsoft", "Edge", "Application", "msedge.exe"],
    command: "microsoft-edge",
  },
  firefox: {
    env: "FIREFOX",
    windows: ["Mozilla Firefox", "firefox.exe"],
    command: "firefox",
  },
};

export function browserExecutable(name) {
  const browser = BROWSERS[name];
  if (!Object.hasOwn(BROWSERS, name)) throw new Error("Select chrome, edge or firefox");
  const configured = process.env[browser.env];
  if (configured) return configured;
  if (process.platform === "win32") {
    const roots = [process.env.ProgramFiles, process.env["ProgramFiles(x86)"], process.env.LOCALAPPDATA];
    const installed = roots.filter(Boolean).map((root) => join(root, ...browser.windows)).find(existsSync);
    return installed ?? browser.windows.at(-1);
  }
  return browser.command;
}
