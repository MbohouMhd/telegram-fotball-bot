import os
import re
import math
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SPORTMONKS_TOKEN = os.getenv("SPORTMONKS_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans Railway Variables.")

if not SPORTMONKS_TOKEN:
    raise RuntimeError(
        "SPORTMONKS_TOKEN manquant dans Railway Variables. "
        "Crée ton token Sportmonks puis ajoute-le dans Railway."
    )

BASE_URL = "https://api.sportmonks.com/v3/football"


# ============================================================
# OUTILS
# ============================================================

def norm(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text)


def parse_match(text: str):
    text = re.split(r"\s*\|\s*", text, maxsplit=1)[0].strip()
    parts = re.split(
        r"\s+(?:vs?\.?|contre)\s+|\s+-\s+|\s+–\s+|\s+—\s+",
        text,
        maxsplit=1,
        flags=re.I,
    )
    if len(parts) != 2:
        return None, None
    return parts[0].strip(), parts[1].strip()


async def api_get(path: str, params=None):
    params = dict(params or {})
    params["api_token"] = SPORTMONKS_TOKEN

    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(BASE_URL + path, params=params)

    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text[:500]
        raise RuntimeError(
            f"Sportmonks HTTP {response.status_code}: {detail}"
        )

    data = response.json()

    if isinstance(data, dict) and data.get("message"):
        # Certaines réponses Sportmonks mettent l'erreur dans message.
        if not data.get("data"):
            raise RuntimeError(str(data["message"]))

    return data


def collection(data):
    if isinstance(data, dict):
        value = data.get("data", [])
        return value if isinstance(value, list) else [value]
    return data if isinstance(data, list) else []


# ============================================================
# RECHERCHE DES ÉQUIPES / MATCHS
# ============================================================

async def search_teams(name: str):
    # Endpoint de recherche d'équipes.
    data = await api_get(
        "/teams/search/" + name,
        {"include": "country"},
    )
    return collection(data)


def team_matches_name(team, query):
    q = norm(query)
    names = [
        team.get("name", ""),
        team.get("short_code", ""),
        team.get("short_name", ""),
    ]
    names = [norm(x) for x in names if x]

    if q in names:
        return 100
    if any(q in x or x in q for x in names):
        return 80
    words = q.split()
    score = sum(any(w in x for x in names) for w in words)
    return score * 10


async def resolve_team(name: str):
    teams = await search_teams(name)
    if not teams:
        raise RuntimeError(
            f"Équipe introuvable : {name}. Essaie son nom officiel."
        )

    teams.sort(key=lambda t: team_matches_name(t, name), reverse=True)
    return teams[0]


