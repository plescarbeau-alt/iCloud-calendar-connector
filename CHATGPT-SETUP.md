# Connexion privée à ChatGPT

Le serveur est une app ChatGPT privée sans interface graphique. Il fournit cinq
outils MCP et utilise OAuth 2.1 avec PKCE.

## Ajouter l’app dans ChatGPT

1. Dans ChatGPT, ouvrir **Réglages > Sécurité et connexion**.
2. Activer **Mode développeur**.
3. Ouvrir **Plugins**, puis sélectionner le bouton **+**.
4. Nom : `iCloud Calendar`.
5. Description : `Consulter et gérer mes calendriers iCloud autorisés.`
6. Connexion : `https://codex-icloud-calendar.onrender.com/mcp`.
7. Créer la connexion et vérifier que cinq outils sont découverts.
8. Pendant l’autorisation, saisir la valeur secrète
   `ICLOUD_MCP_BEARER_TOKEN` enregistrée dans Render.

Ne jamais copier les secrets Render dans une conversation.

## Test

Dans une nouvelle conversation, sélectionner **iCloud Calendar** et demander :

> Liste mes calendriers iCloud autorisés. N’utilise ni Google Calendar ni
> l’application Calendrier du Mac.

