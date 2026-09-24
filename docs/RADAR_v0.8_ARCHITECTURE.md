# LOCAL CRYPTO RADAR — Proposta arquitetural v0.8

> HISTORICAL PROPOSAL: retained for provenance. Current implementation and accepted design are in ../ARCHITECTURE.md; delivery status is in ../ROADMAP.md. The original implementation status, cloud direction and phase numbering below are not current authority.

Base: leitura integral de `radar_v0.7.py` (705 linhas). Estado: proposta, sem código.
Documentação Kraken consultada a 13 set 2026 para confirmar semântica de campos (Ticker Spot, Tickers Futures, Historical Funding Rates).

---

## 0. Resumo executivo (ler isto se mais nada)

A v0.7 tem três defeitos que nenhum ajuste de threshold resolve:

1. **O pré-filtro escolhe os 35 candidatos pelo critério errado.** `prefilter_score` é dominado por |variação 24h| (até 22 pontos) e por liquidez/spread (até 22 pontos). Um ativo grande, líquido e parado (ARB, HYPE) entra sempre nos 35; um ativo a acelerar agora mas plano em 24h pode nunca chegar à fase OHLC. O ranking "fraco" nasce aqui, não em `score_candidate`.
2. **Nada é normalizado pela volatilidade do próprio ativo.** +1% em 5m no BTC e +1% em 5m na LSK (depois de +197%) valem os mesmos 7 pontos. Sem z-scores/ATR, "movimento interessante" e "ruído normal daquele ativo" são indistinguíveis.
3. **Não há estado entre execuções.** Cada run parte do zero: não sabe o que era o mercado há 5 min, não sabe que já alertou PUMP no ciclo anterior, não sabe se o OI subiu ou desceu. Sem memória curta não há "movimento agora vs antigo", não há OI delta, não há dedupe, e não há forma de calibrar thresholds.

A v0.8 resolve os três com uma peça nova: **um snapshot store local (SQLite) alimentado pelo Ticker global a cada ciclo.** Com isso, momentum 5m/15m/1h e intensidade de volume passam a ser calculáveis para TODOS os mercados com zero pedidos extra (o Ticker devolve `v[0]` = volume desde 00:00 UTC; a diferença entre dois snapshots é o volume exato do intervalo). O OHLC passa a ser usado só na shortlist, e o order book só nos finalistas.

Além disso há bugs concretos (secção 1.6), dos quais dois são graves: BTC e DOGE **nunca** fazem match com Futures (XBT/XDG na Spot vs BTC/DOGE na Futures), e o "24h change" é na verdade "variação desde 00:00 UTC".

**Veredicto: BUILD v0.8**, em fases, com a fase 1 a correr em sombra ao lado da v0.7. Detalhe na secção 16.

---

## 1. Review do código real (v0.7)

### 1.1 Arquitetura atual

Script único, sequencial, sem estado, sem cache, sem persistência de output (só `print`). Cada execução:

```
AssetPairs (1 req) + Ticker all (1 req)
  → is_crypto()            filtro estático quote/base/status
  → prefilter_score()      ranking por 24h/volume/spread/range      → top 35
  → spot_ohlc() ×35        OHLC 5m sequencial (720 barras cada)     ← ~60% do tempo
  → futures_tickers (1 req) → index_perpetuals() → enrich_futures()
  → score_candidate()      score 0–100 aditivo
  → build_qwen_payload()   score ≥ 25, máx 12
  → qwen_review()          1 chamada Ollama, format=json
  → print
```

### 1.2 Endpoints usados

| Endpoint | Auth | Uso | Custo/ciclo |
|---|---|---|---|
| `GET /0/public/AssetPairs` | não | metadados | 1 (poderia ser cache diário) |
| `GET /0/public/Ticker` (sem pair) | não | ~1000 pares num só payload | 1 |
| `GET /0/public/OHLC?pair=&interval=5` | não | 5m/15m/1h + volume 15m/1h | 35 sequenciais, ~50 KB cada |
| `GET /derivatives/api/v3/tickers` | não | todos os PF_ | 1 |
| `POST localhost:11434/api/generate` | local | Qwen | 1 |

Nenhum endpoint privado, nenhuma chave lida. Segurança confirmada (secção 14).

### 1.3 Onde está o custo

Dos ~15 s: ~1-2 s nos dois tickers, **~8-10 s nos 35 OHLC sequenciais** (sem `requests.Session`, sem keep-alive, sem `since`, 720 barras = 60 h de dados por par quando só se usam 12), ~3-5 s no Qwen. O OHLC é o único custo que escala com o número de candidatos e é exatamente o que a shortlist errada desperdiça.

### 1.4 Fragilidades e assumptions

