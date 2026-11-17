"""
Escaneia os 5 grandes hubs hisec (Jita, Amarr, Dodixie, Rens, Hek) e lista as
combinações mais lucrativas para COMPRA E VENDA INSTANTÂNEA (pegando ordens
existentes, sem criar as suas).

Diferente de olhar só a melhor ordem, isso desce no livro de ofertas:
compra as unidades mais baratas disponíveis e vende nas ordens de compra
mais generosas, unidade por unidade, até o lucro daquela unidade zerar —
ou seja, a quantidade reportada é o máximo que **ainda dá lucro**, somando
várias ordens, não só a do topo.

Uso:
    python scanner.py
    python scanner.py --csv resultado.csv
"""
from __future__ import annotations

import argparse
import csv as csv_module
import sys
from dataclasses import dataclass, field

import config
import esi_client


@dataclass
class HubBook:
    hub: str
    station_id: int
    # type_id -> lista de [preço, volume_remain], ordens de venda em ordem
    # crescente de preço (a mais barata primeiro — é a que você compraria antes)
    sell_orders: dict[int, list[list[float]]] = field(default_factory=dict)
    # type_id -> lista de [preço, volume_remain, min_volume], ordens de compra
    # em ordem decrescente de preço (a mais generosa primeiro — é onde você
    # venderia antes). min_volume = menor lote que a ordem aceita.
    buy_orders: dict[int, list[list[float]]] = field(default_factory=dict)


def build_hub_book(hub: str, region_id: int, station_id: int) -> HubBook:
    print(f"Baixando ordens de {hub} (region {region_id})...")
    orders = esi_client.get_region_orders(region_id, order_type="all")
    station_orders = [o for o in orders if o["location_id"] == station_id]
    print(f"  {len(station_orders)} ordens na estação de {hub} (de {len(orders)} na região)")

    sell_orders: dict[int, list[list[float]]] = {}
    buy_orders: dict[int, list[list[float]]] = {}

    for o in station_orders:
        tid = o["type_id"]
        if o["is_buy_order"]:
            buy_orders.setdefault(tid, []).append([o["price"], o["volume_remain"], o.get("min_volume", 1)])
        else:
            sell_orders.setdefault(tid, []).append([o["price"], o["volume_remain"]])

    for lst in sell_orders.values():
        lst.sort(key=lambda e: e[0])              # mais barata primeiro
    for lst in buy_orders.values():
        lst.sort(key=lambda e: e[0], reverse=True)  # mais generosa primeiro

    return HubBook(hub=hub, station_id=station_id, sell_orders=sell_orders, buy_orders=buy_orders)


def _profitable_supply(sell: list[list[float]], i: int, buy_price: float, tax_rate: float, cap: float) -> float:
    """Unidades à venda (a partir de i) que ainda dão lucro vendendo a buy_price."""
    total = 0.0
    for price, qty in (e[:2] for e in sell[i:]):
        if buy_price * (1 - tax_rate) - price <= 0 or total >= cap:
            break
        total += qty
    return min(total, cap)


def walk_lots(sell_book: list[list[float]], buy_book: list[list[float]], tax_rate: float) -> list[dict]:
    """
    Casa ordens de venda (crescente) com ordens de compra (decrescente),
    unidade por unidade, enquanto ainda houver lucro. Cada "lote" é um
    trecho comprado a um preço e vendido a outro (preços mudam conforme
    se desce no livro). Para de casar assim que o próximo par não é mais
    lucrativo — como um lado sobe de preço e o outro desce, o lucro só
    piora a partir daí, então parar cedo é seguro.

    Ordens de compra com min_volume (3º campo) só são usadas se a oferta
    lucrativa disponível alcança esse mínimo; senão a ordem é pulada (no
    jogo, a venda seria recusada). Se a ordem tem menos unidades restantes
    que o mínimo, vale o que resta.
    """
    sell = [list(e) for e in sell_book]  # cópia, porque vamos consumir
    buy = [list(e) for e in buy_book]

    i = j = 0
    lots = []
    filled_j = 0  # unidades já vendidas na ordem de compra j

    while i < len(sell) and j < len(buy):
        sell_price, sell_qty = sell[i][0], sell[i][1]
        buy_price, buy_qty = buy[j][0], buy[j][1]

        unit_profit = buy_price * (1 - tax_rate) - sell_price
        if unit_profit <= 0:
            break

        if filled_j == 0 and len(buy[j]) > 2:
            min_vol = min(buy[j][2], buy_qty)
            if min_vol > 1 and _profitable_supply(sell, i, buy_price, tax_rate, buy_qty) < min_vol:
                j += 1
                continue

        qty = int(min(sell_qty, buy_qty))
        if qty <= 0:
            break

        lots.append(
            {
                "buy_price": sell_price,   # preço que você paga (comprando)
                "sell_price": buy_price,   # preço que você recebe (vendendo)
                "qty": qty,
                "unit_profit": unit_profit,
            }
        )

        sell[i][1] -= qty
        buy[j][1] -= qty
        filled_j += qty
        if sell[i][1] <= 0:
            i += 1
        if buy[j][1] <= 0:
            j += 1
            filled_j = 0

    return lots


