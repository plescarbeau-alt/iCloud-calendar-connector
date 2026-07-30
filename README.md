# iCloud Calendar pour Codex

Connecteur MCP privé pour accéder à un ensemble explicitement autorisé de
calendriers iCloud via CalDAV.

## Sécurité

- Utiliser uniquement un mot de passe Apple spécifique à l’application.
- Ne jamais placer de secret dans le dépôt, `.mcp.json` ou une conversation.
- Définir `ICLOUD_ALLOWED_CALENDARS` avec le nom exact des calendriers autorisés.
- En hébergement, imposer HTTPS et conserver tous les secrets uniquement dans Render.
- L’autorisation distante utilise OAuth 2.1 avec PKCE. La page d’autorisation
  demande la valeur `ICLOUD_MCP_BEARER_TOKEN`, sans jamais la transmettre au
  client MCP.
- Révoquer immédiatement le mot de passe d’application Apple en cas de doute.

## Variables

Copier `.env.example` vers `.env` uniquement pour un test local. Pour un
hébergement, saisir les mêmes variables dans le gestionnaire de secrets de la
plateforme.

## Test local

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a
source .env
set +a
.venv/bin/python scripts/server.py --stdio
```

## Hébergement Docker

Construire l’image à partir du `Dockerfile`, ajouter les variables secrètes sur
la plateforme, puis exposer le port `8000` derrière HTTPS. Le point d’accès MCP
est `/mcp`. Le client découvre automatiquement l’autorisation OAuth, obtient un
jeton lié à ce service et l’envoie dans l’en-tête `Authorization`.

## Déploiement Render

Le fichier `render.yaml` crée un service Docker et laisse les données Apple
marquées `sync: false`, afin qu'elles soient saisies uniquement dans le tableau
de bord Render. Le mot de passe du connecteur et la clé de signature OAuth sont
générés automatiquement.

Render déploie depuis un dépôt Git. Après avoir relié le dépôt, choisir
**New > Blueprint**, sélectionner ce dépôt, puis renseigner uniquement dans
Render :

- `ICLOUD_USERNAME`
- `ICLOUD_APP_PASSWORD`
- `ICLOUD_ALLOWED_CALENDARS`

## Outils MCP

- `list_calendars`
- `list_events`
- `create_event`
- `update_event`
- `delete_event`

Chaque écriture est relue depuis iCloud avant d’être annoncée comme vérifiée.