- `except Exception: continue` na fase OHLC apaga o candidato silenciosamente. Um 429 da Kraken a meio faz desaparecer metade da shortlist sem aviso.
- `quote_to_usd` usa EUR×1.16 hardcoded. O mesmo payload do Ticker contém EUR/USD; a conversão podia ser ao vivo.
- `futures_quote_fresh = bool(lastTime)` é sempre `True` se o campo existir. Não mede idade. Um perpétuo sem fills há 3 h conta como fresco.
- `is_crypto` aceita `post_only` como negociável. Um mercado post-only não aceita ordens a mercado; para o Fable isso muda a tradeability.
- Não há filtro por classe de ativo: tokenized stocks/ETFs listados na Kraken (xStocks) passam como cripto se tiverem quote USD e $1M de volume. O Developer deve verificar o que `AssetPairs` expõe (`aclass_base`) para os excluir sem lista hardcoded.
- Qwen recebe `local_radar_score` e devolve exatamente esse número (26.1). Não está a pontuar, está a ecoar. O prompt pede "score" mas não define escala; sem schema, o `format: "json"` só garante JSON válido, não a forma.
- Qwen pode devolver símbolos que não estavam no input; nada valida.
- Não há retry, backoff, timeout diferenciado, nem escrita de resultado em ficheiro. Não pode ser agendado em loop com utilidade.
- Sem dedupe/cooldown: o mesmo alerta dispararia em todos os ciclos.

### 1.5 Onde o scoring produz falsos positivos/negativos

- **Pré-filtro (falsos negativos):** ativo com +2% em 15m, 24h plano, volume $3M, spread 30 bps → 0 + 3 + 0 + 0 = 3 pontos. BTC parado → 0 + 14 + 8 + 0 = 22. O ativo que interessa não chega ao OHLC.
- **Sem normalização (falsos positivos e negativos):** 5m×7, 15m×4, 1h×2 sobre percentagens brutas. Satura aos 2.86% em 5m. Memecoin com ATR 5m de 1.5% ganha 10 pontos por respirar.
- **Aceleração:** `|15m| - |1h|/4` assume linearidade e ignora sinal. Reversão (+2% em 15m depois de -2% em 45m) e breakout dão o mesmo bónus.
- **Volume:** baseline 1h = volume24h/24 ignora sazonalidade intradiária (sessão US vs Ásia dá 1.5-2× sozinha). A janela de 15m inclui a barra parcial atual, logo o rácio 15m está sistematicamente subestimado no início da barra.
- **Spread pesa tanto como momentum:** -15 por spread > 60 bps foi o que empurrou a LSK para fora do gate enquanto PUMP entrou com movimento nulo. Liquidez deve ser um portão de tradeability, não um termo aditivo que compete com o sinal.
- **Threshold 25 numa escala sem significado:** a soma de constantes arbitrárias não tem interpretação; o gate passa ou não por acidente.

### 1.6 Bugs concretos

1. **BTC e DOGE nunca fazem match com Futures.** `wsname` da Spot dá `XBT/USD` e `XDG/USD`; `normalize_future_base` só normaliza o lado Futures (`XBT→BTC`). `by_base.get("XBT")` falha. É preciso normalizar o lado Spot com o mesmo mapa (mapa de códigos legados da Kraken, não lista de moedas).
2. **"24h change" é "desde 00:00 UTC".** Confirmado na documentação: Ticker `o` = "Today's opening price" (escalar), enquanto `v/h/l/p/t` têm `[today, last24h]`. À 01:00 UTC a "variação 24h" é a variação de 1 hora; às 23:00 é de 23 h. Incoerente com o range 24h e com o volume 24h usados na mesma fórmula.
3. **Basis com quotes diferentes.** Se o mercado Spot escolhido for EUR (ativo sem par USD), `futures_basis_pct = futures_last/spot_last - 1` dá ~16% de "basis". Deve usar `markPrice/indexPrice - 1` do próprio ticker Futures, que é a definição correta e não depende da Spot.
4. **"5m change" tem janela variável.** Compara o close da barra parcial atual com o close da barra anterior: mede entre 0 e 5 minutos consoante a hora do ciclo.
5. **Volume 15m/1h inclui barra parcial** (`rows[-3:]`, `rows[-12:]`).
6. **Trade count, VWAP 24h e tamanho no melhor bid/ask são ignorados** apesar de virem gratuitamente no Ticker (`t[1]`, `p[1]`, `a[2]`, `b[2]`). São os três melhores detetores baratos de volume falso e de liquidez fina.

---

## 2. Universe discovery (dinâmico, sem listas de moedas)

Regra: **lista permitida de quotes e mapa de códigos legados da venue são configuração aceitável; lista de moedas não é.**

