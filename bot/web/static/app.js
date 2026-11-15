"use strict";

// Everything comes from /api/guilds/<id> (bot/web/data.py) in one JSON, and
// every view below is rendered from it in the browser. Routes live in the
// URL hash: #/classement, #/joueur/<id>, #/echanges/<id>, #/animaux.

const state = {
  guilds: [],
  guildId: null,
  data: null,
  players: new Map(), // id -> player, with a few derived fields (see index())
  sort: { key: "score", desc: true },
  animalsFilter: { text: "", rarity: "", collection: "", owned: true, doubles: false },
  animalsCards: false, // "Fiches" view instead of the table
  tradesSingles: false,
  me: null, // the signed-in Discord user ({id, name, avatar}), if sign-in is on
};

const nf = new Intl.NumberFormat("fr-FR");
const nf1 = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const RARITY_RANK = { common: 0, uncommon: 1, rare: 2, epic: 3, legendary: 4, mythic: 5 };
const IUCN_LABEL = {
  LC: "Préoccupation mineure", NT: "Quasi menacée", VU: "Vulnérable", EN: "En danger",
  CR: "En danger critique", EW: "Éteinte à l'état sauvage", EX: "Éteinte", DD: "Données insuffisantes",
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function storage(key, value) {
  // Per-viewer conveniences only (last guild); the page works without it.
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { return null; }
}

async function fetchJSON(url) {
  const response = await fetch(url);
  if (response.status === 401) { showLogin(); throw new Error("connexion requise"); }
  if (!response.ok) throw new Error(`${url} : ${response.status}`);
  return response.json();
}

// ---------- Data ----------

// Sign in with Discord (bot/web/auth.py): only the servers the visitor is in.
function showLogin() {
  const failed = new URLSearchParams(location.search).has("login");
  main().innerHTML = `<div class="card login">
    <h2>Connexion</h2>
    <p>Connecte-toi avec ton compte Discord : le site montre les serveurs dont tu es membre.</p>
    ${failed ? `<p class="small" style="color:var(--warn)">La connexion n'a pas abouti, réessaie.</p>` : ""}
    <a class="login-button" href="/login">Se connecter avec Discord</a>
  </div>`;
}

function showUser() {
  const box = document.getElementById("user");
  if (!state.me) return;
  box.hidden = false;
  box.innerHTML = `<img class="avatar" src="${esc(state.me.avatar)}" alt=""><span>${esc(state.me.name)}</span>
    <a href="/logout" class="small">Se déconnecter</a>`;
}

async function boot() {
  try {
    const me = await fetchJSON("/api/me");
    state.me = me.user;
    if (me.auth && !me.user) { showLogin(); return; }
    showUser();
    if (location.search) history.replaceState(null, "", location.pathname + location.hash);
    state.guilds = await fetchJSON("/api/guilds");
  } catch (error) {
    if (error.message !== "connexion requise") {
      main().innerHTML = `<p class="empty">Impossible de joindre le serveur (${esc(error.message)}).</p>`;
    }
    return;
  }
  if (!state.guilds.length) {
    main().innerHTML = state.me
      ? `<p class="empty">Tu n'es membre d'aucun serveur où le bot a des joueurs.</p>`
      : `<p class="empty">Aucun serveur n'a encore de joueur dans la base.</p>`;
    return;
  }
  const saved = storage("guild");
  state.guildId = state.guilds.some((g) => g.id === saved) ? saved : state.guilds[0].id;

  const select = document.getElementById("guild-select");
  if (state.guilds.length > 1) {
    select.hidden = false;
    select.innerHTML = state.guilds.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("");
    select.value = state.guildId;
    select.addEventListener("change", async () => {
      state.guildId = select.value;
      storage("guild", select.value);
      await load();
      render();
    });
  }
  await load();
  window.addEventListener("hashchange", render);
  render();
}

async function load() {
  main().innerHTML = `<p class="muted">Chargement…</p>`;
  state.data = await fetchJSON(`/api/guilds/${state.guildId}`);
  index();
  const guild = state.data.guild;
  document.getElementById("guild-name").textContent = guild.name;
  document.title = `${guild.name} · Réserve des animaux`;
  const icon = document.getElementById("guild-icon");
  icon.hidden = !guild.icon;
  if (guild.icon) icon.src = guild.icon;
  const at = new Date(state.data.generatedAt * 1000);
  document.getElementById("updated").textContent =
    `Mis à jour à ${at.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })} · recharge la page pour actualiser`;
}

