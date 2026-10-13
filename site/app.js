"use strict";

/* Category colours, loosely following the AWS architecture icon palette. */
const CATEGORY = {
  "Compute": "#E8761C", "Containers": "#E8761C", "Storage": "#6E9A1A", "Database": "#B530C4",
  "Networking": "#7B4FE0", "Security": "#D13447", "Integration": "#D4146E", "Analytics": "#7B4FE0",
  "AI/ML": "#0F9A82", "Management": "#D4146E", "Developer Tools": "#3B6FD8", "Architecture": "#5B6B8C",
};
const LENGTHS = [5, 10, 20];
const LEGACY_STORE_KEY = "awsquiz:v1";   // scores kept in the browser before accounts existed
const AUTH_KEY = "awsquiz:auth";         // tokens (localStorage)
const PKCE_KEY = "awsquiz:pkce";         // sign-in round trip (sessionStorage)
const LINK_RETRY_KEY = "awsquiz:linkretry"; // one automatic retry after Google joins an email account

const main = document.getElementById("main");
let INDEX = null;           // data/index.json
let AUTHOR = null;          // data/author.json (optional)
let CONFIG = null;          // config.json: { auth: { domain, clientId } | null }
let SESSION = null;         // { access, id, refresh, exp, email } when signed in
let STORE = null;           // this user's progress, loaded from the API
let PROFILE = null;         // { name, needsName } for the leaderboard
const BANKS = new Map();    // service id -> bank
let run = null;             // the quiz in progress

/* ---------- small helpers ---------- */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") {
      // CSSOM instead of a style attribute, so a strict Content-Security-Policy allows it
      for (const decl of String(v).split(";")) {
        const i = decl.indexOf(":");
        if (i > 0) el.style.setProperty(decl.slice(0, i).trim(), decl.slice(i + 1).trim());
      }
    }
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function shuffle(a) {
  a = a.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}
function shortName(name) { return name.replace(/^(AWS|Amazon)\s+/i, ""); }
function symbol(svc) {
  if (svc.symbol) return svc.symbol;
  const n = shortName(svc.name);
  if (n.length <= 3) return n;
  const words = n.split(/[\s-]+/).filter(Boolean);
  if (words.length > 1) return words[0][0].toUpperCase() + words[1][0].toLowerCase();
  const rest = n.slice(1).replace(/[aeiou]/gi, "");
  return n[0].toUpperCase() + (rest[0] || n[1]).toLowerCase();
}
function mark(svc, cls) {
  // official service icon when the build found one, otherwise the letter symbol
  return svc.icon
    ? h("img", { class: `${cls} icon`, src: svc.icon, alt: "", width: 56, height: 56, decoding: "async" })
    : h("span", { class: cls, "aria-hidden": "true" }, symbol(svc));
}
function catColor(cat) { return CATEGORY[cat] || "var(--ink-soft)"; }
function setTitle(t) { document.title = t ? `${t} · Know Your Cloud` : "Know Your Cloud"; }
function focusMain() { main.focus({ preventScroll: true }); window.scrollTo(0, 0); }

/* Browser storage may be blocked: every read and write is guarded. */
function readStorage(area, key) {
  try { return JSON.parse(window[area].getItem(key)); } catch { return null; }
}
function writeStorage(area, key, value) {
  try { value == null ? window[area].removeItem(key) : window[area].setItem(key, JSON.stringify(value)); }
  catch { /* ignore */ }
}

/* ---------- sign-in (Cognito managed login, authorization code + PKCE) ---------- */
const authOn = () => Boolean(CONFIG && CONFIG.auth);
const signedIn = () => !authOn() || Boolean(SESSION);
const redirectUri = () => `${location.origin}/`;