| Caso | Tratamento v0.8 |
|---|---|
| Quote USD | primário |
| Quote USDT / USDC | só quando o ativo não tem par USD; prioridade USD > USDT > USDC |
| Quote EUR | só quando não há par em USD/USDT/USDC; conversão ao vivo via `EUR/USD` do mesmo payload Ticker; marcado `quote_fallback=true` no output |
| XBT/BTC, XDG/DOGE | mapa `LEGACY_CODES = {"XBT":"BTC","XDG":"DOGE"}` aplicado nos dois lados (Spot e Futures) antes do match. Developer verifica `Assets` para outros códigos com prefixo X/Z |
| Stablecoins | lista de quote-assets conhecida (é config de venue) **mais** regra dinâmica: preço em [0.97, 1.03] contra USD e range 24h < 1% → `stable_like`, excluído. Apanha stables novas sem código |
| Fiat como base | excluído (lista de fiat é config de venue) |
| Tokenized stocks/ETFs | excluídos por `aclass_base`/naming da venue, a verificar pelo Developer no `AssetPairs`; se não houver campo fiável, regra dinâmica: sem fills entre sexta 21:00 e domingo 21:00 UTC no store → `non_crypto_like` |
| Duplicados | agrupar por base normalizada; escolher mercado por prioridade de quote e depois volume; **a liquidez do ativo é a soma dos volumes de todos os quotes** (para o portão), a análise usa o mercado primário |
| Volume artificialmente baixo / mercados mortos | `t[1]` (trades 24h) < 200 → `dead`; volume 24h < $1M → fora do universo negociável mas **mantido no snapshot store** (para detetar arranques) |
| Post-only, cancel_only, limit_only, reduce_only | fora do universo negociável, registado no output com o status |
| Spread extremo | spread top-of-book > 150 bps → `untradeable`; 50-150 → flag, penaliza tradeability, não exclui |

Uma memecoin nova na Kraken aparece no `AssetPairs` (cache 24 h, com refresh forçado se um símbolo do Ticker não existir no cache) e no Ticker no mesmo ciclo. Entra no store imediatamente; passa a candidata quando cumprir liquidez e tiver ≥ 1 h de snapshots (ou OHLC de backfill).

---

## 3. Market data architecture (o que se recolhe em cada camada)

Princípio: **tudo o que o Ticker global e o Tickers Futures dão é CHEAP GLOBAL e é recolhido para todos os mercados em 2 pedidos. Tudo o que exige um pedido por ativo é CANDIDATE ou FINALIST.**

### CHEAP GLOBAL (2 pedidos/ciclo, ~1000 mercados)

Spot Ticker: last, bid, ask, **bidSize/askSize** (`a[2]`, `b[2]`), volume today/24h, **VWAP today/24h**, **trades today/24h**, high/low today/24h, open today.
Futures Tickers: markPrice, **indexPrice**, last, lastTime, bid/ask/sizes, vol24h, volumeQuote, openInterest, fundingRate (raw), fundingRatePrediction (raw), open24h, suspended, postOnly, tag.

Derivados sem pedidos extra:
- spread bps, USD no melhor bid/ask (proxy de profundidade nível 1);
- distância ao VWAP 24h (`last/p[1] - 1`): melhor "posição" do que range position;
- avg trade size 24h (`volume/trades`) e trades por hora: detetor de wash/fake volume;
- **com o snapshot store:** retorno 5m/15m/1h/4h (preço vs snapshot de há N min), volume exato do intervalo (`Δ v[0]`, com reset à meia-noite tratado), trades do intervalo (`Δ t[0]`), **ΔOI 15m/1h/4h**, Δfunding, basis `mark/index - 1`, idade do último fill Futures;
- retorno relativo ao mercado (`r_asset - r_BTC`) e atividade agregada do mercado (soma de Δvolume em USD de todo o universo): distingue movimento idiossincrático de beta.

### CANDIDATE ONLY (shortlist ≤ 40, 1 pedido por ativo, incremental)

OHLC 5m com `since` = último timestamp em cache (após warm-up, cada pedido traz 1-2 barras). Dele derivam 15m/1h/4h/24h por agregação. Calcula-se:
- ATR(14) em barras 5m e em barras 1h; realized vol 24h;
- retornos em unidades de ATR (a normalização que falta à v0.7);
- baseline de volume: mediana das últimas 96 janelas de 15m e fator hora-do-dia (dos 2 dias anteriores);
- estrutura: N-bar high/low (4h, 24h), distância ao breakout em ATR, higher-highs/lower-lows nas últimas 12 barras, compressão de range (ATR 1h / ATR 24h em percentil);
- VWAP intradiário a partir do OHLC (aproximação por barra).

**Não proponho 1m.** Com 5m e snapshots ao minuto o ganho de 1m é ruído e custa 1 pedido extra por ativo. Rejeito também 4h como pedido separado: agrega-se de 5m (60 h de histórico chegam para 4h e 24h).

### FINALIST ONLY (≤ 8, 2 pedidos por ativo)

