import urllib.request
import urllib.error
import urllib.parse
import json
import os
import csv
import re
import time
import base64
import shutil
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from collections import defaultdict

SHOP_HOST = "www.perkyshoes.com"
RSS_URL = "https://product-feeder.vnda.com.br/feeds/95/products.rss"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
BACKUP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "PerkyShoesDashboard", "backup")


def _secret(env_name, local_key):
    """Le um segredo de variavel de ambiente (GitHub Actions Secrets em producao)
    ou, localmente, de local_secrets.json (nunca versionado - fica de fora do
    git pelo .gitignore em allowlist, igual aos outros arquivos de credencial)."""
    val = os.environ.get(env_name)
    if val:
        return val
    local_path = os.path.join(BASE_DIR, "local_secrets.json")
    if os.path.isfile(local_path):
        with open(local_path, encoding="utf-8") as f:
            local = json.load(f)
        if local.get(local_key):
            return local[local_key]
    raise RuntimeError(
        f"Segredo '{env_name}' nao encontrado: defina a variavel de ambiente {env_name} "
        f"ou adicione \"{local_key}\" em local_secrets.json"
    )


API_TOKEN = _secret("VNDA_API_TOKEN", "vnda_api_token")
headers = {"Authorization": f"Bearer {API_TOKEN}", "X-Shop-Host": SHOP_HOST}
GOOGLE_NS = "{http://base.google.com/ns/1.0}"


def log_line(script_name, message):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{script_name}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


# Arquivos de estado mutavel (pedidos, estoque, enderecos, cursors etc) que os
# scripts leem/escrevem entre execucoes. No GitHub Actions cada execucao comeca
# do zero (runner efemero), entao esses arquivos vivem num bucket privado no
# Cloudflare R2 - baixados no inicio (r2_pull_all) e devolvidos no fim
# (r2_push_all) de cada script de entrada. Nunca versionados no git.
STATE_FILES = [
    "avise_me.json",
    "banco_enderecos_pedidos.json",
    "controle_prazo.json",
    "cupons_promocoes.json",
    "dias_manuseio_gerenciados.json",
    "estoque_deposito.json",
    "expedicao.json",
    "github_config.json",
    "historico_descontos.csv",
    "historico_estoque_eventos.csv",
    "historico_estoque_sku.csv",
    "historico_pageviews.csv",
    "historico_vendas.json",
    "historico_vendas_sku.json",
    "orders_master.json",
    "pedidos_itens.json",
    "reposicoes.json",
    "sync_state.json",
    "tiny_config.json",
    "webhook_config.json",
    "webhook_cursor.json",
]


def _r2_client():
    import boto3
    account_id = _secret("R2_ACCOUNT_ID", "r2_account_id")
    access_key = _secret("R2_ACCESS_KEY_ID", "r2_access_key_id")
    secret_key = _secret("R2_SECRET_ACCESS_KEY", "r2_secret_access_key")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def r2_pull_all():
    """Baixa todo STATE_FILES do bucket R2 pra pasta local, antes de rodar
    qualquer sync. So faz algo no GitHub Actions (GITHUB_ACTIONS=true) -
    localmente os arquivos ja estao no disco, e essa chamada e um no-op."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    from botocore.exceptions import ClientError
    bucket = _secret("R2_BUCKET_NAME", "r2_bucket_name")
    client = _r2_client()
    baixados = 0
    for name in STATE_FILES:
        path = os.path.join(BASE_DIR, name)
        try:
            client.download_file(bucket, name, path)
            baixados += 1
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code not in ("404", "NoSuchKey"):
                raise
    print(f"[r2] {baixados}/{len(STATE_FILES)} arquivos de estado baixados do bucket")


def r2_push_all():
    """Envia STATE_FILES de volta pro bucket R2 apos rodar o sync. So faz algo
    no GitHub Actions - localmente e um no-op (os arquivos ja ficam no disco)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    bucket = _secret("R2_BUCKET_NAME", "r2_bucket_name")
    client = _r2_client()
    enviados = 0
    for name in STATE_FILES:
        path = os.path.join(BASE_DIR, name)
        if os.path.isfile(path):
            client.upload_file(path, bucket, name)
            enviados += 1
    print(f"[r2] {enviados}/{len(STATE_FILES)} arquivos de estado enviados pro bucket")


def _read_asset_with_fallback(filename):
    """Le um arquivo estatico da pasta principal (OneDrive) e mantem uma copia
    de seguranca local (fora do OneDrive). Se a pasta principal estiver
    inacessivel (ex: OneDrive fora do ar), usa a copia de backup automaticamente."""
    path = os.path.join(BASE_DIR, filename)
    backup_path = os.path.join(BACKUP_DIR, filename)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            content = f.read()
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            shutil.copy2(path, backup_path)
        except OSError:
            pass
        return content
    if os.path.isfile(backup_path):
        log_line("dashboard_lib", f"AVISO: {filename} ausente em {BASE_DIR}, usando copia de backup local")
        with open(backup_path, encoding="utf-8") as f:
            return f.read()
    raise FileNotFoundError(f"{filename} nao encontrado nem na pasta principal nem no backup ({backup_path})")


def fetch_with_retry(url, max_retries=5, timeout=20):
    delay = 1.5
    for _ in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(delay)
                delay *= 1.7
                continue
            raise
    raise RuntimeError(f"max retries exceeded: {url}")


def fetch_address_with_retry(code, max_retries=5):
    url = f"https://api.vnda.com.br/api/v2/orders/{code}/shipping_address"
    delay = 1.5
    for _ in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(delay)
                delay *= 1.7
                continue
            return {"_error": f"HTTP {e.code}"}
        except Exception as e:
            return {"_error": str(e)}
    return {"_error": "max_retries_exceeded"}


