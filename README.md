# EVE Market Scanner — Arbitragem hisec (compra/venda instantânea)

Escaneia os 5 grandes hubs hisec (Jita, Amarr, Dodixie, Rens, Hek) usando a
**ESI oficial da CCP** (dados de mercado são públicos, sem autenticação) e
lista os itens mais lucrativos para comprar barato num hub, haulear e
vender direto (instant-sell) em outro.

## Sobre o EULA

Isso usa exclusivamente a API pública e documentada da CCP
(`esi.evetech.net`) — o mesmo mecanismo usado por ferramentas conhecidas
como EVE Trade, Adam4EVE, etc. Não lê memória do jogo, não automatiza
nada dentro do cliente, só consulta dados de mercado que a própria CCP
disponibiliza para isso.

## Instalação

```bash
pip install -r requirements.txt
```

## Interface web local

```bash
python webapp.py
```

Abre um servidor **só em `127.0.0.1` (sua máquina, ninguém de fora acessa)**.
Abra `http://127.0.0.1:5000` no navegador. A página tem:

1. Botão **"Escanear mercado"** — dispara o scan em segundo plano no
   servidor e vai atualizando o status a cada segundo, até preencher a
   tabela de oportunidades.
2. Clique numa linha da tabela pra selecioná-la.
3. Digite seu **sistema de origem** (fica salvo em `gui_settings.json`
   pra próxima vez), marque/desmarque "evitar low-sec" e clique em
   **"Calcular rotas"**.
4. Mostra as duas rotas: **até o hub de compra** (de onde você está) e
   **do hub de compra até o hub de venda**.

### Circuito de trade (loop contínuo)

Depois de rodar um scan, use o painel "Circuito de trade" pra planejar
uma volta completa pelos hubs, comprando e vendendo em cada perna:

- **Ordem dos hubs**: lista separada por vírgula (ex:
  `Jita,Amarr,Dodixie,Rens,Hek`). O circuito fecha automaticamente —
  a última perna sempre volta pro primeiro hub da lista.
- **Cargo (m³)**: capacidade de carga do seu hauler. Pode ser o valor
  puro do cargo hold, ou já somado com expansores/riggeamento se for
  esse o fit que você for usar.
- **Budget (ISK)**: quanto você tem disponível pra girar em cada
  perna (não é total do circuito — é reaplicado a cada trecho, já que
  o dinheiro da venda da perna anterior vira o budget da próxima).

O algoritmo escolhe os itens por **densidade de lucro (ISK de lucro
por m³)** — enche o cargo com o que dá mais lucro por espaço primeiro,
respeitando tanto o limite de volume quanto o de ISK e a quantidade
realmente disponível nas ordens do mercado.

### Otimizar a ordem automaticamente (lucro × distância)

Marque **"Otimizar ordem automaticamente"** e o campo "Ordem dos hubs"
vira "Hubs a incluir" — digite quais entram, em qualquer ordem. O
sistema testa todas as combinações possíveis do loop (com 5 hubs são só
24 combinações, instantâneo) e escolhe a que maximiza:

```
score = lucro_total_do_circuito - penalidade_por_jump * jumps_totais
```

Com a **penalidade em 0** (padrão), ele ordena puramente pelo hub mais
lucrativo em cada trecho, ignorando distância. Suba a penalidade (ex:
500.000 ISK/jump) pra ele passar a preferir circuitos mais compactos
mesmo abrindo mão de um pouco de lucro. A distância em jumps entre cada
par de hubs é calculada uma vez via ESI e cacheada (`distance_cache.json`).

Deixa essa aba aberta e o EVE em modo janela ao lado — sem nenhuma
integração com o cliente do jogo, é só uma página consultando a mesma
ESI pública.

## Uso via linha de comando

```bash
python scanner.py
```

Isso baixa as ordens dos 5 hubs (pode levar um tempinho — Jita sozinho tem
dezenas de milhares de ordens), calcula margens e imprime uma tabela tipo:

```
Item                                   | Compra    |     Preço médio | Venda     |     Melhor preço |       Lucro/un | Margem | Qtd total |       Lucro pot.
Tritanium                              | Jita      |            5.35 | Rens      |             6.10 |            0.55 |  10.2% |     42000 |          23,100
...
```

Salvar em CSV também:

```bash
python scanner.py --csv oportunidades.csv
```

## Como o lucro é calculado (modo instantâneo)

Como você **não cria ordens** — só pega ordens de venda existentes pra
comprar e ordens de compra existentes pra vender — não há broker fee em
nenhuma das pontas. Só entra a **sales tax**, cobrada na venda:

```
sales_tax_rate = 7.5% * (1 - 0.11 * nível_de_Accounting)
lucro_por_unidade = preço_venda * (1 - sales_tax_rate) - preço_compra
```

Por padrão o script assume **Accounting nível 0** (conta Alpha, que não
pode treinar essa skill). Se sua conta for Omega e tiver a skill treinada:

```bash
python scanner.py --accounting 5
```

### Quantidade: soma profundidade real do livro de ofertas

Em vez de olhar só a melhor ordem de cada lado, o cálculo **desce no
livro**: casa as ordens de venda mais baratas (comprar) com as ordens de
compra mais generosas (vender), unidade por unidade, até o lucro daquela
unidade específica zerar. A "Qtd total" na tabela já é essa soma — o
máximo que ainda dá lucro nesse instante, não só o topo do book. O mesmo
vale pro planejador de circuito: ele monta uma lista de "lotes" (preço +
quantidade) de todos os itens candidatos, ordena tudo por **lucro por
m³** e enche o cargo/budget pegando sempre a maior densidade disponível
primeiro — sempre que há lucro, pega o máximo daquele material antes de
passar pro próximo.

## Limitações conhecidas (próximos passos)

- **Preço "agora" muda rápido.** Isso é uma foto do momento; convém rodar
  de novo antes de sair fazendo a rota.
- Modo "criar suas próprias ordens" (station trading) não está implementado
  — se quiser, dá pra adicionar calculando também broker fee de cada lado.
- A rota "evitar low-sec" pode falhar se não existir caminho 100% hisec
  entre dois sistemas — nesse caso a ESI retorna erro, que aparece na
  caixa de rota da interface.
- O planejador de circuito não recalcula rota/jumps entre as pernas —
  ele assume que você vai viajar do hub A pro hub B, sem otimizar a
  ordem de visita por distância. Se quiser, dá pra integrar com o
  `/api/route` pra escolher automaticamente a ordem que minimiza jumps.
- O budget é reaplicado igual em cada perna (não acumula o lucro da
  perna anterior automaticamente) — na prática seu ISK disponível cresce
  a cada volta, então o budget informado deveria ser atualizado conforme
  o circuito avança.
