import assert from "node:assert/strict";
import test from "node:test";

import {
  clearSnapshotAccess,
  isSnapshotAccessError,
  loadDashboardData,
  readAccessToken,
  saveAccessToken,
} from "../js/data.js";

const PRIVATE_URL =
  "https://api.github.com/repos/cliffordfok/portfolio-tracker-data/contents/portfolio-snapshot.json?ref=portfolio-data";
const TOKEN = `github_pat_${"A1b2C3d4".repeat(5)}`;
const PREFIX = "access-test";

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }
  getItem(key) {
    return this.values.has(key) ? this.values.get(key) : null;
  }
  setItem(key, value) {
    this.values.set(key, String(value));
  }
  removeItem(key) {
    this.values.delete(key);
  }
}

function snapshot() {
  const portfolio = () => ({
    data_status: "NO_DATA",
    holdings: [],
    recent_trades: [],
    daily: [],
    metrics: {
      data_status: "NO_DATA",
      performance_effective_date: null,
      performance_scope: null,
      total_return: null,
      realized_pnl: "0",
      income_expense: "0",
      win_rate: null,
      max_drawdown: null,
      sharpe_ratio: null,
      closed_episodes: 0,
    },
  });
  const emptyHead = () => ({
    count: 0,
    last_event_id: null,
    hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  });
  return {
    schema_version: 4,
    revision: 1,
    generated_at: "2026-10-09T21:00:00Z",
    data_as_of: null,
    prices_as_of: null,
    currency: "USD",
    source_head: { paper: emptyHead(), live: emptyHead(), market: emptyHead() },
    portfolios: { paper: portfolio(), live: portfolio() },
    benchmark: { symbol: "SPY", daily: [] },
    warnings: [],
  };
}

function config(overrides = {}) {
  return {
    snapshotUrls: [PRIVATE_URL],
    requireReadToken: true,
    storagePrefix: PREFIX,
    staleAfterMinutes: 999999,
    fallbackUrls: {
      paper: "./data/paper.json",
      live: "./data/live.json",
      benchmark: "./data/benchmark.json",
    },
    fallbackInitialCash: { paper: 100000, live: 50000 },
    ...overrides,
  };
}

async function withBrowser(fetchImpl, body) {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const storage = new MemoryStorage();
  const calls = [];
  let responder = fetchImpl;
  globalThis.window = {
    location: { href: "https://cliffordfok.github.io/portfolio-tracker/" },
    localStorage: storage,
  };
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    return responder(new URL(String(url)), init);
  };
  clearSnapshotAccess(config());
  try {
    await body({ storage, calls, respond: (next) => { responder = next; } });
  } finally {
    clearSnapshotAccess(config());
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
}

// Nothing secret may ever reach origin-wide localStorage.
function assertNoSecretsPersisted(storage) {
  for (const [key, value] of storage.values) {
    assert.ok(!value.includes(TOKEN), `token persisted under ${key}`);
    assert.ok(!key.endsWith("last-good-snapshot"), `snapshot persisted under ${key}`);
    assert.ok(!key.endsWith("github-read-token"), `token key persisted: ${key}`);
  }
}

async function primeMemoryCache(respond) {
  respond(async () => ok(snapshot()));
  saveAccessToken(config(), TOKEN);
  const first = await loadDashboardData(config(), { now: 1 });
  assert.equal(first.source, "snapshot");
}

const ok = (payload) => ({ ok: true, status: 200, json: async () => payload });
const fail = (status, headers = {}) => ({
  ok: false,
  status,
  statusText: "denied",
  headers: { get: (name) => headers[name.toLowerCase()] ?? null },
  json: async () => ({}),
});

test("without a token nothing is fetched and no cache or demo data is shown", async () => {
  await withBrowser(async () => assert.fail("must not fetch"), async ({ storage, calls }) => {
    storage.setItem(
      `${PREFIX}:last-good-snapshot`,
      JSON.stringify({ cachedAt: 1, snapshot: snapshot() }),
    );
    await assert.rejects(
      loadDashboardData(config(), { now: 2 }),
      (error) => isSnapshotAccessError(error) && error.code === "AUTH_REQUIRED",
    );
    assert.equal(calls.length, 0);
  });
});

test("saved token is sent only as a bearer header to api.github.com", async () => {
  await withBrowser(async () => ok(snapshot()), async ({ calls }) => {
    saveAccessToken(config(), `  ${TOKEN}  `);
    assert.equal(readAccessToken(config()), TOKEN);
    const result = await loadDashboardData(config(), { now: 10 });
    assert.equal(result.source, "snapshot");
    assert.equal(calls.length, 1);
    const { url, init } = calls[0];
    assert.equal(new URL(url).hostname, "api.github.com");
    assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(init.credentials, "omit");
    assert.equal(init.referrerPolicy, "no-referrer");
    assert.ok(!url.includes(TOKEN), "token must never appear in the URL");
  });
});

