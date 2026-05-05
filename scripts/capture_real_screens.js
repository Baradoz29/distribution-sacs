const fs = require("node:fs/promises");
const path = require("node:path");
const { spawn } = require("node:child_process");

const APP_URL = "http://127.0.0.1:8000";
const CDP_PORT = 9223;
const VIEWPORT_WIDTH = 1920;
const VIEWPORT_HEIGHT = 1080;
const CHROME_CANDIDATES = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function getBrowserPath() {
  for (const candidate of CHROME_CANDIDATES) {
    if (await exists(candidate)) {
      return candidate;
    }
  }
  throw new Error("Aucun navigateur Chrome/Edge compatible n'a ete trouve.");
}

async function waitFor(check, { timeoutMs = 30000, intervalMs = 250, label = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;

  while (Date.now() < deadline) {
    try {
      const result = await check();
      if (result) {
        return result;
      }
    } catch (error) {
      lastError = error;
    }
    await sleep(intervalMs);
  }

  if (lastError) {
    throw lastError;
  }
  throw new Error(`Delai depasse en attente de ${label}.`);
}

class CDPClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.socket = null;
    this.nextId = 1;
    this.pending = new Map();
    this.eventQueue = [];
    this.eventWaiters = [];
  }

  async connect() {
    await new Promise((resolve, reject) => {
      const socket = new WebSocket(this.wsUrl);
      this.socket = socket;

      socket.addEventListener("open", () => resolve(), { once: true });
      socket.addEventListener(
        "error",
        (event) => {
          reject(event.error || new Error("Connexion WebSocket CDP impossible."));
        },
        { once: true }
      );
      socket.addEventListener("message", (event) => this.#onMessage(event.data));
      socket.addEventListener("close", () => {
        for (const { reject } of this.pending.values()) {
          reject(new Error("Connexion CDP fermee."));
        }
        this.pending.clear();
      });
    });
  }

  async close() {
    if (!this.socket) {
      return;
    }
    this.socket.close();
    await sleep(100);
  }

  send(method, params = {}, sessionId = null) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) {
      payload.sessionId = sessionId;
    }

    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify(payload));
    });
  }

  async waitForEvent(method, predicate = () => true, { timeoutMs = 10000 } = {}) {
    const queuedIndex = this.eventQueue.findIndex(
      (event) => event.method === method && predicate(event.params, event.sessionId)
    );
    if (queuedIndex >= 0) {
      const [event] = this.eventQueue.splice(queuedIndex, 1);
      return event;
    }

    return new Promise((resolve, reject) => {
      const timeoutId = setTimeout(() => {
        this.eventWaiters = this.eventWaiters.filter((entry) => entry !== waiter);
        reject(new Error(`Evenement ${method} non recu dans le delai attendu.`));
      }, timeoutMs);

      const waiter = {
        method,
        predicate,
        resolve: (event) => {
          clearTimeout(timeoutId);
          resolve(event);
        },
      };

      this.eventWaiters.push(waiter);
    });
  }

  #onMessage(rawPayload) {
    const message = JSON.parse(rawPayload);

    if (typeof message.id === "number") {
      const pending = this.pending.get(message.id);
      if (!pending) {
        return;
      }
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error.message || "Erreur CDP inconnue."));
        return;
      }
      pending.resolve(message.result);
      return;
    }

    const event = {
      method: message.method,
      params: message.params || {},
      sessionId: message.sessionId || null,
    };

    const waiterIndex = this.eventWaiters.findIndex(
      (waiter) => waiter.method === event.method && waiter.predicate(event.params, event.sessionId)
    );

    if (waiterIndex >= 0) {
      const [waiter] = this.eventWaiters.splice(waiterIndex, 1);
      waiter.resolve(event);
      return;
    }

    this.eventQueue.push(event);
  }
}

async function evaluate(client, sessionId, expression) {
  const result = await client.send(
    "Runtime.evaluate",
    {
      expression,
      awaitPromise: true,
      returnByValue: true,
    },
    sessionId
  );

  if (result.exceptionDetails) {
    throw new Error(`Evaluation echouee: ${result.exceptionDetails.text || expression}`);
  }
  return result.result?.value;
}

async function captureScreenshot(client, sessionId, filePath) {
  const result = await client.send(
    "Page.captureScreenshot",
    {
      format: "png",
      captureBeyondViewport: false,
      fromSurface: true,
    },
    sessionId
  );
  await fs.writeFile(filePath, Buffer.from(result.data, "base64"));
}