- `Depth?pair=&count=25` Spot: USD disponível a ±0.5% e ±1% do mid, imbalance bid/ask, slippage estimado para um tamanho de ordem configurável;
- `Trades?pair=` Spot (últimos 1000 fills): rácio taker buy/sell, tamanho médio, tempo coberto (agressão real, não só volume);
- `orderbook?symbol=PF_…` Futures: mesma métrica de profundidade;
- `historical-funding-rates?symbol=` Futures: **uma vez por dia para 2-3 símbolos de referência**, não por finalista, para a verificação de semântica (secção 9).

**Liquidações:** a API pública da Kraken Futures não expõe feed de liquidações. Não se inventa. O único proxy é ΔOI brusco com movimento de preço contrário ao funding, e é rotulado como `proxy`, nunca como liquidation data.

---

## 4. Multi-stage radar

Proponho seis camadas e **duas cadências**: um ciclo leve ao minuto e um ciclo completo a cada 5 min ou quando o ciclo leve dispara.

```
L0  UNIVERSE      2 req    AssetPairs (cache 24h) + Ticker + Futures Tickers → snapshot store
L1  ANOMALY       0 req    z-scores de retorno/volume/trades/OI para TODOS  → shortlist ≤ 40
L2  STRUCTURE    ≤40 req   OHLC incremental → features em ATR, setup rule-based, opportunity score → ≤ 10
L3  MICRO/DERIV  ≤16 req   depth + trades (+ futures book) → tradeability, risk flags → finalists ≤ 8
L4  QWEN         0-1 call  classificação + veto + call_fable (só se houver finalistas acima do pré-gate)
L5  FABLE GATE    0 req    regras determinísticas ∧ Qwen ∧ cooldown ∧ budget → lista para o Fable
```

Porquê esta ordem e não "momentum → structure → derivatives": derivativos já estão no L0 de graça (o ticker Futures é global), por isso OI/funding/basis entram nas features do L1 e não numa camada tardia. O que é caro (order book) fica no fim.

**Cadência:** `heartbeat` a cada 60 s executa só L0+L1 (~1-2 s, 2 pedidos, sem Qwen). Se algum ativo tiver anomaly ≥ limiar de disparo, corre-se L2-L5 imediatamente (event-driven). Independentemente disso, L2-L5 corre a cada 5 min. Resultado: cobertura ao minuto, custo de OHLC a cada 5 min, Qwen só quando há algo.

**Cold start:** nos primeiros 60 min o store não tem histórico. L1 degrada para os sinais que o Ticker dá sozinho (distância ao VWAP 24h, posição no range, spread, trades/h) e L2 faz backfill OHLC (sem `since`) para os 40 melhores. O output marca `warmup=true`.

**Squeeze:** um squeeze é compressão, logo não é anomalia de atividade e o L1 não o apanha, e é correto: só é acionável na libertação, e a libertação É uma anomalia (expansão de range com volume). O L2 reconhece que a expansão vem de compressão porque tem o percentil de ATR no cache.

---

## 5. Scoring

Quatro números separados, porque respondem a perguntas diferentes e misturá-los foi o erro da v0.7:

- **anomaly_score (L1, todos):** "quão invulgar é a atividade atual para este ativo?" Direction-agnostic. Média ponderada de z-scores (retorno 15m vs desvio-padrão dos retornos 15m do próprio ativo nas últimas 48 h; volume 15m vs mediana das janelas homólogas; trades 15m idem; |ΔOI 1h| vs histórico), com ajuste pela atividade agregada do mercado (se todo o universo tem z=2, o z relativo cai). Serve só para ordenar a shortlist. Um ativo líquido e parado tem z≈0 e não entra, seja BTC ou não.
- **opportunity_score (L2, shortlist):** "há um setup?" 0-100 construído a partir de features limitadas a [-1, 1]:
  - momentum: retorno 15m e 1h em unidades de ATR, com **coerência de sinal** entre 5m/15m/1h (incoerência penaliza);
  - acceleration: retorno 15m vs (retorno 1h − retorno 15m), com sinal;
  - volume expansion **confirmada por preço**: volume 15m/baseline só conta se o range da barra também expandiu (volume sem range = absorção, não expansão);
  - breakout distance: close vs máximo/mínimo 4h e 24h em ATR; positivo perto/acima, com volume;
  - freshness: fração do movimento 24h ocorrida na última hora. Se |r_24h| ≫ |r_1h| e |r_1h| < 0.5 ATR → movimento antigo, feature negativa;
  - exhaustion: |r_1h| > 3 ATR ∧ volume das últimas 3 barras a cair ∧ close longe do high da barra → negativa;
  - reversal: 15m contra 4h, com volume no extremo e rejeição (pavio) → setup próprio, não penalização;
  - squeeze release: percentil de compressão de ATR nas 24 h anteriores baixo ∧ expansão agora;
  - relative strength: r_asset − r_BTC na janela (idiossincrático > beta);
  - derivatives coherence: ΔOI com o sinal do movimento (OI a subir com preço a subir = posições novas; OI a cair = fecho), basis dentro de banda normal, funding só se VERIFIED.
  Cada setup_type (BREAKOUT, CONTINUATION, REVERSAL, SQUEEZE_RELEASE, EXHAUSTION, NONE) é atribuído por regras determinísticas sobre estas features, com os pesos por tipo em config. Direction vem do sinal das features de momentum, não do LLM.
