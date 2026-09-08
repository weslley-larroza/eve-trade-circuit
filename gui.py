"""
Interface gráfica do EVE Market Scanner.

Fluxo:
1. Clique em "Escanear mercado" -> baixa ordens dos 5 hubs e lista as
   melhores oportunidades de arbitragem instantânea numa tabela.
2. Selecione uma linha da tabela.
3. Informe seu sistema de origem (lembrado entre execuções) e clique em
   "Calcular rotas" -> mostra a rota até o hub de compra e, a partir dele,
   até o hub de venda.

Roda como janela separada — nada de overlay ou integração com o cliente
do EVE. Alt+Tab entre esta janela e o jogo.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import config
import esi_client
import scanner

SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "gui_settings.json")


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"origin_system": "Jita", "avoid_lowsec": True}


def save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)


class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EVE Market Scanner")
        self.geometry("1150x650")

        self.settings = load_settings()
        self.opportunities: list[dict] = []
        self.names: dict[int, str] = {}
        self._work_queue: queue.Queue = queue.Queue()

        self._build_widgets()
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Nível Accounting:").pack(side="left")
        self.accounting_var = tk.IntVar(value=config.ACCOUNTING_SKILL_LEVEL)
        ttk.Spinbox(top, from_=0, to=5, width=3, textvariable=self.accounting_var).pack(
            side="left", padx=(4, 16)
        )

        self.scan_btn = ttk.Button(top, text="Escanear mercado", command=self._start_scan)
        self.scan_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Pronto.")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=12)

        # --- Tabela de oportunidades ---
        columns = ("item", "buy_hub", "buy_price", "sell_hub", "sell_price",
                   "profit", "margin", "qty", "potential")
        headers = {
            "item": "Item", "buy_hub": "Compra", "buy_price": "Preço compra",
            "sell_hub": "Venda", "sell_price": "Preço venda", "profit": "Lucro/un",
            "margin": "Margem", "qty": "Qtd", "potential": "Lucro potencial",
        }
        widths = {
            "item": 260, "buy_hub": 70, "buy_price": 110, "sell_hub": 70,
            "sell_price": 110, "profit": 100, "margin": 70, "qty": 60, "potential": 120,
        }

        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=18)
        for col in columns:
            self.tree.heading(col, text=headers[col])
            anchor = "w" if col == "item" else "e"
            self.tree.column(col, width=widths[col], anchor=anchor)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # --- Painel de rota ---
        route_frame = ttk.LabelFrame(self, text="Rota", padding=8)
        route_frame.pack(fill="x", padx=8, pady=(0, 8))

        ttk.Label(route_frame, text="Sistema de origem:").grid(row=0, column=0, sticky="w")
        self.origin_var = tk.StringVar(value=self.settings.get("origin_system", "Jita"))
        ttk.Entry(route_frame, textvariable=self.origin_var, width=20).grid(
            row=0, column=1, padx=6
        )

        self.avoid_lowsec_var = tk.BooleanVar(value=self.settings.get("avoid_lowsec", True))
        ttk.Checkbutton(
            route_frame, text="Evitar low-sec/null-sec", variable=self.avoid_lowsec_var
        ).grid(row=0, column=2, padx=12)

        self.route_btn = ttk.Button(
            route_frame, text="Calcular rotas", command=self._start_route, state="disabled"
        )
        self.route_btn.grid(row=0, column=3, padx=12)

        self.selected_label = ttk.Label(route_frame, text="Nenhum item selecionado.")
        self.selected_label.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

        self.route_text = tk.Text(route_frame, height=6, width=120, wrap="word")
        self.route_text.grid(row=2, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.route_text.configure(state="disabled")

    # ------------------------------------------------------------- scan
    def _start_scan(self) -> None:
        self.scan_btn.configure(state="disabled")
        self.status_var.set("Escaneando... (isso pode levar alguns minutos, Jita é grande)")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            tax_rate = config.sales_tax_rate(self.accounting_var.get())
            books = []
            for hub, cfg in config.HUBS.items():
                self._work_queue.put(("status", f"Baixando ordens de {hub}..."))
                book = scanner.build_hub_book(hub, cfg["region_id"], cfg["station_id"])
                books.append(book)

            self._work_queue.put(("status", "Calculando oportunidades..."))
            opportunities = scanner.find_opportunities(books, tax_rate)

            all_type_ids = {o["type_id"] for o in opportunities}
            names = esi_client.resolve_type_names(all_type_ids) if all_type_ids else {}

            self._work_queue.put(("scan_done", (opportunities, names)))
        except Exception as exc:  # noqa: BLE001 - queremos mostrar qualquer erro na UI
            self._work_queue.put(("error", str(exc)))

    def _populate_table(self, opportunities: list[dict], names: dict[int, str]) -> None:
        self.opportunities = opportunities
        self.names = names
        self.tree.delete(*self.tree.get_children())

        for i, o in enumerate(opportunities):
            name = names.get(o["type_id"], f"type_id {o['type_id']}")
            self.tree.insert(
                "", "end", iid=str(i),
                values=(
                    name, o["buy_hub"], f"{o['buy_price']:,.2f}",
                    o["sell_hub"], f"{o['sell_price']:,.2f}",
                    f"{o['profit_per_unit']:,.2f}", f"{o['margin_pct']:.1f}%",
                    o["max_qty_now"], f"{o['max_profit_now']:,.0f}",
                ),
            )

    # ------------------------------------------------------------ seleção
    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        o = self.opportunities[idx]
        name = self.names.get(o["type_id"], f"type_id {o['type_id']}")
        self.selected_label.configure(
            text=f"Selecionado: {name} — comprar em {o['buy_hub']}, vender em {o['sell_hub']}"
        )
        self.route_btn.configure(state="normal")

    # -------------------------------------------------------------- rota
    def _start_route(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        o = self.opportunities[idx]

        origin_name = self.origin_var.get().strip()
        avoid_lowsec = self.avoid_lowsec_var.get()

        self.settings["origin_system"] = origin_name
        self.settings["avoid_lowsec"] = avoid_lowsec
        save_settings(self.settings)

        self.route_btn.configure(state="disabled")
        self._set_route_text("Calculando rotas...")
        threading.Thread(
            target=self._route_worker, args=(origin_name, o, avoid_lowsec), daemon=True
        ).start()

    def _route_worker(self, origin_name: str, opp: dict, avoid_lowsec: bool) -> None:
        try:
            origin_id = esi_client.resolve_system_id(origin_name)
            buy_system_id = config.HUBS[opp["buy_hub"]]["system_id"]
            sell_system_id = config.HUBS[opp["sell_hub"]]["system_id"]

            leg1 = esi_client.get_route(origin_id, buy_system_id, avoid_lowsec)
            leg2 = esi_client.get_route(buy_system_id, sell_system_id, avoid_lowsec)

            all_ids = list(dict.fromkeys(leg1 + leg2))
            system_names = esi_client.resolve_type_names(all_ids)

            self._work_queue.put(("route_done", (leg1, leg2, system_names, opp)))
        except Exception as exc:  # noqa: BLE001
            self._work_queue.put(("route_error", str(exc)))

    def _set_route_text(self, text: str) -> None:
        self.route_text.configure(state="normal")
        self.route_text.delete("1.0", "end")
        self.route_text.insert("1.0", text)
        self.route_text.configure(state="disabled")

    # -------------------------------------------------------------- fila
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._work_queue.get_nowait()

                if kind == "status":
                    self.status_var.set(payload)

                elif kind == "scan_done":
                    opportunities, names = payload
                    self._populate_table(opportunities, names)
                    self.status_var.set(f"{len(opportunities)} oportunidade(s) encontrada(s).")
                    self.scan_btn.configure(state="normal")

                elif kind == "error":
                    self.status_var.set("Erro no scan.")
                    self.scan_btn.configure(state="normal")
                    messagebox.showerror("Erro", payload)

                elif kind == "route_done":
                    leg1, leg2, system_names, opp = payload
                    self._render_routes(leg1, leg2, system_names, opp)
                    self.route_btn.configure(state="normal")

                elif kind == "route_error":
                    self.route_btn.configure(state="normal")
                    self._set_route_text(f"Erro ao calcular rota: {payload}")

        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def _render_routes(self, leg1: list[int], leg2: list[int], names: dict[int, str], opp: dict) -> None:
        def fmt(leg: list[int]) -> str:
            path = " -> ".join(names.get(sid, str(sid)) for sid in leg)
            return f"{len(leg) - 1} jump(s): {path}"

        text = (
            f"Trecho 1 — até {opp['buy_hub']} (comprar):\n{fmt(leg1)}\n\n"
            f"Trecho 2 — {opp['buy_hub']} até {opp['sell_hub']} (vender):\n{fmt(leg2)}"
        )
        self._set_route_text(text)


if __name__ == "__main__":
    app = ScannerApp()
    app.mainloop()
