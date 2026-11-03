# Le Fil Politique

Veille des déclarations et de l'actualité politiques pour un étudiant en journalisme : Élysée et Matignon, gouvernement, Parlement, partis. Un site installable sur iPhone, mis à jour toutes les deux heures de 7 h 45 à 23 h, avec une notification chaque matin à 8 h. Entièrement gratuit, sans IA : le texte sous chaque titre reprend les premières phrases de l'article.

## Ce que l'on y trouve

- **L'essentiel des dernières 24 h** : les sujets repris par le plus de médias.
- **Le fil** des 3 derniers jours, groupé par jour et par rubrique, avec le type de déclaration (Citation, Interview, Discours, Annonce, Réaction, Tribune, Communiqué).
- Le filtre **Qui a dit quoi** ne garde que les déclarations ; **Sources officielles** ne garde que l'Élysée et le Sénat.
- L'étoile garde un article **À relire**.

## Sources

Élysée et communiqués du Sénat ; rubriques politiques de LCP, Public Sénat, Le Monde, franceinfo, Le Figaro, Libération, La Croix, L'Express, BFMTV et 20 Minutes ; recherches Google Actualités dont seuls les médias reconnus de la liste `trusted_publishers` sont gardés. Tout se règle dans `sources.json`. Quand un ministre ou un chef de parti change, ajoutez son nom dans `keywords`. Un flux en panne n'empêche pas les autres et son nom s'affiche en bas du site.

## Mise en ligne

Fichiers à la racine du dépôt GitHub (glisser-déposer), sauf `.github/workflows/veille.yml`, à créer avec **Add file > Create new file**. Dans **Settings > Pages**, **Source** = **GitHub Actions**. Secrets : `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` et `PUSH_SUBSCRIPTIONS` (le code affiché par l'application après **Activer la notification de 8 h**, un code par ligne pour plusieurs iPhone).

Pour tester : **Actions > Mise à jour du fil > Run workflow**, en cochant **Envoyer la notification maintenant** si besoin.