def load_json(name, default):
    path = os.path.join(BASE_DIR, name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(name, obj):
    path = os.path.join(BASE_DIR, name)
    tmp_path = path + f".tmp{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp_path, path)


# ---------------- PARTE 1: PEDIDOS NOVOS (incremental) ----------------

def _process_order(o, ref_agg, sku_sales):
    """Extrai os itens de uma order da API da VNDA e acumula em ref_agg/sku_sales
    (ambos mutados in-place). Retorna (master_row, item_list); master_row e None
    se a order nao tiver data de recebimento."""
    code = o.get("code")
    status = o.get("status")
    received = o.get("received_at") or ""
    order_date = received[:10]
    hour = None
    if len(received) >= 13:
        try:
            hour = int(received[11:13])
        except ValueError:
            hour = None
    total = o.get("total", 0.0)

    if not order_date:
        return None, None

    item_list = []
    for item in o.get("items", []):
        ref = item.get("reference") or "SEM_REFERENCIA"
        sku = item.get("sku")
        qty = item.get("quantity", 0) or 0
        revenue = item.get("total", 0) or 0.0
        name = item.get("product_name", "")
        key = (ref, order_date, status)
        ref_agg[key]["qty"] += qty
        ref_agg[key]["revenue"] += revenue
        ref_agg[key]["name"] = name
        if sku:
            rec = sku_sales.setdefault(sku, {}).setdefault(order_date, {}).setdefault(status, [0, 0.0])
            rec[0] += qty
            rec[1] = round(rec[1] + revenue, 2)
        item_list.append({"ref": ref, "sku": sku, "qty": qty, "revenue": revenue, "name": name})

    master_row = {
        "code": code, "date": order_date, "hour": hour, "status": status, "total": total,
        "coupon_code": o.get("coupon_code"), "discount_price": o.get("discount_price") or 0.0,
    }
    return master_row, item_list


def sync_orders():
    sync_state = load_json("sync_state.json", {"ultima_data_sincronizada": None})
    last_sync = sync_state.get("ultima_data_sincronizada")
    today_str = date.today().isoformat()

    if not last_sync:
        print("[pedidos] sync_state.json sem data de referencia, abortando sync incremental")
        return

    print(f"[pedidos] sincronizando pedidos criados entre {last_sync} e {today_str}")

    all_new_orders = []
    page = 1
    while True:
        url = f"https://api.vnda.com.br/api/v2/orders?per_page=100&page={page}&start={last_sync}&finish={today_str}"
        data = fetch_with_retry(url)
        if not data:
            break
        all_new_orders.extend(data)
        page += 1
        if len(data) < 100:
            break
    print(f"[pedidos] {len(all_new_orders)} pedidos no periodo")

    sales_base = load_json("historico_vendas.json", {"rows": [], "names": {}})
    kept_rows = [r for r in sales_base["rows"] if r[1] < last_sync]
    ref_agg = defaultdict(lambda: {"qty": 0, "revenue": 0.0, "name": ""})

    sku_sales = load_json("historico_vendas_sku.json", {})
    for sku in list(sku_sales.keys()):
        for d in list(sku_sales[sku].keys()):
            if d >= last_sync:
                del sku_sales[sku][d]
        if not sku_sales[sku]:
            del sku_sales[sku]

    orders_master = load_json("orders_master.json", [])
    orders_master = [o for o in orders_master if o["date"] < last_sync]

    addr_db = load_json("banco_enderecos_pedidos.json", {})
    new_codes_for_address = []

    pedidos_itens = load_json("pedidos_itens.json", {})
    for code in list(pedidos_itens.keys()):
        if pedidos_itens[code]["date"] >= last_sync:
            del pedidos_itens[code]
    order_items_temp = {}

    for o in all_new_orders:
        master_row, item_list = _process_order(o, ref_agg, sku_sales)
        if master_row is None:
            continue
        code = master_row["code"]

        orders_master.append(master_row)

        if code and code not in addr_db:
            new_codes_for_address.append(code)

        if code:
            order_items_temp[code] = {"date": master_row["date"], "status": master_row["status"], "items": item_list}

    new_rows = [[ref, dt, status, v["qty"], round(v["revenue"], 2)] for (ref, dt, status), v in ref_agg.items()]
    merged_rows = kept_rows + new_rows
    names = dict(sales_base["names"])
    for (ref, dt, status), v in ref_agg.items():
        if v["name"]:
            names[ref] = v["name"]

    save_json("historico_vendas.json", {"rows": merged_rows, "names": names})
    save_json("historico_vendas_sku.json", sku_sales)
    save_json("orders_master.json", orders_master)

    print(f"[enderecos] buscando {len(new_codes_for_address)} enderecos novos")
    for code in new_codes_for_address:
        addr_db[code] = fetch_address_with_retry(code)
    save_json("banco_enderecos_pedidos.json", addr_db)

    for code, rec in order_items_temp.items():
        addr = addr_db.get(code, {})
        rec["email"] = addr.get("email") if isinstance(addr, dict) else None
        pedidos_itens[code] = rec
    save_json("pedidos_itens.json", pedidos_itens)

    sync_state["ultima_data_sincronizada"] = today_str
    sync_state["total_pedidos_na_base"] = len(orders_master)
    save_json("sync_state.json", sync_state)

    print(f"[pedidos] sincronizacao concluida: base agora com {len(orders_master)} pedidos")


def backfill_pedidos_itens(start="2023-07-01"):
    """Reconstroi pedidos_itens.json com o historico completo (itens por pedido + email).
    Rodar uma unica vez; dali em diante sync_orders() mantem o arquivo atualizado."""
    finish = date.today().isoformat()
    addr_db = load_json("banco_enderecos_pedidos.json", {})

    all_orders = []
    page = 1
    while True:
        url = f"https://api.vnda.com.br/api/v2/orders?per_page=100&page={page}&start={start}&finish={finish}"
        data = fetch_with_retry(url)
        if not data:
            break
        all_orders.extend(data)
        if page % 20 == 0:
            print(f"[backfill] pagina {page}, {len(all_orders)} pedidos ate agora")
        page += 1
        if len(data) < 100:
            break

    pedidos_itens = {}
    for o in all_orders:
        code = o.get("code")
        if not code:
            continue
        received = o.get("received_at") or ""
        order_date = received[:10]
        status = o.get("status")
        addr = addr_db.get(code, {})
        email = addr.get("email") if isinstance(addr, dict) else None
        items = [
            {
                "ref": item.get("reference") or "SEM_REFERENCIA",
                "sku": item.get("sku"),
                "qty": item.get("quantity", 0) or 0,
                "revenue": item.get("total", 0) or 0.0,
                "name": item.get("product_name", ""),
            }
            for item in o.get("items", [])
        ]
        pedidos_itens[code] = {"date": order_date, "status": status, "email": email, "items": items}

    save_json("pedidos_itens.json", pedidos_itens)
    com_email = sum(1 for v in pedidos_itens.values() if v["email"])
    print(f"[backfill] concluido: {len(pedidos_itens)} pedidos salvos em pedidos_itens.json ({com_email} com email)")


def rebuild_vendas_sku_com_status():
    """Reconstroi historico_vendas_sku.json a partir de pedidos_itens.json, agora com
    status por linha: {sku: {data: {status: [qty, revenue]}}}. Rodar uma unica vez
    depois de atualizar pedidos_itens.json com o campo revenue."""
    pedidos = load_json("pedidos_itens.json", {})
    sku_sales = {}
    for o in pedidos.values():
        order_date = o.get("date")
        status = o.get("status")
        if not order_date:
            continue
        for item in o.get("items", []):
            sku = item.get("sku")
            if not sku:
                continue
            qty = item.get("qty", 0) or 0
            revenue = item.get("revenue", 0) or 0.0
            rec = sku_sales.setdefault(sku, {}).setdefault(order_date, {}).setdefault(status, [0, 0.0])
            rec[0] += qty
            rec[1] = round(rec[1] + revenue, 2)

    save_json("historico_vendas_sku.json", sku_sales)
    print(f"[rebuild] historico_vendas_sku.json reconstruido com status: {len(sku_sales)} skus")


# ---------------- PARTE 2: ESTOQUE E DESCONTO (retrato do dia, substitui se ja rodou hoje) ----------------

def fetch_all_products():
    products = []
    page = 1
    while True:
        url = f"https://api.vnda.com.br/api/v2/products?per_page=100&page={page}"
        data = fetch_with_retry(url)
        if not data:
            break
        products.extend(data)
        page += 1
        if page > 30:
            break
    return products


def fetch_rss_items():
    """Le o feed de produtos (formato Google Shopping, 1 item por SKU/tamanho) e devolve
    1 item por REFERENCIA (dedup pelo primeiro tamanho encontrado) - preco e desconto sao
    sempre iguais entre tamanhos de uma mesma referencia, confirmado por amostragem: 0
    referencias com preco/desconto divergente entre tamanhos, nas 174 referencias do feed."""
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        xml_bytes = resp.read()
    root = ET.fromstring(xml_bytes)

    def parse_money(raw):
        if not raw:
            return None
        m = re.match(r"([\d.]+)", raw.strip())
        return float(m.group(1)) if m else None

    por_referencia = {}
    for item in root.iter("item"):
        reference = item.findtext(f"{GOOGLE_NS}item_group_id")
        if not reference or reference in por_referencia:
            continue
        por_referencia[reference] = {
            "reference": reference,
            "price": parse_money(item.findtext(f"{GOOGLE_NS}price")),
            "sale_price": parse_money(item.findtext(f"{GOOGLE_NS}sale_price")),
            "availability": item.findtext(f"{GOOGLE_NS}availability"),
        }
    return list(por_referencia.values())


def write_csv_replace_today(path, fieldnames, rows, today):
    existing = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing = [r for r in reader if r.get("data") != today]
    tmp_path = path + f".tmp{os.getpid()}"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing)
        writer.writerows(rows)
    os.replace(tmp_path, path)


def snapshot_estoque_desconto():
    today = date.today().isoformat()
    products = fetch_all_products()

    nome_por_referencia = {}
    sku_rows = []
    for p in products:
        reference = p.get("reference") or ""
        product_id = p.get("id")
        nome_por_referencia[reference] = p.get("name", "")
        categoria = next(
            (t.get("title") for t in (p.get("category_tags") or []) if t.get("tag_type") == "categoria"), "")
        for variant_wrapper in p.get("variants", []):
            for variant in variant_wrapper.values():
                props = variant.get("properties", {}) or {}
                cor = props.get("property1", {}).get("value", "")
                tamanho = props.get("property2", {}).get("value", "")
                sku_rows.append({
                    "data": today,
                    "referencia": reference,
                    "sku": variant.get("sku", ""),
                    "nome_produto": p.get("name", ""),
                    "product_id": product_id,
                    "variant_id": variant.get("id", ""),
                    "cor": cor,
                    "tamanho": tamanho,
                    "categoria": categoria,
                    "estoque": variant.get("stock", 0) or 0,
                    "disponivel": bool(variant.get("available")),
                    "ativo": p.get("active", ""),
                })

    write_csv_replace_today(
        os.path.join(BASE_DIR, "historico_estoque_sku.csv"),
        ["data", "referencia", "sku", "nome_produto", "product_id", "variant_id",
         "cor", "tamanho", "categoria", "estoque", "disponivel", "ativo"],
        sku_rows, today
    )
    print(f"[estoque] {len(sku_rows)} SKUs salvos para {today} (substituiu snapshot anterior do dia, se houve)")

    rss_items = fetch_rss_items()
    desconto_rows = []
    for item in rss_items:
        reference = item["reference"]
        price = item["price"]
        sale_price = item["sale_price"]
        if sale_price is None:
            sale_price = price
        discount_pct = 0.0
        if price and price > 0:
            discount_pct = round((1 - (sale_price / price)) * 100, 2)

        desconto_rows.append({
            "data": today,
            "referencia": reference,
            "nome_produto": nome_por_referencia.get(reference, ""),
            "product_id": None,
            "preco_cheio": price,
            "preco_com_desconto": sale_price,
            "desconto_pct": discount_pct,
            "disponivel_rss": item["availability"],
        })

    write_csv_replace_today(
        os.path.join(BASE_DIR, "historico_descontos.csv"),
        ["data", "referencia", "nome_produto", "product_id",
         "preco_cheio", "preco_com_desconto", "desconto_pct", "disponivel_rss"],
        desconto_rows, today
    )
    print(f"[descontos] {len(desconto_rows)} produtos salvos para {today} (substituiu snapshot anterior do dia, se houve)")


# ---------------- PARTE 2B: AVISE-ME (planilha de restock via Gmail) ----------------

AVISE_ME_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1o3teFyxgiqaORgkW5noucjalaFjRprywURZr04KLrq4"
    "/export?format=csv&gid=0"
)


def fetch_avise_me():
    req = urllib.request.Request(AVISE_ME_SHEET_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")

    linhas = raw.splitlines()
    reader = csv.DictReader(linhas)
    registros = []
    for r in reader:
        data_email = (r.get("data_email") or "").strip()
        email = (r.get("email_cliente") or "").strip().lower()
        if not data_email or not email:
            continue
        try:
            data_iso = datetime.strptime(data_email, "%d/%m/%Y %H:%M").date().isoformat()
        except ValueError:
            continue
        registros.append({
            "data": data_iso,
            "email": email,
            "phone": (r.get("phone") or "").strip(),
            "product_name": (r.get("product_name") or "").strip(),
            "referencia": (r.get("referencia") or "").strip(),
            "sku": (r.get("sku") or "").strip(),
        })

    save_json("avise_me.json", registros)
    print(f"[avise-me] {len(registros)} registros salvos")
    return registros


# ---------------- PARTE 2C/2D: REPOSICOES E CONTROLE DE PRAZO (planilha unificada) ----------------
# Migrado das antigas abas separadas "Calendario de Reposicoes" (gid 104092470) e
# "Controle de Prazo" (gid 1172471419) para uma unica aba "Reposicoes e Controle de Prazo"
# (identificada pelo sheetId, imune a renomeacao da aba). Le via API do Sheets (nao CSV
# export) porque a aba agora e editada por humanos e a API lida melhor com formulas e
# formatacao do que o export CSV publico.

REPOSICOES_PRAZO_SPREADSHEET_ID = "1HmoyIdSMZwuUg0HbVH8trJfN-6SA6WLQDVp9WR4BviM"
REPOSICOES_PRAZO_SHEET_GID = 1132947474
GOOGLE_SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "google_service_account.json")


def _urlopen_json_retry(req, max_retries=5, timeout=20):
    """Como fetch_with_retry, mas tambem tolera erros transitorios do servidor
    (5xx) alem de 429 - a API do Sheets solta 503 de vez em quando, e sem
    retry isso derruba a sincronizacao inteira por um soluco passageiro."""
    delay = 1.5
    for _ in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(delay)
                delay *= 1.7
                continue
            raise
    raise RuntimeError(f"max retries exceeded: {req.full_url}")