// Derived lookups on each player, used by every view.
function index() {
  state.players.clear();
  for (const p of state.data.players) {
    p.bySpecies = new Map(); // animal id -> copies, best aura first
    for (const copy of p.copies) {
      if (!p.bySpecies.has(copy.animal)) p.bySpecies.set(copy.animal, []);
      p.bySpecies.get(copy.animal).push(copy);
    }
    for (const copies of p.bySpecies.values()) copies.sort((a, b) => b.appeal - a.appeal);
    p.wishSet = new Set(p.wishes);
    state.players.set(p.id, p);
  }
}

const animal = (id) => state.data.animals[id];
const sortedPlayers = () => [...state.players.values()].sort((a, b) => b.score - a.score);

// ---------- Small renderers ----------

const main = () => document.getElementById("main");

function who(p, link = true) {
  const inner = `<img class="avatar" src="${esc(p.avatar)}" alt="" loading="lazy">${esc(p.name)}`;
  return link ? `<a class="who" href="#/joueur/${p.id}">${inner}</a>` : `<span class="who">${inner}</span>`;
}

function stars(n) {
  return `<span class="stars" title="Aura ${n}/5">${"★".repeat(n)}${"☆".repeat(5 - n)}</span>`;
}

function rarityLabel(key) {
  return state.data.rarities.find((r) => r.key === key).label;
}

function animalName(id, { dot = true } = {}) {
  const a = animal(id);
  const name = a.wiki ? `<a href="${esc(a.wiki)}" target="_blank" rel="noopener">${esc(a.name)}</a>` : esc(a.name);
  return `${dot ? `<span class="dot ${a.rarity}" title="${esc(rarityLabel(a.rarity))}"></span>` : ""}${name}`;
}

function collectionChips(id) {
  return animal(id).collections.map((i) => `<span class="chip">${esc(state.data.collections[i].label)}</span>`).join("");
}

function bar(count, total) {
  const pct = total ? Math.min(100, (count / total) * 100) : 0;
  return `<div class="bar ${count >= total && total ? "done" : ""}"><span style="width:${pct}%"></span></div>`;
}

