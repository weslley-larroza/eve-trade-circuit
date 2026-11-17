"""
Configuração dos 5 grandes hubs hisec e dos parâmetros fiscais da conta.

Os station_id/region_id abaixo são IDs numéricos fixos, estáveis e
publicamente documentados (os mesmos usados por ferramentas como
Adam4EVE e EVE Trade) — não dependem de resolução de nome via ESI, que
não funciona de forma confiável para estações NPC.
"""

HUBS = {
    "Jita": {
        "region_id": 10000002,        # The Forge
        "station_id": 60003760,       # Jita IV - Moon 4 - Caldari Navy Assembly Plant
        "system_id": 30000142,
    },
    "Amarr": {
        "region_id": 10000043,        # Domain
        "station_id": 60008494,       # Amarr VIII (Oris) - Emperor Family Academy
        "system_id": 30002187,
    },
    "Dodixie": {
        "region_id": 10000032,        # Sinq Laison
        "station_id": 60011866,       # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
        "system_id": 30002659,
    },
    "Rens": {
        "region_id": 10000030,        # Heimatar
        "station_id": 60004588,       # Rens VI - Moon 8 - Brutor Tribe Treasury
        "system_id": 30002510,
    },
    "Hek": {
        "region_id": 10000042,        # Metropolis
        "station_id": 60005686,       # Hek VIII - Moon 12 - Boundless Creation Factory
        "system_id": 30002053,
    },
}

# --- Parâmetros fiscais ---------------------------------------------------
# No modo "sempre instantâneo" (comprar de ordem de venda existente e
# vender direto numa ordem de compra existente) NÃO há broker fee em
# nenhuma das pontas — só sales tax na venda.
#
# Sales tax = 7.5% * (1 - 0.11 * nível de Accounting)
# Conta Alpha não pode treinar Accounting -> nível 0 -> 7.5% cheio.
ACCOUNTING_SKILL_LEVEL = 0          # 0 para Alpha (não treinável)
SALES_TAX_BASE = 0.075
SALES_TAX_REDUCTION_PER_LEVEL = 0.11

def sales_tax_rate(accounting_level: int = ACCOUNTING_SKILL_LEVEL) -> float:
    if not 0 <= int(accounting_level) <= 5:
        raise ValueError(f"Nível de Accounting deve ser 0–5 (recebido: {accounting_level}).")
    return SALES_TAX_BASE * (1 - SALES_TAX_REDUCTION_PER_LEVEL * accounting_level)

# --- Filtros do scan -------------------------------------------------------
MIN_TOTAL_PROFIT = 1000          # ISK — lucro total mínimo do achado (todas as unidades)
MIN_MARGIN_PCT = 8.0             # % líquido mínimo pra listar
TOP_N_RESULTS = 30

# --- ESI ------------------------------------------------------------------
APP_VERSION = "0.2.0"
# Enviada em todo request (X-Compatibility-Date): congela o formato das
# respostas. Só atualize depois de conferir o changelog da ESI.
ESI_COMPATIBILITY_DATE = "2025-12-16"

# --- Locais / estruturas -----------------------------------------------------
# Política para estruturas RED (inimigas onde você ainda tem docking):
#   "reference_only" → aparecem só como referência de preço; vêm desmarcadas
#                      (não contam como origem/destino). Padrão.
#   "pass_through"   → vêm marcadas, sempre com aviso de risco.
# Em qualquer caso o usuário pode marcar/desmarcar cada local na interface.
RED_STRUCTURE_POLICY = "reference_only"

# O otimizador testa (n-1)! ordens; acima disso fica lento demais.
MAX_OPTIMIZE_LOCATIONS = 8
