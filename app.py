# ============================================================
#  Impressions 3D QC — Backend API (Flask + SQLite)
#  Déploiement : Render (Web Service)
#  Démarrage    : gunicorn app:app
# ============================================================
#  Variables d'environnement :
#    ADMIN_PASSWORD   (REQUIS)  Mot de passe du panneau admin
#    BREVO_API_KEY    (requis pour les courriels) Clé API Brevo
#    SENDER_EMAIL     Expéditeur vérifié dans Brevo (défaut: impressions3dqc@proton.me)
#    SENDER_NAME      Nom d'expéditeur (défaut: Impressions 3D QC)
#    ADMIN_EMAIL      Adresse qui reçoit les notifications admin
#                     (défaut: impressions3dqc@proton.me)
#    SITE_URL         URL publique du site GitHub Pages, sans / final
#                     (ex: https://votrecompte.github.io/impressions3dqc)
#    CORS_ORIGINS     Origines autorisées, séparées par des virgules
#                     (ex: https://votrecompte.github.io) — défaut: *
#    DB_PATH          Chemin du fichier SQLite (défaut: data.db)
#    DEV_MODE         "1" = renvoie le code de vérification dans la réponse
#                     (POUR TESTS SEULEMENT — ne jamais activer en production)
# ============================================================

import os
import re
import time
import html
import hashlib
import secrets
import sqlite3
import threading

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

