import os
import math
import re
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")  # optional: historical/injury fallback

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN manquant dans Railway Variables.")

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
AF_BASE = "https://v3.football.api-sports.io"


# ------------------------------------------------------------
# ESPN = source principale pour les matchs actuels.
# Elle ne demande pas de clé API.
# ------------------------------------------------------------

COMPETITIONS = {
    "premier league": "eng.1",
    "pl": "eng.1",
    "la liga": "esp.1",
    "liga": "esp.1",
    "serie a": "ita.1",
    "bundesliga": "ger.1",
    "ligue 1": "fra.1",
    "champions league": "uefa.champions",
    "ucl": "uefa.champions",
    "europa league": "uefa.europa",
    "conference league": "uefa.europa.conf",
}

# Quelques noms courants pour améliorer la recherche.
ALIASES = {
    "man united": "Manchester United",
    "man utd": "Manchester United",
    "manchester utd": "Manchester United",
    "man city": "Manchester City",
    "psg": "Paris Saint-Germain",
    "paris sg": "Paris Saint-Germain",
    "barca": "Barcelona",
    "fc barcelona": "Barcelona",
    "atleti": "Atletico Madrid",
    "atletico": "Atletico Madrid",
    "inter": "Inter Milan",
    "inter milan": "Inter Milan",
    "ac milan": "AC Milan",
    "bayern": "Bayern Munich",
    "dortmund": "Borussia Dortmund",
}


def norm(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s)
    return s


