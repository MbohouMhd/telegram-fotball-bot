# bot.py
# Version corrigée du bot Telegram d'analyse football.
# Remplace le contenu de ton bot.py par ce fichier.

import os
import re
import math
import asyncio
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_KEY = os.getenv("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN est manquant.")
if not API_KEY:
    raise RuntimeError("API_FOOTBALL_KEY est manquant.")

HEADERS = {"x-apisports-key": API_KEY}


class FootballAPI:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL, headers=HEADERS, timeout=30.0
        )

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, **params) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        response = await self.client.get(path, params=params)
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data

    async def search_league(self, name):
        return (await self.get("/leagues", search=name)).get("response", [])

    async def fixtures(self, **params):
        return (await self.get("/fixtures", **params)).get("response", [])

    async def events(self, fixture_id):
        return (await self.get("/fixtures/events", fixture=fixture_id)).get("response", [])

    async def injuries(self, fixture_id):
        return (await self.get("/injuries", fixture=fixture_id)).get("response", [])

    async def players(self, team_id, season):
        return (await self.get("/players", team=team_id, season=season)).get("response", [])


api = FootballAPI()


def clean_name(name):
    return re.sub(r"\s+", " ", name.strip())


def normalize(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def percentage(value):
    return f"{round(value * 100)}%"


def parse_request(text):
    text = re.sub(r"^/analyse\s*", "", text.strip(), flags=re.I)
    parts = [p.strip() for p in text.split("|")]

    if len(parts) != 3:
        raise ValueError(
            "Format incorrect.\n\n"
            "Utilise :\n"
            "/analyse Équipe 1 vs Équipe 2 | Compétition | AAAA-MM-JJ\n\n"
            "Exemple :\n"
            "/analyse PSG vs Marseille | Ligue 1 | 2026-09-20"
        )

    teams = re.split(r"\s+vs\.?\s+|\s+v\s+", parts[0], flags=re.I)

    if len(teams) != 2:
        raise ValueError("Utilise le format « Équipe 1 vs Équipe 2 ».")

    try:
        datetime.strptime(parts[2], "%Y-%m-%d")
    except ValueError:
        raise ValueError("La date doit être au format AAAA-MM-JJ.")

    return clean_name(teams[0]), clean_name(teams[1]), clean_name(parts[1]), parts[2]


def season_for_date(date_text):
    date = datetime.strptime(date_text, "%Y-%m-%d")
    return date.year if date.month >= 7 else date.year - 1


async def resolve_fixture(home, away, competition, date_text):
    leagues = await api.search_league(competition)

    if not leagues:
        raise ValueError(f"Compétition introuvable : {competition}")

    target = normalize(competition)

    exact = [
        x for x in leagues
        if normalize(x.get("league", {}).get("name", "")) == target
    ]

    league = (exact[0] if exact else leagues[0])["league"]
    league_id = league["id"]
    season = season_for_date(date_text)

    try:
        fixtures = await api.fixtures(
            league=league_id,
            season=season,
            date=date_text
        )
    except RuntimeError as error:
        if "Free plans do not have access" in str(error):
            raise ValueError(
                f"⚠️ Ton forfait API-Football gratuit ne permet pas "
                f"d'accéder à la saison {season}.\n\n"
                "Le bot fonctionne, mais l'API bloque cette saison. "
                "Il faut une source ou un plan donnant accès aux données actuelles."
            )
        raise

    if not fixtures:
        fixtures = await api.fixtures(date=date_text)

    nh, na = normalize(home), normalize(away)

    for f in fixtures:
        h = normalize(f["teams"]["home"]["name"])
        a = normalize(f["teams"]["away"]["name"])

        if (nh in h or h in nh) and (na in a or a in na):
            return f, league_id, season

    for f in fixtures:
        h = normalize(f["teams"]["home"]["name"])
        a = normalize(f["teams"]["away"]["name"])

        if (nh in a or a in nh) and (na in h or h in na):
            return f, league_id, season

    raise ValueError(
        f"Match introuvable le {date_text}.\n"
        f"Équipes : {home} vs {away}\n"
        f"Compétition : {league['name']}\n"
        f"Saison recherchée : {season}"
    )


async def recent_matches(team_id, season, last=5):
    try:
        matches = await api.fixtures(team=team_id, last=last)
    except Exception:
        return []

    return [
        x for x in matches
        if x.get("league", {}).get("season") == season
    ][:last]


def result_for_team(fixture, team_id):
    is_home = fixture["teams"]["home"]["id"] == team_id

    gf = fixture["goals"]["home"] if is_home else fixture["goals"]["away"]
    ga = fixture["goals"]["away"] if is_home else fixture["goals"]["home"]

    opponent = (
        fixture["teams"]["away"]["name"]
        if is_home
        else fixture["teams"]["home"]["name"]
    )

    if gf is None or ga is None:
        return None

    result = "V" if gf > ga else "N" if gf == ga else "D"

    return {
        "result": result,
        "gf": gf,
        "ga": ga,
        "opponent": opponent,
        "date": fixture["fixture"]["date"][:10],
    }


def summarize_form(matches, team_id):
    rows = [result_for_team(x, team_id) for x in matches]
    rows = [x for x in rows if x]

    if not rows:
        return {
            "rows": [],
            "ppg": 0,
            "gf": 0,
            "ga": 0,
            "btts": 0,
            "over25": 0,
        }

    points = sum(
        3 if x["result"] == "V"
        else 1 if x["result"] == "N"
        else 0
        for x in rows
    )

    return {
        "rows": rows,
        "ppg": points / len(rows),
        "gf": sum(x["gf"] for x in rows) / len(rows),
        "ga": sum(x["ga"] for x in rows) / len(rows),
        "btts": sum(
            1 for x in rows
            if x["gf"] > 0 and x["ga"] > 0
        ) / len(rows),
        "over25": sum(
            1 for x in rows
            if x["gf"] + x["ga"] >= 3
        ) / len(rows),
    }


def venue_form(matches, team_id, home):
    selected = [
        f for f in matches
        if (f["teams"]["home"]["id"] == team_id) == home
    ]
    return summarize_form(selected, team_id)


def poisson(k, expected):
    return math.exp(-expected) * expected ** k / math.factorial(k)


def poisson_1x2(home_expected, away_expected):
    home_win = draw = away_win = 0.0

    for hg in range(8):
        for ag in range(8):
            p = poisson(hg, home_expected) * poisson(ag, away_expected)

            if hg > ag:
                home_win += p
            elif hg == ag:
                draw += p
            else:
                away_win += p

    total = home_win + draw + away_win

    return (
        home_win / total,
        draw / total,
        away_win / total,
    )


def model_probability(hf, af, hv, av, h2h_home, h2h_away):
    home_attack = hv["gf"] or hf["gf"] or 1
    home_defense = hv["ga"] or hf["ga"] or 1
    away_attack = av["gf"] or af["gf"] or 1
    away_defense = av["ga"] or af["ga"] or 1

    expected_home = (
        0.58 * home_attack
        + 0.42 * away_defense
        + 0.20
    )

    expected_away = (
        0.58 * away_attack
        + 0.42 * home_defense
        - 0.05
    )

    form_difference = hf["ppg"] - af["ppg"]

    expected_home *= 1 + form_difference * 0.08
    expected_away *= 1 - form_difference * 0.08

    if h2h_home + h2h_away > 0:
        difference = h2h_home - h2h_away
        expected_home *= 1 + difference * 0.025
        expected_away *= 1 - difference * 0.025

    expected_home = max(0.15, expected_home)
    expected_away = max(0.10, expected_away)

    ph, pd, pa = poisson_1x2(
        expected_home,
        expected_away
    )

    return (
        ph,
        pd,
        pa,
        expected_home,
        expected_away,
    )


def market_probabilities(expected_home, expected_away):
    total = expected_home + expected_away

    under15 = poisson(0, total) + poisson(1, total)

    under25 = (
        poisson(0, total)
        + poisson(1, total)
        + poisson(2, total)
    )

    p0h = poisson(0, expected_home)
    p0a = poisson(0, expected_away)

    return {
        "over15": 1 - under15,
        "over25": 1 - under25,
        "btts": 1 - p0h - p0a + p0h * p0a,
    }


def probable_score(expected_home, expected_away):
    best_probability = -1
    best_home = 0
    best_away = 0

    for hg in range(7):
        for ag in range(7):
            p = poisson(hg, expected_home) * poisson(ag, expected_away)

            if p > best_probability:
                best_probability = p
                best_home = hg
                best_away = ag

    return best_home, best_away, best_probability


def confidence(probability):
    return max(
        50,
        min(
            88,
            round(50 + abs(probability - 0.5) * 70)
        )
    )


async def player_form(team_id, season):
    try:
        players = await api.players(team_id, season)
    except Exception:
        return []

    candidates = []

    for item in players:
        player = item.get("player", {})
        goals = 0
        appearances = 0

        for stat in item.get("statistics") or []:
            goals_data = stat.get("goals") or {}
            games = stat.get("games") or {}

            goals += goals_data.get("total") or 0
            appearances += games.get("appearences") or 0

        if goals:
            candidates.append(
                (
                    goals,
                    appearances,
                    player.get("name", "Joueur")
                )
            )

    candidates.sort(reverse=True)

    return candidates[:3]


async def recent_goal_scorers(matches, team_id):
    scorers = {}

    for match in matches:
        try:
            events = await api.events(match["fixture"]["id"])
        except Exception:
            continue

        for event in events:
            if event.get("type") != "Goal":
                continue

            if event.get("team", {}).get("id") != team_id:
                continue

            name = (event.get("player") or {}).get("name")

            if name:
                scorers[name] = scorers.get(name, 0) + 1

    return sorted(
        scorers.items(),
        key=lambda x: x[1],
        reverse=True
    )


def injuries_for_team(injuries, team_id):
    result = []

    for injury in injuries:
        if injury.get("team", {}).get("id") != team_id:
            continue

        player = injury.get("player") or {}

        name = player.get("name", "Joueur")
        reason = (
            player.get("reason")
            or player.get("type")
            or "indisponibilité"
        )

        result.append(f"{name} ({reason})")

    return result


def form_string(summary):
    return " ".join(
        x["result"]
        for x in summary["rows"]
    ) or "N/D"


async def build_analysis(home, away, competition, date_text):
    fixture, league_id, season = await resolve_fixture(
        home,
        away,
        competition,
        date_text
    )

    home_id = fixture["teams"]["home"]["id"]
    away_id = fixture["teams"]["away"]["id"]

    actual_home = fixture["teams"]["home"]["name"]
    actual_away = fixture["teams"]["away"]["name"]

    home_matches, away_matches = await asyncio.gather(
        recent_matches(home_id, season, 5),
        recent_matches(away_id, season, 5),
    )

    hf = summarize_form(home_matches, home_id)
    af = summarize_form(away_matches, away_id)

    home_all, away_all = await asyncio.gather(
        recent_matches(home_id, season, 10),
        recent_matches(away_id, season, 10),
    )

    hv = venue_form(home_all, home_id, True)
    av = venue_form(away_all, away_id, False)

    try:
        h2h = await api.fixtures(
            h2h=f"{home_id}-{away_id}",
            last=5
        )
    except Exception:
        h2h = []

    h2h_home = 0
    h2h_draw = 0
    h2h_away = 0
    h2h_rows = []

    for match in h2h:
        hg = match["goals"]["home"]
        ag = match["goals"]["away"]

        if hg is None or ag is None:
            continue

        match_home_id = match["teams"]["home"]["id"]

        if hg == ag:
            h2h_draw += 1
        elif (
            (match_home_id == home_id and hg > ag)
            or
            (match_home_id != home_id and ag > hg)
        ):
            h2h_home += 1
        else:
            h2h_away += 1

        h2h_rows.append(
            (
                match["teams"]["home"]["name"],
                hg,
                ag,
                match["teams"]["away"]["name"],
            )
        )

    try:
        injuries = await api.injuries(
            fixture["fixture"]["id"]
        )
    except Exception:
        injuries = []

    home_injuries = injuries_for_team(
        injuries,
        home_id
    )

    away_injuries = injuries_for_team(
        injuries,
        away_id
    )

    ph, pd, pa, expected_home, expected_away = model_probability(
        hf,
        af,
        hv,
        av,
        h2h_home,
        h2h_away,
    )

    markets = market_probabilities(
        expected_home,
        expected_away
    )

    score_home, score_away, score_probability = probable_score(
        expected_home,
        expected_away
    )

    home_players, away_players = await asyncio.gather(
        player_form(home_id, season),
        player_form(away_id, season),
    )

    home_scorers, away_scorers = await asyncio.gather(
        recent_goal_scorers(home_matches, home_id),
        recent_goal_scorers(away_matches, away_id),
    )

    double_1x = ph + pd
    double_x2 = pd + pa

    bets = [
        (
            double_1x,
            f"Double chance 1X — {percentage(double_1x)}"
        ),
        (
            double_x2,
            f"Double chance X2 — {percentage(double_x2)}"
        ),
        (
            markets["over15"],
            f"Plus de 1,5 buts — {percentage(markets['over15'])}"
        ),
        (
            markets["over25"],
            f"Plus de 2,5 buts — {percentage(markets['over25'])}"
        ),
        (
            markets["btts"],
            f"Les deux équipes marquent — {percentage(markets['btts'])}"
        ),
    ]

    bets.sort(
        key=lambda x: x[0],
        reverse=True
    )

    lines = [
        "⚽ <b>ANALYSE DU MATCH</b>",
        f"<b>{actual_home}</b> 🆚 <b>{actual_away}</b>",
        f"🏆 {fixture['league']['name']}",
        f"📅 {date_text}",
        "",
        "📊 <b>1. 5 DERNIERS MATCHS</b>",
        f"• {actual_home}: {form_string(hf)} | "
        f"{hf['gf']:.2f} buts marqués/m | "
        f"{hf['ga']:.2f} encaissés/m",
        f"• {actual_away}: {form_string(af)} | "
        f"{af['gf']:.2f} buts marqués/m | "
        f"{af['ga']:.2f} encaissés/m",
        "",
        "🛡️ <b>2. ATTAQUE / DÉFENSE</b>",
        f"• {actual_home}: {hf['ppg']:.2f} PPM | "
        f"BTTS {percentage(hf['btts'])} | "
        f"+2,5 {percentage(hf['over25'])}",
        f"• {actual_away}: {af['ppg']:.2f} PPM | "
        f"BTTS {percentage(af['btts'])} | "
        f"+2,5 {percentage(af['over25'])}",
        f"• Domicile {actual_home}: {hv['ppg']:.2f} PPM",
        f"• Extérieur {actual_away}: {av['ppg']:.2f} PPM",
        "",
        "🤝 <b>3. CONFRONTATIONS DIRECTES</b>",
    ]

    if h2h_rows:
        lines.append(
            f"• {h2h_home} victoire(s) {actual_home}, "
            f"{h2h_draw} nul(s), "
            f"{h2h_away} victoire(s) {actual_away}"
        )

        for h, hg, ag, a in h2h_rows[:5]:
            lines.append(f"  {h} {hg}-{ag} {a}")
    else:
        lines.append("• Données H2H indisponibles.")

    lines += [
        "",
        "⭐ <b>4. JOUEURS CLÉS</b>",
    ]

    if home_players:
        lines.append(
            f"• {actual_home}: " +
            ", ".join(
                f"{name} ({goals} buts)"
                for goals, apps, name in home_players
            )
        )

    if away_players:
        lines.append(
            f"• {actual_away}: " +
            ", ".join(
                f"{name} ({goals} buts)"
                for goals, apps, name in away_players
            )
        )

    if home_scorers:
        lines.append(
            f"• Buteurs récents {actual_home}: " +
            ", ".join(
                f"{name} x{goals}"
                for name, goals in home_scorers[:3]
            )
        )

    if away_scorers:
        lines.append(
            f"• Buteurs récents {actual_away}: " +
            ", ".join(
                f"{name} x{goals}"
                for name, goals in away_scorers[:3]
            )
        )

    lines += [
        "",
        "🏟️ <b>5. AVANTAGE DU TERRAIN</b>",
        f"• {actual_home} reçoit. Le modèle applique un avantage domicile modéré.",
        "",
        "🚑 <b>6. BLESSURES / SUSPENSIONS</b>",
        f"• {actual_home}: "
        f"{', '.join(home_injuries) if home_injuries else 'aucune indisponibilité retournée par l’API.'}",
        f"• {actual_away}: "
        f"{', '.join(away_injuries) if away_injuries else 'aucune indisponibilité retournée par l’API.'}",
        "",
        "📈 <b>PROBABILITÉS</b>",
        f"• Victoire {actual_home}: <b>{percentage(ph)}</b>",
        f"• Match nul: <b>{percentage(pd)}</b>",
        f"• Victoire {actual_away}: <b>{percentage(pa)}</b>",
        "",
        f"🎯 <b>SCORE PROBABLE: {score_home}-{score_away}</b>",
        f"Probabilité du score exact: {percentage(score_probability)}",
        "",
        "💰 <b>PARIS STATISTIQUES INTÉRESSANTS</b>",
    ]

    for probability, label in bets[:3]:
        lines.append(
            f"• {label} | confiance {confidence(probability)}%"
        )

    overall = max(ph, pd, pa)

    lines += [
        "",
        f"🧠 <b>CONFIANCE GLOBALE: {confidence(overall)}%</b>",
        "",
        "ℹ️ Les probabilités sont des estimations statistiques, "
        "pas une garantie de résultat.",
        "📐 Modèle : forme récente + buts marqués/encaissés + "
        "domicile/extérieur + H2H + joueurs disponibles.",
    ]

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ <b>BOT D'ANALYSE FOOTBALL</b>\n\n"
        "Utilise :\n"
        "/analyse Équipe 1 vs Équipe 2 | Compétition | AAAA-MM-JJ\n\n"
        "Exemple :\n"
        "/analyse PSG vs Marseille | Ligue 1 | 2026-09-20",
        parse_mode="HTML",
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        home, away, competition, date_text = parse_request(
            update.message.text or ""
        )
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    message = await update.message.reply_text(
        "⏳ Je recherche les statistiques...\n"
        "📊 Forme\n"
        "⚽ Attaque/défense\n"
        "🤝 H2H\n"
        "🚑 Absences\n"
        "🧮 Calcul du modèle..."
    )

    try:
        report = await build_analysis(
            home,
            away,
            competition,
            date_text
        )

        chunks = [
            report[i:i + 3900]
            for i in range(0, len(report), 3900)
        ]

        await message.delete()

        for chunk in chunks:
            await update.message.reply_text(
                chunk,
                parse_mode="HTML"
            )

    except Exception as error:
        await message.edit_text(
            "❌ <b>Impossible de produire l'analyse.</b>\n\n"
            f"Détail : {str(error)[:1500]}",
            parse_mode="HTML"
        )


async def error_handler(update, context):
    print("ERREUR TELEGRAM :", context.error)


async def main():
    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("analyse", analyze)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            analyze
        )
    )

    application.add_error_handler(error_handler)

    print("✅ Bot Telegram démarré.")

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