function b64url(bytes) {
  return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomString(n = 32) { return b64url(crypto.getRandomValues(new Uint8Array(n))); }
function jwtPayload(token) {
  try { return JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return {}; }
}

// Federated sign-in providers turned on for this deployment, as Cognito names them
const IDP = { google: "Google" };
function providers() { return authOn() ? Object.keys(IDP).filter(p => CONFIG.auth[p]) : []; }
// email + password accounts: on when Google isn't set up, or turned on next to it
const emailOn = () => authOn() && (CONFIG.auth.email ?? providers().length === 0);

// mode: "signin" | "signup" | "google". A provider name skips the Cognito page; "signin" goes
// straight to the only provider when there's just one, otherwise to the Cognito page listing them all.
async function startSignIn(mode = "signin", returnTo = location.hash || "#/") {
  const verifier = randomString(48);
  const state = randomString(16);
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  writeStorage("sessionStorage", PKCE_KEY, { verifier, state, returnTo });
  const q = new URLSearchParams({
    client_id: CONFIG.auth.clientId, response_type: "code", scope: "openid email",
    redirect_uri: redirectUri(), state, code_challenge: challenge, code_challenge_method: "S256",
  });
  // Google-only sites skip the Cognito page entirely
  const direct = IDP[mode] || (!emailOn() && providers().length === 1 ? IDP[providers()[0]] : null);
  if (direct) q.set("identity_provider", direct);
  location.assign(`${CONFIG.auth.domain}/${mode === "signup" ? "signup" : "oauth2/authorize"}?${q}`);
}

async function tokenRequest(params) {
  const r = await fetch(`${CONFIG.auth.domain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CONFIG.auth.clientId, ...params }),
  });
  if (!r.ok) throw new Error(`sign-in failed (${r.status})`);
  return r.json();
}
function setSession(t, refresh) {
  SESSION = {
    access: t.access_token, id: t.id_token, refresh: refresh || t.refresh_token,
    exp: Date.now() + (t.expires_in || 3600) * 1000, email: jwtPayload(t.id_token).email || "",
  };
  writeStorage("localStorage", AUTH_KEY, SESSION);
}
function clearSession() { SESSION = null; STORE = null; PROFILE = null; writeStorage("localStorage", AUTH_KEY, null); }

/* Back from the sign-in page: ?code=...&state=... (or ?error=...). Returns a message to show, if any. */
async function finishSignIn() {
  const q = new URLSearchParams(location.search);
  if (!q.has("code") && !q.has("error")) return null;
  const pending = readStorage("sessionStorage", PKCE_KEY);
  writeStorage("sessionStorage", PKCE_KEY, null);
  const returnTo = (pending && pending.returnTo) || "#/";
  history.replaceState(null, "", `${location.pathname}${returnTo}`);
  if (q.has("error")) {
    const why = q.get("error_description") || "";
    // The first Google sign-in for an address that already has an email account links the two, and
    // Cognito fails that one attempt. Going straight back to Google works (no new prompt), so do it once.
    if (/already found an entry for username/i.test(why) && !readStorage("sessionStorage", LINK_RETRY_KEY)) {
      writeStorage("sessionStorage", LINK_RETRY_KEY, true);
      await startSignIn("google", returnTo);
      return "Signing you in…";
    }
    writeStorage("sessionStorage", LINK_RETRY_KEY, null);
    return why.replace(/^PreSignUp failed with error\s*/i, "") || "Sign-in was cancelled.";
  }
  writeStorage("sessionStorage", LINK_RETRY_KEY, null);
  if (!pending || pending.state !== q.get("state")) return "That sign-in link expired. Please sign in again.";
  try {
    setSession(await tokenRequest({
      grant_type: "authorization_code", code: q.get("code"),
      redirect_uri: redirectUri(), code_verifier: pending.verifier,
    }));
  } catch (err) {
    console.error(err);
    return "Sign-in didn't complete. Please try again.";
  }
  return null;
}

async function accessToken() {
  if (!SESSION) return null;
  if (Date.now() < SESSION.exp - 60_000) return SESSION.access;
  try { setSession(await tokenRequest({ grant_type: "refresh_token", refresh_token: SESSION.refresh }), SESSION.refresh); }
  catch { clearSession(); return null; }
  return SESSION.access;
}

function signOut() {
  const refresh = SESSION && SESSION.refresh;
  clearSession();
  if (!authOn()) return route();
  // revoke the refresh token, then end the Cognito session too so the next sign-in asks again
  const done = () => location.assign(`${CONFIG.auth.domain}/logout?${new URLSearchParams({
    client_id: CONFIG.auth.clientId, logout_uri: redirectUri() })}`);
  if (!refresh) return done();
  fetch(`${CONFIG.auth.domain}/oauth2/revoke`, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ token: refresh, client_id: CONFIG.auth.clientId }),
  }).catch(() => {}).finally(done);
}

/* ---------- API (questions and progress need a signed-in user) ---------- */
class SignedOut extends Error {}
async function api(path, opts = {}, retried = false) {
  const headers = { ...(opts.headers || {}) };
  if (authOn()) {
    const token = await accessToken();
    if (!token) throw new SignedOut("signed out");
    headers.authorization = `Bearer ${token}`;
  }
  const r = await fetch(`api/${path}`, { ...opts, headers, cache: "no-cache" });
  if (r.status === 401 && authOn()) {
    if (!retried && SESSION) { SESSION.exp = 0; return api(path, opts, true); }  // force a refresh once
    clearSession();
    throw new SignedOut("signed out");
  }
  if (!r.ok) {
    let msg = null;
    if (r.status === 400) { try { msg = (await r.json()).message; } catch { /* not JSON */ } }
    throw new Error(msg || `api/${path} returned ${r.status}`);
  }
  return r.json();
}

function mergeProgress(a, b) {
  const out = { best: { ...a.best }, missed: { ...a.missed } };
  for (const [k, v] of Object.entries(b.best || {})) {
    const cur = out.best[k];
    if (!cur || v.score / v.total > cur.score / cur.total) out.best[k] = v;
  }
  for (const [k, ids] of Object.entries(b.missed || {})) out.missed[k] = [...new Set([...(out.missed[k] || []), ...ids])];
  return out;
}
async function loadProgress() {
  if (STORE) return STORE;
  STORE = await api("progress");
  STORE.best = STORE.best || {};
  STORE.missed = STORE.missed || {};
  // one-time move of scores saved in this browser before sign-in existed
  const legacy = readStorage("localStorage", LEGACY_STORE_KEY);
  if (legacy && (Object.keys(legacy.best || {}).length || Object.keys(legacy.missed || {}).length)) {
    STORE = mergeProgress(STORE, legacy);
    try { await saveProgress(); writeStorage("localStorage", LEGACY_STORE_KEY, null); } catch { /* try again next time */ }
  }
  return STORE;
}
function loadStore() { return STORE || { best: {}, missed: {} }; }
async function saveProgress() {
  return api("progress", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(STORE) });
}
function saveStore(s) {
  STORE = s;
  saveProgress().catch(err => console.warn("progress not saved", err));
}

/* ---------- data ---------- */
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url} returned ${r.status}`);
  return r.json();
}
async function loadIndex() {
  if (!INDEX) INDEX = await getJSON("data/index.json");
  return INDEX;
}
async function loadConfig() {
  if (CONFIG === null) {
    try { CONFIG = await getJSON("config.json"); } catch { CONFIG = { auth: null }; }
    if (authOn()) SESSION = readStorage("localStorage", AUTH_KEY);
  }
  return CONFIG;
}
async function loadAuthor() {
  if (AUTHOR === null) {
    try { AUTHOR = await getJSON("data/author.json"); } catch { AUTHOR = false; }
    renderFooter();
    renderHeader();
  }
  return AUTHOR;
}
function authorLinks(a) {
  return Object.entries(a.links || {}).filter(([, url]) => /^https:\/\//.test(url || ""));
}
function renderHeader() {
  const nav = document.querySelector(".nav");
  const items = [];
  if (signedIn() && (SESSION || !authOn())) items.push(h("a", { class: "nav-link lb-link", href: "#/leaderboard" }, "Leaderboard"));
  if (AUTHOR) items.push(h("a", { class: "nav-link about-link", href: "#/about" }, "About"));
  if (authOn() && SESSION) {
    items.push(
      h("span", { class: "who", title: SESSION.email }, SESSION.email),
      h("button", { type: "button", class: "nav-link", onclick: signOut }, "Sign out"));
  } else if (providers().length) {
    items.push(h("button", { type: "button", class: "nav-link solid", onclick: () => startSignIn("signin", "#/") }, "Sign in"));
  } else if (authOn()) {
    items.push(
      h("button", { type: "button", class: "nav-link", onclick: () => startSignIn("signin", "#/") }, "Sign in"),
      h("button", { type: "button", class: "nav-link solid", onclick: () => startSignIn("signup", "#/") }, "Sign up"));
  }
  nav.replaceChildren(...items);
  nav.hidden = items.length === 0;
}
function renderFooter() {
  const foot = document.querySelector(".foot");
  if (!AUTHOR) return;
  foot.hidden = false;
  foot.replaceChildren(h("div", { class: "foot-in" },
    h("p", {}, "Built by ", h("a", { href: "#/about" }, AUTHOR.name),
      AUTHOR.title ? `, ${AUTHOR.title.toLowerCase()}` : "", ". Not affiliated with Amazon Web Services."),
    h("p", {}, `© ${new Date().getFullYear()} ${AUTHOR.name}. All rights reserved.`),
    authorLinks(AUTHOR).length > 0 && h("p", { class: "foot-links" },
      authorLinks(AUTHOR).map(([label, url]) => h("a", { href: url, target: "_blank", rel: "noopener me" }, label)))));
}

async function loadBank(id) {
  if (!BANKS.has(id)) BANKS.set(id, await api(`questions/${encodeURIComponent(id)}`));
  return BANKS.get(id);
}
async function questionsFor(id) {
  if (id !== "mixed") {
    const b = await loadBank(id);
    return b.questions.map(q => ({ ...q, service: b.service }));
  }
  const all = await Promise.all(INDEX.services.map(s => questionsFor(s.id)));
  return all.flat();
}

/* The logo lives once in index.html; views clone it (SVG can't be built with createElement). */
function heroArt() {
  const src = document.querySelector(".brand .logo");
  if (!src) return null;
  const svg = src.cloneNode(true);
  svg.removeAttribute("width"); svg.removeAttribute("height");
  return h("div", { class: "hero-art", "aria-hidden": "true" }, svg);
}

// Who wrote the questions and where they come from (welcome page only)
function credibility() {
  if (!AUTHOR) return null;
  const initials = AUTHOR.name.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
  return h("div", { class: "cred" },
    h("span", { class: "cred-avatar", "aria-hidden": "true" }, initials),
    h("p", {},
      "Written by ", h("a", { href: "#/about" }, AUTHOR.name), ", a principal engineer. ",
      "Questions come from real projects, proofs of concept and my own engineering notes. " +
      "Every answer is checked against the AWS docs, and I review and correct them as the site grows."));
}

/* Email sign-ups have no name, so the leaderboard would call them "Player 1A2B". Ask once. */
async function loadProfile() {
  if (!authOn() || !SESSION) return null;
  if (!PROFILE) { try { PROFILE = await api("profile"); } catch { PROFILE = { needsName: false }; } }
  return PROFILE;
}
function namePrompt(onSaved) {
  if (!PROFILE || !PROFILE.needsName) return null;
  const input = h("input", { type: "text", class: "name-input", maxlength: "40", autocomplete: "given-name",
    placeholder: "e.g. Alex Kim", "aria-label": "Your name for the leaderboard", required: true });
  const err = h("p", { class: "name-err", role: "alert" });
  const box = h("form", { class: "name-card", onsubmit: async (e) => {
      e.preventDefault();
      const btn = box.querySelector("button"); btn.disabled = true; err.textContent = "";
      try {
        PROFILE = await api("profile", { method: "PUT", headers: { "content-type": "application/json" },
                                         body: JSON.stringify({ name: input.value }) });
        box.replaceWith(h("p", { class: "name-done", role: "status" }, `You'll show as ${PROFILE.name} on the leaderboard.`));
        if (onSaved) onSaved();
      } catch (ex) { err.textContent = ex.message || "That didn't save. Try again."; btn.disabled = false; }
    } },
    h("label", {}, h("strong", {}, "What should we call you?"),
      h("span", {}, " Shown on the leaderboard as first name and last initial. Your email never appears.")),
    h("div", { class: "name-row" }, input, h("button", { type: "submit", class: "btn primary" }, "Save")),
    err);
  return box;
}

/* ---------- views ---------- */
function viewWelcome(message) {
  setTitle();
  const services = INDEX.services;
  const total = services.reduce((n, s) => n + s.count, 0);
  const cats = [...new Set(services.map(s => s.category))].sort();
  const feature = (title, text) => h("li", { class: "feature" }, h("h2", {}, title), h("p", {}, text));
  main.replaceChildren(h("div", { class: "wrap" },
    message && h("p", { class: "notice", role: "alert" }, message),
    h("section", { class: "hero welcome" },
      heroArt(),
      h("p", { class: "eyebrow" }, providers().length && emailOn()
        ? "Free · Sign in with Google or your email"
        : providers().length ? "Free · Sign in with your Google account" : "Free · Sign up with your email"),
      h("h1", {}, "How well do you know AWS?"),
      h("p", { class: "lede" },
        "Short, practical quizzes on the AWS services engineers use every day. " +
        "Every answer comes with an explanation, so you learn as you go."),
      h("p", { class: "stats" },
        h("strong", {}, total), " questions across ", h("strong", {}, services.length), " topics"),
      credibility(),
      providers().length && emailOn()
        ? h("div", { class: "signin-choices" },
            h("div", { class: "actions" },
              h("button", { type: "button", class: "btn primary",
                onclick: () => startSignIn("google", "#/") }, "Continue with Google"),
              h("button", { type: "button", class: "btn ghost",
                onclick: () => startSignIn("signup", "#/") }, "Sign up with email")),
            h("p", { class: "note" }, "Already have an account? ",
              h("button", { type: "button", class: "linklike", onclick: () => startSignIn("signin", "#/") }, "Sign in")))
        : providers().length
        ? h("div", { class: "actions" },
            h("button", { type: "button", class: "btn primary",
              onclick: () => startSignIn("google", "#/") }, "Continue with Google"))
        : h("div", { class: "actions" },
            h("button", { type: "button", class: "btn primary", onclick: () => startSignIn("signup", "#/") }, "Create a free account"),
            h("button", { type: "button", class: "btn ghost", onclick: () => startSignIn("signin", "#/") }, "Sign in"))),
    h("ul", { class: "features" },
      feature("Learn, don't just score", "Each question explains the right answer and links to the AWS docs."),
      feature("Pick your level", "Choose how many questions and how hard, per service or mixed."),
      feature("Progress that follows you", "Best scores and missed questions are saved to your account, on any device.")),
    h("section", { class: "topics" },
      h("h2", {}, "Topics"),
      // chips look tappable, so make them work: sign in, then land straight in that topic
      h("p", { class: "topics-hint" },
        CONFIG.auth.google && !emailOn() ? "Pick a topic to sign in with Google and start right away."
          : "Pick a topic to sign in and start right away."),
      h("ul", { class: "topic-chips" }, services.map(s =>
        h("li", { style: `--cat:${catColor(s.category)}` },
          h("button", { type: "button", "aria-label": `Sign in to start ${s.name}`,
                        onclick: () => startSignIn("signin", `#/service/${s.id}`) },
            mark(s, "mini"), shortName(s.name))))),
      h("p", { class: "note" }, cats.join(" · ")))));
  focusMain();
}

function viewHome() {
  setTitle();
  const store = loadStore();
  const services = INDEX.services;
  const total = services.reduce((n, s) => n + s.count, 0);
  const cats = [...new Set(services.map(s => s.category))].sort();
  let query = "", cat = "all";

  const tile = (s) => {
    const best = store.best[s.id];
    const pct = best ? Math.round((best.score / best.total) * 100) : null;
    return h("li", { "data-id": s.id },
      h("a", { class: "el", href: `#/service/${s.id}`, style: `--cat:${catColor(s.category)}`,
               "aria-label": `${s.name}, ${s.category}, ${s.count} questions${best ? `, best ${best.score} of ${best.total}` : ""}` },
        h("span", { class: "el-top" },
          mark(s, "sym"),
          h("span", { class: "count", "aria-hidden": "true" }, s.count)),
        h("span", { class: "name", title: s.name }, shortName(s.name)),
        h("span", { class: "cat", "aria-hidden": "true" }, s.category),
        pct !== null && h("span", { class: "bar", "aria-hidden": "true", title: `Best ${best.score}/${best.total}` },
          h("i", { style: `width:${pct}%` }))));
  };

  const grid = h("ul", { class: "table", "aria-label": "Topics" }, services.map(tile));
  const empty = h("p", { class: "empty", hidden: true }, "No topics match. Try another word or category.");
  const status = h("p", { class: "sr-only", role: "status" });

  function apply() {
    const q = query.trim().toLowerCase();
    let shown = 0;
    for (const li of grid.children) {
      const s = services.find(x => x.id === li.dataset.id);
      const hit = (cat === "all" || s.category === cat) &&
        (!q || `${s.name} ${s.category} ${s.id}`.toLowerCase().includes(q));
      li.hidden = !hit;
      if (hit) shown++;
    }
    empty.hidden = shown > 0;
    status.textContent = `${shown} of ${services.length} topics shown`;
  }

  const search = h("input", { type: "search", class: "search", placeholder: "Search topics", "aria-label": "Search topics",
    oninput: (e) => { query = e.target.value; apply(); } });
  const filterBtn = (value, label) => h("button", { type: "button", class: "filter", "aria-pressed": String(value === cat),
    style: value === "all" ? null : `--cat:${catColor(value)}`,
    onclick: (e) => {
      cat = value;
      for (const b of e.currentTarget.parentElement.children) b.setAttribute("aria-pressed", String(b === e.currentTarget));
      apply();
    } }, label);

  main.replaceChildren(h("div", { class: "wrap" },
    h("section", { class: "hero" },
      heroArt(),
      SESSION && SESSION.email && h("p", { class: "eyebrow" }, `Signed in as ${SESSION.email}`),
      h("h1", {}, "How well do you know AWS?"),
      h("p", { class: "lede" },
        "Short, practical quizzes on the AWS services engineers use every day. " +
        "Every answer comes with an explanation, so you learn as you go."),
      h("p", { class: "stats" },
        h("strong", {}, total), " questions across ", h("strong", {}, services.length), " topics"),
      services.length > 1 && h("div", { class: "actions" },
        h("a", { class: "btn primary", href: "#/service/mixed" }, "Start a mixed quiz"),
        h("a", { class: "btn ghost", href: "#/leaderboard" }, "Leaderboard"))),
    namePrompt(),
    services.length === 0
      ? h("p", { class: "empty" }, "No questions are published yet.")
      : [
          h("div", { class: "tools" },
            search,
            cats.length > 1 && h("div", { class: "filters", role: "group", "aria-label": "Filter by category" },
              filterBtn("all", "All"), cats.map(c => filterBtn(c, c)))),
          grid, empty, status,
        ]));
}

async function viewSetup(id) {
  const svc = id === "mixed"
    ? { id: "mixed", name: "Mixed quiz", category: null, symbol: "All",
        count: INDEX.services.reduce((n, s) => n + s.count, 0) }
    : INDEX.services.find(s => s.id === id);
  if (!svc) return viewNotFound();
  setTitle(svc.name);
  const store = loadStore();
  const missed = (store.missed[id] || []);
  const lengths = LENGTHS.filter(n => n < svc.count).concat([svc.count]);
  const defaultLen = lengths.includes(10) ? 10 : lengths[lengths.length - 1];

  const chip = (name, value, label, checked, disabled) =>
    h("label", {}, h("input", { type: "radio", name, value, checked, disabled }), h("span", {}, label));

  const form = h("form", { class: "panel", onsubmit: (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      startQuiz(id, { length: Number(f.get("length")), difficulty: f.get("difficulty") });
    } },
    h("fieldset", {}, h("legend", {}, "Questions"),
      h("div", { class: "chips" }, lengths.map(n =>
        chip("length", n, n === svc.count ? `All ${n}` : n, n === defaultLen)))),
    h("fieldset", {}, h("legend", {}, "Difficulty"),
      h("div", { class: "chips" },
        ["any", "easy", "medium", "hard"].map(d => chip("difficulty", d, d[0].toUpperCase() + d.slice(1), d === "any")))),
    h("p", { class: "note" }, "If a difficulty has fewer questions than you picked, you get all of them."),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", type: "submit" }, "Start quiz"),
      missed.length > 0 && h("button", { class: "btn ghost", type: "button",
        onclick: () => startQuiz(id, { only: missed }) }, `Retry ${missed.length} missed`)));

  main.replaceChildren(h("div", { class: "wrap narrow" },
    h("a", { class: "back", href: "#/" }, "All services"),
    h("div", { class: "setup-head", style: `--cat:${catColor(svc.category)}` },
      mark(svc, "sym"),
      h("div", {}, h("h1", {}, svc.name),
        h("p", {}, `${svc.count} questions${svc.category ? ` · ${svc.category}` : ""}`))),
    form));
}

async function startQuiz(id, opts) {
  try { await buildQuiz(id, opts); } catch (err) { console.error(err); viewError(err); }
}
async function buildQuiz(id, { length, difficulty, only } = {}) {
  let pool = await questionsFor(id);
  if (only) pool = pool.filter(q => only.includes(q.id));
  else if (difficulty && difficulty !== "any") pool = pool.filter(q => q.difficulty === difficulty);
  pool = shuffle(pool).slice(0, length || pool.length);
  if (pool.length === 0) {
    main.replaceChildren(h("div", { class: "wrap narrow" },
      h("a", { class: "back", href: `#/service/${id}` }, "Back"),
      h("p", { class: "empty" }, "No questions match. Pick a different difficulty.")));
    return;
  }
  run = {
    id, i: 0, answers: [],
    questions: pool.map(q => ({ ...q, options: shuffle(q.options) })),
  };
  const target = `#/quiz/${id}`;
  if (location.hash === target) route();   // e.g. "Retry missed" from the results page: no hashchange
  else location.hash = target;
}

function viewQuestion() {
  const q = run.questions[run.i];
  const n = run.questions.length;
  const multi = q.type === "multi";
  const need = q.need || (q.answer ? q.answer.length : 1);   // the API sends "need", not the answer
  const picked = new Set();
  let locked = false;
  let checking = false;
  setTitle(`Question ${run.i + 1} of ${n}`);

  const track = h("div", { class: "track", "aria-hidden": "true" },
    run.questions.map((_, k) => h("i", {
      class: k < run.answers.length ? (run.answers[k].correct ? "ok" : "no") : k === run.i ? "now" : "" })));

  const error = h("p", { class: "error", role: "alert" });
  const list = h("ul", { class: "options", role: multi ? "group" : "radiogroup", "aria-label": "Answers" });
  const rows = q.options.map((o, k) => {
    const input = h("input", { type: multi ? "checkbox" : "radio", name: "opt", value: o.id });
    const row = h("label", { class: `opt${multi ? " multi" : ""}` },
      input, h("span", { class: "key", "aria-hidden": "true" }, k + 1), h("span", {}, o.text));
    input.addEventListener("change", () => {
      if (locked) return;
      if (!multi) picked.clear();
      input.checked ? picked.add(o.id) : picked.delete(o.id);
      rows.forEach(r => r.row.classList.toggle("picked", picked.has(r.o.id)));
      error.textContent = "";
    });
    list.append(h("li", {}, row));
    return { o, row, input };
  });

  const feedbackSlot = h("div", { "aria-live": "polite" });
  const button = h("button", { class: "btn primary", type: "button" }, "Check answer");

  async function check() {
    if (checking) return;
    if (picked.size === 0) { error.textContent = multi ? `Choose ${need} answers first.` : "Choose an answer first."; return; }
    if (multi && picked.size !== need) { error.textContent = `Choose exactly ${need} answers.`; return; }
    // graded on the server: the browser never has the answers
    checking = true;
    button.disabled = true;
    button.textContent = "Checking…";
    let res;
    try {
      res = await api("answer", { method: "POST", headers: { "content-type": "application/json" },
                                   body: JSON.stringify({ qid: q.id, picked: [...picked] }) });
    } catch (err) {
      checking = false;
      button.disabled = false;
      button.textContent = "Check answer";
      if (err instanceof SignedOut) return viewError(err);
      error.textContent = "Couldn't check that answer. Check your connection and try again.";
      return;
    }
    checking = false;
    button.disabled = false;
    locked = true;
    const right = new Set(res.answer);
    const correct = res.correct;
    run.answers.push({ id: q.id, correct, picked: [...picked], service: q.service, point: res.point });
    list.closest(".qbody").classList.add("locked");
    rows.forEach(({ o, row, input }) => {
      input.disabled = true;
      if (right.has(o.id)) { row.classList.add("is-right"); row.append(h("span", { class: "tag" }, "Correct")); }
      else if (picked.has(o.id)) { row.classList.add("is-wrong"); row.append(h("span", { class: "tag" }, "Your pick")); }
    });
    track.children[run.i].className = correct ? "ok" : "no";
    const refs = [];
    const slideRef = res.slideRef ?? q.slideRef;
    const docUrl = res.docUrl ?? q.docUrl;
    if (slideRef) refs.push(`Covered on slide ${slideRef} of the ${shortName(q.service.name)} session.`);
    feedbackSlot.append(h("div", { class: `feedback ${correct ? "good" : "bad"}` },
      h("h2", {}, correct ? "Correct" : multi ? "Not quite: all correct answers are needed" : "Not quite",
        res.point && h("span", { class: "pt" }, "+1 point")),
      h("p", {}, res.explanation),
      (docUrl || refs.length) && h("p", { class: "refs" }, refs.join(" "),
        docUrl && [refs.length ? " " : "", h("a", { href: docUrl, target: "_blank", rel: "noopener" }, "Read the AWS docs")])));
    button.textContent = run.i + 1 < n ? "Next question" : "See results";
    button.focus();
  }
  function next() {
    run.i += 1;
    run.i < n ? viewQuestion() : viewResults();
  }
  button.addEventListener("click", () => (locked ? next() : check()));

  main.replaceChildren(h("div", { class: "wrap narrow" },
    track,
    h("div", { class: "meta" },
      h("span", {}, `Question ${run.i + 1} of ${n}`),
      h("span", {}, `${run.id === "mixed" ? shortName(q.service.name) + " · " : ""}${q.difficulty}`)),
    h("div", { class: "qbody" },
      h("p", { class: "prompt", id: "prompt" }, q.prompt),
      multi && h("p", { class: "hint" }, `Choose ${need}.`),
      list),
    error,
    feedbackSlot,
    h("div", { class: "actions" }, button),
    h("p", { class: "keys" }, h("kbd", {}, "1"), "–", h("kbd", {}, String(q.options.length)),
      " to choose, ", h("kbd", {}, "Enter"), " to check and continue")));
  list.setAttribute("aria-labelledby", "prompt");

  run.keyHandler = (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target.tagName === "BUTTON" && e.key === "Enter") return; // button handles its own Enter
    const k = Number(e.key);
    if (!locked && !checking && k >= 1 && k <= rows.length) {
      const inp = rows[k - 1].input;
      inp.checked = multi ? !inp.checked : true;
      inp.dispatchEvent(new Event("change"));
      e.preventDefault();
    } else if (e.key === "Enter") {
      button.click();
      e.preventDefault();
    }
  };
  focusMain();
}

