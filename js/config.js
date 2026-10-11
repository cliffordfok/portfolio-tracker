window.PORTFOLIO_CONFIG = Object.freeze({
  // 正式快照存放喺 private repo，只可以用擁有者自己嘅唯讀 fine-grained PAT 讀取。
  // GitHub raw 網域唔支援以 header 驗證 private 檔案，所以只用 Contents API。
  snapshotUrls: Object.freeze([
    "https://api.github.com/repos/cliffordfok/portfolio-tracker-data/contents/portfolio-snapshot.json?ref=portfolio-data",
  ]),
  requireReadToken: true,
  fallbackUrls: Object.freeze({
    paper: "./data/paper.json",
    live: "./data/live.json",
    benchmark: "./data/benchmark.json",
  }),
  fallbackInitialCash: Object.freeze({
    paper: 100000,
    live: 50000,
  }),
  cacheTtlMs: 2 * 60 * 1000,
  requestTimeoutMs: 5 * 1000,
  loadTimeoutMs: 16 * 1000,
  fetchLeaseMs: 18 * 1000,
  refreshCooldownMs: 30 * 1000,
  maxFetchesPerHour: 60,
  storagePrefix: "portfolio-tracker-cplus",
  staleAfterMinutes: 15,
});
