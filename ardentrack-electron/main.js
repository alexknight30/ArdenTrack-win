"use strict";

const {
  app,
  Tray,
  Menu,
  nativeImage,
  shell,
  Notification,
} = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const http = require("http");

const log = {
  info: (...a) => console.log("[ArdenTrack]", ...a),
  warn: (...a) => console.warn("[ArdenTrack]", ...a),
  error: (...a) => console.error("[ArdenTrack]", ...a),
};

const { autoUpdater } = require("electron-updater");

let tray = null;
let pythonProcess = null;
let pythonRestartCount = 0;
const PYTHON_RESTART_MAX = 3;
const PYTHON_RESTART_DELAY_MS = 5000;

let cachedAuthPort = null;
let cachedAuthToken = null;

function ensureEnvForPython() {
  if (!cachedAuthPort) {
    cachedAuthPort = process.env.ARDEN_AUTH_CALLBACK_PORT || "17951";
  }
  if (!cachedAuthToken) {
    cachedAuthToken =
      process.env.ARDEN_AUTH_CALLBACK_TOKEN ||
      crypto.randomBytes(32).toString("hex");
  }
  process.env.ARDEN_AUTH_CALLBACK_PORT = cachedAuthPort;
  process.env.ARDEN_AUTH_CALLBACK_TOKEN = cachedAuthToken;
  return { port: cachedAuthPort, token: cachedAuthToken };
}

function resolvePythonExe() {
  if (process.env.ARDENT_PYTHON_EXE) {
    return process.env.ARDENT_PYTHON_EXE;
  }
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "ardentrack.exe");
  }
  return path.join(
    __dirname,
    "..",
    "dist",
    "ardentrack.exe"
  );
}

function appendElectronLog(line) {
  const dir = path.join(
    process.env.LOCALAPPDATA || app.getPath("userData"),
    "Arden"
  );
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, "electron.log"), line + "\n", "utf8");
  } catch (e) {
    log.error("electron.log write failed", e);
  }
}

function postTokensToPython(port, token, bodyObj) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(bodyObj);
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port: Number(port),
        path: "/auth/tokens",
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data),
          "X-Arden-Auth-Token": token,
        },
      },
      (res) => {
        let chunks = "";
        res.on("data", (c) => {
          chunks += c;
        });
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve();
          } else {
            reject(new Error(`HTTP ${res.statusCode}`));
          }
        });
      }
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

function parseProtocolUrl(argv) {
  const hit = argv.find((a) => typeof a === "string" && a.startsWith("arden://"));
  return hit || null;
}

function queryParams(urlStr) {
  try {
    const u = new URL(urlStr);
    const o = {};
    u.searchParams.forEach((v, k) => {
      o[k] = v;
    });
    return o;
  } catch {
    return {};
  }
}

async function handleProtocolUrl(urlStr) {
  const q = queryParams(urlStr);
  const access = q.access_token;
  const refresh = q.refresh_token;
  const expiresIn = q.expires_in;
  if (!access || !refresh || expiresIn === undefined) {
    log.warn("protocol callback missing tokens");
    if (Notification.isSupported()) {
      new Notification({
        title: "ArdenTrack",
        body: "Authentication failed — please try again",
      }).show();
    }
    return;
  }
  const port = process.env.ARDEN_AUTH_CALLBACK_PORT;
  const token = process.env.ARDEN_AUTH_CALLBACK_TOKEN;
  if (!port || !token) {
    log.error("Missing ARDEN_AUTH_CALLBACK_* env for token POST");
    return;
  }
  try {
    await postTokensToPython(port, token, {
      access_token: access,
      refresh_token: refresh,
      expires_in: Number(expiresIn),
    });
    log.info("tokens delivered to Python");
    if (Notification.isSupported()) {
      new Notification({
        title: "ArdenTrack",
        body: "Arden is connected",
      }).show();
    }
  } catch (e) {
    log.error("token POST failed", e);
    if (Notification.isSupported()) {
      new Notification({
        title: "ArdenTrack",
        body: "Authentication failed — please try again",
      }).show();
    }
  }
}

function spawnPython() {
  const exe = resolvePythonExe();
  if (!fs.existsSync(exe)) {
    log.error("Python executable not found:", exe);
    return;
  }
  const { port, token } = ensureEnvForPython();
  log.info("Spawning Python", exe, "port", port);

  const childEnv = { ...process.env };
  childEnv.ARDEN_AUTH_CALLBACK_PORT = port;
  childEnv.ARDEN_AUTH_CALLBACK_TOKEN = token;

  pythonProcess = spawn(exe, [], {
    env: childEnv,
    stdio: ["ignore", "pipe", "pipe"],
  });

  pythonProcess.stdout.on("data", (buf) => {
    const s = buf.toString();
    appendElectronLog(s.trimEnd());
    if (s.includes("AUTH_LISTENING=")) {
      const line = s.split("\n").find((l) => l.includes("AUTH_LISTENING="));
      if (line && !process.env.ARDEN_SKIP_BROWSER_OPEN) {
        shell.openExternal("https://ardentime.com/auth/desktop-callback");
      }
    }
  });
  pythonProcess.stderr.on("data", (buf) => {
    appendElectronLog("[stderr] " + buf.toString().trimEnd());
  });
  pythonProcess.on("exit", (code) => {
    log.warn("Python exited", code);
    pythonProcess = null;
    if (code !== 0 && pythonRestartCount < PYTHON_RESTART_MAX) {
      pythonRestartCount += 1;
      setTimeout(() => spawnPython(), PYTHON_RESTART_DELAY_MS);
    }
  });
}

function buildTray() {
  const iconPath = path.join(__dirname, "build", "icon.ico");
  let image = null;
  if (fs.existsSync(iconPath)) {
    image = nativeImage.createFromPath(iconPath);
  }
  if (!image || image.isEmpty()) {
    image = nativeImage.createEmpty();
  }
  try {
    tray = new Tray(image);
  } catch (e) {
    log.error("Tray creation failed", e);
    return;
  }

  const menu = Menu.buildFromTemplate([
    { label: "ArdenTrack is running", enabled: false },
    { type: "separator" },
    {
      label: "Check for updates",
      click: () => {
        autoUpdater.checkForUpdates().catch((err) => log.warn(err));
      },
    },
    {
      label: "Quit",
      click: () => {
        if (pythonProcess && !pythonProcess.killed) {
          pythonProcess.kill();
        }
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
  tray.setToolTip("ArdenTrack");
}

function setupAutoUpdater() {
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on("update-downloaded", () => {
    if (Notification.isSupported()) {
      new Notification({
        title: "ArdenTrack",
        body: "Update ready — will install on next restart",
      }).show();
    }
  });
  autoUpdater.on("error", (e) => log.warn("updater error", e));
  autoUpdater.checkForUpdates().catch((e) => log.warn("checkForUpdates", e));
  setInterval(
    () => autoUpdater.checkForUpdates().catch(() => {}),
    4 * 60 * 60 * 1000
  );
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.setAsDefaultProtocolClient("arden");

  app.on("second-instance", (_e, argv) => {
    const url = parseProtocolUrl(argv);
    if (url) {
      handleProtocolUrl(url);
    }
  });

  app.whenReady().then(() => {
    buildTray();
    spawnPython();
    setupAutoUpdater();

    if (process.platform === "win32") {
      const startup = parseProtocolUrl(process.argv);
      if (startup) {
        handleProtocolUrl(startup);
      }
    }
  });

  app.on("window-all-closed", () => {});
  app.on("will-quit", () => {
    if (pythonProcess && !pythonProcess.killed) {
      pythonProcess.kill();
    }
  });
}