def _sheets_api_get(spreadsheet_id, sheet_gid, want_grid_range=False):
    from google.oauth2 import service_account
    import google.auth.transport.requests

    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    auth_headers = {"Authorization": f"Bearer {creds.token}"}

    # resolve o titulo atual da aba pelo sheetId, pra sobreviver a renomeacoes
    meta_req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}?fields=sheets.properties",
        headers=auth_headers,
    )
    meta = _urlopen_json_retry(meta_req)
    title = next(
        s["properties"]["title"] for s in meta["sheets"] if s["properties"]["sheetId"] == sheet_gid
    )

    range_a1 = urllib.parse.quote(f"'{title}'")
    values_req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_a1}"
        f"?valueRenderOption=FORMATTED_VALUE",
        headers=auth_headers,
    )
    return _urlopen_json_retry(values_req).get("values", [])


# ---------------- PARTE 2E: VISUALIZACOES DE PAGINA DE PRODUTO (GA4) ----------------
# Usa o mesmo service account ja usado pro Sheets (ja tem acesso de leitura
# concedido na propriedade GA4 "Perky Shoes"). A URL da PDP termina em
# "-<id numerico da VNDA>"; cruzamos com o product_id que ja vem no snapshot
# diario de estoque (historico_estoque_sku.csv) pra chegar na referencia.

GA4_PROPERTY_ID = "316363343"


