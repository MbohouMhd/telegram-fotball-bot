# Telegram Football Analysis Bot

Bot Telegram qui récupère automatiquement les données football via API-Football et produit une analyse statistique.

## Données utilisées
- 5 derniers matchs de chaque équipe
- buts marqués/encaissés
- over 2,5 et BTTS
- domicile/extérieur
- confrontations directes
- joueurs offensifs clés et buts récents
- blessures/suspensions disponibles dans l'API
- probabilités 1X2 calculées par un modèle simple et transparent
- score probable
- paris statistiques: double chance, over 1,5 / 2,5, BTTS

API-Football documente notamment les endpoints Fixtures, Events, Players, Injuries, Statistics et Predictions.

## Installation
1. Crée un bot avec @BotFather et récupère le token.
2. Crée une clé API-Football.
3. Copie `.env.example` vers `.env`.
4. Installe Python 3.11+.
5. Dans ce dossier:
   `pip install -r requirements.txt`
6. Lance:
   `python bot.py`

## Commande
`/analyse Equipe 1 vs Equipe 2 | Compétition | 2026-09-20`

Exemple:
`/analyse Manchester United vs Manchester City | Premier League | 2026-09-20`

Tu peux aussi envoyer directement la même ligne sans `/analyse`.

## Important
Les pourcentages sont des estimations statistiques, pas des garanties de résultat. Si l'API ne fournit pas une donnée, le bot l'indique au lieu de l'inventer.