async def find_fixture(home_name: str, away_name: str):
    home = await resolve_team(home_name)
    away = await resolve_team(away_name)

    home_id = home.get("id")
    away_id = away.get("id")

    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%d")
    end = (now + timedelta(days=30)).strftime("%Y-%m-%d")

    # Un seul appel pour chercher les matchs entre les deux équipes
    # dans une fenêtre proche de la date actuelle.
    data = await api_get(
        f"/fixtures/between/{start}/{end}",
        {
            "include": "participants;league;season;state;scores;venue",
            "per_page": 100,
        },
    )

    fixtures = collection(data)

    candidates = []
    for f in fixtures:
        participants = f.get("participants") or []
        ids = {p.get("id") for p in participants}
        if home_id not in ids or away_id not in ids:
            continue

        home_participant = next(
            (p for p in participants if p.get("id") == home_id), None
        )
        away_participant = next(
            (p for p in participants if p.get("id") == away_id), None
        )

        # Sportmonks expose généralement la position home/away
        # dans participants.meta.location.
        if home_participant:
            loc = (home_participant.get("meta") or {}).get("location")
            if loc and loc != "home":
                continue

        if away_participant:
            loc = (away_participant.get("meta") or {}).get("location")
            if loc and loc != "away":
                continue

        candidates.append(f)

    if not candidates:
        # Deuxième essai : recherche H2H.
        data = await api_get(
            f"/fixtures/head-to-head/{home_id}/{away_id}",
            {
                "include": "participants;league;season;state;scores;venue",
                "per_page": 100,
            },
        )
        candidates = collection(data)

        upcoming = []
        for f in candidates:
            try:
                dt = datetime.fromisoformat(
                    f["starting_at"].replace(" ", "T")
                ).replace(tzinfo=timezone.utc)
                if dt >= now - timedelta(hours=6):
                    upcoming.append((dt, f))
            except Exception:
                pass

        if upcoming:
            upcoming.sort(key=lambda x: x[0])
            candidates = [x[1] for x in upcoming]

    if not candidates:
        raise RuntimeError(
            "Match actuel introuvable. Vérifie les noms des équipes, "
            "la compétition et la date du match."
        )

    # Le match le plus proche dans le futur.
    def fixture_dt(f):
        try:
            return datetime.fromisoformat(
                f["starting_at"].replace(" ", "T")
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.max.replace(tzinfo=timezone.utc)

    future = [f for f in candidates if fixture_dt(f) >= now - timedelta(hours=6)]
    if future:
        return sorted(future, key=fixture_dt)[0], home, away

    return sorted(candidates, key=fixture_dt, reverse=True)[0], home, away


# ============================================================
# DONNÉES D'UNE RENCONTRE
# ============================================================

async def fixture_details(fixture_id: int):
    return (
        await api_get(
            f"/fixtures/{fixture_id}",
            {
                "include": (
                    "participants;scores;events;lineups.player;"
                    "statistics.type;league;season;state;venue;"
                    "xGFixture;predictions.type;sidelined"
                )
            },
        )
    ).get("data", {})


async def recent_matches(team_id: int, limit=5):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=120)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")

    data = await api_get(
        f"/fixtures/between/{start}/{end}/{team_id}",
        {
            "include": "participants;scores;state;league",
            "per_page": 100,
        },
    )

    fixtures = collection(data)

    finished = []
    for f in fixtures:
        state = f.get("state") or {}
        state_name = norm(
            state.get("name", "")
            or state.get("short_name", "")
            or state.get("developer_name", "")
        )

        # On conserve uniquement les matchs terminés.
        if state_name and not any(
            x in state_name
            for x in ("finished", "full time", "ft", "after")
        ):
            continue

        if not f.get("scores"):
            continue

        participants = f.get("participants") or []
        me = next((p for p in participants if p.get("id") == team_id), None)
        opp = next((p for p in participants if p.get("id") != team_id), None)
        if not me or not opp:
            continue

        scores = f.get("scores") or []

        def get_score(participant_id, description):
            for s in scores:
                if (
                    s.get("participant_id") == participant_id
                    and norm(s.get("description", "")) == norm(description)
                ):
                    try:
                        return int(s.get("score", {}).get("goals", 0))
                    except Exception:
                        pass
            return None

        gf = get_score(team_id, "CURRENT")
        ga = get_score(opp.get("id"), "CURRENT")

        if gf is None or ga is None:
            # Fallback sur le premier score compatible.
            vals = {}
            for s in scores:
                try:
                    vals[s.get("participant_id")] = int(
                        s.get("score", {}).get("goals", 0)
                    )
                except Exception:
                    pass
            gf = vals.get(team_id)
            ga = vals.get(opp.get("id"))

        if gf is None or ga is None:
            continue

        location = (me.get("meta") or {}).get("location", "")
        result = "V" if gf > ga else "N" if gf == ga else "D"

        finished.append(
            {
                "date": f.get("starting_at", ""),
                "opponent": opp.get("name", "?"),
                "gf": gf,
                "ga": ga,
                "result": result,
                "home": location == "home",
            }
        )

    finished.sort(key=lambda x: x["date"], reverse=True)
    return finished[:limit]


def form_summary(matches):
    if not matches:
        return {
            "v": 0,
            "n": 0,
            "d": 0,
            "gf": 0,
            "ga": 0,
            "points": 0,
        }

    v = sum(m["result"] == "V" for m in matches)
    n = sum(m["result"] == "N" for m in matches)
    d = sum(m["result"] == "D" for m in matches)
    gf = sum(m["gf"] for m in matches)
    ga = sum(m["ga"] for m in matches)

    return {
        "v": v,
        "n": n,
        "d": d,
        "gf": gf,
        "ga": ga,
        "points": v * 3 + n,
    }


# ============================================================
# MODÈLE DE PRONOSTIC
# ============================================================