- **tradeability_score (L3, finalistas):** spread, USD a ±0.5%, slippage estimado, trades/h, status, existência de perpétuo, spread/profundidade Futures, idade do último fill. **É um portão, não um termo aditivo:** abaixo do mínimo, o ativo não vai ao Qwen, por muito bonito que seja o setup. Acima, entra no output como número para o Fable.
- **confidence:** data_quality (snapshots suficientes? OHLC atualizado? Futures fresco? funding verified?) × número de confirmações independentes (momentum, volume, breakout, derivativos, microestrutura). Bucket LOW/MEDIUM/HIGH.

Sobre custos, um ponto que a v0.7 ignora por completo: **na Spot Kraken a ida e volta taker é ~160 bps** (tier verificado no projeto sextant; o Developer confirma na config). Um alerta de +2% em 15m na Spot é economicamente irrelevante; o mesmo movimento nos perpétuos (2/5 bps) não é. O opportunity score deve exigir amplitude esperada mínima em função da venue executável: `market=FUTURES` tem barra mais baixa que `market=SPOT`. Sem isto o radar produz alertas verdadeiros e inúteis.

**Calibração em vez de adivinha:** o store regista, para cada ativo que chegou a L2, as features e o retorno 15m/1h/4h seguinte (forward return). Após alguns dias há dados para escolher pesos e thresholds por evidência, e para responder à pergunta que importa: "os alertas que geramos foram seguidos de movimento?" Sem isto, v0.8 seria outra rodada de constantes inventadas.

---

## 6. Papel do Qwen3:14b

**Não pontua.** LLMs de 14B são maus em aritmética e ecoam números que lhes dão; o teste da v0.7 provou-o (26.1 → 26.1).

Faz quatro coisas, sobre ≤ 8 finalistas já com setup rule-based e features:

1. **Veto de falsos positivos:** o setup rule-based é coerente com todas as features apresentadas? (Ex.: "BREAKOUT" mas volume expansion negativa e spread a alargar → veto com razão.)
2. **Classificação:** confirma ou corrige `setup_type` e `direction` dentro de enums fechados.
3. **Recomendação `call_fable` + `confidence` bucket + razão de uma frase** com referência aos dados fornecidos.
4. **Flags de data quality** que note (funding raw, quote fallback, warmup).

Chama-se **só quando há finalistas acima do pré-gate determinístico** (opportunity ≥ 50 e tradeability ok). Muitos ciclos terão zero chamadas Qwen. `think=false`, temperatura 0, **structured output com JSON schema** no campo `format` do Ollama (não a string `"json"`), símbolos validados contra o input, uma repetição se o JSON falhar.

Nota honesta: com features bem construídas, o valor marginal de um 14B é modesto. Mantém-se porque é barato e reduz chamadas ao Fable, mas **o log regista sempre a decisão determinística e a do Qwen lado a lado**. Se ao fim de duas semanas o Qwen concordar com as regras em > 95% dos casos, remove-se e poupa-se latência.

---

## 7. Fable gate

Chamar o Fable é a ação cara. Regras determinísticas, avaliadas depois do Qwen:

**Obrigatórias (todas):**
- tradeability ≥ mínimo e status online;
- data_quality sem flag crítica (sem warmup, OHLC atualizado, Futures fresco se `market≠SPOT`);
- opportunity ≥ 60;
- não em cooldown: o mesmo ativo não volta ao Fable nas próximas N horas (config, ex. 4 h) salvo se `setup_type` mudou ou opportunity subiu ≥ 15 pontos;
- Qwen não vetou com HIGH confidence (ou Qwen indisponível, caso em que se exige uma confirmação extra).

**Pelo menos duas confirmações independentes de:**
- momentum ≥ 2 ATR em 1h com coerência de sinal 5m/15m/1h;
- volume expansion ≥ 3× baseline com confirmação de range;
- breakout de máximo/mínimo 24h com volume;
- squeeze release;
- derivativos coerentes (ΔOI 1h com o sinal do preço; basis normal; futures volume a subir);
- agressão na fita (taker imbalance ≥ 65/35 nos últimos trades);
- Qwen `call_fable=true` com HIGH.

**Vetos:** flag `exhaustion`, flag `illiquid_pump`, flag `late_pump` (freshness negativa), funding extremo se VERIFIED e contra a direção.