function viewResults() {
  const ans = run.answers;
  const score = ans.filter(a => a.correct).length;
  const missed = ans.filter(a => !a.correct).map(a => a.id);
  const store = loadStore();
  const prev = store.best[run.id];
  if (!prev || score / ans.length > prev.score / prev.total) {
    store.best[run.id] = { score, total: ans.length, at: new Date().toISOString() };
  }
  // keep a running list of missed questions; answering one right removes it
  const known = new Set(store.missed[run.id] || []);
  ans.forEach(a => (a.correct ? known.delete(a.id) : known.add(a.id)));
  store.missed[run.id] = [...known];
  saveStore(store);
  setTitle(`${score} of ${ans.length}`);

  const byId = new Map(run.questions.map(q => [q.id, q]));
  const verdict = score === ans.length ? "Perfect run." : score / ans.length >= 0.7 ? "Solid." : "Worth another pass.";
  const points = ans.filter(a => a.point).length;
  main.replaceChildren(h("div", { class: "wrap narrow" },
    h("a", { class: "back", href: "#/" }, "All services"),
    h("p", { class: "score" }, score, h("small", {}, ` / ${ans.length}`)),
    h("p", { class: "lede" }, verdict, missed.length ? ` You missed ${missed.length}; they're saved so you can retry just those.` : ""),
    h("p", { class: "points-line" },
      points > 0
        ? [h("strong", {}, `+${points} leaderboard point${points === 1 ? "" : "s"}`), " · "]
        : "No new points this time: points come from your first answer to each question. ",
      h("a", { href: "#/leaderboard" }, "See the leaderboard")),
    h("div", { class: "actions" },
      missed.length > 0 && h("button", { class: "btn primary", type: "button",
        onclick: () => startQuiz(run.id, { only: missed }) }, `Retry ${missed.length} missed`),
      h("a", { class: "btn ghost", href: `#/service/${run.id}` }, "New quiz"),
      h("a", { class: "btn ghost", href: "#/" }, "All services")),
    h("ol", { class: "review", "aria-label": "Your answers" }, ans.map(a => {
      const q = byId.get(a.id);
      return h("li", {},
        h("span", { class: `mark ${a.correct ? "ok" : "no"}`, "aria-label": a.correct ? "Correct" : "Missed" },
          a.correct ? "✓" : "✗"),
        h("span", {}, q.prompt,
          run.id === "mixed" && h("span", { class: "svc" }, ` · ${shortName(q.service.name)}`)));
    }))));
  run.keyHandler = null;
  focusMain();
}

