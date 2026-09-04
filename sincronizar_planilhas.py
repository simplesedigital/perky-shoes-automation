import traceback
from datetime import datetime
from dashboard_lib import r2_pull_all, r2_push_all, fetch_avise_me, fetch_reposicoes_prazo, fetch_calendario_crm, sync_handling_days, build_dashboard_data, build_final_html, deploy_github_pages, log_line

if __name__ == "__main__":
    print(f"=== Sincronizacao de planilhas iniciada: {datetime.now().isoformat(timespec='minutes')} ===")
    log_line("sincronizar_planilhas", "=== iniciada ===")
    try:
        r2_pull_all()
        fetch_avise_me()
        fetch_reposicoes_prazo()
        fetch_calendario_crm()
        sync_handling_days()
        build_dashboard_data()
        build_final_html()
        r2_push_all()
        deploy_github_pages()
        print("=== Sincronizacao de planilhas concluida ===")
        log_line("sincronizar_planilhas", "=== concluida com sucesso ===")
    except Exception as e:
        log_line("sincronizar_planilhas", f"ERRO: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