test("token is never attached to any other host, including fallback JSON", async () => {
  await withBrowser(
    async (url) => (url.pathname.endsWith(".json") && url.hostname !== "evil.test"
      ? ok([])
      : ok(snapshot())),
    async ({ calls }) => {
      saveAccessToken(config(), TOKEN);
      await loadDashboardData(
        config({ snapshotUrls: ["https://evil.test/portfolio-snapshot.json"] }),
        { now: 10 },
      );
      for (const call of calls) {
        assert.equal(call.init.headers?.Authorization, undefined, call.url);
      }
      assert.ok(calls.every((call) => !call.url.startsWith("https://evil.test")));
    },
  );
});

for (const status of [401, 403, 404]) {
  test(`GitHub ${status} rejects access, clears cache and skips demo fallback`, async () => {
    await withBrowser(async () => ok(snapshot()), async ({ storage, calls, respond }) => {
      await primeMemoryCache(respond);
      respond(async () => fail(status));
      await assert.rejects(
        loadDashboardData(config(), { force: true, now: 10_000_000 }),
        (error) => isSnapshotAccessError(error) && error.code === "AUTH_INVALID",
      );
      // The rejected token's cached data must not be served afterwards either.
      respond(async (url) => (url.hostname === "api.github.com" ? fail(503) : ok([])));
      const after = await loadDashboardData(config(), { force: true, now: 10_000_001 });
      assert.notEqual(after.source, "cache");
      assert.ok(
        calls.filter((call) => call.init.headers?.Authorization)
          .every((call) => new URL(call.url).hostname === "api.github.com"),
      );
      assertNoSecretsPersisted(storage);
    });
  });
}

for (const [label, headers] of [
  ["primary rate limit (x-ratelimit-remaining 0)", { "x-ratelimit-remaining": "0" }],
  ["secondary rate limit (Retry-After)", { "retry-after": "60" }],
]) {
  test(`${label} keeps the token and serves the in-memory cache`, async () => {
    await withBrowser(async () => ok(snapshot()), async ({ storage, respond }) => {
      await primeMemoryCache(respond);
      respond(async (url) => (url.hostname === "api.github.com" ? fail(403, headers) : ok([])));
      const result = await loadDashboardData(config(), { force: true, now: 10_000_000 });
      assert.equal(result.source, "cache");
      assert.equal(readAccessToken(config()), TOKEN);
      assertNoSecretsPersisted(storage);
    });
  });
}

test("token and real snapshot never touch localStorage during a full session", async () => {
  await withBrowser(async () => ok(snapshot()), async ({ storage, respond }) => {
    await primeMemoryCache(respond);
    await loadDashboardData(config(), { now: 2 });
    await loadDashboardData(config(), { force: true, now: 3 });
    assertNoSecretsPersisted(storage);
    assert.equal(storage.getItem(`${PREFIX}:fetch-lease`), null, "private mode needs no lease");
  });
});

test("legacy persisted token and snapshot are purged on load", async () => {
  await withBrowser(async () => ok(snapshot()), async ({ storage }) => {
    storage.setItem(`${PREFIX}:github-read-token`, TOKEN);
    storage.setItem(
      `${PREFIX}:last-good-snapshot`,
      JSON.stringify({ cachedAt: 1, snapshot: snapshot() }),
    );
    await assert.rejects(
      loadDashboardData(config(), { now: 2 }),
      (error) => error.code === "AUTH_REQUIRED",
      "a token left in localStorage must not grant access",
    );
    assert.equal(storage.getItem(`${PREFIX}:github-read-token`), null);
    assert.equal(storage.getItem(`${PREFIX}:last-good-snapshot`), null);
  });
});

test("malformed tokens are refused and lock clears token and cache", async () => {
  await withBrowser(async () => ok(snapshot()), async ({ calls, respond }) => {
    for (const bad of [
      "",
      "hello",
      "Bearer github_pat_x",
      `${TOKEN}\nX-Evil: 1`,
      `ghp_${"A1b2C3d4".repeat(5)}`,
    ]) {
      assert.throws(
        () => saveAccessToken(config(), bad),
        (error) => error.code === "AUTH_FORMAT",
      );
    }
    await primeMemoryCache(respond);
    clearSnapshotAccess(config());
    assert.equal(readAccessToken(config()), null);
    const before = calls.length;
    await assert.rejects(
      loadDashboardData(config(), { now: 5 }),
      (error) => error.code === "AUTH_REQUIRED",
    );
    assert.equal(calls.length, before, "no cached data or request after locking");
  });
});