async function viewLeaderboard(which = "week") {
  setTitle("Leaderboard");
  const tab = (value, label) => h("button", { type: "button", class: "filter", "aria-pressed": String(value === which),
    onclick: () => { if (value !== which) viewLeaderboard(value); } }, label);
  const body = h("div", { class: "board-body", "aria-live": "polite" }, h("p", { class: "note" }, "Loading…"));
  main.replaceChildren(h("div", { class: "wrap narrow" },
    h("a", { class: "back", href: "#/" }, "All services"),
    h("h1", { class: "page-title" }, "Leaderboard"),
    h("p", { class: "lede" }, "One point for each question you get right on your first try. " +
      "The weekly board starts fresh every Monday."),
    namePrompt(() => viewLeaderboard(which)),
    h("div", { class: "filters board-tabs", role: "group", "aria-label": "Period" },
      tab("week", "This week"), tab("all", "All time")),
    body));
  let data;
  try { data = await api(`leaderboard?board=${which}`); }
  catch (err) {
    if (err instanceof SignedOut) return viewError(err);
    body.replaceChildren(h("p", { class: "empty" }, "The leaderboard didn't load. Try again in a moment."));
    return;
  }
  if (!data.top.length) {
    body.replaceChildren(h("p", { class: "empty" },
      which === "week" ? "No points yet this week. " : "No points yet. ",
      h("a", { href: "#/" }, "Take a quiz"), " to get on the board."));
    return;
  }
  const meInTop = data.top.some(r => r.me);
  const row = (r) => h("li", { class: `board-row${r.me ? " me" : ""}${r.rank <= 3 ? ` top${r.rank}` : ""}` },
    h("span", { class: "rank" }, r.rank),
    h("span", { class: "who-name" }, r.name, r.me && h("span", { class: "you" }, "You")),
    h("span", { class: "pts" }, r.points, h("small", {}, r.points === 1 ? " pt" : " pts")));
  body.replaceChildren(...[
    h("ol", { class: "board", "aria-label": which === "week" ? "This week" : "All time" }, data.top.map(row)),
    data.me && !meInTop && h("ol", { class: "board mine", "aria-label": "Your rank" },
      row({ rank: data.me.rank, name: data.me.name || "You", points: data.me.points, me: true })),
    !data.me && h("p", { class: "note" }, "You're not on this board yet. Get a question right on your first try to join."),
  ].filter(Boolean));
  focusMain();
}