function stat(label, value, sub = "") {
  return `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function badges(p) {
  return p.milestonesDone.map((i) => state.data.milestones[i].badge).join(" ");
}

function playerSelect(current, route, placeholder) {
  return `<select data-route="${route}" class="player-select" aria-label="Joueur">
    ${placeholder ? `<option value="">${placeholder}</option>` : ""}
    ${sortedPlayers().map((p) => `<option value="${p.id}" ${p.id === current ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
  </select>`;
}

// ---------- Router ----------

function render() {
  if (!state.data) return;
  const [, tab = "classement", arg] = location.hash.split("/");
  for (const link of document.querySelectorAll(".tabs a")) {
    link.classList.toggle("active", link.dataset.tab === tab);
  }
  const views = { classement: viewRanking, joueur: viewPlayer, echanges: viewTrades, animaux: viewAnimals };
  main().innerHTML = (views[tab] || viewRanking)(arg);
  for (const select of main().querySelectorAll(".player-select")) {
    select.addEventListener("change", () => { location.hash = `#/${select.dataset.route}/${select.value}`; });
  }
  bind[tab]?.();
}

const bind = {};

// ---------- Classement ----------

const RANKING_COLUMNS = [
  { key: "score", label: "Score", value: (p) => p.score, show: (p) => nf.format(p.score) },
  { key: "income", label: "Baies/h", value: (p) => p.income, show: (p) => nf1.format(p.income) },
  { key: "points", label: "Baies", value: (p) => p.points, show: (p) => nf.format(p.points) },
  { key: "species", label: "Espèces", value: (p) => p.species, show: (p) => `${p.species}` },
  { key: "collections", label: "Collections", value: (p) => p.collectionsDone.length, show: (p) => `${p.collectionsDone.length}/${state.data.collections.length}` },
  { key: "milestones", label: "Milestones", value: (p) => p.milestonesDone.length, show: (p) => badges(p) || "—" },
  { key: "rescues", label: "Sauvetages", value: (p) => p.rescues, show: (p) => nf.format(p.rescues) },
];

function viewRanking() {
  const { key, desc } = state.sort;
  const column = RANKING_COLUMNS.find((c) => c.key === key);
  const players = [...state.players.values()].sort((a, b) => (desc ? -1 : 1) * (column.value(a) - column.value(b)));
  const total = Object.keys(state.data.animals).length;
  const all = [...state.players.values()];
  const sum = (f) => all.reduce((s, p) => s + f(p), 0);
  const speciesHere = new Set(all.flatMap((p) => [...p.bySpecies.keys()])).size;
  return `
    <div class="grid stats" style="margin-top:16px">
      ${stat("Joueurs", all.length)}
      ${stat("Animaux sauvés", nf.format(sum((p) => p.rescues)))}
      ${stat("Espèces sur le serveur", `${speciesHere}<span class="muted small"> / ${total}</span>`, `${Math.round((speciesHere / total) * 100)} % du jeu`)}
      ${stat("Revenu total", `${nf1.format(sum((p) => p.income))}`, "baies/h, tous les totems")}
    </div>
    <h2>Classement</h2>
    <p class="muted small">Clique sur une colonne pour trier, sur un joueur pour voir son profil.</p>
    <div class="table-wrap"><table>
      <thead><tr><th class="rank">#</th><th>Joueur</th>
        ${RANKING_COLUMNS.map((c) => `<th class="num sortable ${c.key === key ? "sorted" : ""}" data-sort="${c.key}">${c.label}</th>`).join("")}
      </tr></thead>
      <tbody>
        ${players.map((p, i) => `<tr class="clickable" data-player="${p.id}">
          <td class="rank">${i + 1}</td><td>${who(p, false)}</td>
          ${RANKING_COLUMNS.map((c) => `<td class="num">${c.show(p)}</td>`).join("")}
        </tr>`).join("")}
      </tbody>
    </table></div>`;
}

bind.classement = () => {
  for (const th of main().querySelectorAll("th[data-sort]")) {
    th.addEventListener("click", () => {
      state.sort = { key: th.dataset.sort, desc: true };
      render();
    });
  }
  for (const row of main().querySelectorAll("tr[data-player]")) {
    row.addEventListener("click", () => { location.hash = `#/joueur/${row.dataset.player}`; });
  }
};

// ---------- Joueur ----------

function viewPlayer(id) {
  const p = state.players.get(id);
  if (!p) {
    return `<div class="toolbar">${playerSelect("", "joueur", "Choisis un joueur…")}</div>
      <div class="grid cards">${sortedPlayers().map((q) => `<a class="card" href="#/joueur/${q.id}">${who(q, false)}
        <div class="muted small" style="margin-top:6px">Score ${nf.format(q.score)} · ${nf1.format(q.income)} baies/h ${badges(q)}</div></a>`).join("")}</div>`;
  }
  const d = state.data;
  const totems = p.copies.filter((c) => c.totem).sort((a, b) => incomeOf(b) - incomeOf(a));
  const upgrades = d.upgrades.map((u) => {
    const level = p.upgrades[u.key];
    return `<span class="chip">${u.label} : <b>${level}</b>${u.max != null ? `/${u.max}` : ""}</span>`;
  }).join("");

  return `
    <div class="toolbar">${playerSelect(p.id, "joueur")}<a href="#/echanges/${p.id}">Voir ses échanges possibles →</a></div>
    <div class="player-head">
      <img src="${esc(p.avatar)}" alt="">
      <div><h2>${esc(p.name)}</h2><div class="badges">${badges(p) || '<span class="muted small">Pas encore de milestone</span>'}</div></div>
    </div>
    <div class="grid stats">
      ${stat("Score", nf.format(p.score))}
      ${stat("Baies", nf.format(p.points), `record : ${nf.format(p.peak)}`)}
      ${stat("Revenu des totems", `${nf1.format(p.income)}<span class="muted small"> /h</span>`,
        `base ${p.incomeBase}${p.collectionBonus ? ` · +${p.collectionBonus} % collections` : ""}${p.milestoneBonus ? ` · +${p.milestoneBonus} % milestones` : ""}`)}
      ${stat("Espèces", `${p.species}<span class="muted small"> / ${Object.keys(d.animals).length}</span>`, `${p.rescues} animaux en tout`)}
      ${stat("Collections", `${p.collectionsDone.length}<span class="muted small"> / ${d.collections.length}</span>`)}
      ${stat("Échanges du jour", p.tradesLeft ?? state.data.maxTrades, "restants")}
    </div>
    <div style="margin-top:12px">${upgrades}
      <span class="chip">${p.maxRolls} rolls/h</span><span class="chip">${p.maxClaims} claim${p.maxClaims > 1 ? "s" : ""} max</span></div>

    <h2>Totems <span class="muted small">${totems.length}/${p.totemSlots}</span></h2>
    ${totems.length ? `<div class="grid cards">${totems.map(totemCard).join("")}</div>` : `<p class="empty card">Aucun totem.</p>`}

    <div class="grid two">
      <div>
        <h2>Collections</h2>
        <div class="card">${collectionsBlock(p)}</div>
      </div>
      <div>
        <h2>Milestones</h2>
        <div class="card">${milestonesBlock(p)}</div>
        <h2>Wishlist</h2>
        <div class="card">${wishlistBlock(p)}</div>
      </div>
    </div>

    <h2>Réserve <span class="muted small">${p.species} espèces · ${p.rescues} animaux</span></h2>
    ${reserveTable(p)}`;
}

function incomeOf(copy) {
  return animal(copy.animal).income[copy.appeal - 1];
}

function totemCard(copy) {
  const a = animal(copy.animal);
  return `<div class="card animal-card">
    ${a.image ? `<img src="${esc(a.image)}" alt="${esc(a.name)}" loading="lazy">` : ""}
    <div class="body">
      <div class="name">${animalName(copy.animal)}</div>
      <div class="small"><span class="rarity ${a.rarity}">${esc(rarityLabel(a.rarity))}</span> · ${stars(copy.appeal)}</div>
      <div class="small muted">${incomeOf(copy)} baies/h</div>
    </div>
  </div>`;
}

function collectionsBlock(p) {
  const d = state.data;
  const target = d.collectionTarget;
  const order = d.collections.map((c, i) => i).sort((a, b) => p.collectionProgress[b] - p.collectionProgress[a]);
  return order.map((i) => {
    const c = d.collections[i];
    const count = p.collectionProgress[i];
    const wished = p.collectionWish === i ? ' <span class="chip wish">souhaitée</span>' : "";
    const missing = Object.entries(d.animals)
      .filter(([aid, a]) => a.collections.includes(i) && !p.bySpecies.has(Number(aid)))
      .sort(([, a], [, b]) => RARITY_RANK[a.rarity] - RARITY_RANK[b.rarity] || a.name.localeCompare(b.name, "fr"))
      .map(([aid]) => `${animalName(aid)}${ownersHint(Number(aid), p.id)}`);
    return `<details>
      <summary><div class="progress-row" style="display:inline-grid;width:calc(100% - 20px)">
        <span>${esc(c.label)}${wished}<br><span class="muted small">${esc(c.difficulty)} · +${c.bonus} %</span></span>
        ${bar(count, target)}<span class="count">${count}/${target}${count >= target ? " ✓" : ""}</span>
      </div></summary>
      <div class="missing">${missing.length ? `<b>Il manque (${missing.length}) :</b><br>${missing.join("<br>")}` : "Toutes les espèces !"}</div>
    </details>`;
  }).join("");
}

// " · chez Bob, Alice" when other players own a species, to know who to ask.
function ownersHint(animalId, exceptId) {
  const owners = [...state.players.values()].filter((q) => q.id !== exceptId && q.bySpecies.has(animalId));
  if (!owners.length) return "";
  return ` <span class="muted small">· chez ${owners.map((q) => esc(q.name)).join(", ")}</span>`;
}

function milestonesBlock(p) {
  const d = state.data;
  return d.milestones.map((m, i) => {
    const count = p.milestoneProgress[i];
    return `<div class="progress-row"><span>${m.badge} ${esc(m.label)}</span>${bar(count, m.size)}
      <span class="count">${count}/${m.size}</span></div>`;
  }).join("");
}

function wishlistBlock(p) {
  const d = state.data;
  const lines = p.wishes
    .slice()
    .sort((a, b) => RARITY_RANK[animal(b).rarity] - RARITY_RANK[animal(a).rarity])
    .map((aid) => `<div>${animalName(aid)}${p.bySpecies.has(aid) ? ' <span class="chip good">possédé</span>' : ""}${ownersHint(aid, p.id)}</div>`);
  if (p.collectionWish != null) {
    lines.unshift(`<div>Collection : <b>${esc(d.collections[p.collectionWish].label)}</b></div>`);
  }
  return lines.length ? lines.join("") : `<span class="muted">Wishlist vide.</span>`;
}

function reserveTable(p) {
  if (!p.copies.length) return `<p class="empty card">Aucun animal sauvé.</p>`;
  const rows = [...p.bySpecies.entries()].sort(([a, ca], [b, cb]) =>
    RARITY_RANK[animal(b).rarity] - RARITY_RANK[animal(a).rarity] || cb[0].appeal - ca[0].appeal);
  return `<div class="table-wrap"><table>
    <thead><tr><th>Animal</th><th>Rareté</th><th>Collections</th><th>Auras</th><th class="num">Depuis</th></tr></thead>
    <tbody>${rows.map(([aid, copies]) => {
      const a = animal(aid);
      const first = Math.min(...copies.map((c) => c.at || Infinity));
      return `<tr>
        <td>${copies.some((c) => c.totem) ? "🔺 " : ""}${animalName(aid)}${copies.length > 1 ? ` <b>×${copies.length}</b>` : ""}</td>
        <td><span class="rarity ${a.rarity}">${esc(rarityLabel(a.rarity))}</span></td>
        <td>${collectionChips(aid) || '<span class="muted">—</span>'}</td>
        <td>${copies.map((c) => `${c.appeal}★${c.totem ? "🔺" : ""}`).join(" ")}</td>
        <td class="num muted small">${Number.isFinite(first) ? new Date(first * 1000).toLocaleDateString("fr-FR") : ""}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

// ---------- Échanges ----------

// What `giver` could hand `receiver`: species the receiver doesn't own yet,
// from a copy the giver has free (not a totem), the weakest one. A giver's
// last copy only counts with includeSingles, flagged with what it would cost.
function offers(giver, receiver, includeSingles) {
  const target = state.data.collectionTarget;
  const out = [];
  for (const [aid, copies] of giver.bySpecies) {
    if (receiver.bySpecies.has(aid)) continue;
    const free = copies.filter((c) => !c.totem);
    if (!free.length) continue;
    const double = copies.length >= 2;
    if (!double && !includeSingles) continue;
    const a = animal(aid);
    const reasons = [];
    let value = 5 + RARITY_RANK[a.rarity] * 3;
    if (receiver.wishSet.has(aid)) { reasons.push(`<span class="chip wish">⭐ dans sa wishlist</span>`); value += 100; }
    if (receiver.collectionWish != null && a.collections.includes(receiver.collectionWish)) {
      reasons.push(`<span class="chip wish">collection souhaitée</span>`);
      value += 50;
    }
    for (const i of a.collections) {
      const count = receiver.collectionProgress[i];
      const label = esc(state.data.collections[i].label);
      if (count === target - 1) { reasons.push(`<span class="chip good">complète ${label} (+${state.data.collections[i].bonus} %)</span>`); value += 80; }
      else if (count < target) { reasons.push(`<span class="chip">${label} ${count}→${count + 1}/${target}</span>`); value += 15; }
    }
    if (a.milestone != null) {
      const m = state.data.milestones[a.milestone];
      const count = receiver.milestoneProgress[a.milestone];
      if (count === m.size - 1) { reasons.push(`<span class="chip good">complète ${m.badge} ${esc(m.label)}</span>`); value += 120; }
    }
    const cost = [];
    if (!double) {
      cost.push(`<span class="chip warn">son dernier exemplaire</span>`);
      value -= 20;
      for (const i of a.collections) {
        if (giver.collectionProgress[i] === target) cost.push(`<span class="chip warn">lui fait perdre ${esc(state.data.collections[i].label)}</span>`);
      }
      if (a.milestone != null && giver.milestonesDone.includes(a.milestone)) {
        cost.push(`<span class="chip warn">lui fait perdre ${state.data.milestones[a.milestone].badge}</span>`);
      }
    }
    // The weakest free copy: the giver keeps their best.
    const copy = free[free.length - 1];
    out.push({ aid, copy, double, reasons, cost, value });
  }
  return out.sort((x, y) => y.value - x.value);
}

function offerRow(o, giver, receiver, showWho) {
  return `<div class="card offer">
    <div>
      ${showWho ? `<div class="from">${who(giver)} → ${who(receiver)}</div>` : ""}
      <div>${animalName(o.aid)} ${stars(o.copy.appeal)} ${o.double ? `<span class="muted small">(×${giver.bySpecies.get(o.aid).length} chez ${esc(giver.name)})</span>` : ""}</div>
      <div>${o.reasons.join("")}${o.cost.join("")}</div>
    </div>
    <div class="small muted">${esc(rarityLabel(animal(o.aid).rarity))}</div>
  </div>`;
}

function viewTrades(id) {
  const me = state.players.get(id ?? state.me?.id);
  const singles = `<label class="check"><input type="checkbox" id="singles" ${state.tradesSingles ? "checked" : ""}> Inclure les derniers exemplaires (pas seulement les doublons)</label>`;
  if (!me) {
    return `<div class="toolbar">${playerSelect("", "echanges", "Je suis…")}</div>
      <p class="note">Choisis ton joueur : la page liste ce que les autres peuvent te donner (des espèces que tu n'as pas,
      qu'ils ont en double), ce que toi tu peux leur donner, et les échanges où vous gagnez tous les deux.</p>
      ${duplicatesTable()}`;
  }
  const others = sortedPlayers().filter((p) => p.id !== me.id);
  const incoming = others.flatMap((g) => offers(g, me, state.tradesSingles).map((o) => ({ o, giver: g })))
    .sort((x, y) => y.o.value - x.o.value);
  const outgoing = others.flatMap((r) => offers(me, r, state.tradesSingles).map((o) => ({ o, receiver: r })))
    .sort((x, y) => y.o.value - x.o.value);
  const mutual = others.map((o) => ({
    other: o,
    give: offers(me, o, state.tradesSingles),
    get: offers(o, me, state.tradesSingles),
  })).filter((m) => m.give.length && m.get.length)
    .sort((x, y) => (y.give[0].value + y.get[0].value) - (x.give[0].value + x.get[0].value));
  const LIMIT = 30;

  return `
    <div class="toolbar">${playerSelect(me.id, "echanges")} ${singles}</div>
    <p class="note">Tu as encore <b>${me.tradesLeft ?? state.data.maxTrades}</b> échange(s) aujourd'hui. Un totem doit être retiré avant d'être échangé.
      Commande : <code>/trade @joueur give_animal: … want_animal: …</code></p>

    <h2>Échanges gagnant-gagnant <span class="muted small">${mutual.length}</span></h2>
    ${mutual.length ? `<div class="offer-list">${mutual.map((m) => `
      <div class="card">
        <h3>${who(m.other)} <span class="muted small">· ${m.other.tradesLeft ?? state.data.maxTrades} échange(s) restant(s) aujourd'hui</span></h3>
        <div class="pair">
          <div><div class="muted small">Tu donnes</div>${offerRow(m.give[0], me, m.other, false)}</div>
          <div><div class="muted small">Tu reçois</div>${offerRow(m.get[0], m.other, me, false)}</div>
        </div>
        ${m.give.length + m.get.length > 2 ? `<div class="muted small" style="margin-top:6px">Et ${m.give.length - 1} autre(s) à donner, ${m.get.length - 1} à recevoir : voir plus bas.</div>` : ""}
      </div>`).join("")}</div>` : `<p class="empty card">Aucun échange où vous gagnez tous les deux pour l'instant.</p>`}

    <div class="grid two">
      <div>
        <h2>Ce qu'on peut te donner <span class="muted small">${incoming.length}</span></h2>
        ${incoming.length ? `<div class="offer-list">${incoming.slice(0, LIMIT).map(({ o, giver }) => offerRow(o, giver, me, true)).join("")}</div>
          ${incoming.length > LIMIT ? `<p class="muted small">… et ${incoming.length - LIMIT} autres.</p>` : ""}`
          : `<p class="empty card">Personne n'a de doublon d'une espèce qui te manque.</p>`}
      </div>
      <div>
        <h2>Ce que tu peux donner <span class="muted small">${outgoing.length}</span></h2>
        ${outgoing.length ? `<div class="offer-list">${outgoing.slice(0, LIMIT).map(({ o, receiver }) => offerRow(o, me, receiver, true)).join("")}</div>
          ${outgoing.length > LIMIT ? `<p class="muted small">… et ${outgoing.length - LIMIT} autres.</p>` : ""}`
          : `<p class="empty card">Tu n'as pas de doublon d'une espèce qui manque à quelqu'un.</p>`}
      </div>
    </div>`;
}

bind.echanges = () => {
  document.getElementById("singles")?.addEventListener("change", (event) => {
    state.tradesSingles = event.target.checked;
    render();
  });
};

function duplicatesTable() {
  const rows = [];
  for (const p of sortedPlayers()) {
    for (const [aid, copies] of p.bySpecies) {
      if (copies.length >= 2) rows.push({ p, aid, copies });
    }
  }
  if (!rows.length) return `<h2>Doublons</h2><p class="empty card">Personne n'a encore de doublon.</p>`;
  rows.sort((x, y) => RARITY_RANK[animal(y.aid).rarity] - RARITY_RANK[animal(x.aid).rarity]);
  return `<h2>Tous les doublons <span class="muted small">${rows.length}</span></h2>
    <div class="table-wrap"><table>
      <thead><tr><th>Animal</th><th>Chez</th><th>Auras</th><th>Collections</th><th>Il manque à</th></tr></thead>
      <tbody>${rows.map(({ p, aid, copies }) => {
        const lacking = [...state.players.values()].filter((q) => !q.bySpecies.has(aid));
        return `<tr><td>${animalName(aid)} <b>×${copies.length}</b></td><td>${who(p)}</td>
          <td>${copies.map((c) => `${c.appeal}★${c.totem ? "🔺" : ""}`).join(" ")}</td>
          <td>${collectionChips(aid) || '<span class="muted">—</span>'}</td>
          <td class="small">${lacking.map((q) => (q.wishSet.has(aid) ? `<b>⭐ ${esc(q.name)}</b>` : esc(q.name))).join(", ") || '<span class="muted">tout le monde l\'a</span>'}</td></tr>`;
      }).join("")}</tbody></table></div>`;
}

// ---------- Animaux ----------

function viewAnimals() {
  const d = state.data;
  const f = state.animalsFilter;
  return `
    <div class="toolbar">
      <input type="search" id="f-text" placeholder="Chercher un animal…" value="${esc(f.text)}">
      <select id="f-rarity"><option value="">Toutes les raretés</option>
        ${d.rarities.map((r) => `<option value="${r.key}" ${f.rarity === r.key ? "selected" : ""}>${esc(r.label)}</option>`).join("")}</select>
      <select id="f-collection"><option value="">Toutes les collections</option>
        ${d.collections.map((c, i) => `<option value="${i}" ${String(f.collection) === String(i) ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select>
      <label class="check"><input type="checkbox" id="f-owned" ${f.owned ? "checked" : ""}> Sauvés sur le serveur</label>
      <label class="check"><input type="checkbox" id="f-doubles" ${f.doubles ? "checked" : ""}> En double chez quelqu'un</label>
      <div class="segmented" role="group" aria-label="Affichage">
        <button type="button" data-cards="0" class="${state.animalsCards ? "" : "active"}">Liste</button>
        <button type="button" data-cards="1" class="${state.animalsCards ? "active" : ""}">Fiches</button>
      </div>
    </div>
    <div id="animals-result">${animalsResult()}</div>`;
}

function animalsResult() {
  const d = state.data;
  const f = state.animalsFilter;
  const needle = fold(f.text);
  const players = sortedPlayers();
  const rows = Object.entries(d.animals)
    .map(([aid, a]) => ({ aid: Number(aid), a, owners: players.filter((p) => p.bySpecies.has(Number(aid))) }))
    .filter(({ aid, a, owners }) =>
      (!needle || fold(a.name).includes(needle) || fold(a.sci || "").includes(needle))
      && (!f.rarity || a.rarity === f.rarity)
      && (f.collection === "" || a.collections.includes(Number(f.collection)))
      && (!f.owned || owners.length)
      && (!f.doubles || owners.some((p) => p.bySpecies.get(aid).length >= 2)))
    .sort((x, y) => RARITY_RANK[y.a.rarity] - RARITY_RANK[x.a.rarity] || x.a.name.localeCompare(y.a.name, "fr"));
  // Fewer cards than rows: each one loads a photo.
  const LIMIT = state.animalsCards ? 120 : 300;
  if (!rows.length) return `<p class="empty card">Aucun animal ne correspond.</p>`;
  const count = `<p class="muted small">${rows.length} animal(aux)${rows.length > LIMIT ? `, les ${LIMIT} premiers affichés (affine la recherche pour voir les autres)` : ""}.</p>`;
  if (state.animalsCards) {
    return `${count}<div class="grid cards">${rows.slice(0, LIMIT).map((row) => animalCard(row, players)).join("")}</div>`;
  }
  return `${count}
    <div class="table-wrap"><table>
      <thead><tr><th>Animal</th><th>Collections</th><th>Chez</th><th>Souhaité par</th></tr></thead>
      <tbody>${rows.slice(0, LIMIT).map(({ aid, a, owners }) => {
        const wishers = players.filter((p) => p.wishSet.has(aid));
        return `<tr>
          <td>${animalName(aid)}<br><span class="muted small"><i>${esc(a.sci)}</i> · <span class="rarity ${a.rarity}">${esc(rarityLabel(a.rarity))}</span></span></td>
          <td>${collectionChips(aid) || '<span class="muted">—</span>'}</td>
          <td class="small">${owners.map((p) => {
            const copies = p.bySpecies.get(aid);
            return `<div class="owner">${who(p)} ${copies.length > 1 ? `<b>×${copies.length}</b> ` : ""}<span class="muted">${copies.map((c) => `${c.appeal}★${c.totem ? "🔺" : ""}`).join(" ")}</span></div>`;
          }).join("") || '<span class="muted">personne</span>'}</td>
          <td class="small">${wishers.map((p) => esc(p.name)).join(", ") || '<span class="muted">—</span>'}</td>
        </tr>`;
      }).join("")}</tbody></table></div>`;
}

// One card per animal: photo, rarity, IUCN status, groups, who has it.
function animalCard({ aid, a, owners }, players) {
  const wishers = players.filter((p) => p.wishSet.has(aid));
  const milestone = a.milestone != null ? state.data.milestones[a.milestone] : null;
  return `<div class="card animal-card">
    ${a.image ? `<img src="${esc(a.image)}" alt="${esc(a.name)}" loading="lazy">` : `<div class="no-image">Pas de photo</div>`}
    <div class="body">
      <div class="name">${milestone ? `${milestone.badge} ` : ""}${animalName(aid, { dot: false })}</div>
      <div class="small muted"><i>${esc(a.sci)}</i></div>
      <div class="small"><span class="rarity ${a.rarity}">${esc(rarityLabel(a.rarity))}</span>${a.iucn ? ` · <span class="muted">${esc(IUCN_LABEL[a.iucn] || a.iucn)} (${esc(a.iucn)})</span>` : ""}</div>
      <div class="small muted">Totem : ${a.income[0]} à ${a.income[4]} baies/h selon l'aura</div>
      ${a.collections.length ? `<div style="margin-top:6px">${collectionChips(aid)}</div>` : ""}
      <div class="small" style="margin-top:8px">${owners.length
        ? `Chez ${owners.map((p) => {
          const copies = p.bySpecies.get(aid);
          return `<a href="#/joueur/${p.id}">${esc(p.name)}</a> <span class="muted">(${copies.map((c) => `${c.appeal}★${c.totem ? "🔺" : ""}`).join(" ")})</span>`;
        }).join(", ")}`
        : '<span class="muted">Sauvé par personne ici</span>'}</div>
      ${wishers.length ? `<div class="small" style="margin-top:4px">⭐ Souhaité par ${wishers.map((p) => esc(p.name)).join(", ")}</div>` : ""}
    </div>
  </div>`;
}

function fold(text) {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

bind.animaux = () => {
  const f = state.animalsFilter;
  const refresh = () => { document.getElementById("animals-result").innerHTML = animalsResult(); };
  document.getElementById("f-text").addEventListener("input", (e) => { f.text = e.target.value; refresh(); });
  document.getElementById("f-rarity").addEventListener("change", (e) => { f.rarity = e.target.value; refresh(); });
  document.getElementById("f-collection").addEventListener("change", (e) => { f.collection = e.target.value; refresh(); });
  document.getElementById("f-owned").addEventListener("change", (e) => { f.owned = e.target.checked; refresh(); });
  document.getElementById("f-doubles").addEventListener("change", (e) => { f.doubles = e.target.checked; refresh(); });
  for (const button of main().querySelectorAll("[data-cards]")) {
    button.addEventListener("click", () => {
      state.animalsCards = button.dataset.cards === "1";
      for (const b of main().querySelectorAll("[data-cards]")) b.classList.toggle("active", b === button);
      refresh();
    });
  }
};

boot();
