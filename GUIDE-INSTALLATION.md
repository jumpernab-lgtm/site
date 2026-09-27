# Guide d'installation — Impressions 3D QC

Le projet est en **deux parties** (deux dépôts GitHub) :

```
impressions3dqc/
├── site/          → dépôt 1 : le site (GitHub Pages)
│   ├── index.html            Page d'accueil
│   ├── shop.html             Formulaire de demande (Shop)
│   ├── commandes.html        Commandes du client + discussion
│   ├── admin/index.html      Panneau admin  →  URL : /admin/
│   └── assets/
│       ├── style.css
│       └── app.js            ⚙️ URL du backend à modifier ici
└── backend/       → dépôt 2 : l'API (Render)
    ├── app.py
    └── requirements.txt
```

GitHub Pages ne sert que des fichiers statiques : il ne peut pas stocker les tickets, envoyer des
courriels ni protéger un mot de passe. C'est le rôle du backend sur Render (même modèle que votre
Fusion SDK).

---

## 1) Déployer le backend sur Render

1. Créez un dépôt GitHub avec le contenu du dossier `backend/` (les 2 fichiers à la racine).
2. Sur [render.com](https://render.com) → **New → Web Service** → connectez ce dépôt.
3. Réglages :
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `gunicorn app:app`
4. Variables d'environnement (onglet **Environment**) :

| Variable | Requis | Exemple / valeur |
|---|---|---|
| `ADMIN_PASSWORD` | ✅ | Un mot de passe long et unique (c'est le mot de passe du panneau admin) |
| `BREVO_API_KEY` | ✅ (pour les courriels) | Clé API Brevo (voir étape 2) |
| `SENDER_EMAIL` | — | `impressions3dqc@proton.me` (défaut) |
| `ADMIN_EMAIL` | — | `impressions3dqc@proton.me` (défaut) — reçoit les notifications |
| `SENDER_NAME` | — | `Impressions 3D QC` (défaut) |
| `SITE_URL` | recommandé | URL publique du site, ex. `https://VOTRECOMPTE.github.io/impressions3dqc` (sert aux liens dans les courriels) |
| `CORS_ORIGINS` | recommandé | `https://VOTRECOMPTE.github.io` (verrouille l'API à votre site) |
| `DB_PATH` | — | `data.db` (défaut) |
| `DEV_MODE` | ⚠️ jamais en prod | `1` = renvoie le code de vérification dans la réponse (tests seulement) |

5. Déployez, puis notez l'URL du service, ex. `https://impressions3dqc-api.onrender.com`.
   Testez : ouvrir `https://…onrender.com/api/health` doit afficher `{"ok": true, …}`.

### ⏰ Anti-veille (plan gratuit)
Comme pour vos autres services Render : ajoutez un moniteur **UptimeRobot** (HTTP, toutes les
5 minutes) sur `https://…onrender.com/api/health` pour éviter la mise en veille.

### ⚠️ IMPORTANT — Persistance des données (plan gratuit)
Sur le plan gratuit de Render, le disque est **éphémère** : le fichier SQLite (`data.db`) est
**effacé à chaque redéploiement ou redémarrage** du service. UptimeRobot évite la mise en veille,
mais pas les redéploiements.
- Évitez de redéployer le backend quand des commandes sont actives.
- Pour une vraie persistance : ajoutez un **disque persistant Render** (payant) et mettez
  `DB_PATH=/data/data.db`, ou on pourra migrer vers une base hébergée plus tard.

---

## 2) Configurer Brevo (courriels)

1. Créez un compte gratuit sur [brevo.com](https://www.brevo.com) (300 courriels/jour).
2. **Senders & IP → Senders → Add a sender** : ajoutez `impressions3dqc@proton.me`, puis cliquez
   le lien de confirmation reçu dans votre boîte Proton.
3. **SMTP & API → API Keys → Generate a new API key** → copiez la clé dans `BREVO_API_KEY` sur Render.

Sans clé Brevo, le site fonctionne mais aucun courriel ne part (les envois sont consignés dans les
logs Render à la place).

---

## 3) Déployer le site sur GitHub Pages

1. Créez un dépôt GitHub et téléversez le **contenu du dossier `site/`** (les fichiers à la racine
   du dépôt, en gardant les dossiers `assets/` et `admin/`).
2. **Avant de téléverser** : ouvrez `assets/app.js` et remplacez la première ligne :
   ```js
   window.API_BASE = "https://VOTRE-BACKEND.onrender.com";
   ```
   par l'URL réelle de votre service Render (sans `/` à la fin).
3. Dépôt → **Settings → Pages → Deploy from a branch** → branche `main`, dossier `/ (root)`.
4. Le site sera à `https://VOTRECOMPTE.github.io/NOM-DU-DEPOT/`
   et le panneau admin à `https://VOTRECOMPTE.github.io/NOM-DU-DEPOT/admin/`.
5. Retournez sur Render et mettez à jour `SITE_URL` et `CORS_ORIGINS` avec cette adresse.

### Domaine personnalisé (optionnel)
Avec un domaine (ex. `monshop.com`) configuré dans GitHub Pages, le panneau devient
`monshop.com/admin/` automatiquement (c'est le dossier `admin/`).

> Note sécurité : la page admin est un fichier public, mais elle ne contient **aucun secret** —
> toute la protection est côté serveur (mot de passe jamais dans le code, jetons de session,
> limite de tentatives). Quelqu'un qui ouvre `/admin/` sans mot de passe ne peut rien voir ni faire.

---

## 4) Tester de bout en bout

1. Ouvrez le site → **Shop** → remplissez une demande avec votre propre courriel.
2. Recevez le code à 6 chiffres → validez → le ticket est créé (courriel client + courriel admin).
3. Ouvrez `/admin/` → connectez-vous → onglet **Tickets** → répondez au client (il reçoit un courriel).
4. Côté client, onglet **Commandes** → ouvrez le ticket → répondez → **Confirmer ma commande**.
5. Le ticket passe dans **Commandes en cours** côté admin → **Marquer comme complétée ✔**.
6. Il passe dans **Commandes passées** (suppression automatique 30 jours après).

---

## Sécurité incluse

- Mot de passe admin : jamais dans le code, haché en mémoire, comparaison sécurisée,
  **5 tentatives max / 15 min / IP**.
- Sessions : jetons aléatoires 256 bits, hachés en base, expiration (admin 12 h, client 30 jours).
- Clients : vérification du courriel par code à 6 chiffres (10 min, 8 essais max) — personne ne
  peut voir les adresses/commandes d'un autre client.
- Limites anti-abus : demandes de code, création de tickets, messages.
- Validation Québec : code postal `G/H/J` obligatoire.
- CORS verrouillable sur votre domaine, en-têtes de sécurité, taille des requêtes limitée.

## Paiement

Volontairement en **placeholder** pour l'instant : à la confirmation, le client voit
« Nous vous contacterons par courriel pour organiser le paiement (ex. virement Interac) ».
L'intégration d'un vrai paiement en ligne pourra être ajoutée plus tard.