function viewAbout() {
  if (!AUTHOR) return viewNotFound();
  const a = AUTHOR;
  setTitle(`About ${a.name}`);
  const initials = a.name.split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase();
  main.replaceChildren(h("div", { class: "wrap narrow" },
    h("a", { class: "back", href: "#/" }, "All services"),
    h("div", { class: "setup-head about-head", style: "--cat:var(--accent)" },
      h("span", { class: "sym", "aria-hidden": "true" }, initials),
      h("div", {}, h("h1", {}, a.name),
        h("p", {}, [a.title, a.location].filter(Boolean).join(" · ")))),
    (a.bio || []).map(p => h("p", { class: "bio" }, p)),
    authorLinks(a).length > 0 && h("ul", { class: "creds links" }, authorLinks(a).map(([label, url]) =>
      h("li", {}, h("a", { href: url, target: "_blank", rel: "noopener me" }, label))))));
  focusMain();
}

function viewNotFound() {
  setTitle("Not found");
  main.replaceChildren(h("div", { class: "wrap narrow" },
    h("p", { class: "empty" }, "That page doesn't exist. ", h("a", { href: "#/" }, "Go to all services"), ".")));
}

function viewError(err) {
  if (err instanceof SignedOut) { renderHeader(); return viewWelcome("Your session ended. Please sign in again."); }
  main.replaceChildren(h("div", { class: "wrap narrow" },
    h("h1", {}, "Questions didn't load"),
    h("p", { class: "lede" }, `${err.message}. If you opened index.html straight from disk, serve the folder ` +
      "instead (python -m http.server) because browsers block loading data from file:// pages.")));
}

