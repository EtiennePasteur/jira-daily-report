---
name: jira-daily-report
description: Rapport du daily Jira pour le sprint actif d'un board — burnup en JPG, mouvements depuis hier, état du sprint par colonne (assignés, MR/PR, tickets non affectés). Use when the daily routine or the user needs the sprint burnup, sprint progress, what changed since yesterday, or who is on which ticket.
---

# Rapport du daily Jira

`scripts/daily_report.py` produit `burnup.jpg` et `daily.md`, dans cet ordre de lecture :

1. **Burnup** : Jira n'exporte pas ce graphique ; le script rejoue l'appel interne du front
   (`/rest/greenhopper/1.0/rapid/charts/scopechangeburndownchart`) et le redessine à l'identique.
2. **Depuis hier** : diff de l'état des tickets entre « hier » (même heure, jour ouvré précédent selon
   le calendrier du board : le lundi, vendredi) et maintenant — terminés, réouverts, ajoutés, retirés, ré-estimés.
3. **État du sprint** (API publique, `sprint = <id>`) : une rubrique par colonne du board, sous-tâches
   regroupées sous leur story. Rubriques réglées par `JIRA_BOARD_SECTIONS` (voir Configuration).

## Étapes

1. Vérifier l'environnement : `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (jeton API classique, Basic auth),
   `JIRA_BOARD_ID` ; `JIRA_BOARD_SECTIONS` est facultatif.
2. Générer :
   ```bash
   uv run --script .claude/skills/jira-daily-report/scripts/daily_report.py -o burnup.jpg --report daily.md
   ```
   Sans `uv` : `pip install matplotlib` puis `python3 .claude/skills/jira-daily-report/scripts/daily_report.py …`.
   Sprint actif et champ d'estimation sont détectés ; `--sprint` / `--estimate` les forcent,
   `--since '2026-10-13 09:00'` (heure du board) force le début de la fenêtre « depuis hier ».
3. Terminé quand `burnup.jpg` et `daily.md` existent et que stdout est une ligne JSON
   `{"sprint", "statistic", "done", "scope", "guideline", "dataTime", "image", "report", "sinceYesterday", "board"}`.
   `daily.md` est le rapport de référence ; `sinceYesterday` et `board` portent les mêmes données pour un autre format de diffusion.

## Configuration des rubriques

`JIRA_BOARD_SECTIONS` : liste JSON, dans l'ordre du rapport, de `{"column", "title" (facultatif), "layout"}`.
`column` est le nom exact de la colonne du board (casse ignorée). Présentations :

| `layout`   | Contenu |
|------------|---------|
| `topic`    | nombre de tickets par epic |
| `review`   | par assigné, avec les MR/PR trouvées dans les commentaires |
| `assignee` | par assigné |
| `todo`     | nombre par assigné, liste des tickets non affectés |

```json
[{"column": "EN RECETTE", "title": "Recette à faire", "layout": "topic"},
 {"column": "EN REVIEW", "title": "Review à faire", "layout": "review"},
 {"column": "EN COURS", "layout": "assignee"},
 {"column": "À FAIRE", "layout": "todo"}]
```

Sans cette variable : toutes les colonnes sauf la dernière (terminé), de droite à gauche, la première en `todo`.
Une colonne introuvable est signalée sur stderr et sa rubrique sautée.

## Merge requests / pull requests

Aucun appel à GitLab ou GitHub : ils sont souvent internes, donc injoignables depuis le cloud.
Les liens viennent des commentaires Jira (connecteur GitLab « mentioned this issue in a merge request », liens collés à la main).
Leur état (ouverte, fusionnée) est donc inconnu : le rapport affiche les 3 numéros les plus récents par ticket,
et signale « aucune MR/PR dans les commentaires » quand il n'en trouve pas.

## En cas d'échec

- **HTTP 401/403** : jeton expiré ou e-mail incorrect.
- **HTTP 403 `host_not_allowed`** (routine cloud) : le domaine Jira manque dans l'accès réseau de l'environnement.
- **HTTP 404 / JSON inattendu sur greenhopper** : l'endpoint interne a changé. Rouvrir la page
  `…/boards/<id>/reports/burnup-chart` avec les DevTools (onglet Réseau, filtre `scopechange`) et adapter `fetch_bundle`.
- Pour reproduire hors ligne : `--save-json data.json` à la récupération, puis `--from-json data.json`.
  `examples/demo-sprint.json` est une capture anonymisée (burnup 14 / 68 SP) ; `examples/daily.md` en est le rendu
  avec la configuration ci-dessus.
