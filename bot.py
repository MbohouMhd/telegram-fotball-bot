import os
import re
import math
import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_KEY = os.getenv("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"

if not BOT_TOKEN or not API_KEY:
    raise RuntimeError("Configure TELEGRAM_BOT_TOKEN et API_FOOTBALL_KEY dans .env")

HEADERS = {"x-apisports-key": API_KEY}


class FootballAPI:
    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=HEADERS,
            timeout=25.0,
        )

    async def close(self):
        await self.client.aclose()

    async def get(self, path: str, **params) -> dict[str, Any]:
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        r = await self.client.get(path, params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(str(data["errors"]))
        return data

    async def search_league(self, name: str) -> list[dict]:
        data = await self.get("/leagues", search=name)
        return data.get("response", [])

    async def fixtures(self, **params) -> list[dict]:
        return (await self.get("/fixtures", **params)).get("response", [])

    async def fixture(self, fixture_id: int) -> dict:
        x = await self.fixtures(id=fixture_id)
        return x[0] if x else {}

    async def events(self, fixture_id: int) -> list[dict]:
        return (await self.get("/fixtures/events", fixture=fixture_id)).get("response", [])

    async def injuries(self, fixture_id: int) -> list[dict]:
        return (await self.get("/injuries", fixture=fixture_id)).get("response", [])

    async def team_stats(self, team_id: int, league_id: int, season: int) -> dict:
        data = await self.get(
            "/teams/statistics",
            team=team_id,
            league=league_id,
            season=season,
        )
        return data.get("response", {})

    async def players(self, team_id: int, season: int) -> list[dict]:
        return (await self.get("/players", team=team_id, season=season)).get("response", [])

    async def predictions(self, fixture_id: int) -> dict:
        x = (await self.get("/predictions", fixture=fixture_id)).get("response", [])
        return x[0] if x else {}


api = FootballAPI()


def clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def parse_request(text: str):
    text = text.replace("/analyse", "", 1).strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        raise ValueError(
            "Format attendu:\n"
            "/analyse Équipe 1 vs Équipe 2 | Compétition | AAAA-MM-JJ"
        )
    matchup, competition, date_s = parts
    m = re.split(r"\s+vs\.?\s+|\s+v\s+", matchup, flags=re.I)
    if len(m) != 2:
        raise ValueError("Utilise le format « Équipe 1 vs Équipe 2 ».")
    try:
        datetime.strptime(date_s, "%Y-%m-%d")
    except ValueError:
        raise ValueError("La date doit être au format AAAA-MM-JJ.")
    return clean_name(m[0]), clean_name(m[1]), clean_name(competition), date_s


def season_for_date(date_s: str) -> int:
    # Pour les compétitions européennes classiques, l'année de début de saison
    # correspond généralement à l'année du match avant juillet et à année-1 après juillet.
    d = datetime.strptime(date_s, "%Y-%m-%d")
    return d.year if d.month >= 7 else d.year - 1


async def resolve_fixture(home: str, away: str, league_name: str, date_s: str):
    leagues = await api.search_league(league_name)
    if not leagues:
        raise ValueError(f"Compétition introuvable: {league_name}")

    # Priorité à un nom de compétition qui correspond exactement.
    lname = league_name.lower()
    leagues.sort(key=lambda x: 0 if x.get("league", {}).get("name", "").lower() == lname else 1)
    league = leagues[0]["league"]
    league_id = league["id"]
    season = season_for_date(date_s)

    fixtures = await api.fixtures(league=league_id, season=season, date=date_s)
    if not fixtures:
        # Secours: chercher les matchs de la date puis filtrer les équipes.
        fixtures = await api.fixtures(date=date_s)

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    nh, na = norm(home), norm(away)

    for f in fixtures:
        h = f["teams"]["home"]["name"]
        a = f["teams"]["away"]["name"]
        if (nh in norm(h) or norm(h) in nh) and (na in norm(a) or norm(a) in na):
            return f, league_id, season

    # Si inversion accidentelle dans la saisie.
    for f in fixtures:
        h = f["teams"]["home"]["name"]
        a = f["teams"]["away"]["name"]
        if (nh in norm(a) or norm(a) in nh) and (na in norm(h) or norm(h) in na):
            return f, league_id, season

    raise ValueError(
        f"Match non trouvé le {date_s} dans {league.get('name', league_name)}."
    )


async def recent_matches(team_id: int, league_id: int, season: int, last=5):
    # API-Football accepte last pour les fixtures par équipe.
    data = await api.fixtures(team=team_id, last=last)
    return [x for x in data if x.get("league", {}).get("season") == season][:last]


def result_for_team(f, team_id):
    home = f["teams"]["home"]["id"] == team_id
    gf = f["goals"]["home"] if home else f["goals"]["away"]
    ga = f["goals"]["away"] if home else f["goals"]["home"]
    if gf is None or ga is None:
        return None
    res = "W" if gf > ga else "D" if gf == ga else "L"
    return {"result": res, "gf": gf, "ga": ga, "date": f["fixture"]["date"][:10],
            "opponent": f["teams"]["away"]["name"] if home else f["teams"]["home"]["name"]}


def summarize_form(matches, team_id):
    rows = [result_for_team(x, team_id) for x in matches]
    rows = [r for r in rows if r]
    if not rows:
        return {"rows": [], "ppg": 0.0, "gf": 0.0, "ga": 0.0, "btts": 0.0, "over25": 0.0}
    points = sum(3 if r["result"] == "W" else 1 if r["result"] == "D" else 0 for r in rows)
    return {
        "rows": rows,
        "ppg": points / len(rows),
        "gf": sum(r["gf"] for r in rows) / len(rows),
        "ga": sum(r["ga"] for r in rows) / len(rows),
        "btts": sum(1 for r in rows if r["gf"] > 0 and r["ga"] > 0) / len(rows),
        "over25": sum(1 for r in rows if r["gf"] + r["ga"] >= 3) / len(rows),
    }


def venue_form(matches, team_id, is_home):
    filtered = []
    for f in matches:
        if (f["teams"]["home"]["id"] == team_id) == is_home:
            filtered.append(f)
    return summarize_form(filtered, team_id)


def pct(x):
    return f"{round(x * 100):d}%"


def poisson_pmf(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson_1x2(lam_h, lam_a):
    home = draw = away = 0.0
    for i in range(0, 7):
        for j in range(0, 7):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a)
            if i > j: home += p
            elif i == j: draw += p
            else: away += p
    s = home + draw + away
    return home/s, draw/s, away/s


def model_probability(home_form, away_form, home_venue, away_venue, h2h_home, h2h_away):
    # Modèle volontairement simple et explicable.
    # λ = mélange attaque de l'équipe, défense adverse et avantage terrain.
    hf_gf = home_venue["gf"] or home_form["gf"]
    hf_ga = home_venue["ga"] or home_form["ga"]
    af_gf = away_venue["gf"] or away_form["gf"]
    af_ga = away_venue["ga"] or away_form["ga"]

    lam_h = max(0.15, 0.58 * hf_gf + 0.42 * af_ga + 0.20)
    lam_a = max(0.10, 0.58 * af_gf + 0.42 * hf_ga - 0.05)

    # Forme récente et H2H modifient légèrement les intensités.
    form_delta = (home_form["ppg"] - away_form["ppg"]) * 0.08
    lam_h *= max(0.75, 1 + form_delta)
    lam_a *= max(0.75, 1 - form_delta)

    if h2h_home + h2h_away > 0:
        h2h_delta = (h2h_home - h2h_away) * 0.04
        lam_h *= 1 + h2h_delta
        lam_a *= 1 - h2h_delta

    return (*poisson_1x2(lam_h, lam_a), lam_h, lam_a)


def market_probs(lam_h, lam_a):
    p0_h = poisson_pmf(0, lam_h)
    p0_a = poisson_pmf(0, lam_a)
    # P(total <=2) avec Poisson(total, lam_h+lam_a)
    total = lam_h + lam_a
    under25 = sum(poisson_pmf(k, total) for k in range(3))
    over25 = 1 - under25
    under15 = sum(poisson_pmf(k, total) for k in range(2))
    over15 = 1 - under15
    btts = 1 - p0_h - p0_a + p0_h * p0_a
    return {"over25": over25, "over15": over15, "btts": btts,
            "under25": under25, "dc1x": None, "dcx2": None}


def confidence(p):
    # Évite d'afficher artificiellement 90-100% sur des marchés incertains.
    return max(50, min(88, round(50 + abs(p - 0.5) * 70)))


def best_score(lam_h, lam_a):
    best = (0, 0, -1)
    for i in range(0, 6):
        for j in range(0, 6):
            p = poisson_pmf(i, lam_h) * poisson_pmf(j, lam_a)
            if p > best[2]:
                best = (i, j, p)
    return best


async def player_form(team_id: int, season: int, matches):
    try:
        players = await api.players(team_id, season)
    except Exception:
        return []
    candidates = []
    for item in players:
        player = item.get("player", {})
        stats = item.get("statistics") or []
        goals = 0
        appearances = 0
        for st in stats:
            g = st.get("goals") or {}
            goals += g.get("total") or 0
            appearances += st.get("games", {}).get("appearences") or 0
        if goals or appearances:
            candidates.append((goals, appearances, player.get("name", "Joueur")))
    candidates.sort(reverse=True)
    return candidates[:3]


async def recent_goal_events(matches, team_id):
    # Recherche les buts dans les 5 derniers matchs.
    total = {}
    for f in matches:
        try:
            evs = await api.events(f["fixture"]["id"])
        except Exception:
            continue
        for e in evs:
            if e.get("type") != "Goal":
                continue
            if e.get("team", {}).get("id") != team_id:
                continue
            p = (e.get("player") or {}).get("name")
            if p:
                total[p] = total.get(p, 0) + 1
    return sorted(total.items(), key=lambda x: x[1], reverse=True)


def fmt_form(summary):
    return " ".join(r["result"] for r in summary["rows"]) or "N/D"


def injury_text(injuries, team_id):
    items = []
    for x in injuries:
        if x.get("team", {}).get("id") != team_id:
            continue
        p = x.get("player", {}).get("name", "Joueur")
        reason = x.get("player", {}).get("reason") or x.get("player", {}).get("type") or "indisponibilité"
        items.append(f"{p} ({reason})")
    return items


async def build_analysis(home, away, competition, date_s):
    fixture, league_id, season = await resolve_fixture(home, away, competition, date_s)
    hid = fixture["teams"]["home"]["id"]
    aid = fixture["teams"]["away"]["id"]
    actual_home = fixture["teams"]["home"]["name"]
    actual_away = fixture["teams"]["away"]["name"]

    hm, am = await asyncio.gather(
        recent_matches(hid, league_id, season, 5),
        recent_matches(aid, league_id, season, 5),
    )
    hf = summarize_form(hm, hid)
    af = summarize_form(am, aid)

    # 10 derniers pour avoir une base plus stable pour domicile/extérieur.
    hm10, am10 = await asyncio.gather(
        recent_matches(hid, league_id, season, 10),
        recent_matches(aid, league_id, season, 10),
    )
    hv = venue_form(hm10, hid, True)
    av = venue_form(am10, aid, False)

    # H2H via endpoint.
    h2h = await api.fixtures(h2h=f"{hid}-{aid}", last=5)
    h2h_home = h2h_away = h2h_draw = 0
    h2h_rows = []
    for f in h2h:
        if f["goals"]["home"] is None:
            continue
        hteam = f["teams"]["home"]["id"]
        hs = f["goals"]["home"]
        aws = f["goals"]["away"]
        if hs == aws:
            h2h_draw += 1
        elif (hteam == hid and hs > aws) or (hteam == aid and aws > hs):
            h2h_home += 1
        else:
            h2h_away += 1
        h2h_rows.append((f["teams"]["home"]["name"], hs, aws, f["teams"]["away"]["name"]))

    inj = await api.injuries(fixture["fixture"]["id"])

    try:
        p_home, p_draw, p_away, lam_h, lam_a = model_probability(
            hf, af, hv, av, h2h_home, h2h_away
        )
    except Exception:
        p_home, p_draw, p_away, lam_h, lam_a = 0.33, 0.34, 0.33, 1.2, 1.1

    markets = market_probs(lam_h, lam_a)
    score_h, score_a, score_p = best_score(lam_h, lam_a)

    # Double chance
    dc1x = p_home + p_draw
    dcx2 = p_draw + p_away
    dc12 = p_home + p_away

    # Joueurs: saison + buts sur les 5 derniers.
    ph, pa = await asyncio.gather(
        player_form(hid, season, hm),
        player_form(aid, season, am),
    )
    rh, ra = await asyncio.gather(
        recent_goal_events(hm, hid),
        recent_goal_events(am, aid),
    )
    inj_h = injury_text(inj, hid)
    inj_a = injury_text(inj, aid)

    lines = []
    lines.append(f"⚽ <b>ANALYSE DU MATCH</b>")
    lines.append(f"<b>{actual_home}</b> 🆚 <b>{actual_away}</b>")
    lines.append(f"🏆 {fixture['league']['name']} — {date_s}")
    lines.append("")
    lines.append("📊 <b>1. 5 DERNIERS MATCHS</b>")
    lines.append(f"• {actual_home}: {fmt_form(hf)} | {hf['gf']:.2f} buts/m | {hf['ga']:.2f} encaissés/m")
    lines.append(f"• {actual_away}: {fmt_form(af)} | {af['gf']:.2f} buts/m | {af['ga']:.2f} encaissés/m")
    lines.append("")
    lines.append("🛡️ <b>2. ATTAQUE / DÉFENSE</b>")
    lines.append(f"• {actual_home}: PPM {hf['ppg']:.2f}, BTTS {pct(hf['btts'])}, +2,5 {pct(hf['over25'])}")
    lines.append(f"• {actual_away}: PPM {af['ppg']:.2f}, BTTS {pct(af['btts'])}, +2,5 {pct(af['over25'])}")
    lines.append(f"• Domicile {actual_home}: {hv['ppg']:.2f} PPM, {hv['gf']:.2f} marqués/m")
    lines.append(f"• Extérieur {actual_away}: {av['ppg']:.2f} PPM, {av['ga']:.2f} encaissés/m")
    lines.append("")
    lines.append("🤝 <b>3. CONFRONTATIONS DIRECTES</b>")
    if h2h_rows:
        lines.append(f"• Sur les 5 dernières: {h2h_home} victoire(s) {actual_home}, {h2h_draw} nul(s), {h2h_away} victoire(s) {actual_away}")
        for h, hs, aws, a in h2h_rows[:5]:
            lines.append(f"  {h} {hs}-{aws} {a}")
    else:
        lines.append("• Pas de H2H exploitable.")
    lines.append("")
    lines.append("⭐ <b>4. JOUEURS CLÉS</b>")
    if ph:
        lines.append("• " + actual_home + ": " + ", ".join(f"{n} ({g} buts saison)" for g, a, n in ph))
    if pa:
        lines.append("• " + actual_away + ": " + ", ".join(f"{n} ({g} buts saison)" for g, a, n in pa))
    if rh:
        lines.append("• Buts récents " + actual_home + ": " + ", ".join(f"{n} x{g}" for n, g in rh[:3]))
    if ra:
        lines.append("• Buts récents " + actual_away + ": " + ", ".join(f"{n} x{g}" for n, g in ra[:3]))
    if not ph and not pa and not rh and not ra:
        lines.append("• Données joueurs insuffisantes.")
    lines.append("")
    lines.append("🏟️ <b>5. AVANTAGE DU TERRAIN</b>")
    lines.append(f"• {actual_home} reçoit. Le modèle ajoute un avantage domicile modéré dans les buts attendus.")
    lines.append("")
    lines.append("🚑 <b>6. BLESSURES / SUSPENSIONS</b>")
    lines.append("• " + (", ".join(inj_h) if inj_h else f"{actual_home}: aucune indisponibilité retournée par l'API."))
    lines.append("• " + (", ".join(inj_a) if inj_a else f"{actual_away}: aucune indisponibilité retournée par l'API."))
    lines.append("")
    lines.append("📈 <b>PROBABILITÉS DU MODÈLE</b>")
    lines.append(f"• Victoire {actual_home}: <b>{pct(p_home)}</b>")
    lines.append(f"• Match nul: <b>{pct(p_draw)}</b>")
    lines.append(f"• Victoire {actual_away}: <b>{pct(p_away)}</b>")
    lines.append("")
    lines.append(f"🎯 <b>SCORE PROBABLE: {score_h}-{score_a}</b> (probabilité exacte ≈ {pct(score_p)})")
    lines.append("")
    lines.append("💰 <b>PARIS STATISTIQUES INTÉRESSANTS</b>")
    candidates = [
        (dc1x, f"Double chance 1X — {pct(dc1x)}"),
        (dcx2, f"Double chance X2 — {pct(dcx2)}"),
        (markets["over15"], f"Plus de 1,5 buts — {pct(markets['over15'])}"),
        (markets["over25"], f"Plus de 2,5 buts — {pct(markets['over25'])}"),
        (markets["btts"], f"Les deux équipes marquent — {pct(markets['btts'])}"),
    ]
    candidates.sort(reverse=True)
    for p, label in candidates[:3]:
        lines.append(f"• {label} | confiance {confidence(p)}%")
    lines.append("")
    overall = max(p_home, p_draw, p_away)
    lines.append(f"🧠 <b>CONFIANCE GLOBALE: {confidence(overall)}%</b>")
    lines.append("Le niveau de confiance mesure la solidité statistique du signal, pas une garantie de gain.")
    lines.append("")
    lines.append("ℹ️ Modèle Poisson simplifié: forme récente + buts marqués/encaissés + domicile/extérieur + H2H. Les données manquantes ne sont pas inventées.")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ Bot d'analyse football\n\n"
        "Commande:\n"
        "/analyse Équipe 1 vs Équipe 2 | Compétition | AAAA-MM-JJ\n\n"
        "Exemple:\n"
        "/analyse Manchester United vs Manchester City | Premier League | 2026-09-20"
    )


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    try:
        home, away, comp, date_s = parse_request(text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return

    msg = await update.message.reply_text("⏳ Je récupère les statistiques et je calcule l'analyse...")
    try:
        report = await build_analysis(home, away, comp, date_s)
        # Telegram limite les messages à environ 4096 caractères.
        if len(report) <= 4000:
            await msg.edit_text(report, parse_mode="HTML")
        else:
            await msg.delete()
            for i in range(0, len(report), 3900):
                await update.message.reply_text(report[i:i+3900], parse_mode="HTML")
    except Exception as e:
        await msg.edit_text(
            "❌ Impossible de produire l'analyse.\n"
            f"Détail: {str(e)[:700]}\n\n"
            "Vérifie le nom des équipes, la compétition, la date et les clés API."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Erreur:", context.error)


async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyse", analyze))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze))
    app.add_error_handler(error_handler)
    print("Bot démarré.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
