import traceback
from datetime import date
from dashboard_lib import r2_pull_all, r2_push_all, sync_webhook_events, sync_orders, snapshot_estoque_desconto, fetch_product_pageviews, fetch_avise_me, fetch_reposicoes_prazo, sync_handling_days, compute_estoque_deposito, compute_expedicao, build_dashboard_data, build_final_html, deploy_github_pages, log_line

if __name__ == "__main__":
    print(f"=== Atualizacao diaria (pedidos + estoque + desconto) iniciada: {date.today().isoformat()} ===")
    log_line("atualizacao_diaria", "=== iniciada ===")
    try:
        r2_pull_all()
        sync_webhook_events()
        sync_orders()
        snapshot_estoque_desconto()
        fetch_product_pageviews()
        fetch_avise_me()
        fetch_reposicoes_prazo()
        sync_handling_days()
        compute_estoque_deposito()
        compute_expedicao()
        build_dashboard_data()
        build_final_html()
        r2_push_all()
        deploy_github_pages()
        print("=== Atualizacao diaria concluida ===")
        log_line("atualizacao_diaria", "=== concluida com sucesso ===")
    except Exception as e:
        log_line("atualizacao_diaria", f"ERRO: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