def find_opportunities(books: list[HubBook], tax_rate: float) -> list[dict]:
    opportunities = []

    for buy_hub in books:
        for sell_hub in books:
            if buy_hub.hub == sell_hub.hub:
                continue  # arbitragem entre hubs diferentes (precisa haulear)

            for type_id, sell_book in buy_hub.sell_orders.items():
                buy_book = sell_hub.buy_orders.get(type_id)
                if not buy_book:
                    continue

                lots = walk_lots(sell_book, buy_book, tax_rate)
                if not lots:
                    continue

                total_qty = sum(l["qty"] for l in lots)
                total_cost = sum(l["qty"] * l["buy_price"] for l in lots)
                total_profit = sum(l["qty"] * l["unit_profit"] for l in lots)

                if total_profit < config.MIN_TOTAL_PROFIT:
                    continue

                margin_pct = (total_profit / total_cost) * 100 if total_cost > 0 else 0
                if margin_pct < config.MIN_MARGIN_PCT:
                    continue

                opportunities.append(
                    {
                        "type_id": type_id,
                        "buy_hub": buy_hub.hub,
                        "buy_price": total_cost / total_qty,       # preço médio pago
                        "sell_hub": sell_hub.hub,
                        "sell_price": lots[0]["sell_price"],       # melhor preço de venda (referência)
                        "profit_per_unit": total_profit / total_qty,
                        "margin_pct": margin_pct,
                        "max_qty_now": total_qty,
                        "max_profit_now": total_profit,
                    }
                )

    opportunities.sort(key=lambda o: o["max_profit_now"], reverse=True)
    return opportunities[: config.TOP_N_RESULTS]


def leg_lots(buy_hub: HubBook, sell_hub: HubBook, tax_rate: float, top_k_items: int = 40) -> dict[int, list[dict]]:
    """
    Pra cada item com arbitragem lucrativa entre os dois hubs, retorna os
    lotes (preço/qty/lucro por unidade) obtidos descendo no livro. Limita
    aos top_k_items por lucro total, pra não gastar tempo/API com itens
    irrelevantes no planejamento de circuito.
    """
    per_item = []
    for type_id, sell_book in buy_hub.sell_orders.items():
        buy_book = sell_hub.buy_orders.get(type_id)
        if not buy_book:
            continue
        lots = walk_lots(sell_book, buy_book, tax_rate)
        if not lots:
            continue
        total_profit = sum(l["qty"] * l["unit_profit"] for l in lots)
        per_item.append((type_id, total_profit, lots))

    per_item.sort(key=lambda t: t[1], reverse=True)
    return {type_id: lots for type_id, _, lots in per_item[:top_k_items]}