def fetch_product_pageviews(days=90):
    from google.oauth2 import service_account
    import google.auth.transport.requests

    creds = service_account.Credentials.from_service_account_file(
        GOOGLE_SERVICE_ACCOUNT_FILE, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    headers = {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}

    id_to_ref = {}
    estoque_path = os.path.join(BASE_DIR, "historico_estoque_sku.csv")
    if os.path.isfile(estoque_path):
        with open(estoque_path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("product_id"):
                    id_to_ref[r["product_id"]] = r["referencia"]

    body = json.dumps({
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "date"}, {"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}],
        "dimensionFilter": {"filter": {
            "fieldName": "pagePath",
            "stringFilter": {"matchType": "BEGINS_WITH", "value": "/produto/"},
        }},
        "limit": 100000,
    }).encode()
    req = urllib.request.Request(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runReport",
        data=body, method="POST", headers=headers,
    )
    data = _urlopen_json_retry(req)

    rows = []
    unmatched_views = 0
    for row in data.get("rows", []):
        date_raw = row["dimensionValues"][0]["value"]  # YYYYMMDD
        path = row["dimensionValues"][1]["value"]
        views = int(row["metricValues"][0]["value"])
        m = re.search(r"-(\d+)$", path.rstrip("/"))
        ref = id_to_ref.get(m.group(1)) if m else None
        if not ref:
            unmatched_views += views
            continue
        rows.append([ref, f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}", views])

    csv_path = os.path.join(BASE_DIR, "historico_pageviews.csv")
    tmp_path = csv_path + f".tmp{os.getpid()}"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["referencia", "data", "views"])
        writer.writerows(rows)
    os.replace(tmp_path, csv_path)

    print(f"[pageviews] {len(rows)} linhas salvas (ultimos {days} dias) | "
          f"{unmatched_views} views sem referencia correspondente")
    return rows


# ---------------- PARTE 2F: MAPA CUPOM -> PROMOCAO (VNDA discounts) ----------------
# Cada "discount" na VNDA e uma campanha/promocao (tem nome, ex: "Cupons Lojas
# Fisicas Franquias"), e /discounts/{id}/coupons devolve os codigos de cupom
# reais associados a ela - a peca que faltava pra ligar codigo -> nome legivel.

def fetch_coupon_promotions():
    all_discounts = []
    page = 1
    while True:
        data = fetch_with_retry(f"https://api.vnda.com.br/api/v2/discounts?per_page=100&page={page}")
        if not data:
            break
        all_discounts.extend(data)
        page += 1
        if len(data) < 100:
            break

    mapping = {}
    erros = 0
    for d in all_discounts:
        try:
            coupons = fetch_with_retry(f"https://api.vnda.com.br/api/v2/discounts/{d['id']}/coupons?per_page=100")
        except urllib.error.HTTPError:
            erros += 1
            continue
        for c in (coupons or []):
            code = c.get("code")
            if code:
                mapping[code] = {"discount_id": d["id"], "discount_name": (d.get("name") or "").strip()}

    save_json("cupons_promocoes.json", mapping)
    print(f"[cupons-promocoes] {len(all_discounts)} promocoes verificadas ({erros} erros), "
          f"{len(mapping)} codigos de cupom mapeados")
    return mapping


def fetch_reposicoes_prazo():
    rows = _sheets_api_get(REPOSICOES_PRAZO_SPREADSHEET_ID, REPOSICOES_PRAZO_SHEET_GID)
    if not rows:
        save_json("reposicoes.json", [])
        save_json("controle_prazo.json", [])
        print("[reposicoes-prazo] planilha vazia")
        return [], []

    header = [h.strip() for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}

    def cell(r, name):
        i = idx.get(name)
        if i is None or i >= len(r):
            return ""
        return (r[i] or "").strip()

    reposicoes, controle_prazo = [], []
    for r in rows[1:]:
        sku = cell(r, "SKU")
        if not sku:
            continue
        referencia = cell(r, "Referência")
        nome = cell(r, "Nome")
        data_str = cell(r, "Data prevista de chegada")
        qtd_str = cell(r, "Qtd pedida")
        presale = cell(r, "Em pré-venda no site?").lower() == "sim"

        data_iso = None
        if data_str:
            try:
                data_iso = datetime.strptime(data_str, "%d/%m/%Y").date().isoformat()
            except ValueError:
                pass

        if qtd_str and data_iso:
            try:
                qtd = int(float(qtd_str.replace(",", ".")))
            except ValueError:
                qtd = 0
            if qtd > 0:
                reposicoes.append({
                    "referencia": referencia, "nome": nome, "sku": sku,
                    "data_prevista": data_iso, "quantidade": qtd,
                })

        if presale and data_iso:
            controle_prazo.append({
                "referencia": referencia, "nome": nome, "sku": sku,
                "dias_add_frete": max((date.fromisoformat(data_iso) - date.today()).days, 0),
                "data_chegada": data_iso,
            })

    save_json("reposicoes.json", reposicoes)
    save_json("controle_prazo.json", controle_prazo)
    print(f"[reposicoes-prazo] {len(reposicoes)} reposicoes em aberto | {len(controle_prazo)} SKUs em pre-venda ativa")
    return reposicoes, controle_prazo


def sync_handling_days():
    """Sincroniza o campo handling_days (dias de manuseio) na VNDA para os SKUs
    em pre-venda ativa (aba Controle de Prazo). O prazo e recalculado localmente
    a partir da data de chegada, sem depender da coluna da planilha (que so fica
    correta se a celula B1 for atualizada manualmente todo dia). SKUs que saem da
    planilha (produto chegou) sao zerados automaticamente, mas so os que essa
    funcao gerenciou anteriormente - nunca mexe em SKUs fora desse controle."""
    presale = load_json("controle_prazo.json", [])
    today = date.today()
    target_by_sku = {
        r["sku"]: max((date.fromisoformat(r["data_chegada"]) - today).days, 0)
        for r in presale
    }

    managed = set(load_json("dias_manuseio_gerenciados.json", []))
    to_zero = managed - set(target_by_sku)

    if not target_by_sku and not to_zero:
        print("[handling-days] nada a sincronizar")
        return

    products = fetch_all_products()
    variant_by_sku = {}
    for p in products:
        product_id = p.get("id")
        for variant_wrapper in p.get("variants", []):
            for variant in variant_wrapper.values():
                sku = variant.get("sku")
                if sku:
                    variant_by_sku[sku] = {
                        "product_id": product_id,
                        "variant_id": variant.get("id"),
                        "quantity": variant.get("stock", 0) or 0,
                        "price": variant.get("price", 0) or 0,
                        "handling_days": variant.get("handling_days", 0) or 0,
                    }

    erros = []

    def patch_handling_days(sku, target_days):
        info = variant_by_sku.get(sku)
        if not info:
            erros.append(f"{sku}: nao encontrado na VNDA")
            return False
        if info["handling_days"] == target_days:
            return True
        url = f"https://api.vnda.com.br/api/v2/products/{info['product_id']}/variants/{info['variant_id']}"
        body = json.dumps({
            "sku": sku,
            "quantity": info["quantity"],
            "price": info["price"],
            "handling_days": target_days,
        }).encode()
        req = urllib.request.Request(
            url, data=body, method="PATCH",
            headers={**headers, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                resp.read()
            return True
        except urllib.error.HTTPError as e:
            erros.append(f"{sku}: HTTP {e.code} - {e.read().decode()[:200]}")
            return False

    atualizados = sum(1 for sku, days in target_by_sku.items() if patch_handling_days(sku, days))
    zerados = sum(1 for sku in to_zero if patch_handling_days(sku, 0))

    save_json("dias_manuseio_gerenciados.json", sorted(target_by_sku))

    print(f"[handling-days] {atualizados} SKUs em pre-venda atualizados | "
          f"{zerados} zerados (saida da pre-venda) | {len(erros)} erros")
    for e in erros[:20]:
        print(f"  [handling-days][erro] {e}")


# ---------------- PARTE 2F: WEBHOOKS VNDA (via relay no Cloudflare Worker) ----------------

STOCK_EVENT_TYPES = {"stocks-changed", "sku-available", "sku-unavailable"}
ORDER_EVENT_TYPES = {"order-confirmed", "order-received", "order-canceled"}
PRODUCT_EVENT_TYPES = {"product-changed", "product-activated"}


def _load_webhook_config():
    path = os.path.join(BASE_DIR, "webhook_config.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_webhook_events():
    """Busca no relay (Cloudflare Worker) os eventos novos desde o ultimo cursor salvo."""
    config = _load_webhook_config()
    if not config:
        print("[webhook] webhook_config.json ausente, pulando")
        return []

    cursor_data = load_json("webhook_cursor.json", {"cursor": ""})
    cursor = cursor_data.get("cursor", "")

    url = f"{config['worker_base_url']}/events/{config['read_secret']}"
    if cursor:
        url += f"?since={urllib.parse.quote(cursor)}"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    events = data.get("events", [])
    next_cursor = data.get("nextCursor", cursor)
    if next_cursor and next_cursor != cursor:
        save_json("webhook_cursor.json", {"cursor": next_cursor})
    return events


def _fetch_order_detail(code):
    url = f"https://api.vnda.com.br/api/v2/orders/{code}"
    try:
        return fetch_with_retry(url)
    except urllib.error.HTTPError as e:
        log_line("webhook", f"AVISO: nao consegui buscar pedido {code}: HTTP {e.code}")
        return None


def apply_order_event(event):
    """Processa um evento de pedido (order-confirmed/received/canceled): busca o pedido
    completo (estado atual) na API e faz merge incremental nos arquivos locais. Um mesmo
    pedido pode gerar varios eventos (received, depois confirmed, etc) - por isso a
    contribuicao anterior desse codigo (guardada em pedidos_itens.json) e revertida antes
    de aplicar a nova, pra reprocessar o mesmo pedido varias vezes ser idempotente."""
    payload = event.get("payload", {}) or {}
    code = payload.get("code") or payload.get("order_code") or (payload.get("order") or {}).get("code")
    if not code:
        log_line("webhook", f"AVISO: evento {event.get('type')} sem codigo de pedido identificavel")
        return

    o = _fetch_order_detail(code)
    if not o:
        return

    ref_agg = defaultdict(lambda: {"qty": 0, "revenue": 0.0, "name": ""})
    sku_sales_delta = {}
    master_row, item_list = _process_order(o, ref_agg, sku_sales_delta)
    if master_row is None:
        return

    pedidos_itens = load_json("pedidos_itens.json", {})
    old = pedidos_itens.get(code)

    sales_base = load_json("historico_vendas.json", {"rows": [], "names": {}})
    rows_index = {(r[0], r[1], r[2]): i for i, r in enumerate(sales_base["rows"])}

    if old:
        old_ref_agg = defaultdict(lambda: {"qty": 0, "revenue": 0.0})
        for item in old.get("items", []):
            key = (item["ref"], old["date"], old["status"])
            old_ref_agg[key]["qty"] += item["qty"]
            old_ref_agg[key]["revenue"] += item["revenue"]
        for key, v in old_ref_agg.items():
            if key in rows_index:
                row = sales_base["rows"][rows_index[key]]
                row[3] -= v["qty"]
                row[4] = round(row[4] - v["revenue"], 2)

    for (ref, dt, status), v in ref_agg.items():
        key = (ref, dt, status)
        if key in rows_index:
            row = sales_base["rows"][rows_index[key]]
            row[3] += v["qty"]
            row[4] = round(row[4] + v["revenue"], 2)
        else:
            sales_base["rows"].append([ref, dt, status, v["qty"], round(v["revenue"], 2)])
            rows_index[key] = len(sales_base["rows"]) - 1
        if v["name"]:
            sales_base["names"][ref] = v["name"]
    save_json("historico_vendas.json", sales_base)

    sku_sales = load_json("historico_vendas_sku.json", {})
    if old:
        for item in old.get("items", []):
            sku = item.get("sku")
            if not sku:
                continue
            rec = sku_sales.get(sku, {}).get(old["date"], {}).get(old["status"])
            if rec:
                rec[0] -= item["qty"]
                rec[1] = round(rec[1] - item["revenue"], 2)
    for sku, days in sku_sales_delta.items():
        for day, statuses in days.items():
            for status, (qty, revenue) in statuses.items():
                rec = sku_sales.setdefault(sku, {}).setdefault(day, {}).setdefault(status, [0, 0.0])
                rec[0] += qty
                rec[1] = round(rec[1] + revenue, 2)
    save_json("historico_vendas_sku.json", sku_sales)

    orders_master = load_json("orders_master.json", [])
    orders_master = [x for x in orders_master if x["code"] != code]
    orders_master.append(master_row)
    save_json("orders_master.json", orders_master)

    addr_db = load_json("banco_enderecos_pedidos.json", {})
    if code not in addr_db:
        addr_db[code] = fetch_address_with_retry(code)
        save_json("banco_enderecos_pedidos.json", addr_db)

    addr = addr_db.get(code, {})
    pedidos_itens[code] = {
        "date": master_row["date"],
        "status": master_row["status"],
        "items": item_list,
        "email": addr.get("email") if isinstance(addr, dict) else None,
    }
    save_json("pedidos_itens.json", pedidos_itens)


def _parse_stock_event(event):
    """Retorna uma lista de (sku, estoque). O payload de stocks-changed vem como lista
    de objetos (um por SKU alterado); sku-available/sku-unavailable vem como objeto
    simples. Aceita os dois formatos."""
    payload = event.get("payload")
    if payload is None:
        return []
    items = payload if isinstance(payload, list) else [payload]

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        sku = item.get("sku") or item.get("variant_sku") or (item.get("variant") or {}).get("sku")
        if not sku:
            continue
        estoque = item.get("stock")
        if estoque is None:
            estoque = item.get("quantity")
        if estoque is None:
            estoque = item.get("available_quantity")
        if estoque is None and event.get("type") == "sku-unavailable":
            estoque = 0
        results.append((sku, estoque))
    return results


def apply_product_event(event):
    """Eventos de produto (product-changed/product-activated) sao so registrados em log;
    a varredura completa diaria (snapshot_estoque_desconto) ja mantem nome/referencia/
    status ativo atualizados dentro de 24h, entao nao ha necessidade de acao imediata."""
    payload = event.get("payload", {}) or {}
    log_line("webhook", f"evento de produto recebido: {event.get('type')} - {payload}")


def _append_estoque_eventos(rows):
    path = os.path.join(BASE_DIR, "historico_estoque_eventos.csv")
    is_new = not os.path.isfile(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "sku", "tipo_evento", "estoque"])
        writer.writerows(rows)


def _patch_estoque_snapshot(stock_updates):
    """Atualiza o valor de estoque de hoje em historico_estoque_sku.csv para os SKUs que
    ja tem uma linha no snapshot diario. SKUs sem linha ainda (evento chegou antes da
    varredura completa das 01:00) ficam para a proxima varredura diaria."""
    csv_path = os.path.join(BASE_DIR, "historico_estoque_sku.csv")
    if not os.path.isfile(csv_path):
        return
    today = date.today().isoformat()
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    changed = False
    for r in rows:
        if r["data"] == today and r["sku"] in stock_updates:
            novo_estoque = stock_updates[r["sku"]]
            r["estoque"] = str(novo_estoque)
            r["disponivel"] = "True" if novo_estoque > 0 else "False"
            changed = True

    if changed:
        tmp_path = csv_path + f".tmp{os.getpid()}"
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, csv_path)


def sync_webhook_events():
    """Busca os eventos novos no relay e aplica: pedidos sao processados incrementalmente,
    eventos de estoque viram log fino (historico_estoque_eventos.csv) + patch no snapshot
    do dia, eventos de produto so ficam logados. O relay devolve no maximo 200 eventos por
    chamada, entao repete ate esvaziar (evita ficar um resto acumulado de uma execucao pra
    outra caso o volume tenha crescido, ex: script parado por um tempo)."""
    total_eventos = 0
    total_pedidos = 0
    total_estoque = 0

    while True:
        events = fetch_webhook_events()
        if not events:
            break

        stock_updates = {}
        eventos_rows = []

        for ev in events:
            etype = ev.get("type")
            try:
                if etype in STOCK_EVENT_TYPES:
                    parsed = _parse_stock_event(ev)
                    if not parsed:
                        log_line("webhook", f"AVISO: evento {etype} sem sku identificavel: {ev.get('payload')}")
                        continue
                    for sku, estoque in parsed:
                        eventos_rows.append([ev.get("receivedAt"), sku, etype, estoque if estoque is not None else ""])
                        if estoque is not None:
                            stock_updates[sku] = int(estoque)
                elif etype in ORDER_EVENT_TYPES:
                    apply_order_event(ev)
                    total_pedidos += 1
                elif etype in PRODUCT_EVENT_TYPES:
                    apply_product_event(ev)
                else:
                    log_line("webhook", f"evento desconhecido ignorado: {etype}")
            except Exception as e:
                # Um evento com formato inesperado nao pode travar o lote inteiro: o
                # cursor ja avancou ao buscar, entao um erro aqui perderia esse e todos
                # os eventos seguintes do lote silenciosamente.
                log_line("webhook", f"ERRO processando evento {ev.get('id')} ({etype}): "
                                     f"{type(e).__name__}: {e} | payload={ev.get('payload')}")

        if eventos_rows:
            _append_estoque_eventos(eventos_rows)
        if stock_updates:
            _patch_estoque_snapshot(stock_updates)

        total_eventos += len(events)
        total_estoque += len(stock_updates)

        if len(events) < 200:
            break

    if total_eventos == 0:
        print("[webhook] nenhum evento novo")
    else:
        print(f"[webhook] {total_eventos} eventos processados "
              f"({total_pedidos} pedidos, {total_estoque} atualizacoes de estoque)")


def _mask_email(email):
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def _mask_phone(phone):
    if not phone:
        return phone
    return re.sub(r"\d{4}$", "****", phone)


def _compute_avise_me_atendido():
    registros = load_json("avise_me.json", [])
    if not registros:
        return []

    pedidos = load_json("pedidos_itens.json", {})
    compras_por_email = defaultdict(list)
    for o in pedidos.values():
        email = (o.get("email") or "").strip().lower()
        data = o.get("date")
        if not email or not data:
            continue
        skus_do_pedido = {item.get("sku") for item in o.get("items", []) if item.get("sku")}
        compras_por_email[email].append((data, skus_do_pedido))

    estoque_por_sku = defaultdict(list)
    estoque_path = os.path.join(BASE_DIR, "historico_estoque_sku.csv")
    if os.path.isfile(estoque_path):
        with open(estoque_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("estoque") and int(row["estoque"]) > 0:
                    estoque_por_sku[row["sku"]].append(row["data"])

    # Log fino de eventos de estoque (via webhook): pega reabastecimentos que o snapshot
    # diario pode ter perdido (ex: estoque voltou e zerou de novo no mesmo dia).
    eventos_path = os.path.join(BASE_DIR, "historico_estoque_eventos.csv")
    if os.path.isfile(eventos_path):
        with open(eventos_path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sku = row.get("sku")
                if not sku or not row.get("timestamp"):
                    continue
                estoque_val = row.get("estoque")
                is_available = row.get("tipo_evento") == "sku-available" or (
                    estoque_val and estoque_val.isdigit() and int(estoque_val) > 0
                )
                if is_available:
                    estoque_por_sku[sku].append(row["timestamp"][:10])

    resultado = []
    for r in registros:
        atendido = False
        for data_pedido, skus in compras_por_email.get(r["email"], []):
            if data_pedido > r["data"] and r["sku"] in skus:
                atendido = True
                break
        reabastecido = any(d > r["data"] for d in estoque_por_sku.get(r["sku"], []))
        resultado.append({**r, "atendido": atendido, "reabastecido": reabastecido})

    return resultado


# ---------------- PARTE 2G: ESTOQUE FISICO NO DEPOSITO (organizacao de prateleiras) ----------------
# Ajuda o Otavio (logistica) a organizar as caixas fisicas no galpao. Calculado a partir
# do estoque da VNDA (site) descontando o que esta em pre-venda (estoque "ficticio", ainda
# nao chegou fisicamente). Classifica cada produto numa das 3 caixas pelo titulo, aloca
# estantes inteiras por categoria (priorizando quem tem mais deficit) e, dentro de cada
# categoria, preenche primeiro os produtos que mais vendem (ficam mais acessiveis).

CAIXAS = {
    "pequena": {"altura": 12, "largura": 13, "comprimento": 32, "por_camada": 4},
    "media":   {"altura": 12, "largura": 18, "comprimento": 32, "por_camada": 2},
    "grande":  {"altura": 12, "largura": 30, "comprimento": 30, "por_camada": 2},
}
CAMADAS_ANDAR_COMPLETO = 3
CAMADAS_ANDAR_TOPO = 1
CAP_ANDAR = {
    tipo: {
        "completo": c["por_camada"] * CAMADAS_ANDAR_COMPLETO,
        "topo": c["por_camada"] * CAMADAS_ANDAR_TOPO,
    }
    for tipo, c in CAIXAS.items()
}
CAP_ESTANTE = {tipo: cap["completo"] * 4 + cap["topo"] for tipo, cap in CAP_ANDAR.items()}

CORREDORES = ["A", "B", "C", "D", "E", "F"]
ANDARES_POR_ESTANTE = [("1", "completo"), ("2", "completo"), ("3", "completo"), ("4", "completo"), ("5 (topo)", "topo")]


def _sequencia_estantes():
    """Ordem fisica de caminhada pelas 33 estantes: corredores A-E com 3 a esquerda +
    3 a direita cada, corredor F so com 3 a esquerda (as 3 estantes do lado direito de F
    sao reservadas para Feirinha/acessorios, fora dessa alocacao)."""
    seq = []
    for cor in CORREDORES:
        lados = ["E", "D"] if cor != "F" else ["E"]
        for lado in lados:
            for pos in (1, 2, 3):
                seq.append(f"{cor}-{lado}{pos}")
    return seq


def _distancia_ate_f(estante_id):
    """Otavio fica perto do corredor F, entao a proximidade dele com cada estante e
    medida pela distancia ao corredor F (0=F, 5=A). Usado pra colocar os produtos que
    mais vendem nos andares mais acessiveis primeiro."""
    cor = estante_id.split("-")[0]
    return (len(CORREDORES) - 1) - CORREDORES.index(cor)


def _normaliza(s):
    s = (s or "").lower()
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _classifica_caixa(nome, tag_names, reference):
    tags = set(tag_names or [])
    ref = reference or ""
    if "feirinha" in tags or ref.startswith("feir-"):
        return "separada"
    t = _normaliza(nome)
    if "bota" in t or "mule clog plataforma" in t:
        return "grande"
    if "alpargata" in t:
        return "pequena"
    if ("tenis" in t or "sandalia" in t or "mule clog" in t) and "mule clog plataforma" not in t:
        return "media"
    return "separada"


def compute_estoque_deposito():
    products = fetch_all_products()
    sales_by_sku = load_json("historico_vendas_sku.json", {})
    corte_velocidade = (date.today() - timedelta(days=60)).isoformat()

    def velocidade_sku(sku):
        total = 0
        for d, status_map in sales_by_sku.get(sku, {}).items():
            if d < corte_velocidade:
                continue
            for status, (qtd, _valor) in status_map.items():
                if status != "canceled":
                    total += qtd
        return total

    referencias = {}  # referencia -> {categoria, nome, velocidade, skus: [(sku, tamanho, unidades)]}
    for p in products:
        categoria = _classifica_caixa(p.get("name"), p.get("tag_names"), p.get("reference"))
        if categoria == "separada":
            continue
        presale_native = bool(p.get("presale_end_at")) or bool(p.get("activate_at")) or bool(p.get("presale_only"))
        ref = p.get("reference") or p.get("name")
        for wrapper in p.get("variants", []):
            for v in wrapper.values():
                sku = v.get("sku")
                if not sku:
                    continue
                estoque = v.get("stock", 0) or 0
                handling = v.get("handling_days", 0) or 0
                em_presale = handling > 0 or presale_native
                fisico = 0 if em_presale else estoque
                tamanho = sku.rsplit("-", 1)[-1] if "-" in sku else ""
                entry = referencias.setdefault(ref, {
                    "categoria": categoria, "nome": p.get("name"), "referencia": ref,
                    "velocidade": 0, "skus": [],
                })
                # velocidade soma TODOS os tamanhos, mesmo esgotados (fisico=0) - senao um
                # produto que vendeu tanto que esgotou alguns tamanhos aparece como "parado"
                # (bug real encontrado: Sandalia Slide Fivela Camel vendeu ~24 pares em 60
                # dias mas so contava 4, porque 5 dos 7 tamanhos ja esgotaram).
                entry["velocidade"] += velocidade_sku(sku)
                if fisico <= 0:
                    continue
                entry["skus"].append({"sku": sku, "tamanho": tamanho, "unidades": fisico})

    demanda_caixas = {"pequena": 0, "media": 0, "grande": 0}
    for r in referencias.values():
        r["caixas"] = sum(s["unidades"] for s in r["skus"])
        demanda_caixas[r["categoria"]] += r["caixas"]

    # aloca estantes inteiras por categoria, sempre pra quem tem maior deficit restante
    sequencia = _sequencia_estantes()
    restante = dict(demanda_caixas)
    estantes_por_categoria = {"pequena": [], "media": [], "grande": []}
    for estante_id in sequencia:
        cat = max(restante, key=lambda c: restante[c])
        if restante[cat] <= 0:
            break
        estantes_por_categoria[cat].append(estante_id)
        restante[cat] -= CAP_ESTANTE[cat]

    # dentro de cada categoria, preenche andar por andar, priorizando quem mais vende.
    # aloca no nivel de SKU (nao so referencia) pra saber exatamente quais tamanhos
    # foram parar em cada andar - e o que alimenta o drill-down do mapa do galpao.
    enderecos = []
    sem_espaco = []
    sku_por_andar = {}  # "estante|andar" -> lista de {sku, tamanho, referencia, nome, unidades}
    for cat, estantes in estantes_por_categoria.items():
        refs_ordenadas = sorted(
            [r for r in referencias.values() if r["categoria"] == cat],
            key=lambda r: r["velocidade"], reverse=True,
        )
        # estantes mais pertas do corredor F (onde o Otavio fica) entram primeiro na
        # fila de preenchimento, entao os produtos que mais vendem caem nelas.
        estantes_por_proximidade = sorted(estantes, key=_distancia_ate_f)

        slots = []
        for estante_id in estantes_por_proximidade:
            for andar_label, tipo_andar in ANDARES_POR_ESTANTE:
                slots.append({"estante": estante_id, "andar": andar_label, "cap": CAP_ANDAR[cat][tipo_andar], "usado": 0})

        slot_idx = 0
        for r in refs_ordenadas:
            caixas_por_slot_ref = {}
            for sku_info in r["skus"]:
                restante_sku = sku_info["unidades"]
                while restante_sku > 0 and slot_idx < len(slots):
                    slot = slots[slot_idx]
                    disponivel = slot["cap"] - slot["usado"]
                    if disponivel <= 0:
                        slot_idx += 1
                        continue
                    aloca_aqui = min(disponivel, restante_sku)
                    slot["usado"] += aloca_aqui
                    restante_sku -= aloca_aqui
                    key = (slot["estante"], slot["andar"])
                    caixas_por_slot_ref[key] = caixas_por_slot_ref.get(key, 0) + aloca_aqui
                    sku_por_andar.setdefault(f'{slot["estante"]}|{slot["andar"]}', []).append({
                        "sku": sku_info["sku"], "tamanho": sku_info["tamanho"],
                        "referencia": r["referencia"], "nome": r["nome"], "unidades": aloca_aqui,
                    })
                    if slot["usado"] >= slot["cap"]:
                        slot_idx += 1
                if restante_sku > 0:
                    sem_espaco.append({
                        "sku": sku_info["sku"], "tamanho": sku_info["tamanho"],
                        "referencia": r["referencia"], "nome": r["nome"], "categoria": cat,
                        "unidades": restante_sku, "velocidade": r["velocidade"],
                    })

            for (estante_id, andar_label), caixas in caixas_por_slot_ref.items():
                enderecos.append({
                    "referencia": r["referencia"], "nome": r["nome"], "categoria": cat,
                    "estante": estante_id, "andar": andar_label, "caixas": caixas,
                    "velocidade": r["velocidade"],
                })

    estante_categoria = {}
    for cat, estantes in estantes_por_categoria.items():
        for e in estantes:
            estante_categoria[e] = cat

    mapa_estantes = []
    for cor in CORREDORES:
        lados = [("E", "esquerda"), ("D", "direita")] if cor != "F" else [("E", "esquerda")]
        for lado_code, lado_label in lados:
            for pos in (1, 2, 3):
                estante_id = f"{cor}-{lado_code}{pos}"
                mapa_estantes.append({
                    "estante": estante_id, "corredor": cor, "lado": lado_label, "posicao": pos,
                    "categoria": estante_categoria.get(estante_id, "vazia"),
                })
    # 3 estantes reservadas: lado direito do ultimo corredor, fora da alocacao de calcados
    for pos in (1, 2, 3):
        mapa_estantes.append({
            "estante": f"{CORREDORES[-1]}-D{pos}", "corredor": CORREDORES[-1], "lado": "direita",
            "posicao": pos, "categoria": "reservada",
        })

    # detalhe por andar de cada estante dedicada (pra o drill-down do mapa do galpao:
    # clicar na estante -> ve os 5 andares -> clicar num andar -> ve os SKUs ali)
    estante_andares = {}
    for cat, estantes in estantes_por_categoria.items():
        for estante_id in estantes:
            andares_info = []
            for andar_label, tipo_andar in ANDARES_POR_ESTANTE:
                skus_aqui = sku_por_andar.get(f"{estante_id}|{andar_label}", [])
                andares_info.append({
                    "andar": andar_label,
                    "categoria": cat,
                    "usado": sum(s["unidades"] for s in skus_aqui),
                    "capacidade": CAP_ANDAR[cat][tipo_andar],
                    "skus": skus_aqui,
                })
            estante_andares[estante_id] = andares_info

    capacidade_total = {cat: len(estantes_por_categoria[cat]) * CAP_ESTANTE[cat] for cat in demanda_caixas}

    # projecao de ocupacao pras proximas 4 semanas: desconta a venda projetada (velocidade
    # media dos ultimos 60 dias) e soma as reposicoes ja mapeadas que chegam em cada semana.
    hoje = date.today()
    velocidade_diaria_cat = {cat: 0.0 for cat in demanda_caixas}
    for r in referencias.values():
        velocidade_diaria_cat[r["categoria"]] += r["velocidade"] / 60.0

    reposicoes_raw = load_json("reposicoes.json", [])
    reposicao_por_cat_semana = [{cat: 0 for cat in demanda_caixas} for _ in range(4)]
    for rep in reposicoes_raw:
        cat = _classifica_caixa(rep.get("nome"), [], rep.get("referencia"))
        if cat not in demanda_caixas:
            continue
        try:
            dias_ate = (date.fromisoformat(rep["data_prevista"]) - hoje).days
        except (KeyError, ValueError, TypeError):
            continue
        if dias_ate < 0 or dias_ate >= 28:
            continue
        reposicao_por_cat_semana[dias_ate // 7][cat] += rep.get("quantidade", 0)

    capacidade_total_geral = sum(capacidade_total.values())
    projecao_ocupacao = []
    reposicao_acumulada_cat = {cat: 0 for cat in demanda_caixas}
    for semana in range(4):
        for cat in demanda_caixas:
            reposicao_acumulada_cat[cat] += reposicao_por_cat_semana[semana][cat]
        demanda_projetada_total = 0
        for cat in demanda_caixas:
            consumo = velocidade_diaria_cat[cat] * 7 * (semana + 1)
            demanda_projetada_total += max(0, demanda_caixas[cat] - consumo + reposicao_acumulada_cat[cat])
        ocupacao_pct = (demanda_projetada_total / capacidade_total_geral * 100) if capacidade_total_geral else 0
        projecao_ocupacao.append({
            "semana": semana + 1,
            "demanda_projetada": round(demanda_projetada_total),
            "ocupacao_pct": round(ocupacao_pct, 1),
            "reposicao_na_semana": sum(reposicao_por_cat_semana[semana].values()),
        })

    # ranking por produto (referencia): estoque fisico, venda recente, reposicao ja mapeada,
    # cobertura (dias ate zerar no ritmo de venda atual) e classificacao de saude do estoque.
    reposicao_por_referencia = {}
    for rep in reposicoes_raw:
        try:
            if date.fromisoformat(rep["data_prevista"]) < hoje:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        ref = rep.get("referencia")
        reposicao_por_referencia[ref] = reposicao_por_referencia.get(ref, 0) + rep.get("quantidade", 0)

    def _classifica_saude(cobertura_dias):
        if cobertura_dias is None or cobertura_dias > 150:
            return "parado"
        if cobertura_dias > 90:
            return "atencao"
        if cobertura_dias >= 30:
            return "saudavel"
        return "critico"

    ranking_espaco = []
    for r in referencias.values():
        velocidade_diaria = r["velocidade"] / 60.0
        cobertura_dias = round(r["caixas"] / velocidade_diaria) if velocidade_diaria > 0 else None
        ranking_espaco.append({
            "referencia": r["referencia"], "nome": r["nome"], "categoria": r["categoria"],
            "caixas": r["caixas"], "velocidade": r["velocidade"],
            "reposicao_qtd": reposicao_por_referencia.get(r["referencia"], 0),
            "cobertura_dias": cobertura_dias,
            "classe": _classifica_saude(cobertura_dias),
            "pct_espaco": round(r["caixas"] / capacidade_total_geral * 100, 2) if capacidade_total_geral else 0,
        })
    ranking_espaco.sort(key=lambda r: r["caixas"], reverse=True)

    resumo = {
        cat: {
            "demanda": demanda_caixas[cat],
            "capacidade": capacidade_total[cat],
            "estantes_dedicadas": len(estantes_por_categoria[cat]),
            "alocado": min(demanda_caixas[cat], capacidade_total[cat]),
            "excedente": max(0, demanda_caixas[cat] - capacidade_total[cat]),
        }
        for cat in demanda_caixas
    }

    payload = {
        "caixas_spec": CAIXAS,
        "estantes_total": 33,
        "corredores": CORREDORES,
        "resumo": resumo,
        "projecao_ocupacao": projecao_ocupacao,
        "mapa_estantes": mapa_estantes,
        "estante_andares": estante_andares,
        "enderecos": enderecos,
        "sem_espaco": sorted(sem_espaco, key=lambda r: r["velocidade"]),
        "ranking_espaco": ranking_espaco,
    }
    save_json("estoque_deposito.json", payload)
    total_demanda = sum(demanda_caixas.values())
    total_cap = sum(capacidade_total.values())
    print(f"[estoque-deposito] {total_demanda} caixas de estoque fisico | "
          f"{total_cap} caixas de capacidade nas 33 estantes | "
          f"{max(0, total_demanda - total_cap)} caixas sem espaco")
    return payload


EXPEDICAO_JANELA_DIAS = 30

# situacao do pedido no Tiny (public-api/v3, confirmado na documentacao oficial:
# https://api-docs.erp.olist.com/api-reference/pedidos/listar-pedidos)
TINY_SITUACAO_LABEL = {
    0: "aberta", 3: "aprovada", 8: "dados_incompletos",
    1: "faturada", 4: "preparando_envio", 7: "pronto_envio",
    5: "enviada", 6: "entregue", 2: "cancelada", 9: "nao_entregue",
}


def _load_tiny_config():
    path = os.path.join(BASE_DIR, "tiny_config.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _tiny_refresh_token(config):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": config["refresh_token"],
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
    }).encode()
    req = urllib.request.Request(config["token_url"], data=body, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        novo = json.loads(resp.read().decode())
    config["access_token"] = novo["access_token"]
    config["refresh_token"] = novo.get("refresh_token", config["refresh_token"])
    config["token_type"] = novo.get("token_type", config.get("token_type"))
    config["expires_in"] = novo.get("expires_in")
    save_json("tiny_config.json", config)
    return config


def _tiny_get(path, params=None):
    """GET autenticado na API v3 do Tiny. Renova o access_token automaticamente em
    caso de 401 (uma unica tentativa). Retorna None (com log) se a integracao nao
    estiver configurada ou a chamada falhar mesmo apos renovar o token - quem chama
    trata None como 'integracao indisponivel agora', sem quebrar o resto do pipeline."""
    config = _load_tiny_config()
    if not config:
        return None
    url = config["api_base_url"] + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    def _do(token):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    try:
        return _do(config["access_token"])
    except urllib.error.HTTPError as e:
        if e.code == 401:
            try:
                config = _tiny_refresh_token(config)
            except Exception as refresh_err:
                log_line("expedicao", f"AVISO: falha ao renovar token do Tiny: {refresh_err}")
                return None
            try:
                return _do(config["access_token"])
            except urllib.error.HTTPError as e2:
                log_line("expedicao", f"AVISO: Tiny API falhou apos renovar token: HTTP {e2.code} em {path}")
                return None
        log_line("expedicao", f"AVISO: Tiny API HTTP {e.code} em {path}")
        return None
    except Exception as e:
        log_line("expedicao", f"AVISO: erro ao chamar Tiny API em {path}: {e}")
        return None


def fetch_tiny_pedidos_recentes(dias):
    """Busca no Tiny os pedidos dos ultimos `dias` dias (situacao + numero do pedido no
    e-commerce, que e o mesmo 'code' da VNDA - confirmado por amostragem real, 100% dos
    pedidos cruzados bateram). Retorna dict {code_vnda: {...}}, ou None se a integracao
    estiver indisponivel (token invalido, sem config etc) - nesse caso quem chama cai de
    volta pro comportamento antigo (etapas Tiny/NF marcadas como sem integracao)."""
    hoje = date.today()
    start = (hoje - timedelta(days=dias)).isoformat()
    finish = hoje.isoformat()

    por_codigo = {}
    offset = 0
    while True:
        data = _tiny_get("/pedidos", {"dataInicial": start, "dataFinal": finish, "limit": 100, "offset": offset})
        if data is None:
            return None
        itens = data.get("itens", [])
        if not itens:
            break
        for it in itens:
            code = (it.get("ecommerce") or {}).get("numeroPedidoEcommerce")
            if not code:
                continue
            transportador = it.get("transportador") or {}
            forma_frete = transportador.get("formaFrete") or {}
            por_codigo[code] = {
                "situacao": it.get("situacao"),
                "numero_pedido_tiny": it.get("numeroPedido"),
                "data_criacao_tiny": it.get("dataCriacao"),
                "transportadora_tiny": forma_frete.get("nome"),
                "tracking_code_tiny": transportador.get("codigoRastreamento") or None,
            }
        offset += 100
        if len(itens) < 100:
            break
    return por_codigo


def _classifica_estagio_expedicao(recebido, confirmado, entregue, cancelado, tem_rastreio, situacao_tiny):
    if cancelado or situacao_tiny == 2:
        return "cancelado"
    if situacao_tiny == 9:
        return "nao_entregue"
    if entregue or situacao_tiny == 6:
        return "entregue"
    if tem_rastreio or situacao_tiny == 5:
        return "enviado"
    if situacao_tiny in (4, 7):
        return "separado"
    if situacao_tiny == 1:
        return "nf_gerada"
    if situacao_tiny in (0, 3, 8):
        return "integrado_tiny"
    if confirmado:
        return "confirmado"
    if recebido:
        return "recebido"
    return None


def compute_expedicao():
    """Busca os pedidos dos ultimos EXPEDICAO_JANELA_DIAS dias na VNDA (objeto completo,
    com os timestamps de cada etapa) e no Tiny (situacao do pedido, casado pelo codigo do
    e-commerce), e classifica cada um no estagio real do funil de expedicao - incluindo
    'Integracao no Tiny' e 'Nota fiscal gerada', que antes nao tinham dado disponivel.
    Se a integracao com o Tiny estiver fora do ar (token expirado etc), cai de volta pro
    funil so-VNDA e marca tiny_disponivel=False pro frontend exibir essas duas etapas
    como sem integracao, em vez de quebrar."""
    hoje = date.today()
    start = (hoje - timedelta(days=EXPEDICAO_JANELA_DIAS)).isoformat()
    finish = hoje.isoformat()

    orders = []
    page = 1
    while True:
        url = f"https://api.vnda.com.br/api/v2/orders?per_page=100&page={page}&start={start}&finish={finish}"
        data = fetch_with_retry(url)
        if not data:
            break
        orders.extend(data)
        page += 1
        if len(data) < 100:
            break

    tiny_por_codigo = fetch_tiny_pedidos_recentes(EXPEDICAO_JANELA_DIAS)
    tiny_disponivel = tiny_por_codigo is not None
    if tiny_por_codigo is None:
        tiny_por_codigo = {}

    funil = {"recebido": 0, "confirmado": 0, "integrado_tiny": 0, "nf_gerada": 0,
              "separado": 0, "enviado": 0, "entregue": 0, "cancelado": 0, "nao_entregue": 0}
    por_transportadora = defaultdict(lambda: {"pedidos": 0, "entregues": 0, "soma_dias_entrega": 0.0})
    horas_confirmacao = []
    dias_entrega_total = []
    dias_integracao_tiny = []
    atrasados = 0
    pedidos = []
    ESTAGIOS_EM_ANDAMENTO = {"recebido", "confirmado", "integrado_tiny", "nf_gerada", "separado", "enviado"}

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None

    for o in orders:
        code = o.get("code")
        if not code:
            continue
        received_at = o.get("received_at")
        confirmed_dt = _parse_dt(o.get("confirmed_at")) or _parse_dt(o.get("paid_at"))
        delivered_dt = _parse_dt(o.get("delivered_at"))
        received_dt = _parse_dt(received_at)
        canceled = bool(o.get("canceled_at")) or o.get("status") == "canceled"
        expected = o.get("expected_delivery_date")
        tracking_list = o.get("tracking_code_list") or ([o["tracking_code"]] if o.get("tracking_code") else [])
        tiny_info = tiny_por_codigo.get(code)
        situacao_tiny = tiny_info.get("situacao") if tiny_info else None
        # a loja nao preenche shipped_at/shipping_tracked_at no VNDA (confirmado via
        # amostragem real de pedidos) - codigo de rastreio (VNDA ou Tiny) ou a situacao
        # 'enviada' no Tiny sao os sinais confiaveis de que o pedido ja saiu pra entrega.
        tem_rastreio = bool(tracking_list) or bool(tiny_info and tiny_info.get("tracking_code_tiny"))
        transportadora = o.get("delivery_type") or o.get("shipping_label") \
            or (tiny_info.get("transportadora_tiny") if tiny_info else None) or "Não informado"

        estagio = _classifica_estagio_expedicao(
            bool(received_dt), bool(confirmed_dt), bool(delivered_dt), canceled, tem_rastreio, situacao_tiny)
        if estagio is None:
            continue

        funil[estagio] += 1

        atrasado = False
        if estagio in ESTAGIOS_EM_ANDAMENTO and expected:
            try:
                if date.fromisoformat(expected) < hoje:
                    atrasado = True
                    atrasados += 1
            except ValueError:
                pass

        if received_dt and confirmed_dt:
            horas_confirmacao.append((confirmed_dt - received_dt).total_seconds() / 3600)
        if confirmed_dt and delivered_dt:
            dias_entrega_total.append((delivered_dt - confirmed_dt).total_seconds() / 86400)
        if received_dt and tiny_info and tiny_info.get("data_criacao_tiny"):
            try:
                criacao_tiny = date.fromisoformat(tiny_info["data_criacao_tiny"])
                dias_integracao_tiny.append((criacao_tiny - received_dt.date()).days)
            except ValueError:
                pass

        por_transportadora[transportadora]["pedidos"] += 1
        if estagio == "entregue" and confirmed_dt and delivered_dt:
            por_transportadora[transportadora]["entregues"] += 1
            por_transportadora[transportadora]["soma_dias_entrega"] += (delivered_dt - confirmed_dt).total_seconds() / 86400

        pedidos.append({
            "code": code,
            "email": o.get("email"),
            "data": (received_at or "")[:10],
            "total": round(o.get("total") or 0.0, 2),
            "estagio": estagio,
            "atrasado": atrasado,
            "transportadora": transportadora,
            "tracking_code": (tracking_list[0] if tracking_list else None) or (tiny_info.get("tracking_code_tiny") if tiny_info else None),
            "previsao_entrega": expected,
            "confirmado_em": o.get("confirmed_at") or o.get("paid_at"),
            "entregue_em": o.get("delivered_at"),
            "numero_pedido_tiny": tiny_info.get("numero_pedido_tiny") if tiny_info else None,
            "situacao_tiny": TINY_SITUACAO_LABEL.get(situacao_tiny) if situacao_tiny is not None else None,
        })

    pedidos.sort(key=lambda p: p["data"], reverse=True)

    por_transportadora_lista = sorted([
        {
            "transportadora": nome,
            "pedidos": v["pedidos"],
            "prazo_medio_dias": round(v["soma_dias_entrega"] / v["entregues"], 1) if v["entregues"] else None,
        }
        for nome, v in por_transportadora.items()
    ], key=lambda r: r["pedidos"], reverse=True)

    payload = {
        "janela_dias": EXPEDICAO_JANELA_DIAS,
        "atualizado_em": datetime.now().isoformat(timespec="minutes"),
        "tiny_disponivel": tiny_disponivel,
        "funil": funil,
        "atrasados": atrasados,
        "tempo_medio_confirmacao_horas": round(sum(horas_confirmacao) / len(horas_confirmacao), 1) if horas_confirmacao else None,
        "tempo_medio_integracao_tiny_dias": round(sum(dias_integracao_tiny) / len(dias_integracao_tiny), 1) if dias_integracao_tiny else None,
        "tempo_medio_entrega_dias": round(sum(dias_entrega_total) / len(dias_entrega_total), 1) if dias_entrega_total else None,
        "por_transportadora": por_transportadora_lista,
        "pedidos": pedidos,
    }
    save_json("expedicao.json", payload)
    print(f"[expedicao] {len(pedidos)} pedidos nos ultimos {EXPEDICAO_JANELA_DIAS} dias | "
          f"tiny_disponivel={tiny_disponivel} | funil: {funil} | {atrasados} atrasados")
    return payload


# ---------------- PARTE 3: RECONSTRUIR dashboard_data.json ----------------

def _compute_crosssell(min_date=None, pedidos_all=None):
    pedidos = pedidos_all if pedidos_all is not None else load_json("pedidos_itens.json", {})
    if min_date:
        pedidos = {code: o for code, o in pedidos.items() if o.get("date") and o["date"] >= min_date}

    orders_by_ref = defaultdict(set)
    customers_by_ref = defaultdict(set)
    ref_names = {}
    customer_orders = defaultdict(list)
    same_order_pair_counts = defaultdict(lambda: {"orders": 0, "customers": set()})

    for code, o in pedidos.items():
        refs_in_order = set()
        for item in o.get("items", []):
            ref = item["ref"]
            refs_in_order.add(ref)
            if item.get("name"):
                ref_names[ref] = item["name"]
        if not refs_in_order:
            continue
        email = o.get("email")
        for ref in refs_in_order:
            orders_by_ref[ref].add(code)
            if email:
                customers_by_ref[ref].add(email)
        refs_sorted = sorted(refs_in_order)
        for i in range(len(refs_sorted)):
            for j in range(i + 1, len(refs_sorted)):
                key = (refs_sorted[i], refs_sorted[j])
                same_order_pair_counts[key]["orders"] += 1
                if email:
                    same_order_pair_counts[key]["customers"].add(email)
        if email and o.get("date"):
            customer_orders[email].append((o["date"], refs_in_order))

    same_order_pairs = []
    for (ref_a, ref_b), v in same_order_pair_counts.items():
        total_a = len(orders_by_ref[ref_a])
        total_b = len(orders_by_ref[ref_b])
        same_order_pairs.append({
            "ref_a": ref_a, "name_a": ref_names.get(ref_a, ref_a),
            "ref_b": ref_b, "name_b": ref_names.get(ref_b, ref_b),
            "pedidos_juntos": v["orders"],
            "pct": round(v["orders"] / total_a * 100, 1) if total_a else 0,
        })
        same_order_pairs.append({
            "ref_a": ref_b, "name_a": ref_names.get(ref_b, ref_b),
            "ref_b": ref_a, "name_b": ref_names.get(ref_a, ref_a),
            "pedidos_juntos": v["orders"],
            "pct": round(v["orders"] / total_b * 100, 1) if total_b else 0,
        })
    same_order_pairs.sort(key=lambda x: -x["pedidos_juntos"])

    future_pair_customers = defaultdict(set)
    repurchase_samples = defaultdict(list)

    for email, orders in customer_orders.items():
        orders.sort(key=lambda x: x[0])
        for i in range(len(orders)):
            date_i, refs_i = orders[i]
            if i + 1 < len(orders):
                date_next, _ = orders[i + 1]
                try:
                    gap_days = (date.fromisoformat(date_next) - date.fromisoformat(date_i)).days
                except ValueError:
                    gap_days = None
                if gap_days is not None and gap_days >= 0:
                    for ref in refs_i:
                        repurchase_samples[ref].append(gap_days)
            later_refs = set()
            for j in range(i + 1, len(orders)):
                later_refs |= orders[j][1]
            for ref_a in refs_i:
                for ref_b in later_refs:
                    if ref_a != ref_b:
                        future_pair_customers[(ref_a, ref_b)].add(email)

    future_pairs = []
    for (ref_a, ref_b), custs in future_pair_customers.items():
        total_customers_a = len(customers_by_ref[ref_a])
        future_pairs.append({
            "ref_a": ref_a, "name_a": ref_names.get(ref_a, ref_a),
            "ref_b": ref_b, "name_b": ref_names.get(ref_b, ref_b),
            "clientes": len(custs),
            "pct_a": round(len(custs) / total_customers_a * 100, 1) if total_customers_a else 0,
        })
    future_pairs.sort(key=lambda x: -x["clientes"])

    repurchase_time = []
    for ref, samples in repurchase_samples.items():
        if samples:
            repurchase_time.append({
                "ref": ref, "name": ref_names.get(ref, ref),
                "media_dias": round(sum(samples) / len(samples), 1),
                "amostras": len(samples),
            })
    repurchase_time.sort(key=lambda x: -x["amostras"])

    label = min_date or "todo periodo"
    print(f"[crosssell:{label}] {len(same_order_pairs)} pares mesmo pedido | "
          f"{len(future_pairs)} pares futuros | {len(repurchase_time)} produtos com tempo de recompra")

    return {
        "same_order_pairs": same_order_pairs,
        "future_pairs": future_pairs,
        "repurchase_time": repurchase_time,
    }


def build_dashboard_data():
    sales = load_json("historico_vendas.json", {"rows": [], "names": {}})

    def csv_to_records(path):
        with open(os.path.join(BASE_DIR, path), encoding="utf-8") as f:
            return list(csv.DictReader(f))

    stock_sku = csv_to_records("historico_estoque_sku.csv")
    discounts = csv_to_records("historico_descontos.csv")
    pageviews_path = os.path.join(BASE_DIR, "historico_pageviews.csv")
    pageviews_rows = []
    if os.path.isfile(pageviews_path):
        with open(pageviews_path, encoding="utf-8") as f:
            pageviews_rows = [[r["referencia"], r["data"], int(r["views"])] for r in csv.DictReader(f)]

    for r in stock_sku:
        r["estoque"] = int(r["estoque"])
        r["disponivel"] = r["disponivel"] == "True"

    for r in discounts:
        r["preco_cheio"] = float(r["preco_cheio"]) if r["preco_cheio"] else None
        r["preco_com_desconto"] = float(r["preco_com_desconto"]) if r["preco_com_desconto"] else None
        r["desconto_pct"] = float(r["desconto_pct"]) if r["desconto_pct"] else 0.0

    orders_master = load_json("orders_master.json", [])
    orders_raw = [[o["code"], o["date"], o["status"], round(o["total"] or 0.0, 2), o.get("hour"),
                   o.get("coupon_code"), round(o.get("discount_price") or 0.0, 2)]
                  for o in orders_master if o["date"]]

    addr_db = load_json("banco_enderecos_pedidos.json", {})
    orders_geo = {}
    for code, addr in addr_db.items():
        if isinstance(addr, dict) and "_error" not in addr:
            state = addr.get("state")
            city = addr.get("city")
            if state or city:
                orders_geo[code] = [state, city]

    sales_by_sku = load_json("historico_vendas_sku.json", {})
    pedidos_itens_all = load_json("pedidos_itens.json", {})
    today = date.today()
    crosssell = {
        "todo": _compute_crosssell(pedidos_all=pedidos_itens_all),
        "y12": _compute_crosssell(min_date=(today - timedelta(days=365)).isoformat(), pedidos_all=pedidos_itens_all),
        "m3": _compute_crosssell(min_date=(today - timedelta(days=91)).isoformat(), pedidos_all=pedidos_itens_all),
    }

    estoque_atual_por_sku = {}
    for r in stock_sku:
        d = r.get("data")
        sku = r.get("sku")
        if not sku or not d:
            continue
        if sku not in estoque_atual_por_sku or d > estoque_atual_por_sku[sku][0]:
            estoque_atual_por_sku[sku] = (d, r["estoque"])

    controle_prazo_raw = load_json("controle_prazo.json", [])
    dias_extra_por_sku = {r["sku"]: r["dias_add_frete"] for r in controle_prazo_raw}

    reposicoes_raw = load_json("reposicoes.json", [])
    reposicao_por_sku = {r["sku"]: (r["quantidade"], r["data_prevista"]) for r in reposicoes_raw}

    avise_me = [
        {
            **r,
            "email_full": r["email"],
            "phone_full": r["phone"],
            "email": _mask_email(r["email"]),
            "phone": _mask_phone(r["phone"]),
            "estoque_atual": estoque_atual_por_sku.get(r["sku"], (None, 0))[1],
            "presale_dias_extra": dias_extra_por_sku.get(r["sku"]),
            "reposicao_qtd": reposicao_por_sku.get(r["sku"], (None, None))[0],
            "reposicao_data": reposicao_por_sku.get(r["sku"], (None, None))[1],
        }
        for r in _compute_avise_me_atendido()
    ]

    reposicoes = [
        {**r, "estoque_atual": estoque_atual_por_sku.get(r["sku"], (None, 0))[1]}
        for r in load_json("reposicoes.json", [])
    ]
    controle_prazo = [
        {**r, "estoque_atual": estoque_atual_por_sku.get(r["sku"], (None, 0))[1]}
        for r in controle_prazo_raw
    ]

    estoque_deposito = load_json("estoque_deposito.json", {})
    expedicao = load_json("expedicao.json", {})

    payload = {
        "sales_rows": sales["rows"],
        "sales_names": sales["names"],
        "pageviews_rows": pageviews_rows,
        "stock_sku": stock_sku,
        "discounts": discounts,
        "orders_raw": orders_raw,
        "orders_geo": orders_geo,
        "sales_by_sku": sales_by_sku,
        "crosssell": crosssell,
        "avise_me": avise_me,
        "cupons_promocoes": load_json("cupons_promocoes.json", {}),
        "reposicoes": reposicoes,
        "controle_prazo": controle_prazo,
        "estoque_deposito": estoque_deposito,
        "expedicao": expedicao,
    }

    out = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(BASE_DIR, "dashboard_data.json"), "w", encoding="utf-8") as f:
        f.write(out)

    print(f"[dashboard_data] reconstruido: {len(out.encode('utf-8'))} bytes | "
          f"{len(orders_raw)} pedidos | {len(orders_geo)} com geo | {len(sales_by_sku)} skus com venda")


# ---------------- PARTE 4: MONTAR HTML FINAL ----------------

def _build_deposito_ilustracoes_json():
    """Le as 5 ilustracoes do nivel de ocupacao do deposito (assets_deposito/estagio-N.png)
    e monta um objeto {N: data-uri} pra embutir no HTML final (dashboard e um arquivo
    unico autocontido, sem referencia a arquivo externo)."""
    pasta = os.path.join(BASE_DIR, "assets_deposito")
    ilustracoes = {}
    for n in range(1, 6):
        path = os.path.join(pasta, f"estagio-{n}.png")
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ilustracoes[str(n)] = f"data:image/png;base64,{b64}"
    return json.dumps(ilustracoes, separators=(",", ":"))


def build_final_html():
    template = _read_asset_with_fallback("dashboard_template.html")
    chartjs = _read_asset_with_fallback("chart.umd.js")
    with open(os.path.join(BASE_DIR, "dashboard_data.json"), encoding="utf-8") as f:
        data_json = f.read()
    deposito_imgs_json = _build_deposito_ilustracoes_json()

    final = (
        template
        .replace("/*__CHARTJS__*/", chartjs)
        .replace("/*__DATA__*/", data_json)
        .replace("/*__DEPOSITO_IMGS__*/", deposito_imgs_json)
    )

    out_path = os.path.join(BASE_DIR, "curva-abc-perky-shoes.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(final)

    desktop_path = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop", "curva-abc-perky-shoes.html")
    try:
        with open(desktop_path, "w", encoding="utf-8") as f:
            f.write(final)
    except OSError:
        pass

    print(f"[html] dashboard final montado: {len(final.encode('utf-8'))} bytes -> {out_path}")
    return out_path


# ---------------- PARTE 5: PUBLICAR NO NETLIFY ----------------

def deploy_netlify():
    import zipfile

    with open(os.path.join(BASE_DIR, "netlify_config.json"), encoding="utf-8") as f:
        config = json.load(f)

    html_path = os.path.join(BASE_DIR, "curva-abc-perky-shoes.html")
    zip_path = os.path.join(BASE_DIR, "_netlify_deploy.zip")

    headers_content = "/*\n  Content-Type: text/html; charset=utf-8\n"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(html_path, arcname="index.html")
        zf.writestr("_headers", headers_content)

    with open(zip_path, "rb") as f:
        zip_bytes = f.read()

    url = f"https://api.netlify.com/api/v1/sites/{config['site_id']}/deploys"
    req = urllib.request.Request(
        url, data=zip_bytes, method="POST",
        headers={"Authorization": f"Bearer {config['token']}", "Content-Type": "application/zip"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())

    os.remove(zip_path)

    deploy_id = result.get("id")
    status_url = f"https://api.netlify.com/api/v1/sites/{config['site_id']}/deploys/{deploy_id}"
    state = result.get("state")
    for _ in range(20):
        time.sleep(2)
        req2 = urllib.request.Request(status_url, headers={"Authorization": f"Bearer {config['token']}"})
        with urllib.request.urlopen(req2) as resp2:
            status = json.loads(resp2.read().decode())
        state = status.get("state")
        if state in ("ready", "error"):
            break

    if state == "ready":
        print(f"[netlify] publicado com sucesso: {config['site_url']}")
    else:
        print(f"[netlify] deploy terminou com estado inesperado: {state}")


# ---------------- PARTE 6: PUBLICAR NO GITHUB PAGES ----------------

def deploy_github_pages():
    with open(os.path.join(BASE_DIR, "github_config.json"), encoding="utf-8") as f:
        config = json.load(f)
    config["token"] = os.environ.get("GITHUB_PAGES_TOKEN", config.get("token"))

    html_path = os.path.join(BASE_DIR, "curva-abc-perky-shoes.html")
    with open(html_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode()

    api_base = f"https://api.github.com/repos/{config['owner']}/{config['repo']}/contents/{config['path']}"
    gh_headers = {
        "Authorization": f"Bearer {config['token']}",
        "Accept": "application/vnd.github+json",
    }

    sha = None
    req = urllib.request.Request(f"{api_base}?ref={config['branch']}", headers=gh_headers)
    try:
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read().decode()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    body = {
        "message": f"Atualizacao automatica do dashboard - {date.today().isoformat()}",
        "content": content_b64,
        "branch": config["branch"],
    }
    if sha:
        body["sha"] = sha

    data = json.dumps(body).encode()
    req2 = urllib.request.Request(
        api_base, data=data, method="PUT",
        headers={**gh_headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req2) as resp2:
        resp2.read()

    print(f"[github pages] publicado com sucesso: {config['pages_url']}")


