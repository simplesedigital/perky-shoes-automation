import traceback
from datetime import datetime
from dashboard_lib import r2_pull_all, r2_push_all, sync_webhook_events, fetch_reposicoes_prazo, sync_handling_days, compute_expedicao, build_dashboard_data, build_final_html, deploy_github_pages, log_line

if __name__ == "__main__":
    print(f"=== Sincronizacao via webhook iniciada: {datetime.now().isoformat(timespec='minutes')} ===")
    log_line("sincronizar_pedidos", "=== iniciada ===")
    try:
        r2_pull_all()
        sync_webhook_events()
        fetch_reposicoes_prazo()
        sync_handling_days()
        compute_expedicao()
        build_dashboard_data()
        build_final_html()
        r2_push_all()
        deploy_github_pages()
        print("=== Sincronizacao via webhook concluida ===")
        log_line("sincronizar_pedidos", "=== concluida com sucesso ===")
    except Exception as e:
        log_line("sincronizar_pedidos", f"ERRO: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
