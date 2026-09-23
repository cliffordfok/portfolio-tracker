// Keep the NYSE closure rules aligned with backend/portfolio_tracker/market_time.py.
const specialClosures = new Set([
  "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",
  "2004-06-11", "2007-01-02", "2012-10-29", "2012-10-30",
  "2018-12-05", "2025-01-09",
]);

const utcDay = (year, month, day) => new Date(Date.UTC(year, month - 1, day));
const isoDay = (date) => date.toISOString().slice(0, 10);

function nthWeekday(year, month, weekday, occurrence) {
  const first = utcDay(year, month, 1);
  return utcDay(year, month, 1 + (weekday - first.getUTCDay() + 7) % 7 + 7 * (occurrence - 1));
}

function observed(date) {
  const weekday = date.getUTCDay();
  if (weekday === 6) date.setUTCDate(date.getUTCDate() - 1);
  if (weekday === 0) date.setUTCDate(date.getUTCDate() + 1);
  return date;
}

function easterSunday(year) {
  const a = year % 19, b = Math.floor(year / 100), c = year % 100;
  const d = Math.floor(b / 4), e = b % 4, f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4), k = c % 4;
  const ell = (32 + 2 * e + 2 * i - h - k + 7 * 7) % 7;
  const m = Math.floor((a + 11 * h + 22 * ell) / 451);
  const month = Math.floor((h + ell - 7 * m + 114) / 31);
  const day = ((h + ell - 7 * m + 114) % 31) + 1;
  return utcDay(year, month, day);
}

function nyseHolidays(year) {
  const newYear = utcDay(year, 1, 1);
  // A Saturday New Year's Day is not observed on the previous Friday by NYSE.
  if (newYear.getUTCDay() === 0) newYear.setUTCDate(2);
  const goodFriday = easterSunday(year);
  goodFriday.setUTCDate(goodFriday.getUTCDate() - 2);
  const lastMay = utcDay(year, 5, 31);
  lastMay.setUTCDate(lastMay.getUTCDate() - (lastMay.getUTCDay() + 6) % 7);
  const dates = [
    newYear,
    nthWeekday(year, 1, 1, 3),
    nthWeekday(year, 2, 1, 3),
    goodFriday,
    lastMay,
    observed(utcDay(year, 7, 4)),
    nthWeekday(year, 9, 1, 1),
    nthWeekday(year, 11, 4, 4),
    observed(utcDay(year, 12, 25)),
  ];
  if (year >= 2022) dates.push(observed(utcDay(year, 6, 19)));
  return new Set(dates.map(isoDay));
}

const holidaysByYear = new Map();

export function isNyseSession(day) {
  if (typeof day !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
  const date = new Date(`${day}T00:00:00Z`);
  if (!Number.isFinite(date.getTime()) || isoDay(date) !== day) return false;
  if (date.getUTCDay() === 0 || date.getUTCDay() === 6) return false;
  const year = date.getUTCFullYear();
  if (!holidaysByYear.has(year)) holidaysByYear.set(year, nyseHolidays(year));
  return !holidaysByYear.get(year).has(day) && !specialClosures.has(day);
}
