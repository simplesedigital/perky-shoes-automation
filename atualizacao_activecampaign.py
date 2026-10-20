"""
Atualizacao diaria da aba CRM > Active Campaign do dashboard SEO Yamys.
Chamada por atualizacao_dashboard_seo.py (etapa update_activecampaign).

Fontes:
  - API v3 do ActiveCampaign: campanhas (envios, aberturas, cliques, bounces,
    descadastros), contagem de contatos por situacao e por mes de cadastro.
  - GA4: sessoes/pedidos/receita das sessoes com origem ActiveCampaign, ligadas a
    campanha pelo nome da campanha no link (utm_campaign) e pela data de envio.

Credenciais: AC_API_URL e AC_API_KEY (segredos do GitHub) ou, rodando local,
C:\\Users\\adsom\\.claude\\yamys_ac_config.json com {"api_url": ..., "api_key": ...}.
"""
import json
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AC_CONFIG_PATH = r"C:\Users\adsom\.claude\yamys_ac_config.json"
AC_START_DATE = "2025-03-01"
GA4_START_DATE = "2025-03-18"
BRT = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------- configuracao / HTTP
def get_ac_config():
    url, key = os.environ.get("AC_API_URL"), os.environ.get("AC_API_KEY")
    if url and key:
        return {"api_url": url.rstrip("/"), "api_key": key}
    if os.path.exists(AC_CONFIG_PATH):
        with open(AC_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return {"api_url": cfg["api_url"].rstrip("/"), "api_key": cfg["api_key"]}
    return None


def ac_get(cfg, path, params=None):
    url = f"{cfg['api_url']}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Api-Token": cfg["api_key"], "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_campaigns(cfg, get=ac_get):
    out, offset = [], 0
    while True:
        data = get(cfg, "/api/3/campaigns", {"limit": 100, "offset": offset, "orders[sdate]": "DESC"})
        batch = data.get("campaigns", [])
        out.extend(batch)
        total = int(data.get("meta", {}).get("total", 0))
        offset += 100
        if not batch or offset >= total:
            return out


def count_contacts(cfg, get=ac_get, **filters):
    params = {"limit": 1}
    params.update(filters)
    data = get(cfg, "/api/3/contacts", params)
    return int(data.get("meta", {}).get("total", 0))


def fetch_ga4_activecampaign(token, property_id, start_date, end_date, http_json):
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    rows, offset = [], 0
    while True:
        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "date"}, {"name": "sessionCampaignName"}],
            "metrics": [{"name": "sessions"}, {"name": "transactions"}, {"name": "purchaseRevenue"}],
            "dimensionFilter": {"filter": {"fieldName": "sessionSource", "stringFilter": {
                "matchType": "EXACT", "value": "ActiveCampaign", "caseSensitive": False}}},
            "limit": 100000, "offset": offset,
        }
        status, resp = http_json(url, "POST", {"Authorization": f"Bearer {token}"}, body=body)
        if status != 200:
            raise RuntimeError(f"GA4 ActiveCampaign runReport falhou ({status}): {resp}")
        batch = resp.get("rows", [])
        for x in batch:
            d, name = x["dimensionValues"][0]["value"], x["dimensionValues"][1]["value"]
            m = x["metricValues"]
            rows.append([f"{d[:4]}-{d[4:6]}-{d[6:]}", name, int(m[0]["value"]), int(m[1]["value"]), round(float(m[2]["value"]), 2)])
        offset += 100000
        if len(batch) < 100000:
            return rows


# ---------------------------------------------------------------- logica pura (testavel offline)
def norm(name):
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\((copiar|copy)\)", "", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def brt_date(sdate):
    return datetime.fromisoformat(sdate).astimezone(BRT).strftime("%Y-%m-%d")