**Budget:** máximo K chamadas Fable por hora e por dia (config, sugestão inicial 2/h, 6/dia). Se houver mais candidatos do que budget, ordena-se por opportunity × confidence e os restantes ficam no output como `deferred`.

---

## 8. Memecoins / high-beta

Não há tratamento especial nem lista. A elegibilidade automática vem de: universo dinâmico, liquidez como portão (não como score) e normalização por ATR (a memecoin é comparada consigo própria). O que protege contra as armadilhas:

| Armadilha | Deteção |
|---|---|
| Pump tardio | freshness (fração do movimento 24h na última hora) + exhaustion |
| Illiquid pump | USD a ±0.5% no book (finalista), bid/ask size no Ticker (global), trades/h |
| Fake volume | volume alto com trades baixos (avg trade size anómalo), volume sem range, volume Spot sem eco no Futures quando existe perpétuo |
| Spread largo | tradeability gate, com o custo estimado explícito no output |
| Exhaustion | feature própria + veto no Fable gate |

Um ativo com ATR 5m de 2% precisa de mais amplitude absoluta para ter o mesmo z; isso é o comportamento correto, não uma penalização por ser meme.

---

## 9. Futures

Tudo vem do endpoint global `tickers`; o que muda na v0.8 é usar `indexPrice` e o store:

- **basis** = `markPrice/indexPrice − 1` (corrige o bug 3);
- **ΔOI** 15m/1h/4h a partir dos snapshots, em % e em USD (`OI × mark`); sinal de OI vs sinal de preço classifica: novas posições / fecho / squeeze provável;
- **volume Futures** vs Spot (rácio e variação);
- **spread e profundidade nível 1** do ticker; order book só em finalistas;
- **freshness** = `now − lastTime` em segundos; > 300 s → `stale`;
- `suspended` ou `postOnly` → Futures não executável.

**Funding: RAW vs VERIFIED.** Confirmado na documentação: o ticker dá `fundingRate` = "current **absolute** funding rate" e `fundingRatePrediction` = "estimated next absolute funding rate". Existe endpoint público de histórico com `fundingRate` (absoluto) e `relativeFundingRate` por período. A documentação **não** define nem o período nem a relação matemática entre os dois. Por isso:

1. Rotina `verify_funding_semantics()` uma vez por dia (cache 24 h), com 2-3 símbolos de referência (BTC, ETH, um alt): puxa o histórico, mede o espaçamento dos timestamps (período empírico), testa a hipótese `relative ≈ absolute / mark` e compara o último `fundingRate` histórico com o do ticker.
2. Se as três verificações passam dentro de tolerância, o output marca `funding_semantics: "VERIFIED"` com `period_hours` e `relative_rate` derivado. Caso contrário fica `RAW_UNVERIFIED` e o funding **não entra em nenhuma feature direcional**, só no output como contexto.
3. Mesmo VERIFIED, funding entra com peso baixo. O projeto sextant mediu que o funding é o preço do risco de base e não sinal gratuito; aqui usa-se só para detetar extremos (posicionamento lotado) e squeeze potencial.

O Developer confirma o path exato do endpoint de histórico (a documentação lista `historical-funding-rates`; em uso corrente aparece também `/derivatives/api/v4/historicalfundingrates`). Testar os dois, registar o que responde.

---

## 10. Order book

Só finalistas (≤ 8), só no ciclo completo, só quando o candidato já passou o pré-gate de opportunity. Métricas: USD a ±0.5% e ±1%, imbalance, slippage estimado para o tamanho de ordem configurado. Se a shortlist for vazia, zero pedidos de book. Em regime normal isto são 0-16 pedidos por ciclo de 5 min. Não há book no heartbeat.

Acrescento `Trades` (últimos fills) no mesmo escalão: é um pedido, e diz se o volume é agressão compradora ou vendedora, coisa que o OHLC não sabe.

---

## 11. Performance

| | v0.7 | v0.8 heartbeat (60 s) | v0.8 completo (5 min) |
|---|---|---|---|
| Pedidos Kraken | 38 | 2 | 2 + ≤40 OHLC incremental + ≤16 finalistas |
| Payload OHLC | ~1.7 MB | 0 | ~50 KB (com `since`) |
| Qwen | sempre | nunca | só com finalistas |
| Tempo estimado | 15 s | 1-2 s | 4-8 s |

Meios: `requests.Session` com keep-alive, `ThreadPoolExecutor` com 3-4 workers nos OHLC, backoff exponencial em 429/5xx, cache OHLC incremental, AssetPairs em cache diário. Store SQLite com retenção 7 dias (~10 MB/dia com snapshot a cada minuto para ~300 ativos).

---

## 12. Memória

