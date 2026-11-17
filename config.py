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

# --- SSO (EVE Online OAuth2 + PKCE) ----------------------------------------
# Registre o app em https://developers.eveonline.com com:
#   callback: http://127.0.0.1:5000/callback
#   escopos:  TODOS os de SSO_SCOPES abaixo (o login falha se pedir um
#             escopo que não foi registrado no app)
# e coloque o client ID em local_settings.json → "client_id" (ou na variável
# de ambiente EVE_CLIENT_ID). Com PKCE não existe client secret.
SSO_CALLBACK_URL = "http://127.0.0.1:5000/callback"
# Client ID padrão do app (o do projeto). Com PKCE ele NÃO é segredo — pode
# ficar versionado: quem rodar o app só clica em "Entrar com EVE Online".
# local_settings.json → "client_id" ou a variável EVE_CLIENT_ID sobrescrevem.
SSO_CLIENT_ID = "1051d7874ada41baa0997147d39179a1"   # app "logistica marketing"
SSO_SCOPES = [
    "esi-markets.structure_markets.v1",     # livro de ofertas de estruturas
    "esi-universe.read_structures.v1",      # nome/sistema/dono da estrutura
    "esi-search.search_structures.v1",      # descoberta por busca
    "esi-corporations.read_structures.v1",  # estruturas da corp (cargo Station Manager)
    "esi-characters.read_contacts.v1",      # standings pessoais
    "esi-corporations.read_contacts.v1",    # standings da corp
    "esi-alliances.read_contacts.v1",       # standings da aliança
    "esi-location.read_location.v1",        # sistema/região onde o personagem está
    "esi-location.read_ship_type.v1",       # nave atual (alcance de jump freighter, Fase 2)
    "esi-skills.read_skills.v1",            # skills de jump drive (Fase 2)
    "esi-assets.read_assets.v1",            # estruturas onde há assets
]

# --- Teste de acesso a estruturas -------------------------------------------
ACCESS_TTL_OK = 6 * 3600          # acesso confirmado: revalida a cada 6h
ACCESS_TTL_DENIED = 24 * 3600     # 403: não tenta de novo por 24h
ACCESS_TTL_GONE = 7 * 24 * 3600   # 404: estrutura sumiu/destruída
# Quantos erros (403/404) o teste pode gastar por janela do error limit da
# ESI (100/min). Deixa folga pro resto do app.
ACCESS_MAX_ERRORS_PER_WINDOW = 40