def build_manual(campaigns):
    """Campanhas pontuais concluidas desde AC_START_DATE (fora de automacoes)."""
    out = []
    for c in campaigns:
        if c.get("seriesid", "0") != "0" or c.get("status") != "5" or not c.get("sdate"):
            continue
        d = brt_date(c["sdate"])
        if d < AC_START_DATE:
            continue
        out.append({
            "id": int(c["id"]), "name": c["name"].strip(), "date": d, "sent": int(c["send_amt"]),
            "opens": int(c["uniqueopens"]), "vopens": int(c.get("verified_unique_opens") or 0),
            "clicks": int(c["uniquelinkclicks"]), "unsubs": int(c["unsubscribes"]),
            "bounces": int(c["hardbounces"]) + int(c["softbounces"]),
        })
    out.sort(key=lambda m: (m["date"], m["id"]))
    return out


def automation_campaigns(campaigns):
    """Emails de fluxos automaticos que ja enviaram algo: {id: nome}."""
    return {int(c["id"]): c["name"].strip() for c in campaigns
            if c.get("seriesid", "0") != "0" and int(c.get("send_amt") or 0) > 0}


def auto_snapshot(campaigns):
    snap = {}
    for c in campaigns:
        if c.get("seriesid", "0") != "0" and int(c.get("send_amt") or 0) > 0:
            snap[str(int(c["id"]))] = [int(c["send_amt"]), int(c["uniqueopens"]), int(c["uniquelinkclicks"]),
                                       int(c["hardbounces"]) + int(c["softbounces"]), int(c["unsubscribes"])]
    return snap


def join_conversions(ga_rows, manual, auto_names):
    """Liga cada linha (data, nome da campanha) do GA4 a uma campanha manual
    (a de mesmo nome enviada mais recentemente ate aquele dia) ou a um e-mail de fluxo."""
    auto_by_norm = {norm(n): i for i, n in auto_names.items()}
    manual_by_norm = {}
    for m in manual:
        manual_by_norm.setdefault(norm(m["name"]), []).append(m)
    conv = {m["id"]: [0, 0, 0.0] for m in manual}
    auto_conv = {}
    unmatched = [0, 0, 0.0]
    for d, name, sessions, orders, revenue in ga_rows:
        k = norm(name)
        if k in auto_by_norm:
            x = auto_conv.setdefault((d[:7], auto_by_norm[k]), [0, 0, 0.0])
        else:
            cands = [m for m in manual_by_norm.get(k, []) if m["date"] <= d]
            if cands:
                x = conv[max(cands, key=lambda m: (m["date"], m["id"]))["id"]]
            else:
                x = unmatched
        x[0] += sessions
        x[1] += orders
        x[2] += revenue
    return conv, auto_conv, unmatched


def manual_rows(manual, conv):
    return [[m["id"], m["name"], m["date"], m["sent"], m["opens"], m["vopens"], m["clicks"], m["unsubs"], m["bounces"],
             conv[m["id"]][0], conv[m["id"]][1], round(conv[m["id"]][2], 2)] for m in manual]


def update_auto_monthly(existing, prev_snap, cur_snap, activity_ym, auto_conv):
    """existing: linhas [ym, id, enviados, aberturas, cliques, bounces, descad., sessoes, pedidos, receita].
    O ActiveCampaign so informa os fluxos de forma acumulada; a diferenca contra o snapshot
    anterior entra no mes de 'activity_ym' (mes do dia anterior ao da execucao)."""
    rows = {(r[0], r[1]): list(r) for r in existing}
    for sid, cur in cur_snap.items():
        prev = prev_snap.get(sid)
        if prev is None:
            continue  # primeiro snapshot desse e-mail: so serve de base
        delta = [max(0, c - p) for c, p in zip(cur, prev)]
        if any(delta):
            r = rows.setdefault((activity_ym, int(sid)), [activity_ym, int(sid), 0, 0, 0, 0, 0, 0, 0, 0.0])
            for i in range(5):
                r[2 + i] += delta[i]
    for r in rows.values():  # conversoes sempre recalculadas do GA4
        r[7], r[8], r[9] = 0, 0, 0.0
    for (ym, sid), (s, o, rev) in auto_conv.items():
        r = rows.setdefault((ym, sid), [ym, sid, 0, 0, 0, 0, 0, 0, 0, 0.0])
        r[7], r[8], r[9] = s, o, round(rev, 2)
    return [rows[k] for k in sorted(rows)]