O radar tem estado técnico próprio (`radar_state.sqlite`: snapshots, cache OHLC, alertas emitidos, cooldowns, forward returns) e escreve `radar_latest.json` + `alerts.jsonl`. **Nunca escreve em ficheiros de estado ou histórico de trading.** O radar só produz eventos que o Fable (e um humano) consomem.

---

## 13. Output JSON (proposta)

```json
{
  "schema_version": "0.8",
  "run_id": "2026-09-13T14:05:00Z#3812",
  "timestamp": "2026-09-13T14:05:03Z",
  "mode": "FULL | HEARTBEAT",
  "warmup": false,
  "universe": {
    "pairs_seen": 1043, "assets_eligible": 287, "assets_tradeable": 241,
    "futures_perpetuals": 275, "excluded": {"stable_like": 14, "dead": 22, "status": 9, "non_crypto": 31}
  },
  "funnel": {"L1_shortlist": 40, "L2_candidates": 10, "L3_finalists": 5, "qwen_called": true, "fable_recommended": 1},
  "data_quality": {
    "spot_ticker": "OK", "futures_ticker": "OK | STALE | UNAVAILABLE",
    "funding_semantics": "VERIFIED | RAW_UNVERIFIED", "funding_period_hours": 1,
    "ohlc_failures": 0, "qwen": "OK | INVALID_JSON | TIMEOUT | UNAVAILABLE",
    "credentials_used": false
  },
  "candidates": [
    {
      "asset": "LSK", "spot_pair": "LSK/USD", "quote_fallback": false,
      "futures_symbol": "PF_LSKUSD",
      "scores": {"anomaly": 3.4, "opportunity": 71, "tradeability": 58, "confidence": "MEDIUM"},
      "setup": {"type": "CONTINUATION", "direction": "LONG", "market": "FUTURES", "freshness": 0.62},
      "features": {
        "ret_5m_atr": 1.3, "ret_15m_atr": 2.1, "ret_1h_atr": 1.9, "ret_24h_pct": 196.9, "ret_rel_btc_1h_pct": 0.5,
        "vol_15m_x": 3.8, "vol_1h_x": 2.6, "trades_1h_x": 2.9, "range_expansion": true,
        "breakout_dist_24h_atr": -0.3, "squeeze_pctile": 0.71, "exhaustion": false,
        "spread_bps": 78.2, "depth_usd_0_5pct": 18400, "taker_buy_ratio": 0.68,
        "oi_delta_1h_pct": 4.1, "basis_pct": 0.12, "funding_raw": -0.00195, "funding_relative": null
      },
      "flags": ["WIDE_SPREAD", "EXTENDED_24H"],
      "qwen": {"setup_type": "CONTINUATION", "direction": "LONG", "call_fable": true, "confidence": "MEDIUM",
               "veto": false, "reason": "..."},
      "fable_gate": {"decision": "CALL | DEFER | SKIP", "confirmations": ["momentum", "volume", "oi"],
                     "vetoes": [], "cooldown_until": null}
    }
  ],
  "alerts_for_fable": ["LSK"]
}
```

---

## 14. Segurança (confirmado no código v0.7 e mantido na v0.8)

- Só `GET` em `/0/public/*` e `/derivatives/api/v3/*` (tickers, orderbook, historical funding). Nenhum endpoint privado, de conta ou de ordens.
- Nenhuma variável de ambiente com chave lida; v0.8 acrescenta um **guard em arranque** que aborta se `KRAKEN_API_KEY`/`KRAKEN_SECRET` estiverem definidas no ambiente do processo, e um teste que falha se qualquer URL contiver `/private/` ou o método não for GET.
- Ollama em localhost; o Qwen não tem tools.

---

## 15. Plano de implementação

**A. Arquitetura:** pacote `radar/` com `config.py`, `kraken_spot.py`, `kraken_futures.py`, `store.py` (SQLite), `universe.py`, `features.py`, `setups.py`, `scoring.py`, `micro.py`, `qwen_gate.py`, `fable_gate.py`, `output.py`, `radar.py` (orquestrador com `--mode heartbeat|full|loop`). Dataclasses/pydantic para `Snapshot`, `Candidate`, `Features`, `Decision`.

**B. Scoring:** secção 5; pesos e thresholds em `config.py` com valores iniciais marcados `UNCALIBRATED`, a rever com forward returns após 7 dias.

**C. Qwen prompt (inglês, structured output):**

System: "You are the final gatekeeper of a read-only crypto radar. You receive up to 8 finalists with numeric features already computed and a rule-based setup label. You do NOT compute scores. Your job: (1) veto candidates whose rule-based setup is contradicted by the supplied features; (2) confirm or correct setup_type and direction using only the enums; (3) recommend whether a deeper analysis by a senior analyst is worth its cost; (4) note data quality issues. Use only supplied data. Funding fields labeled RAW_UNVERIFIED must not influence direction. Memecoins and high-beta assets are valid. Output must match the schema exactly and reference only symbols from the input."