def poisson(lam, k):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def calculate_probabilities(home_matches, away_matches):
    hf = form_summary(home_matches)
    af = form_summary(away_matches)

    hn = max(1, len(home_matches))
    an = max(1, len(away_matches))

    h_attack = hf["gf"] / hn
    h_defense = hf["ga"] / hn
    a_attack = af["gf"] / an
    a_defense = af["ga"] / an

    # Avantage domicile modéré.
    lambda_home = max(
        0.20,
        0.60 * h_attack + 0.40 * a_defense + 0.25,
    )
    lambda_away = max(
        0.15,
        0.60 * a_attack + 0.40 * h_defense,
    )

    p_home = p_draw = p_away = 0.0
    best_score = (0, 0, 0.0)

    for hg in range(0, 8):
        for ag in range(0, 8):
            p = poisson(lambda_home, hg) * poisson(lambda_away, ag)

            if hg > ag:
                p_home += p
            elif hg == ag:
                p_draw += p
            else:
                p_away += p

            if p > best_score[2]:
                best_score = (hg, ag, p)

    total = p_home + p_draw + p_away

    # Probabilité over 1.5.
    under_15 = poisson(lambda_home, 0) * poisson(lambda_away, 0) + (
        poisson(lambda_home, 1) * poisson(lambda_away, 0)
    ) + (
        poisson(lambda_home, 0) * poisson(lambda_away, 1)
    )
    over_15 = 1 - under_15

    # BTTS.
    btts = (1 - math.exp(-lambda_home)) * (
        1 - math.exp(-lambda_away)
    )

    return {
        "home": p_home / total * 100,
        "draw": p_draw / total * 100,
        "away": p_away / total * 100,
        "score": f"{best_score[0]}-{best_score[1]}",
        "over15": over_15 * 100,
        "btts": btts * 100,
        "lambda_home": lambda_home,
        "lambda_away": lambda_away,
    }


def extract_sportmonks_predictions(fixture):
    predictions = fixture.get("predictions") or []
    out = {}

    for p in predictions:
        typ = p.get("type") or {}
        name = norm(
            typ.get("developer_name", "")
            or typ.get("name", "")
        )
        values = p.get("predictions") or {}

        if "fulltime result probability" in name or "fulltime_result_probability" in name:
            out["1"] = values.get("home")
            out["x"] = values.get("draw")
            out["2"] = values.get("away")

        elif "both teams to score" in name or "btts" in name:
            out["btts_yes"] = values.get("yes")
            out["btts_no"] = values.get("no")

    return out


def extract_xg(fixture):
    xg = fixture.get("xgfixture") or []
    result = {"home": None, "away": None}

    for item in xg:
        loc = item.get("location")
        value = (item.get("data") or {}).get("value")
        if loc in result:
            result[loc] = value

    return result


# ============================================================
# ANALYSE
# ============================================================