import requests as http_client

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
DB_PATH        = os.environ.get("DB_PATH", "data.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BREVO_API_KEY  = os.environ.get("BREVO_API_KEY", "")
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL", "impressions3dqc@proton.me")
SENDER_NAME    = os.environ.get("SENDER_NAME", "Impressions 3D QC")
ADMIN_EMAIL    = os.environ.get("ADMIN_EMAIL", "impressions3dqc@proton.me")
SITE_URL       = os.environ.get("SITE_URL", "").rstrip("/")
CORS_ORIGINS   = os.environ.get("CORS_ORIGINS", "*")
DEV_MODE       = os.environ.get("DEV_MODE", "") == "1"

# Hash du mot de passe admin gardé en mémoire seulement (jamais en clair sur disque)
ADMIN_HASH = generate_password_hash(ADMIN_PASSWORD) if ADMIN_PASSWORD else None

RETENTION_DAYS        = 30            # Commandes passées supprimées après 30 jours
CUSTOMER_SESSION_SECS = 30 * 24 * 3600   # Session client : 30 jours
ADMIN_SESSION_SECS    = 12 * 3600        # Session admin : 12 heures
CODE_TTL_SECS         = 10 * 60          # Code de vérification : 10 minutes

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Codes postaux du Québec : commencent par G, H ou J
POSTAL_QC_RE = re.compile(r"^[GHJ]\d[A-Z]\s?\d[A-Z]\d$", re.IGNORECASE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024  # 32 Ko max par requête

_origins = "*" if CORS_ORIGINS.strip() == "*" else [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
CORS(app, resources={r"/*": {"origins": _origins}}, allow_headers=["Content-Type", "Authorization"])

# ------------------------------------------------------------
# Base de données
# ------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id           TEXT PRIMARY KEY,
  email        TEXT NOT NULL,
  name         TEXT NOT NULL,
  description  TEXT NOT NULL,
  adresse      TEXT NOT NULL,
  ville        TEXT NOT NULL,
  code_postal  TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'ouvert',   -- ouvert | en_cours | terminee
  created_at   INTEGER NOT NULL,
  confirmed_at INTEGER,
  completed_at INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id  TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
  sender     TEXT NOT NULL,          -- 'client' | 'admin'
  body       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS customer_sessions (
  token_hash TEXT PRIMARY KEY,
  email      TEXT NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
  email      TEXT PRIMARY KEY,
  code_hash  TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  tries      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
  key TEXT NOT NULL,
  ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_key ON attempts(key, ts);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def _close_db(exc=None):
    d = g.pop("db", None)
    if d is not None:
        d.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------
def now() -> int:
    return int(time.time())

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"

def bearer_token() -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""

def json_body() -> dict:
    return request.get_json(silent=True) or {}

def err(message: str, status: int):
    return jsonify({"error": message}), status

# --- Limitation de débit (anti force brute) -------------------
def count_attempts(key: str, window_secs: int) -> int:
    cutoff = now() - window_secs
    row = db().execute("SELECT COUNT(*) AS n FROM attempts WHERE key = ? AND ts > ?", (key, cutoff)).fetchone()
    return row["n"]

def record_attempt(key: str):
    db().execute("INSERT INTO attempts (key, ts) VALUES (?, ?)", (key, now()))
    db().commit()

# --- Sessions --------------------------------------------------
def create_customer_session(email: str) -> str:
    token = secrets.token_urlsafe(32)
    db().execute(
        "INSERT INTO customer_sessions (token_hash, email, expires_at) VALUES (?, ?, ?)",
        (sha256(token), email, now() + CUSTOMER_SESSION_SECS),
    )
    db().commit()
    return token

def create_admin_session() -> str:
    token = secrets.token_urlsafe(32)
    db().execute(
        "INSERT INTO admin_sessions (token_hash, expires_at) VALUES (?, ?)",
        (sha256(token), now() + ADMIN_SESSION_SECS),
    )
    db().commit()
    return token

def get_customer_email() -> str | None:
    """Retourne l'email du client connecté, ou None."""
    token = bearer_token()
    if not token:
        return None
    row = db().execute(
        "SELECT email FROM customer_sessions WHERE token_hash = ? AND expires_at > ?",
        (sha256(token), now()),
    ).fetchone()
    return row["email"] if row else None

def is_admin() -> bool:
    token = bearer_token()
    if not token:
        return False
    row = db().execute(
        "SELECT 1 FROM admin_sessions WHERE token_hash = ? AND expires_at > ?",
        (sha256(token), now()),
    ).fetchone()
    return row is not None

# --- Courriels (Brevo) -----------------------------------------
def send_email(to: str, subject: str, html_body: str):
    """Envoie un courriel en arrière-plan via l'API Brevo."""
    if not BREVO_API_KEY:
        app.logger.warning("[COURRIEL NON ENVOYÉ — BREVO_API_KEY manquant] À: %s | Sujet: %s", to, subject)
        return

    def _send():
        try:
            resp = http_client.post(
                "https://api.brevo.com/v3/smtp/email",
                timeout=15,
                headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
                json={
                    "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
                    "to": [{"email": to}],
                    "subject": subject,
                    "htmlContent": html_body,
                },
            )
            if resp.status_code >= 300:
                app.logger.error("Brevo a répondu %s : %s", resp.status_code, resp.text[:300])
        except Exception as exc:  # noqa: BLE001
            app.logger.error("Erreur d'envoi de courriel : %s", exc)

    threading.Thread(target=_send, daemon=True).start()

def email_layout(title: str, lines: list[str], link: str | None = None, link_label: str = "Ouvrir") -> str:
    body = "".join(f"<p style='margin:0 0 12px'>{line}</p>" for line in lines)
    button = ""
    if link:
        button = (
            f"<p style='margin:20px 0'><a href='{link}' "
            "style='background:#FF6B1A;color:#111;padding:12px 20px;border-radius:6px;"
            f"text-decoration:none;font-weight:bold'>{html.escape(link_label)}</a></p>"
        )
    return (
        "<div style='font-family:Arial,sans-serif;max-width:560px;margin:auto;"
        "background:#14171d;color:#edeae3;padding:28px;border-radius:10px'>"
        f"<h2 style='color:#FF6B1A;margin:0 0 16px'>{html.escape(title)}</h2>"
        f"{body}{button}"
        "<hr style='border:none;border-top:1px solid #2a2f3a;margin:20px 0'>"
        f"<p style='color:#9aa0ab;font-size:13px;margin:0'>Impressions 3D QC — Support : {ADMIN_EMAIL}<br>"
        "Livraison au Québec seulement.</p></div>"
    )

def ticket_link_client(ticket_id: str) -> str | None:
    return f"{SITE_URL}/commandes.html?ticket={ticket_id}" if SITE_URL else None

def ticket_link_admin(ticket_id: str) -> str | None:
    return f"{SITE_URL}/admin/?ticket={ticket_id}" if SITE_URL else None

# --- Divers -----------------------------------------------------
def new_ticket_id() -> str:
    while True:
        tid = "T-" + secrets.token_hex(3).upper()
        exists = db().execute("SELECT 1 FROM tickets WHERE id = ?", (tid,)).fetchone()
        if not exists:
            return tid

def ticket_dict(row: sqlite3.Row, include_private: bool) -> dict:
    d = {
        "id": row["id"],
        "status": row["status"],
        "description": row["description"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "completed_at": row["completed_at"],
        "ville": row["ville"],
    }
    if include_private:
        d.update({
            "email": row["email"],
            "name": row["name"],
            "adresse": row["adresse"],
            "code_postal": row["code_postal"],
        })
    return d

# --- Nettoyage automatique (30 jours + sessions expirées) -------
def maybe_cleanup():
    try:
        row = db().execute("SELECT v FROM meta WHERE k = 'last_cleanup'").fetchone()
        last = int(row["v"]) if row else 0
        if now() - last < 3600:  # au plus une fois par heure
            return
        cutoff = now() - RETENTION_DAYS * 24 * 3600
        db().execute(
            "DELETE FROM tickets WHERE status = 'terminee' AND completed_at IS NOT NULL AND completed_at < ?",
            (cutoff,),
        )
        db().execute("DELETE FROM customer_sessions WHERE expires_at < ?", (now(),))
        db().execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now(),))
        db().execute("DELETE FROM auth_codes WHERE expires_at < ?", (now(),))
        db().execute("DELETE FROM attempts WHERE ts < ?", (now() - 24 * 3600,))
        db().execute(
            "INSERT INTO meta (k, v) VALUES ('last_cleanup', ?) "
            "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (str(now()),),
        )
        db().commit()
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Erreur de nettoyage : %s", exc)

@app.before_request
def _before():
    if request.method != "OPTIONS":
        maybe_cleanup()

@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

# ============================================================
#  ROUTES
# ============================================================

@app.get("/")
@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "impressions3dqc"})

# ------------------------------------------------------------
# Authentification client (code par courriel)
# ------------------------------------------------------------
@app.post("/api/auth/request-code")
def request_code():
    body = json_body()
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return err("Adresse courriel invalide.", 400)

    ip = client_ip()
    if count_attempts(f"code:{email}", 15 * 60) >= 3 or count_attempts(f"codeip:{ip}", 15 * 60) >= 10:
        return err("Trop de demandes. Réessayez dans 15 minutes.", 429)
    record_attempt(f"code:{email}")
    record_attempt(f"codeip:{ip}")

    code = f"{secrets.randbelow(10**6):06d}"
    db().execute(
        "INSERT INTO auth_codes (email, code_hash, expires_at, tries) VALUES (?, ?, ?, 0) "
        "ON CONFLICT(email) DO UPDATE SET code_hash = excluded.code_hash, "
        "expires_at = excluded.expires_at, tries = 0",
        (email, sha256(code), now() + CODE_TTL_SECS),
    )
    db().commit()

    send_email(
        email,
        f"Votre code de vérification : {code}",
        email_layout(
            "Code de vérification",
            [f"Votre code : <strong style='font-size:22px;letter-spacing:3px'>{code}</strong>",
             "Ce code est valide pendant 10 minutes.",
             "Si vous n'avez pas demandé ce code, ignorez ce courriel."],
        ),
    )

    resp = {"ok": True}
    if DEV_MODE:
        resp["dev_code"] = code  # TESTS SEULEMENT
    return jsonify(resp)

@app.post("/api/auth/verify")
def verify_code():
    body = json_body()
    email = str(body.get("email", "")).strip().lower()
    code = str(body.get("code", "")).strip()
    if not EMAIL_RE.match(email) or not re.fullmatch(r"\d{6}", code):
        return err("Courriel ou code invalide.", 400)

    row = db().execute("SELECT * FROM auth_codes WHERE email = ?", (email,)).fetchone()
    if not row or row["expires_at"] < now():
        return err("Code expiré. Demandez un nouveau code.", 400)
    if row["tries"] >= 8:
        db().execute("DELETE FROM auth_codes WHERE email = ?", (email,))
        db().commit()
        return err("Trop d'essais. Demandez un nouveau code.", 429)

    if not secrets.compare_digest(row["code_hash"], sha256(code)):
        db().execute("UPDATE auth_codes SET tries = tries + 1 WHERE email = ?", (email,))
        db().commit()
        return err("Code incorrect.", 400)

    db().execute("DELETE FROM auth_codes WHERE email = ?", (email,))
    db().commit()
    token = create_customer_session(email)
    return jsonify({"token": token, "email": email, "expires_at": now() + CUSTOMER_SESSION_SECS})

# ------------------------------------------------------------
# Tickets — côté client
# ------------------------------------------------------------
@app.get("/api/me/tickets")
def my_tickets():
    email = get_customer_email()
    if not email:
        return err("Connexion requise.", 401)
    rows = db().execute(
        "SELECT * FROM tickets WHERE email = ? ORDER BY created_at DESC", (email,)
    ).fetchall()
    return jsonify({"tickets": [ticket_dict(r, include_private=True) for r in rows]})

@app.post("/api/tickets")
def create_ticket():
    email = get_customer_email()
    if not email:
        return err("Connexion requise.", 401)
    if count_attempts(f"newticket:{email}", 24 * 3600) >= 10:
        return err("Limite de demandes atteinte pour aujourd'hui.", 429)

    body = json_body()
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    adresse = str(body.get("adresse", "")).strip()
    ville = str(body.get("ville", "")).strip()
    code_postal = str(body.get("code_postal", "")).strip().upper()

    if not (1 <= len(name) <= 120):
        return err("Veuillez indiquer votre nom.", 400)
    if not (10 <= len(description) <= 4000):
        return err("Décrivez votre projet (au moins 10 caractères).", 400)
    if not (3 <= len(adresse) <= 200) or not (2 <= len(ville) <= 100):
        return err("Adresse ou ville invalide.", 400)
    if not POSTAL_QC_RE.match(code_postal):
        return err("Nous livrons uniquement au Québec (code postal commençant par G, H ou J).", 400)

    tid = new_ticket_id()
    db().execute(
        "INSERT INTO tickets (id, email, name, description, adresse, ville, code_postal, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'ouvert', ?)",
        (tid, email, name, description, adresse, ville, code_postal, now()),
    )
    db().commit()
    record_attempt(f"newticket:{email}")

    desc_html = html.escape(description[:500])
    send_email(
        email,
        f"[{tid}] Votre demande a bien été reçue",
        email_layout(
            "Demande reçue ✔",
            [f"Bonjour {html.escape(name)},",
             f"Votre demande <strong>{tid}</strong> a bien été créée. Nous allons l'étudier et vous "
             "répondre avec un prix (selon la pièce et la distance de livraison).",
             f"<em>« {desc_html} »</em>",
             f"Pour toute question : <strong>{ADMIN_EMAIL}</strong>"],
            ticket_link_client(tid), "Voir ma demande",
        ),
    )
    send_email(
        ADMIN_EMAIL,
        f"[{tid}] Nouveau ticket de {name}",
        email_layout(
            "Nouveau ticket",
            [f"<strong>{html.escape(name)}</strong> ({html.escape(email)})",
             f"Livraison : {html.escape(adresse)}, {html.escape(ville)}, {html.escape(code_postal)}",
             f"<em>« {desc_html} »</em>"],
            ticket_link_admin(tid), "Ouvrir dans le panneau admin",
        ),
    )
    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    return jsonify({"ticket": ticket_dict(row, include_private=True)}), 201

@app.get("/api/tickets/<tid>")
def get_ticket(tid):
    admin = is_admin()
    email = None if admin else get_customer_email()
    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    if not row or (not admin and row["email"] != email):
        return err("Ticket introuvable.", 404)
    msgs = db().execute(
        "SELECT id, sender, body, created_at FROM messages WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (tid,),
    ).fetchall()
    return jsonify({
        "ticket": ticket_dict(row, include_private=True),
        "messages": [dict(m) for m in msgs],
    })

@app.post("/api/tickets/<tid>/messages")
def post_message(tid):
    admin = is_admin()
    email = None if admin else get_customer_email()
    if not admin and not email:
        return err("Connexion requise.", 401)

    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    if not row or (not admin and row["email"] != email):
        return err("Ticket introuvable.", 404)

    body = str(json_body().get("body", "")).strip()
    if not (1 <= len(body) <= 3000):
        return err("Message vide ou trop long (max 3000 caractères).", 400)

    limiter_key = f"msg:{'admin' if admin else email}"
    if count_attempts(limiter_key, 3600) >= 60:
        return err("Trop de messages envoyés. Réessayez plus tard.", 429)
    record_attempt(limiter_key)

    sender = "admin" if admin else "client"
    db().execute(
        "INSERT INTO messages (ticket_id, sender, body, created_at) VALUES (?, ?, ?, ?)",
        (tid, sender, body, now()),
    )
    db().commit()

    preview = html.escape(body[:300])
    if admin:
        send_email(
            row["email"],
            f"[{tid}] Nouvelle réponse à votre demande",
            email_layout(
                "Nouvelle réponse",
                [f"Nous avons répondu à votre demande <strong>{tid}</strong> :",
                 f"<em>« {preview} »</em>"],
                ticket_link_client(tid), "Répondre",
            ),
        )
    else:
        send_email(
            ADMIN_EMAIL,
            f"[{tid}] Nouveau message de {row['name']}",
            email_layout(
                "Nouveau message client",
                [f"<strong>{html.escape(row['name'])}</strong> ({html.escape(row['email'])}) a écrit :",
                 f"<em>« {preview} »</em>"],
                ticket_link_admin(tid), "Répondre dans le panneau admin",
            ),
        )

    msg = db().execute(
        "SELECT id, sender, body, created_at FROM messages WHERE ticket_id = ? ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    return jsonify({"message": dict(msg)}), 201

@app.post("/api/tickets/<tid>/confirm")
def confirm_ticket(tid):
    email = get_customer_email()
    if not email:
        return err("Connexion requise.", 401)
    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    if not row or row["email"] != email:
        return err("Ticket introuvable.", 404)
    if row["status"] != "ouvert":
        return err("Cette commande est déjà confirmée.", 409)

    db().execute(
        "UPDATE tickets SET status = 'en_cours', confirmed_at = ? WHERE id = ?",
        (now(), tid),
    )
    db().commit()

    send_email(
        email,
        f"[{tid}] Commande confirmée",
        email_layout(
            "Commande confirmée ✔",
            [f"Votre commande <strong>{tid}</strong> est confirmée et passe en production.",
             "<strong>Paiement :</strong> nous vous contacterons par courriel pour organiser le paiement.",
             f"Questions : {ADMIN_EMAIL}"],
            ticket_link_client(tid), "Suivre ma commande",
        ),
    )
    send_email(
        ADMIN_EMAIL,
        f"[{tid}] {row['name']} a confirmé sa commande",
        email_layout(
            "Commande confirmée",
            [f"<strong>{html.escape(row['name'])}</strong> ({html.escape(row['email'])}) vient de confirmer "
             f"la commande <strong>{tid}</strong>.",
             "Elle est maintenant dans « Commandes en cours »."],
            ticket_link_admin(tid), "Voir la commande",
        ),
    )
    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    return jsonify({"ticket": ticket_dict(row, include_private=True)})

# ------------------------------------------------------------
# Panneau admin
# ------------------------------------------------------------
@app.post("/api/admin/login")
def admin_login():
    if not ADMIN_HASH:
        return err("ADMIN_PASSWORD n'est pas configuré sur le serveur.", 503)

    ip = client_ip()
    if count_attempts(f"admlogin:{ip}", 15 * 60) >= 5:
        return err("Trop de tentatives. Réessayez dans 15 minutes.", 429)

    password = str(json_body().get("password", ""))
    time.sleep(0.4)  # ralentit la force brute
    if not password or not check_password_hash(ADMIN_HASH, password):
        record_attempt(f"admlogin:{ip}")
        return err("Mot de passe incorrect.", 401)

    token = create_admin_session()
    return jsonify({"token": token, "expires_at": now() + ADMIN_SESSION_SECS})

@app.post("/api/admin/logout")
def admin_logout():
    token = bearer_token()
    if token:
        db().execute("DELETE FROM admin_sessions WHERE token_hash = ?", (sha256(token),))
        db().commit()
    return jsonify({"ok": True})

@app.get("/api/admin/tickets")
def admin_tickets():
    if not is_admin():
        return err("Accès refusé.", 401)
    status = request.args.get("status", "").strip()
    if status:
        rows = db().execute(
            "SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = db().execute("SELECT * FROM tickets ORDER BY created_at DESC").fetchall()
    return jsonify({"tickets": [ticket_dict(r, include_private=True) for r in rows]})

@app.post("/api/admin/tickets/<tid>/complete")
def admin_complete(tid):
    if not is_admin():
        return err("Accès refusé.", 401)
    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    if not row:
        return err("Ticket introuvable.", 404)
    if row["status"] != "en_cours":
        return err("Seule une commande en cours peut être marquée comme complétée.", 409)

    db().execute(
        "UPDATE tickets SET status = 'terminee', completed_at = ? WHERE id = ?",
        (now(), tid),
    )
    db().commit()

    send_email(
        row["email"],
        f"[{tid}] Votre commande est complétée 🎉",
        email_layout(
            "Commande complétée",
            [f"Bonne nouvelle : votre commande <strong>{tid}</strong> est complétée !",
             "Merci d'avoir fait affaire avec Impressions 3D QC.",
             f"Note : cette commande sera automatiquement supprimée de nos systèmes dans {RETENTION_DAYS} jours."],
            ticket_link_client(tid), "Voir ma commande",
        ),
    )
    row = db().execute("SELECT * FROM tickets WHERE id = ?", (tid,)).fetchone()
    return jsonify({"ticket": ticket_dict(row, include_private=True)})

# ------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
