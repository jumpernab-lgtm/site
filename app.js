/* ============================================================
   Impressions 3D QC — Configuration + utilitaires partagés
   ============================================================ */

/* ⚙️ À MODIFIER : URL de votre backend déployé sur Render (sans / à la fin) */
window.API_BASE = "https://backend-4qf6.onrender.com";

window.SUPPORT_EMAIL = "impressions3dqc@proton.me";

/* --- Statuts ------------------------------------------------ */
const STATUS_LABELS = {
  ouvert: "En discussion",
  en_cours: "Commande en cours",
  terminee: "Complétée",
};
const STATUS_CLASS = {
  ouvert: "b-open",
  en_cours: "b-active",
  terminee: "b-done",
};

/* --- Petits helpers DOM (sécurisés : textContent seulement) --- */
function $(sel, root) { return (root || document).querySelector(sel); }

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  attrs = attrs || {};
  for (const key of Object.keys(attrs)) {
    const val = attrs[key];
    if (key === "class") node.className = val;
    else if (key === "text") node.textContent = val;
    else if (key.indexOf("on") === 0) node.addEventListener(key.slice(2), val);
    else node.setAttribute(key, val);
  }
  const kids = [].concat(children || []);
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
  }
  return node;
}

function fmtDate(ts) {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleString("fr-CA", { dateStyle: "medium", timeStyle: "short" });
  } catch (e) { return ""; }
}

function badge(status) {
  return el("span", {
    class: "badge " + (STATUS_CLASS[status] || ""),
    text: STATUS_LABELS[status] || status,
  });
}

/* --- Sessions ------------------------------------------------- */
function custToken() { return localStorage.getItem("i3q_token") || ""; }
function custEmail() { return (localStorage.getItem("i3q_email") || "").toLowerCase(); }
function setCust(token, email) {
  localStorage.setItem("i3q_token", token);
  localStorage.setItem("i3q_email", email);
}
function clearCust() {
  localStorage.removeItem("i3q_token");
  localStorage.removeItem("i3q_email");
}
function adminToken() { return sessionStorage.getItem("i3q_admin") || ""; }
function setAdmin(token) { sessionStorage.setItem("i3q_admin", token); }
function clearAdmin() { sessionStorage.removeItem("i3q_admin"); }

/* --- Appels API ------------------------------------------------ */
async function api(path, opts) {
  opts = opts || {};
  const method = opts.method || "GET";
  const headers = { "Content-Type": "application/json" };
  const token = opts.admin ? adminToken() : custToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  // Indice « le serveur se réveille » si la requête est lente (Render gratuit)
  const hint = document.getElementById("server-hint");
  const slowTimer = setTimeout(function () { if (hint) hint.classList.add("show"); }, 2500);

  let res;
  try {
    res = await fetch(window.API_BASE + path, {
      method: method,
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
  } catch (e) {
    clearTimeout(slowTimer);
    if (hint) hint.classList.remove("show");
    throw {
      status: 0,
      error: "Impossible de joindre le serveur. Il se réveille peut-être — réessayez dans une minute.",
    };
  }
  clearTimeout(slowTimer);
  if (hint) hint.classList.remove("show");

  let data = {};
  try { data = await res.json(); } catch (e) { /* réponse vide */ }
  if (!res.ok) {
    throw { status: res.status, error: data.error || ("Erreur " + res.status) };
  }
  return data;
}

/* --- Affichage d'erreurs ---------------------------------------- */
function showAlert(container, message) {
  container.textContent = message;
  container.classList.remove("hidden");
}
function hideAlert(container) {
  container.textContent = "";
  container.classList.add("hidden");
}