async def analyse(home_query, away_query):
    fixture, home, away = await find_fixture(home_query, away_query)

    fixture_id = fixture.get("id")
    details = await fixture_details(fixture_id)

    participants = details.get("participants") or fixture.get("participants") or []

    # Identifiants réels depuis le fixture.
    home_p = next(
        (p for p in participants if (p.get("meta") or {}).get("location") == "home"),
        None,
    )
    away_p = next(
        (p for p in participants if (p.get("meta") or {}).get("location") == "away"),
        None,
    )

    if not home_p or not away_p:
        # fallback avec les équipes résolues
        home_p = next((p for p in participants if p.get("id") == home.get("id")), None)
        away_p = next((p for p in participants if p.get("id") == away.get("id")), None)

    home_id = home_p.get("id") if home_p else home.get("id")
    away_id = away_p.get("id") if away_p else away.get("id")

    home_matches = await recent_matches(home_id, 5)
    away_matches = await recent_matches(away_id, 5)

    model = calculate_probabilities(home_matches, away_matches)

    sm_predictions = extract_sportmonks_predictions(details)
    xg = extract_xg(details)

    home_name = home_p.get("name") if home_p else home.get("name")
    away_name = away_p.get("name") if away_p else away.get("name")

    league = (details.get("league") or {}).get("name", "Compétition inconnue")
    season = (details.get("season") or {}).get("name", "")
    venue = (details.get("venue") or {}).get("name", "")
    kickoff = details.get("starting_at") or fixture.get("starting_at", "")

    hf = form_summary(home_matches)
    af = form_summary(away_matches)

    # Si le module Predictions Sportmonks est disponible, on l'affiche
    # séparément de notre modèle.
    sm_line = ""
    if any(v is not None for v in sm_predictions.values()):
        sm_line = (
            "\n🧠 **Probabilités Sportmonks**\n"
            f"🏠 1 : {sm_predictions.get('1', 'N/D')}%\n"
            f"🤝 X : {sm_predictions.get('x', 'N/D')}%\n"
            f"✈️ 2 : {sm_predictions.get('2', 'N/D')}%\n"
        )

    xg_line = ""
    if xg["home"] is not None or xg["away"] is not None:
        xg_line = (
            f"\n📈 xG disponibles : "
            f"{home_name} {xg['home'] if xg['home'] is not None else 'N/D'} | "
            f"{away_name} {xg['away'] if xg['away'] is not None else 'N/D'}\n"
        )

    favorite = (
        home_name
        if model["home"] >= max(model["draw"], model["away"])
        else away_name
    )

    confidence = max(model["home"], model["draw"], model["away"])

    def form_line(name, matches, f):
        seq = " ".join(m["result"] for m in matches) if matches else "N/D"
        return (
            f"**{name}** : `{seq}`\n"
            f"⚽ {f['gf']} buts marqués | 🛡️ {f['ga']} encaissés | "
            f"V-N-D : {f['v']}-{f['n']}-{f['d']}"
        )

    return (
        f"⚽ **ANALYSE : {home_name} vs {away_name}**\n\n"
        f"🏆 {league} {('• ' + season) if season else ''}\n"
        f"🕒 {kickoff}\n"
        f"🏟️ {venue or 'Stade non indiqué'}\n\n"
        f"📊 **5 derniers matchs**\n"
        f"{form_line(home_name, home_matches, hf)}\n\n"
        f"{form_line(away_name, away_matches, af)}\n\n"
        f"🤖 **Notre modèle**\n"
        f"🏠 {home_name} : **{model['home']:.1f}%**\n"
        f"🤝 Nul : **{model['draw']:.1f}%**\n"
        f"✈️ {away_name} : **{model['away']:.1f}%**\n"
        f"🎯 Score le plus probable : **{model['score']}**\n"
        f"⚽ Over 1,5 : **{model['over15']:.1f}%**\n"
        f"🔄 BTTS : **{model['btts']:.1f}%**\n"
        f"{xg_line}"
        f"{sm_line}\n"
        f"💡 **Lecture :** avantage théorique à **{favorite}**.\n"
        f"📌 Confiance du modèle 1X2 : **{confidence:.1f}%**\n\n"
        f"⚠️ Une probabilité n'est jamais une garantie de pari. "
        f"Les données disponibles dépendent des compétitions couvertes par ton abonnement Sportmonks."
    )


# ============================================================
# TELEGRAM
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Bot football Sportmonks opérationnel.**\n\n"
        "Envoie simplement :\n"
        "`Manchester United - Chelsea`\n\n"
        "ou :\n"
        "`PSG - Marseille`",
        parse_mode="Markdown",
    )


async def analyse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Utilisation : `/analyse Manchester United - Chelsea`",
            parse_mode="Markdown",
        )
        return
    await run_analysis(update, text)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text:
        await run_analysis(update, text)


async def run_analysis(update: Update, text: str):
    await update.message.reply_text(
        "🔎 Recherche des données Sportmonks actuelles..."
    )

    try:
        home, away = parse_match(text)

        if not home or not away:
            raise RuntimeError(
                "Format incorrect. Utilise : `Equipe A - Equipe B`."
            )

        result = await analyse(home, away)
        await update.message.reply_text(
            result,
            parse_mode="Markdown",
        )

    except Exception as e:
        await update.message.reply_text(
            "❌ **Impossible de produire l'analyse.**\n\n"
            f"Détail : {e}\n\n"
            "Vérifie surtout que ta variable Railway "
            "`SPORTMONKS_TOKEN` contient bien ton token Sportmonks "
            "et que la compétition demandée est couverte par ton plan.",
            parse_mode="Markdown",
        )


def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyse", analyse_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    print("Bot Sportmonks démarré.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