def parse_match(text: str):
    # Accepte: Equipe A - Equipe B / Equipe A vs Equipe B
    parts = re.split(r"\s+(?:vs?\.?|contre)\s+|\s+-\s+", text, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None, None
    return parts[0].strip(), parts[1].strip()


async def get_json(url, params=None, headers=None):
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()


async def espn_scoreboard(league, date=None):
    params = {}
    if date:
        params["dates"] = date.replace("-", "")
    return await get_json(f"{ESPN_BASE}/{league}/scoreboard", params=params)


async def espn_search_team(team_name, league):
    data = await espn_scoreboard(league)
    target = norm(ALIASES.get(norm(team_name), team_name))
    candidates = []

    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            for c in comp.get("competitors", []):
                team = c.get("team", {})
                name = team.get("displayName", "")
                short = team.get("shortDisplayName", "")
                if target == norm(name) or target == norm(short):
                    return team.get("id"), name
                if target in norm(name) or norm(name) in target:
                    candidates.append((team.get("id"), name))

    # Si l'équipe n'est pas dans le scoreboard du jour,
    # on essaie la recherche ESPN.
    data = await get_json(
        "https://site.api.espn.com/apis/site/v2/sports/soccer/teams",
        params={"region": "us", "lang": "en", "limit": 1000},
    )
    for t in data.get("sports", []):
        for league_obj in t.get("leagues", []):
            for team in league_obj.get("teams", []):
                team = team.get("team", team)
                name = team.get("displayName", "")
                if target == norm(name) or target in norm(name):
                    return team.get("id"), name

    return candidates[0] if candidates else (None, None)


async def espn_events_for_league(league, days=14):
    now = datetime.now(timezone.utc)
    events = []
    for i in range(-days, days + 1):
        d = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            data = await espn_scoreboard(league, d)
            events.extend(data.get("events", []))
        except Exception:
            pass
    # dédoublonnage
    out = {}
    for e in events:
        out[str(e.get("id"))] = e
    return list(out.values())


def event_teams(event):
    comp = (event.get("competitions") or [{}])[0]
    home = away = None
    for c in comp.get("competitors", []):
        if c.get("homeAway") == "home":
            home = c
        elif c.get("homeAway") == "away":
            away = c
    return home, away


def team_name(c):
    return ((c or {}).get("team") or {}).get("displayName", "?")


def score(c):
    try:
        return int((c or {}).get("score", 0))
    except Exception:
        return 0


async def find_current_fixture(home_query, away_query):
    # Recherche dans les principales compétitions.
    for league in COMPETITIONS.values():
        try:
            events = await espn_events_for_league(league, days=10)
        except Exception:
            continue
        for e in events:
            h, a = event_teams(e)
            if not h or not a:
                continue
            hn, an = norm(team_name(h)), norm(team_name(a))
            hq = norm(ALIASES.get(norm(home_query), home_query))
            aq = norm(ALIASES.get(norm(away_query), away_query))
            if ((hq in hn or hn in hq) and (aq in an or an in aq)):
                return e, league
    return None, None


async def recent_team_matches(league, team_id, limit=5):
    now = datetime.now(timezone.utc)
    all_events = []
    for i in range(0, 90, 7):
        d1 = (now - timedelta(days=i + 6)).strftime("%Y-%m-%d")
        d2 = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            data = await get_json(
                f"{ESPN_BASE}/{league}/scoreboard",
                params={"dates": f"{d1}-{d2}"},
            )
            all_events.extend(data.get("events", []))
        except Exception:
            pass

    seen = {}
    for e in all_events:
        seen[str(e.get("id"))] = e

    matches = []
    for e in seen.values():
        if e.get("status", {}).get("type", {}).get("state") != "post":
            continue
        h, a = event_teams(e)
        if not h or not a:
            continue
        if str(h.get("team", {}).get("id")) != str(team_id) and str(a.get("team", {}).get("id")) != str(team_id):
            continue

        is_home = str(h.get("team", {}).get("id")) == str(team_id)
        gf = score(h if is_home else a)
        ga = score(a if is_home else h)
        matches.append({
            "date": e.get("date", ""),
            "opponent": team_name(a if is_home else h),
            "home": is_home,
            "gf": gf,
            "ga": ga,
            "result": "V" if gf > ga else "N" if gf == ga else "D",
        })

    matches.sort(key=lambda x: x["date"], reverse=True)
    return matches[:limit]


def summarize_form(matches):
    if not matches:
        return {"v": 0, "n": 0, "d": 0, "gf": 0, "ga": 0, "points": 0}
    v = sum(x["result"] == "V" for x in matches)
    n = sum(x["result"] == "N" for x in matches)
    d = sum(x["result"] == "D" for x in matches)
    gf = sum(x["gf"] for x in matches)
    ga = sum(x["ga"] for x in matches)
    return {"v": v, "n": n, "d": d, "gf": gf, "ga": ga, "points": v * 3 + n}


def poisson(lam, k):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def probabilities(home_form, away_form):
    h_att = max(0.25, home_form["gf"] / max(1, len(home_form["matches"])))
    h_def = max(0.25, home_form["ga"] / max(1, len(home_form["matches"])))
    a_att = max(0.25, away_form["gf"] / max(1, len(away_form["matches"])))
    a_def = max(0.25, away_form["ga"] / max(1, len(away_form["matches"])))

    lam_h = max(0.25, 0.58 * h_att + 0.42 * a_def + 0.25)
    lam_a = max(0.20, 0.58 * a_att + 0.42 * h_def)

    ph = pd = pa = 0.0
    best = (0, 0, 0.0)
    for hg in range(0, 7):
        for ag in range(0, 7):
            p = poisson(lam_h, hg) * poisson(lam_a, ag)
            if hg > ag:
                ph += p
            elif hg == ag:
                pd += p
            else:
                pa += p
            if p > best[2]:
                best = (hg, ag, p)

    total = ph + pd + pa
    return {
        "home": ph / total * 100,
        "draw": pd / total * 100,
        "away": pa / total * 100,
        "score": f"{best[0]}-{best[1]}",
        "over15": 100 * (1 - sum(poisson(lam_h, i) for i in range(2)) * sum(poisson(lam_a, i) for i in range(2))),
        "lam_h": lam_h,
        "lam_a": lam_a,
    }


async def analyse(home_query, away_query):
    event, league = await find_current_fixture(home_query, away_query)

    if not event:
        raise RuntimeError(
            "Match actuel introuvable dans les compétitions prises en charge. "
            "Essaie le nom officiel des équipes et indique la compétition."
        )

    h, a = event_teams(event)
    home_id = (h.get("team") or {}).get("id")
    away_id = (a.get("team") or {}).get("id")
    home_name = team_name(h)
    away_name = team_name(a)

    hm = await recent_team_matches(league, home_id, 5)
    am = await recent_team_matches(league, away_id, 5)

    hf = summarize_form(hm)
    af = summarize_form(am)
    hf["matches"] = hm
    af["matches"] = am

    p = probabilities(hf, af)

    kickoff = event.get("date", "date inconnue")
    status = event.get("status", {}).get("type", {}).get("description", "")
    comp_name = event.get("season", {}).get("displayName", "") or league

    def form_text(name, f):
        results = " ".join(x["result"] for x in f["matches"]) if f["matches"] else "N/D"
        return (
            f"**{name}** : {results}\n"
            f"  Buts marqués : {f['gf']} | encaissés : {f['ga']} | "
            f"V-N-D : {f['v']}-{f['n']}-{f['d']}"
        )

    favorite = home_name if p["home"] >= max(p["draw"], p["away"]) else away_name

    return (
        f"⚽ **Analyse actuelle : {home_name} vs {away_name}**\n\n"
        f"🏆 Compétition : {comp_name}\n"
        f"🕒 Coup d'envoi : {kickoff}\n"
        f"📌 Statut : {status}\n\n"
        f"📊 **Forme récente (5 derniers matchs)**\n"
        f"{form_text(home_name, hf)}\n\n"
        f"{form_text(away_name, af)}\n\n"
        f"🤖 **Probabilités du modèle**\n"
        f"🏠 {home_name} : **{p['home']:.1f}%**\n"
        f"🤝 Match nul : **{p['draw']:.1f}%**\n"
        f"✈️ {away_name} : **{p['away']:.1f}%**\n\n"
        f"🎯 Score le plus probable : **{p['score']}**\n"
        f"⚽ Over 1,5 buts : **{p['over15']:.1f}%**\n\n"
        f"💡 **Choix prudent :** {favorite} ou nul (1X/ X2 selon le favori).\n\n"
        f"⚠️ Les blessures/suspensions détaillées et certains H2H ne sont pas "
        f"garantis par cette source gratuite. Ne considère pas les pourcentages "
        f"comme une certitude de pari."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Bot d'analyse football opérationnel.**\n\n"
        "Envoie par exemple :\n"
        "`Manchester United - Chelsea`\n\n"
        "Tu peux aussi préciser la compétition :\n"
        "`Manchester United - Chelsea | Premier League`",
        parse_mode="Markdown",
    )


async def analyse_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "Utilisation : /analyse Manchester United - Chelsea"
        )
        return
    await do_analysis(update, text)


async def do_analysis(update, text):
    await update.message.reply_text("🔎 Recherche des données actuelles...")
    try:
        # Retire une éventuelle mention de compétition pour garder une commande simple.
        text_clean = re.split(r"\s*\|\s*", text, maxsplit=1)[0]
        home, away = parse_match(text_clean)
        if not home or not away:
            raise RuntimeError(
                "Format incorrect. Utilise : `Equipe A - Equipe B`."
            )
        result = await analyse(home, away)
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(
            "❌ **Impossible de produire l'analyse.**\n\n"
            f"Détail : {e}\n\n"
            "💡 Le bot utilise maintenant une source de données actuelles "
            "indépendante de la saison 2026 d'API-Football."
            ,
            parse_mode="Markdown",
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return
    await do_analysis(update, text)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyse", analyse_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Bot démarré.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