def plan_leg(
    buy_hub: HubBook,
    sell_hub: HubBook,
    tax_rate: float,
    cargo_m3: float,
    budget_isk: float,
    volumes: dict[int, float],
    top_k_items: int = 40,
) -> dict:
    """
    Monta o carregamento ótimo pra uma perna do circuito: desce no livro de
    cada item candidato, transforma cada lote em uma linha com densidade de
    lucro (lucro por m³), ordena TODOS os lotes de TODOS os itens juntos por
    densidade, e enche o cargo/budget pegando sempre o de maior densidade
    primeiro — ou seja, sempre que há lucro, pega o máximo daquele material
    antes de passar pro próximo, seguindo a ordem de "mais lucro por espaço".
    """
    items_lots = leg_lots(buy_hub, sell_hub, tax_rate, top_k_items=top_k_items)

    flat_lots = []
    for type_id, lots in items_lots.items():
        volume = volumes.get(type_id, 0)
        if volume <= 0:
            continue
        for lot in lots:
            flat_lots.append(
                {
                    "type_id": type_id,
                    "buy_price": lot["buy_price"],
                    "sell_price": lot["sell_price"],
                    "qty": lot["qty"],
                    "unit_profit": lot["unit_profit"],
                    "volume": volume,
                    "density": lot["unit_profit"] / volume,
                }
            )

    flat_lots.sort(key=lambda l: l["density"], reverse=True)

    remaining_cargo = cargo_m3
    remaining_budget = budget_isk
    consumed_by_item: dict[int, dict] = {}

    for lot in flat_lots:
        if remaining_cargo <= 0 or remaining_budget <= 0:
            break

        max_by_cargo = int(remaining_cargo // lot["volume"])
        max_by_budget = int(remaining_budget // lot["buy_price"]) if lot["buy_price"] > 0 else 0
        qty = min(lot["qty"], max_by_cargo, max_by_budget)

        if qty <= 0:
            continue

        cost = qty * lot["buy_price"]
        profit = qty * lot["unit_profit"]

        acc = consumed_by_item.setdefault(
            lot["type_id"],
            {"type_id": lot["type_id"], "qty": 0, "total_cost": 0.0, "total_profit": 0.0,
             "volume_each": lot["volume"], "total_volume": 0.0},
        )
        acc["qty"] += qty
        acc["total_cost"] += cost
        acc["total_profit"] += profit
        acc["total_volume"] += qty * lot["volume"]

        remaining_cargo -= qty * lot["volume"]
        remaining_budget -= cost

    items = []
    for acc in consumed_by_item.values():
        avg_buy = acc["total_cost"] / acc["qty"]
        avg_sell_net = (acc["total_profit"] + acc["total_cost"]) / acc["qty"]  # lucro líquido + custo = receita líquida média
        items.append(
            {
                "type_id": acc["type_id"],
                "qty": acc["qty"],
                "buy_price": avg_buy,
                "sell_price": avg_sell_net,
                "volume_each": acc["volume_each"],
                "total_volume": acc["total_volume"],
                "total_cost": acc["total_cost"],
                "total_profit": acc["total_profit"],
            }
        )
    items.sort(key=lambda i: i["total_profit"], reverse=True)

    return {
        "from": buy_hub.hub,
        "to": sell_hub.hub,
        "items": items,
        "cargo_used": cargo_m3 - remaining_cargo,
        "cargo_total": cargo_m3,
        "isk_spent": budget_isk - remaining_budget,
        "isk_budget": budget_isk,
        "leg_profit": sum(i["total_profit"] for i in items),
    }


def all_pair_legs(
    books: list[HubBook],
    hubs: list[str],
    tax_rate: float,
    cargo_m3: float,
    budget_isk: float,
    volumes: dict[int, float],
    top_k_items: int = 40,
) -> dict[tuple[str, str], dict]:
    """Calcula plan_leg pra TODOS os pares direcionados (a != b) dentro de hubs."""
    book_by_hub = {b.hub: b for b in books}
    legs = {}
    for a in hubs:
        for b in hubs:
            if a == b:
                continue
            legs[(a, b)] = plan_leg(
                book_by_hub[a], book_by_hub[b], tax_rate, cargo_m3, budget_isk, volumes, top_k_items
            )
    return legs


def best_circuit_order(
    hubs: list[str],
    leg_profit: dict[tuple[str, str], float],
    jump_count: dict[tuple[str, str], int],
    jump_penalty_isk: float = 0.0,
) -> tuple[list[str], float, int]:
    """
    Testa todas as ordens possíveis do loop (fixando o primeiro hub da
    lista como ponto de partida — não muda o resultado, é só rotação do
    mesmo ciclo) e escolhe a que maximiza:

        score = soma(lucro de cada perna) - jump_penalty_isk * soma(jumps)

    Com jump_penalty_isk = 0, isso vira puramente "ordena pelo mais
    lucrativo". Só 5 hubs -> no máximo 4! = 24 combinações, então força
    bruta é instantâneo.
    """
    from itertools import permutations

    if len(hubs) < 2:
        return hubs, 0.0, 0

    start, rest = hubs[0], hubs[1:]
    best_order = None
    best_score = float("-inf")
    best_total_profit = 0.0
    best_total_jumps = 0

    for perm in permutations(rest):
        order = [start] + list(perm)
        total_profit = 0.0
        total_jumps = 0
        for i in range(len(order)):
            a, b = order[i], order[(i + 1) % len(order)]
            total_profit += leg_profit.get((a, b), 0.0)
            total_jumps += jump_count.get(tuple(sorted((a, b))), 0)

        score = total_profit - jump_penalty_isk * total_jumps
        if score > best_score:
            best_score = score
            best_order = order
            best_total_profit = total_profit
            best_total_jumps = total_jumps

    return best_order, best_total_profit, best_total_jumps


def plan_circuit(
    books: list["HubBook"],
    hub_order: list[str],
    tax_rate: float,
    cargo_m3: float,
    budget_isk: float,
    volumes: dict[int, float],
    top_k_items: int = 40,
) -> list[dict]:
    """
    hub_order define a sequência do loop (ex: ["Jita","Amarr","Dodixie",
    "Rens","Hek"]) — o circuito volta pro primeiro no final automaticamente.
    """
    book_by_hub = {b.hub: b for b in books}
    legs = []

    for i in range(len(hub_order)):
        hub_a = hub_order[i]
        hub_b = hub_order[(i + 1) % len(hub_order)]
        leg = plan_leg(
            book_by_hub[hub_a], book_by_hub[hub_b], tax_rate, cargo_m3, budget_isk, volumes, top_k_items
        )
        legs.append(leg)

    return legs


def print_report(opportunities: list[dict], names: dict[int, str]) -> None:
    if not opportunities:
        print("\nNenhuma oportunidade encontrada com os filtros atuais "
              "(MIN_TOTAL_PROFIT / MIN_MARGIN_PCT em config.py).")
        return

    header = (
        f"{'Item':<38} | {'Compra':<9} | {'Preço médio':>16} | {'Venda':<9} | "
        f"{'Melhor preço':>16} | {'Lucro/un':>14} | {'Margem':>7} | {'Qtd total':>9} | {'Lucro pot.':>16}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    for o in opportunities:
        name = names.get(o["type_id"], f"type_id {o['type_id']}")[:37]
        row = (
            f"{name:<38} | {o['buy_hub']:<9} | {o['buy_price']:>16,.2f} | "
            f"{o['sell_hub']:<9} | {o['sell_price']:>16,.2f} | {o['profit_per_unit']:>14,.2f} | "
            f"{o['margin_pct']:>6.1f}% | {o['max_qty_now']:>9} | {o['max_profit_now']:>16,.0f}"
        )
        print(row)

    print("\nObs: 'Qtd total' já soma várias ordens do livro de ofertas — é o máximo "
          "que ainda dá lucro nesse momento, não só a melhor ordem.")
    print("Isso é um retrato do momento: preços de mercado mudam a cada minuto.")


def write_csv(path: str, opportunities: list[dict], names: dict[int, str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv_module.DictWriter(
            f,
            fieldnames=[
                "item", "type_id", "buy_hub", "buy_price", "sell_hub", "sell_price",
                "profit_per_unit", "margin_pct", "max_qty_now", "max_profit_now",
            ],
        )
        writer.writeheader()
        for o in opportunities:
            row = dict(o)
            row["item"] = names.get(o["type_id"], f"type_id {o['type_id']}")
            writer.writerow(row)
    print(f"\nCSV salvo em {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="Caminho para salvar o resultado em CSV", default=None)
    parser.add_argument(
        "--accounting", type=int, default=config.ACCOUNTING_SKILL_LEVEL,
        help="Nível da skill Accounting (0 para Alpha, que não pode treiná-la)",
    )
    args = parser.parse_args()

    tax_rate = config.sales_tax_rate(args.accounting)
    print(f"Sales tax considerado: {tax_rate*100:.3f}% (Accounting nível {args.accounting})")

    books = []
    for hub, cfg in config.HUBS.items():
        book = build_hub_book(hub, cfg["region_id"], cfg["station_id"])
        books.append(book)

    print("\nCalculando oportunidades de arbitragem (compra/venda instantânea)...")
    opportunities = find_opportunities(books, tax_rate)

    all_type_ids = {o["type_id"] for o in opportunities}
    names = esi_client.resolve_type_names(all_type_ids) if all_type_ids else {}

    print_report(opportunities, names)

    if args.csv:
        write_csv(args.csv, opportunities, names)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
