"""
Atualização diária do Dashboard Orgânico - Yamys Baby.
Busca dados de GA4 + Search Console + pedidos com Order Bump (Vnda) e
publica direto no GitHub Pages (le o index.html publicado, atualiza so
os arrays de dados e as datas, e grava de volta - nao depende de nenhum
arquivo local).
Não usa nenhuma IA em tempo de execução - é um script determinístico puro.
Roda via GitHub Actions (workflow "Atualizacao SEO Yamys").
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

CONFIG_PATH = r"C:\Users\adsom\.claude\yamys_seo_dashboard_config.json"
VNDA_CONFIG_PATH = r"C:\Users\adsom\.claude\yamys_vnda_config.json"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs_rotina")

GITHUB_OWNER = "simplesedigital"
GITHUB_REPO = "yamys-seo-dashboard"
GITHUB_PATH = "index.html"
GA4_PROPERTY_ID = "482634584"
GSC_SITE_URL = "sc-domain:yamysbaby.com"
ARTIFACT_START_DATE = "2025-03-18"
GSC_START_DATE = "2025-03-01"
VNDA_API_BASE = "https://api.vnda.com.br"
VNDA_SHOP_HOST = "www.yamysbaby.com"

log_lines = []


def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    log_lines.append(line)


def save_log():
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(LOG_DIR, f"seo-dashboard-{ts}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"Log salvo em: {path}")


def load_local_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_github_token():
    token = os.environ.get("GITHUB_PAGES_TOKEN")
    if token:
        return token
    return load_local_config()["github_token"]


def get_google_sa_path():
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if path:
        return path
    return load_local_config()["google_service_account_path"]


def get_vnda_token():
    token = os.environ.get("VNDA_API_TOKEN")
    if token:
        return token
    with open(VNDA_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)["api_token"]


def http_json(url, method="GET", headers=None, body=None):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}


def get_google_token(sa_path):
    from google.oauth2 import service_account
    import google.auth.transport.requests

    creds = service_account.Credentials.from_service_account_file(
        sa_path,
        scopes=[
            "https://www.googleapis.com/auth/analytics.readonly",
            "https://www.googleapis.com/auth/webmasters.readonly",
        ],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def fetch_ga4_daily(token, property_id, start_date, end_date):
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "date"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "totalRevenue"},
            {"name": "transactions"},
        ],
        "dimensionFilter": {
            "filter": {
                "fieldName": "sessionMedium",
                "stringFilter": {
                    "matchType": "CONTAINS",
                    "value": "organic",
                    "caseSensitive": False,
                },
            }
        },
        "limit": 600,
    }
    status, resp = http_json(
        url, "POST", {"Authorization": f"Bearer {token}"}, body=body
    )
    if status != 200:
        raise RuntimeError(f"GA4 runReport falhou ({status}): {resp}")
    out = []
    for row in resp.get("rows", []):
        d = row["dimensionValues"][0]["value"]
        date_fmt = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        sessions = int(row["metricValues"][0]["value"] or 0)
        revenue = round(float(row["metricValues"][1]["value"] or 0), 2)
        transactions = int(row["metricValues"][2]["value"] or 0)
        out.append(
            {
                "date": date_fmt,
                "sessions": sessions,
                "revenue": revenue,
                "transactions": transactions,
            }
        )
    out.sort(key=lambda r: r["date"])
    return out


def gsc_query(token, site_url, start_date, end_date, dimensions, row_limit=25000):
    url = f"https://www.googleapis.com/webmasters/v3/sites/{urllib.parse.quote(site_url, safe='')}/searchAnalytics/query"
    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "dataState": "all",
        "rowLimit": row_limit,
    }
    status, resp = http_json(
        url, "POST", {"Authorization": f"Bearer {token}"}, body=body
    )
    if status != 200:
        raise RuntimeError(f"GSC query falhou ({status}): {resp}")
    return resp.get("rows", [])


def fetch_gsc_daily(token, site_url, start_date, end_date):
    rows = gsc_query(token, site_url, start_date, end_date, ["date"])
    out = []
    for r in rows:
        out.append(
            {
                "date": r["keys"][0],
                "clicks": int(r["clicks"]),
                "impressions": int(r["impressions"]),
                "ctr": round(r["ctr"], 4),
                "position": round(r["position"], 2),
            }
        )
    out.sort(key=lambda r: r["date"])
    return out


def vnda_get(path, token, params=None):
    url = f"{VNDA_API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}", "X-Shop-Host": VNDA_SHOP_HOST}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        pagination = resp.headers.get("X-Pagination")
        data = json.loads(resp.read().decode("utf-8"))
    return data, (json.loads(pagination) if pagination else None)


def fetch_orders_in_range(token, start_date, end_date):
    per_page = 100
    _, pagination = vnda_get("/api/v2/orders", token, {"page": 1, "per_page": per_page})
    page = pagination["total_pages"]

    collected = []
    while page >= 1:
        orders, _ = vnda_get("/api/v2/orders", token, {"page": page, "per_page": per_page})
        if not orders:
            break
        dates_on_page = [(o.get("received_at") or "")[:10] for o in orders]
        for o, d in zip(orders, dates_on_page):
            if d and start_date <= d <= end_date:
                collected.append(o)
        oldest_on_page = min(d for d in dates_on_page if d)
        if oldest_on_page < start_date:
            break
        page -= 1
    return collected


def compute_order_bump_updates(orders):
    daily = {}
    items = []
    for o in orders:
        date = (o.get("received_at") or "")[:10]
        if not date:
            continue
        day = daily.setdefault(date, {
            "date": date, "totalOrders": 0, "bumpOrders": 0,
            "bumpOrdersRevenue": 0.0, "bumpItemValue": 0.0,
        })
        day["totalOrders"] += 1
        bump_items = [it for it in o.get("items", []) if (it.get("extra") or {}).get("orderbump_code")]
        if bump_items:
            day["bumpOrders"] += 1
            day["bumpOrdersRevenue"] += o.get("total") or 0
            for it in bump_items:
                day["bumpItemValue"] += it.get("price") or 0
                items.append({
                    "date": date,
                    "sku": it.get("sku"),
                    "name": it.get("product_name"),
                    "qty": it.get("quantity"),
                    "price": it.get("price"),
                    "originalPrice": it.get("original_price"),
                    "orderCode": o.get("code"),
                    "orderTotal": o.get("total"),
                })
    daily_list = sorted(daily.values(), key=lambda d: d["date"])
    for d in daily_list:
        d["bumpOrdersRevenue"] = round(d["bumpOrdersRevenue"], 2)
        d["bumpItemValue"] = round(d["bumpItemValue"], 2)
    items.sort(key=lambda i: i["date"])
    return daily_list, items


def parse_array(content, var_name):
    start, end = find_array_span(content, var_name)
    return json.loads(content[start + len(f"const {var_name} = "):end])


def update_order_bump(content, yesterday):
    existing_daily = parse_array(content, "orderBumpDaily")
    existing_items = parse_array(content, "orderBumpItems")
    last_date = existing_daily[-1]["date"]
    start_date = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if start_date > yesterday:
        return content, 0, 0

    token = get_vnda_token()
    orders = fetch_orders_in_range(token, start_date, yesterday)
    new_daily, new_items = compute_order_bump_updates(orders)

    content = replace_array(content, "orderBumpDaily", existing_daily + new_daily)
    content = replace_array(content, "orderBumpItems", existing_items + new_items)
    return content, len(new_daily), len(new_items)


def find_array_span(content, var_name):
    marker = f"const {var_name} = ["
    start = content.index(marker)
    bracket_start = start + len(marker) - 1
    depth = 0
    i = bracket_start
    while True:
        c = content[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1


def replace_array(content, var_name, data_list):
    start, end = find_array_span(content, var_name)
    new_json = json.dumps(data_list, ensure_ascii=False, separators=(",", ":"))
    new_decl = f"const {var_name} = {new_json}"
    return content[:start] + new_decl + content[end:]


def update_html(content, ga4_daily, gsc_daily):
    for marker in ["const ga4Daily = [", "const gscDaily = ["]:
        if content.count(marker) != 1:
            raise RuntimeError(
                f"Marcador '{marker}' não encontrado (ou duplicado) no HTML publicado."
            )

    content = replace_array(content, "ga4Daily", ga4_daily)
    content = replace_array(content, "gscDaily", gsc_daily)

    today_br = datetime.now().strftime("%d/%m/%Y")

    content, n1 = re.subn(
        r"(snapshot atualizado em )\d{2}/\d{2}/\d{4}",
        rf"\g<1>{today_br}",
        content,
    )
    content, n2 = re.subn(
        r"(Snapshot em )\d{2}/\d{2}/\d{4}",
        rf"\g<1>{today_br}",
        content,
    )
    if n1 != 1 or n2 != 1:
        raise RuntimeError(
            f"Substituição de data inesperada (subtitle={n1}, footer={n2})."
        )

    return content


def fetch_current_html(token):
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    status, resp = http_json(api_url, "GET", headers)
    if status != 200:
        raise RuntimeError(f"Falha ao obter HTML atual do GitHub ({status}): {resp}")
    content = base64.b64decode(resp["content"]).decode("utf-8")
    return resp["sha"], content


def push_html(token, sha, html_content):
    api_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    content_b64 = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    body = {
        "message": f"Atualizacao automatica {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "content": content_b64,
        "branch": "main",
        "sha": sha,
    }
    status, resp = http_json(api_url, "PUT", headers, body=body)
    if status not in (200, 201):
        raise RuntimeError(f"Falha ao publicar no GitHub ({status}): {resp}")
    return resp


def main():
    try:
        log("Iniciando atualização do dashboard SEO orgânico.")
        github_token = get_github_token()
        sa_path = get_google_sa_path()

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        log(f"Data de referência (ontem): {yesterday}")

        token = get_google_token(sa_path)
        log("Token Google obtido com sucesso.")

        ga4_daily = fetch_ga4_daily(
            token, GA4_PROPERTY_ID, ARTIFACT_START_DATE, yesterday
        )
        log(f"GA4: {len(ga4_daily)} dias.")

        gsc_daily = fetch_gsc_daily(token, GSC_SITE_URL, GSC_START_DATE, yesterday)
        log(f"Search Console (diário): {len(gsc_daily)} dias.")

        sha, current_html = fetch_current_html(github_token)
        log("HTML publicado atual obtido do GitHub.")

        html_content = update_html(current_html, ga4_daily, gsc_daily)

        try:
            html_content, n_days, n_items = update_order_bump(html_content, yesterday)
            log(f"Order Bump: {n_days} dia(s) novo(s), {n_items} item(ns) novo(s).")
        except Exception as e:
            log(f"AVISO: falha ao atualizar Order Bump, seguindo sem esse dado: {e}")

        push_html(github_token, sha, html_content)
        log("Publicado no GitHub Pages com sucesso.")

        log(
            f"Resumo: GA4={len(ga4_daily)}d, GSC={len(gsc_daily)}d, "
            f"última data GA4={ga4_daily[-1]['date'] if ga4_daily else 'N/A'}, "
            f"última data GSC={gsc_daily[-1]['date'] if gsc_daily else 'N/A'}."
        )
        log("CONCLUÍDO COM SUCESSO.")
    except Exception as e:
        log(f"ERRO: {e}")
        save_log()
        sys.exit(1)

    save_log()


if __name__ == "__main__":
    main()
