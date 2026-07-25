window.PORTFOLIO_CONFIG = Object.freeze({
  snapshotUrl: "./data/portfolio-snapshot.json",
  fallbackUrls: Object.freeze({
    paper: "./data/paper.json",
    live: "./data/live.json",
    benchmark: "./data/benchmark.json",
  }),
  fallbackInitialCash: Object.freeze({
    paper: 100000,
    live: 50000,
  }),
  staleAfterMinutes: 15,
});