def update_base_monthly(existing, manual, created_by_month):
    created = {r[0]: r[1] for r in existing}
    created.update(created_by_month)
    reach = {}
    for m in manual:
        reach[m["date"][:7]] = m["sent"]
    return [[ym, created[ym], reach.get(ym)] for ym in sorted(created)]


# ---------------------------------------------------------------- leitura/escrita no HTML
def read_const(content, name):
    marker = f"const {name} = "
    start = content.index(marker) + len(marker)
    value, end = json.JSONDecoder().raw_decode(content, start)
    return value, start, end


def write_const(content, name, value):
    _, start, end = read_const(content, name)
    return content[:start] + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + content[end:]


def month_bounds(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = f"{y + (m == 12)}-{(m % 12) + 1:02d}-01"
    return f"{ym}-01", nxt


def update_activecampaign(content, google_token, yesterday, property_id, http_json, cfg=None, get=ac_get):
    cfg = cfg or get_ac_config()
    if not cfg:
        raise RuntimeError("AC_API_URL/AC_API_KEY nao configurados")
    campaigns = fetch_campaigns(cfg, get)
    manual = build_manual(campaigns)
    auto_names = automation_campaigns(campaigns)
    ga = fetch_ga4_activecampaign(google_token, property_id, GA4_START_DATE, yesterday, http_json)
    conv, auto_conv, unmatched = join_conversions(ga, manual, auto_names)

    activity_ym = yesterday[:7]
    prev_snap, _, _ = read_const(content, "acAutoSnap")
    cur_snap = auto_snapshot(campaigns)
    auto_rows, _, _ = read_const(content, "acAutoMonthlyRaw")
    auto_rows = update_auto_monthly(auto_rows, prev_snap, cur_snap, activity_ym, auto_conv)

    # base: situacao atual + novos contatos dos 2 ultimos meses
    today = (datetime.strptime(yesterday, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    now = {
        "date": today,
        "active": count_contacts(cfg, get, status=1),
        "unsubscribed": count_contacts(cfg, get, status=2),
        "bounced": count_contacts(cfg, get, status=3),
        "unconfirmed": count_contacts(cfg, get, status=0),
    }
    prev_month = (datetime.strptime(activity_ym + "-01", "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m")
    created = {}
    for ym in (prev_month, activity_ym):
        after, before = month_bounds(ym)
        created[ym] = count_contacts(cfg, get, **{"filters[created_after]": after, "filters[created_before]": before})
    base_rows, _, _ = read_const(content, "acBaseMonthlyRaw")
    base_rows = update_base_monthly(base_rows, manual, created)

    daily, _, _ = read_const(content, "acBaseDailyRaw")
    daily = [r for r in daily if r[0] != today] + [[today, now["active"], now["unsubscribed"], now["bounced"]]]

    content = write_const(content, "acManualRaw", manual_rows(manual, conv))
    content = write_const(content, "acAutoMonthlyRaw", auto_rows)
    content = write_const(content, "acAutoSnap", cur_snap)
    content = write_const(content, "acAutoNameRaw", {str(i): n for i, n in auto_names.items()})
    content = write_const(content, "acBaseMonthlyRaw", base_rows)
    content = write_const(content, "acBaseDailyRaw", daily)
    content = write_const(content, "acBaseNow", now)
    content = write_const(content, "AC_CONV_LAST", yesterday)
    content = write_const(content, "acUnmatched", [unmatched[0], unmatched[1], round(unmatched[2], 2)])
    return content, {"campanhas": len(manual), "fluxos": len(auto_names), "ativos": now["active"]}
