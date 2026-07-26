window.PORTFOLIO_CONFIG = Object.freeze({
  snapshotUrls: Object.freeze([
    "https://raw.githubusercontent.com/cliffordfok/portfolio-tracker/portfolio-data/portfolio-snapshot.json",
    "https://api.github.com/repos/cliffordfok/portfolio-tracker/contents/portfolio-snapshot.json?ref=portfolio-data",
  ]),
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
  fetchLeaseMs: 4 * 1000,
  refreshCooldownMs: 30 * 1000,
  maxFetchesPerHour: 60,
  storagePrefix: "portfolio-tracker-cplus",
  staleAfterMinutes: 15,
});