User: os finalistas em JSON compacto + "Return JSON matching the schema."

Schema (campo `format` do Ollama): `{"reviews":[{"asset":str, "setup_type": enum[BREAKOUT,CONTINUATION,REVERSAL,SQUEEZE_RELEASE,EXHAUSTION,NONE], "direction": enum[LONG,SHORT,NONE], "market": enum[SPOT,FUTURES,BOTH,NONE], "veto": bool, "call_fable": bool, "confidence": enum[LOW,MEDIUM,HIGH], "reason": str(max 200), "data_quality_notes": [str]}]}`. Pós-validação: `asset` ∈ input, um review por finalista, retry único.

**D. Fable gate:** secção 7, implementado em `fable_gate.py` como função pura sobre `Candidate` + `QwenReview` + estado de cooldown/budget, testável sem rede.

**E. Endpoints:** `AssetPairs`, `Ticker`, `OHLC?since`, `Depth?count=25`, `Trades`, Futures `tickers`, `orderbook`, historical funding. Nada mais.

**F. Classes/funções principais:** `SnapshotStore.write/window(asset, minutes)`, `Universe.build(pairs, ticker, fut) → list[Asset]`, `compute_anomaly(store, asset)`, `OhlcCache.update(pair)`, `compute_features(asset, ohlc, snapshots)`, `classify_setup(features) → (type, direction)`, `opportunity(features, type)`, `tradeability(asset, depth, trades)`, `qwen_review(finalists)`, `fable_gate(candidates, reviews, state)`, `write_output(...)`, `label_forward_returns(store)`.

**G. Caching:** AssetPairs 24 h; EUR/USD por ciclo; OHLC incremental por par (`since`); snapshots 7 dias; verificação de funding 24 h; resultado Qwen por `(run_id)`.

**H. Logging:** `radar.log` humano (uma linha por camada com contagens e tempos) + `runs.jsonl` estruturado (features de todos os L2, decisão determinística e Qwen lado a lado) + `alerts.jsonl`. Sem isto não há calibração nem auditoria do Qwen.

**I. Falhas:** Futures indisponível → continua sem derivativos, `data_quality.futures_ticker=UNAVAILABLE`, tradeability só Spot. OHLC falha num ativo → mantém-se com features L1 e flag `OHLC_MISSING`, nunca desaparece em silêncio. 429 → backoff e redução de workers. Qwen timeout/JSON inválido → um retry, depois gate determinístico com `qwen=UNAVAILABLE` e exigência de confirmação extra. Store corrompido → recria e marca warmup. Ciclo que exceda 45 s aborta o Qwen desse ciclo.

**J. Migração v0.7 → v0.8:**
1. `store.py` + L0/L1 + heartbeat em loop. Correr 3-5 dias em sombra ao lado da v0.7, gravando ambas as shortlists. Critério de passagem: a shortlist L1 contém os ativos que um humano identifica a olho como "a mexer" em pelo menos 9 de 10 verificações manuais, e a v0.7 falha em várias.
2. L2 (OHLC incremental, features em ATR, setups, opportunity) + forward return labeling.
3. L3 (depth, trades, tradeability) + correção dos bugs 1-6 herdados.
4. Qwen com schema + log comparativo.
5. Fable gate + cooldown + budget + output final.
6. Reformar a v0.7. Primeira calibração de pesos com 7+ dias de `runs.jsonl`.

Cada passo é uma alteração separada, entregue uma de cada vez.

---

## 16. Decisão final

**ARCHITECTURE VERDICT: BUILD v0.8**

Porquê: os problemas observados (ranking fraco, um só candidato no gate, incapacidade de distinguir movimento novo de antigo) são consequência direta de três decisões estruturais da v0.7 (pré-filtro por 24h/liquidez, ausência de normalização, ausência de estado), não de thresholds. Ajustar constantes na v0.7 mudaria quais ativos falham, não o facto de falharem. A v0.8 muda a peça certa (snapshot store + ranking por anomalia + liquidez como portão) e fá-lo **reduzindo** pedidos e latência, não aumentando.

Condições que ponho ao BUILD:
- Fase 1 corre em sombra antes de substituir o que existe; não se desliga a v0.7 por promessa.
- Os pesos iniciais são declarados `UNCALIBRATED` e só ganham valores "definitivos" com forward returns medidos. Sem `runs.jsonl` e labeling, a v0.8 é a v0.7 com mais constantes inventadas.
- O Qwen fica em avaliação, com o log comparativo como juiz, e sai se não acrescentar nada.
- O radar não é prova de edge. Detetar movimento não é prever movimento; o que ele compra é tempo do Fable gasto onde há algo a analisar. A questão "isto dá dinheiro?" é respondida por registos de trades medidos, não pelo radar.