/* ---------- routing ---------- */
let SIGNIN_MESSAGE = null;
async function route() {
  if (run) run.keyHandler = null;
  const [, view, id] = location.hash.replace(/^#\/?/, "#/").split("/");
  try {
    await Promise.all([loadIndex(), loadAuthor(), loadConfig()]);
    renderHeader();
    if (view === "about") return viewAbout();
    if (!signedIn()) {
      // anything but About needs an account; remember where they were going
      const msg = SIGNIN_MESSAGE; SIGNIN_MESSAGE = null;
      if (view && !msg) return startSignIn("signin", location.hash);
      if (view) history.replaceState(null, "", `${location.pathname}#/`);
      return viewWelcome(msg);
    }
    await Promise.all([loadProgress(), loadProfile()]);
    if (!view) return viewHome();
    if (view === "leaderboard") return viewLeaderboard(id === "all" ? "all" : "week");
    if (view === "service" && id) return viewSetup(id);
    if (view === "quiz" && run && run.id === id) return viewQuestion();
    if (view === "quiz" && id) { location.replace(`#/service/${id}`); return; }
    viewNotFound();
  } catch (err) {
    console.error(err);
    viewError(err);
  }
}
document.addEventListener("keydown", (e) => run && run.keyHandler && run.keyHandler(e));
window.addEventListener("hashchange", route);
(async () => {
  await loadConfig();
  if (authOn()) SIGNIN_MESSAGE = await finishSignIn();
  route();
})();