async function main() {
  const browserPath = await getBrowserPath();
  const profileDir = path.join(process.cwd(), "data", "chrome-headless-profile");
  const outputDir = path.join(process.cwd(), "static", "images");
  await fs.mkdir(profileDir, { recursive: true });
  await fs.mkdir(outputDir, { recursive: true });

  const browser = spawn(
    browserPath,
    [
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profileDir}`,
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      `--window-size=${VIEWPORT_WIDTH},${VIEWPORT_HEIGHT}`,
      "about:blank",
    ],
    {
      stdio: "ignore",
    }
  );

  let client = null;

  try {
    const versionPayload = await waitFor(
      async () => {
        const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`);
        if (!response.ok) {
          return null;
        }
        return response.json();
      },
      { timeoutMs: 15000, label: "demarrage du navigateur headless" }
    );

    client = new CDPClient(versionPayload.webSocketDebuggerUrl);
    await client.connect();

    const { targetId } = await client.send("Target.createTarget", {
      url: "about:blank",
    });
    const { sessionId } = await client.send("Target.attachToTarget", {
      targetId,
      flatten: true,
    });

    await client.send("Page.enable", {}, sessionId);
    await client.send("Runtime.enable", {}, sessionId);
    await client.send("Network.enable", {}, sessionId);
    await client.send(
      "Emulation.setDeviceMetricsOverride",
      {
        width: VIEWPORT_WIDTH,
        height: VIEWPORT_HEIGHT,
        deviceScaleFactor: 1,
        mobile: false,
      },
      sessionId
    );

    const loadScreen = async () => {
      const loadEvent = client.waitForEvent(
        "Page.loadEventFired",
        (_, eventSessionId) => eventSessionId === sessionId,
        { timeoutMs: 20000 }
      );
      await client.send("Page.navigate", { url: APP_URL }, sessionId);
      await loadEvent;
      await sleep(1200);
    };

    const setSearchValues = async () => {
      await evaluate(
        client,
        sessionId,
        `(() => {
          const setValue = (id, value) => {
            const element = document.getElementById(id);
            element.focus();
            element.value = value;
            element.dispatchEvent(new Event("input", { bubbles: true }));
          };
          setValue("search-last-name", "Le Goff");
          setValue("search-first-name", "Jeanne");
          return true;
        })()`
      );
    };

    const setAddressValue = async (value) => {
      await evaluate(
        client,
        sessionId,
        `(() => {
          const element = document.getElementById("search-address");
          element.focus();
          element.value = ${JSON.stringify(value)};
          element.dispatchEvent(new Event("input", { bubbles: true }));
          return true;
        })()`
      );
    };

    const waitForResults = async () =>
      waitFor(
        () =>
          evaluate(
            client,
            sessionId,
            `(() => document.querySelectorAll(".result-card").length >= 1)()`
          ),
        { timeoutMs: 15000, label: "affichage des resultats de recherche" }
      );

    await loadScreen();
    await setSearchValues();
    await waitForResults();
    await sleep(1200);
    await captureScreenshot(
      client,
      sessionId,
      path.join(outputDir, "presentation-screen-1.png")
    );

    await setAddressValue("1 Rue de la Marne");
    await waitFor(
      () =>
        evaluate(
          client,
          sessionId,
          `(() => {
            const cards = [...document.querySelectorAll(".result-card")];
            return cards.length >= 1 &&
              cards.every((card) => card.textContent.includes("1 Rue de la Marne"));
          })()`
        ),
      { timeoutMs: 15000, label: "filtrage des resultats sur 1 Rue de la Marne" }
    );
    await sleep(1200);

    await evaluate(
      client,
      sessionId,
      `(() => {
        const target = [...document.querySelectorAll(".result-card")].find((card) =>
          card.textContent.includes("Jeanne Le Goff") && card.textContent.includes("1 Rue de la Marne")
        ) || document.querySelector(".result-card");
        if (!target) {
          return false;
        }
        target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
        return true;
      })()`
    );
    await waitFor(
      () =>
        evaluate(
          client,
          sessionId,
          `(() => {
            const detail = document.getElementById("resident-detail");
            return detail && !detail.classList.contains("is-hidden") &&
              document.querySelector(".resident-title")?.textContent?.includes("Jeanne Le Goff");
          })()`
        ),
      { timeoutMs: 20000, label: "ouverture de la fiche Jeanne" }
    );
    await sleep(1200);
    await captureScreenshot(
      client,
      sessionId,
      path.join(outputDir, "presentation-screen-2.png")
    );

    await evaluate(
      client,
      sessionId,
      `(() => {
        const target = [...document.querySelectorAll(".household-card")].find((card) =>
          card.textContent.includes("Joseph Le Goff")
        );
        if (!target) {
          return false;
        }
        target.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
        return true;
      })()`
    );
    await waitFor(
      () =>
        evaluate(
          client,
          sessionId,
          `(() => document.querySelector(".resident-title")?.textContent?.includes("Joseph Le Goff"))()`
        ),
      { timeoutMs: 20000, label: "ouverture de la fiche Joseph" }
    );
    await sleep(1200);
    await captureScreenshot(
      client,
      sessionId,
      path.join(outputDir, "presentation-screen-3.png")
    );

    await client.send("Target.closeTarget", { targetId });
    console.log("Screens captures dans static/images.");
  } finally {
    if (client) {
      await client.close().catch(() => {});
    }
    browser.kill();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
