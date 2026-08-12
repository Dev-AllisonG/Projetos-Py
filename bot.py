# -*- coding: utf-8 -*-
"""
============================================================
 BOT DE ALERTAS v7.0 — DATA FOUNDATION (Fases 1.1-1.6)
============================================================
  ✅ LISTENER WEBSOCKET v6.0: eventos Solana em TEMPO REAL via
     logsSubscribe — chega antes do polling HTTP. O polling
     HTTP continua ativo como fallback — os dois correm juntos.
     Assinaturas detectadas via WS são processadas no mesmo
     ciclo() com analisar_tx() — zero código duplicado.
  ✅ DURAÇÃO RAW: todo payload bruto é gravado em raw_payloads
     (COMMIT) ANTES da assinatura entrar na fila do bot —
     mesmo que o processo morra, nada se perde.
  ✅ SLOT TRACKER: 4 estados (SYNCED/LAGGING/GAP/RECOVERING).
     Falha de RPC ≠ gap: o sistema é conservador e não fabrica
     gaps por indisponibilidade de RPC.
  ✅ GAP RECOVERY: blocos perdidos na reconexão são recuperados
     via getBlock (concurrent, checkpoint após COMMIT). Os gaps
     de sessões anteriores são retomados no próximo boot.
  ✅ STARTUP RECOVERY: sigs commitadas mas não processadas
     são reenfileiradas automaticamente no boot.
  ✅ ANTI-CONGELA v5.1: orçamento de tempo dentro dos loops
     pesados — 1 wallet cheia de moedas novas não trava o ciclo.
  ✅ FEATURE STORE v5.0: banco SQLite IMUTÁVEL (radar_historico.db)
     guarda tokens, alertas, ROI real, reputação e grafo.
  (demais features v5.x mantidas intactas)

 COMO USAR:  python bot.py   (ou dois cliques no INICIAR_BOT.bat)
============================================================
"""

import time
import queue

from radar import config, estado, persistencia, telegram, analise, gui, utils
from radar.version import BOT_VERSION_LABEL   # requisito 27: fonte unica
from radar import db, grafo, autoavaliacao   # 🧬 v5.0
from radar.rpc import rpc
from radar.gui import TEM_GUI

# 🩺 Camada de runtime: saúde dos componentes, observabilidade e proteção
# dos bancos. Não contém inteligência — só diz o que está vivo e por quê.
from radar import runtime as rt
from radar.runtime import dbhealth, health
from radar.runtime.health import runtime
from radar.runtime.logsetup import get_logger, log_exception
from radar.runtime.metrics import metrics as counters
from radar.runtime import shadow
from radar.runtime import ingestion_lifecycle

log = get_logger("bot")

PROGRESSO_CICLO_SEG = 15   # ⏳ console mostra progresso a cada Xs num ciclo demorado


def _iniciar_listener(_deps_missing) -> None:
    """📡 v6.0 — listener WebSocket em thread daemon.

    v9.7.1 (Etapa 1): corrigido bug de indentacao — o mark_failed de
    LEGACY_WEBSOCKET estava FORA do except e reportava o componente morto
    mesmo com o listener no ar (o log mostrava 'Thread iniciada' seguido
    de 'FAILED: bridge nao iniciou'). Sucesso => STARTING: quem vira
    HEALTHY e o bridge.py quando o WS conecta de fato — nada de presumir.
    Sem deps => o boot check ja marcou DISABLED com o motivo exato.
    """
    if "websockets" in _deps_missing or "httpx" in _deps_missing:
        # v9.6.6: sem a dependencia obrigatoria o listener NAO tenta subir.
        # O boot check ja marcou BRIDGE/LEGACY_WEBSOCKET/RPC_WS como
        # DISABLED com o motivo exato — nada de ModuleNotFoundError em run.
        log.error(
            "📡 listener WebSocket NAO iniciado: dependencias ausentes=%s "
            "(pip install -r requirements.txt)", _deps_missing,
            extra={"event": "listener_skipped_missing_deps",
                   "missing": list(_deps_missing)})
        return
    try:
        from radar.listener.bridge import iniciar_listener
        iniciar_listener()
        # Sucesso = thread supervisionada no ar => STARTING (estado real).
        runtime.mark_starting(health.LEGACY_WEBSOCKET)
    except Exception as e:
        # 🩺 Nunca mais "morreu e o bot seguiu como se nada fosse":
        # stack trace completo + estado FAILED bem visível no summary.
        log_exception("bridge", "iniciar_listener", e)
        runtime.mark_failed(health.BRIDGE, reason=str(e), exc=e,
                            operation="iniciar_listener")
        # Agora sim, DENTRO do except: so falhou quem de fato nao subiu.
        runtime.mark_failed(health.LEGACY_WEBSOCKET,
                            reason="bridge não iniciou")


# v9.7.6 (#6/#13/#14): estado de pressao entre ciclos (deltas p/ a
# classificacao e os campos do [CYCLE] report).
_PRESSAO = {"dlq_total_prev": 0, "backlog_prev": None, "dlq_crescendo": False,
            "deadline_prev": False, "ws_itens": 0, "dlq_added": 0,
            "alvos_no_teto": 0,
            "ws_origens": {}, "descobertas_por_origem": {}, "prune_mesmo_ciclo": 0,
            "ws_live_idade_seg": None,
            "dlq_drained": 0, "dlq_pending": None, "backlog_pending": None,
            "backlog_delta": 0}


def _flush_stats_seguro() -> dict:
    """v9.7.13 (Causa 1): p50/p95/max do flush do listener (nunca falha)."""
    try:
        from radar.listener import ingestor as _m
        return _m.flush_stats()
    except Exception:
        return {}


def _loop_stall_seguro():
    """v9.7.13 (Causa 1): maior stall do event loop do bridge (segundos)."""
    try:
        from radar.listener import bridge as _m
        return _m.loop_stall_max()
    except Exception:
        return None


def _processar_sigs_ws(fila, estourado, ciclo_t0=None):
    """📡 v6.0 — Drena a fila do listener WebSocket e processa as sigs.

    Regras:
    - Só processa sigs que ainda não foram vistas (evita duplicatas com polling)
    - Respeita o orçamento de tempo do ciclo (estourado())
    - Marca a sig DEPOIS de processar (mesmo comportamento do polling HTTP)
    - Erros não travam o ciclo: marca a sig e segue

    v9.7.6 (#1/#2): ORCAMENTO PROPRIO do drain — o WS NUNCA consome o
    ciclo inteiro ("live first" nao pode virar "live only": 11/08 o
    ws_drain comeu 45.3s e o polling processou 0/34).
    """
    processadas = 0
    descartadas = 0
    t0 = time.time()
    # v9.7.9 (Etapa 0): origem de cada item drenado (live vs backlog vs dlq)
    # e quantas wallets NOVAS cada origem gerou — decide a triagem do
    # passivo. Instrumentacao pura: NADA de comportamento muda.
    por_origem: dict = {}
    descobertas_por_origem: dict = {}
    idade_live_max = None   # v9.7.10 (Etapa 2): idade do vivo mais antigo

    from radar.runtime import pressure as _pressure
    orcamento = _pressure.OrcamentoWS(
        max_itens=getattr(config, "WS_DRAIN_MAX_ITENS", 1500),
        max_seg=getattr(config, "WS_DRAIN_MAX_SEG", 15.0),
        reserva_seg=getattr(config, "POLLING_RESERVA_SEG", 20.0),
        ciclo_budget_seg=config.MAX_CICLO_SEG,
        ciclo_t0=ciclo_t0)

    while True:   # v9.7.10 (Etapa 2): live pode ter itens com a backfill vazia
        if orcamento.esgotado(processadas) or estourado():
            break
        # v9.7.10 (Etapa 2): LIVE primeiro — drena o vivo ate esvaziar e
        # SO ENTAO usa o tempo restante no passivo (startup/backlog/dlq).
        try:
            item = estado.fila_live.get_nowait()
            origem, sig = item[0], item[1]
            if len(item) > 2 and idade_live_max is None:
                # FIFO: o primeiro vivo drenado e o mais antigo da fila
                idade_live_max = time.time() - item[2]
        except queue.Empty:
            try:
                item = estado.fila_ws.get_nowait()
                origem, sig = item[0], item[1]
            except Exception:
                break

        # Já vista pelo polling HTTP ou por ciclo anterior — pula
        # v9.7.12 (Causa 1): reserva ATOMICA — o dreno continuo (task do
        # bridge) roda em paralelo com este fallback e com o polling.
        if not estado.tentar_reservar_sig(sig):
            descartadas += 1
            continue

        try:
            _antes_desc = len(estado.descobertas_no_ciclo)   # v9.7.9 (Etapa 0)
            msgs = analise.analisar_tx(sig)
            fila.extend(msgs)
            estado.marcar_sig(sig)
            processadas += 1
            por_origem[origem] = por_origem.get(origem, 0) + 1
            _novas_desc = len(estado.descobertas_no_ciclo) - _antes_desc
            if _novas_desc > 0:
                descobertas_por_origem[origem] = (
                    descobertas_por_origem.get(origem, 0) + _novas_desc)
        except Exception as e:
            print(f"[WS] Erro em {sig[:16]}...: {e}")
            estado.marcar_sig(sig)   # 1 tx ruim não trava
        finally:
            estado.liberar_reserva_sig(sig)   # v9.7.12 (Causa 1)

    _PRESSAO["ws_itens"] = processadas
    if not estado.fila_ws.empty():
        # v9.7.6: parou no orcamento com fila cheia — e exatamente o ponto:
        # o polling tem tempo garantido. O restante drena nos proximos ciclos.
        log.info("[WS] drain parou no orcamento (itens=%d/%d, %.1fs/%.0fs) — "
                 "fila segue com %d; polling garantido",
                 processadas, orcamento.max_itens, time.time() - t0,
                 orcamento.max_seg, estado.fila_ws.qsize())

    if processadas or descartadas:
        print(f"[{utils.agora()}] 📡 WS: {processadas} sigs processadas, "
              f"{descartadas} já vistas pelo polling "
              f"(+{time.time() - t0:.1f}s)")
        # v9.7.9 (Etapa 0): de onde veio o que o dreno processou e quantas
        # wallets novas cada origem gerou (a hipotese: lixo do firehose).
        if por_origem:
            log.info("[WS] origens do drain: %s | descobertas por origem: %s",
                     por_origem, descobertas_por_origem,
                     extra={"event": "ws_drain_origens", "origens": por_origem,
                            "descobertas": descobertas_por_origem})
    _PRESSAO["ws_origens"] = por_origem
    _PRESSAO["descobertas_por_origem"] = descobertas_por_origem
    # v9.7.10 (Etapa 2): idade do item vivo mais antigo — meta: segundos.
    _PRESSAO["ws_live_idade_seg"] = (round(idade_live_max, 1)
                                     if idade_live_max is not None else None)

    # v9.7.2 (Etapas 1, 2 e 4): LIVE PRIMEIRO — o loop acima drena o live
    # ate o orcamento do ciclo; SO DEPOIS o backfill (backlog duravel do
    # gap recovery + DLQ) entrega, e com HEADROOM (fila abaixo do limiar).
    # Quem tem rede de seguranca duravel cede para quem nao tem — a
    # prioridade agora e estrutural, nao dependente de tuning.
    try:
        from radar.listener import backlog as _sig_backlog
        from radar.runtime.metrics import (
            PROM_BACKLOG_PENDING, PROM_DLQ_DEPTH, PROM_DLQ_OLDEST_AGE,
            PROM_SIG_CONSUMED, PROM_SIG_CONSUME_RATE, PROM_SIG_QUEUE_DEPTH,
        )
        # v9.7.6 (#4/#5/#6/#14): pressao do pipeline — UMA classificacao
        # (ocupacao + DLQ/backlog crescendo + deadline recorrente). Sob
        # pressao o backfill CEDE (sigs ficam no duravel — NADA se perde)
        # e retoma sozinho quando a pressao cai.
        from radar.listener import ingestor as _ing
        _ocup = 100.0 * max(estado.fila_ws.qsize(), estado.fila_live.qsize()) / max(1, estado.fila_ws.maxsize)
        _dlq_total = _ing.dlq_adicionadas_total()
        _dlq_added = _dlq_total - _PRESSAO["dlq_total_prev"]
        _PRESSAO["alvos_no_teto"] = 0   # v9.7.8 (Etapa 3): reset por ciclo
        _PRESSAO["dlq_total_prev"] = _dlq_total
        _bl_prev = _PRESSAO.get("backlog_prev")
        _bl_now = _sig_backlog.pending_count()
        _backlog_cresc = (_bl_now >= 0 and _bl_prev is not None
                          and _bl_now > _bl_prev)
        if _bl_now >= 0:
            _PRESSAO["backlog_delta"] = (_bl_now - _bl_prev
                                         if _bl_prev is not None else 0)
            _PRESSAO["backlog_prev"] = _bl_now
        _nivel = _pressure.classificar(
            _ocup, _PRESSAO.get("dlq_crescendo", False), _backlog_cresc,
            _PRESSAO.get("deadline_prev", False),
            limiar_elevado=getattr(config, "PRESSAO_ELEVADA_PCT", 50),
            limiar_alto=getattr(config, "PRESSAO_ALTA_PCT", 75),
            limiar_critico=getattr(config, "PRESSAO_CRITICA_PCT", 90))
        _pressure.atualizar(_nivel)
        _headroom = _pressure.headroom_pct(_nivel)
        _bl = _sig_backlog.drain(estado.fila_ws,
                                 rate_limit=config.BACKFILL_POR_CICLO,
                                 headroom_pct=_headroom)
        _dlq = _sig_backlog.drain_dlq(estado.fila_ws,
                                      rate_limit=config.BACKFILL_POR_CICLO,
                                      headroom_pct=_headroom)
        # v9.7.6 (#6): DLQ crescendo de forma SUSTENTADA => DEGRADED com
        # motivo (NUNCA derruba o bot); estancou => sai sozinho (estreito,
        # mesmo padrao do 'reconectando').
        _crescendo = _pressure.registrar_ciclo_dlq(
            _dlq_added, _dlq.get("delivered", 0),
            getattr(config, "DLQ_CRESCIMENTO_CICLOS", 3))
        _PRESSAO["dlq_crescendo"] = _crescendo
        _PRESSAO["dlq_added"] = _dlq_added
        _PRESSAO["dlq_drained"] = _dlq.get("delivered", 0)
        _PRESSAO["dlq_pending"] = _dlq.get("pending")
        _PRESSAO["backlog_pending"] = _bl.get("pending")
        _stb = runtime.get(health.BRIDGE)
        if _crescendo:
            if getattr(_stb, "detail", None) != "DLQ_GROWING":
                runtime.mark_degraded(health.BRIDGE, "DLQ_GROWING")
        elif (getattr(_stb, "status", None) == health.DEGRADED
              and getattr(_stb, "detail", None) == "DLQ_GROWING"):
            runtime.mark_healthy(health.BRIDGE, "DLQ estancou")
        PROM_BACKLOG_PENDING.set(max(0, _bl["pending"] or 0))
        PROM_SIG_QUEUE_DEPTH.set(estado.fila_ws.qsize())
        if _dlq.get("pending") is not None:
            PROM_DLQ_DEPTH.set(max(0, _dlq["pending"]))
        if _dlq.get("oldest_age_s") is not None:
            PROM_DLQ_OLDEST_AGE.set(_dlq["oldest_age_s"])
        # Etapa 4: taxa de consumo real do ciclo, exposta no prometheus.
        _dur = max(0.001, time.time() - t0)
        PROM_SIG_CONSUMED.inc(processadas)
        PROM_SIG_CONSUME_RATE.set(processadas / _dur)
        if _bl["delivered"] or _bl["pending"]:
            log.info("[Backlog] entregues=%d pendentes=%s slots_fechados=%d "
                     "fila=%d/%d",
                     _bl["delivered"], _bl["pending"], _bl["slots_closed"],
                     estado.fila_ws.qsize(), estado.fila_ws.maxsize)
        if _dlq.get("delivered"):
            log.info("[DLQ] drenados=%d pendentes=%s (live primeiro)",
                     _dlq["delivered"], _dlq["pending"])
    except Exception as _e:
        # Backfill e caminho auxiliar: falha aqui nunca derruba o ciclo.
        log.warning("[Backfill] drain falhou (tenta no proximo ciclo): %s", _e)


def ciclo():
    estado._rpc_ok_no_ciclo = False
    txs_novas = 0
    fila = []

    estado.reset_diario()   # ALERTAS HOJE zera na virada do dia UTC
    t0 = time.time()
    ciclo_id = estado.contador_ciclo + 1
    # 🩺 Zera o recorte de métricas deste ciclo (contadores em memória).
    counters.cycle.start(ciclo_id, config.MAX_CICLO_SEG, 0, counters.rpc_http)
    runtime.mark_healthy(health.LEGACY_RADAR)

    def estourado():
        return time.time() - t0 > config.MAX_CICLO_SEG

    # 📡 v6.0 — PROCESSA SIGS DO WEBSOCKET PRIMEIRO
    # (chegam antes do polling HTTP — vantagem de latency do WS)
    _t_fase = time.time()
    try:
        _processar_sigs_ws(fila, estourado, ciclo_t0=t0)
    except Exception as e:
        print(f"[{utils.agora()}] ⚠️ processar_sigs_ws falhou (seguindo): {e}")
    counters.cycle.add_phase("ws_drain", time.time() - _t_fase)

    # 🔄 POLLING HTTP EXISTENTE (continua igual — fallback + wallets não monitoradas pelo WS)
    _t_fase = time.time()

    # 🔄 POLLING HTTP EXISTENTE (continua igual — fallback + wallets não monitoradas pelo WS)
    # v9.7.9 (Etapa 0): quantas das wallets descobertas NESTE ciclo o
    # PRUNE arquiva no MESMO ciclo (o churn descoberta->poda infla
    # calls/alvo, descobertas e o JSON).
    _desc_pre_prune = set(estado.descobertas_no_ciclo)
    analise.podar_wallets()
    _PRESSAO["prune_mesmo_ciclo"] = sum(
        1 for w in _desc_pre_prune if w in estado.wallets_arquivadas)
    outras = [w for w in analise.wallets_ordenadas() if w not in config.PRIORITARIAS]
    if outras:
        lote = [outras[(estado.rotacao_idx + i) % len(outras)]
                for i in range(min(config.ROTACAO_LOTE, len(outras)))]
        estado.rotacao_idx = (estado.rotacao_idx + config.ROTACAO_LOTE) % len(outras)
    else:
        lote = []
    alvos = [w for w in estado.WALLETS if w in config.PRIORITARIAS] + lote
    # v9.6.7 (Tarefa 4 — fairness do deferral): alvos adiados no ciclo
    # anterior entram NA FRENTE. Antes a ordem era estavel (PRIORITARIAS
    # sempre primeiro + rotacao), e a cauda podia NUNCA ser varrida —
    # starvation silenciosa, sem erro nenhum no log.
    alvos = estado.ordenar_por_varredura(alvos)
    counters.cycle.targets_planned = len(alvos)

    ultimo_prog = t0
    _alvo_ant, _t_alvo = None, time.time()
    for idx, w in enumerate(alvos, 1):
        # Fecha a medição do alvo anterior (robusto a continue/break).
        if _alvo_ant is not None:
            counters.cycle.target_done(_alvo_ant, time.time() - _t_alvo)
            estado.registrar_varredura(_alvo_ant, ciclo_id)
            _alvo_ant = None
        if estourado():
            counters.cycle.mark_deadline()
            # ⏱️ Não basta dizer que estourou: precisa dizer POR QUE.
            log.warning("\n%s", counters.deadline_report(),
                        extra={"event": "cycle_deadline", "cycle_id": ciclo_id})
            break
        # v9.6.7: o alvo so vira "anterior" DEPOIS de passar no deadline.
        # Antes ele era marcado antes do break e o alvo NAO processado era
        # contado como done no fechamento do loop (processed inflado).
        _alvo_ant, _t_alvo = w, time.time()
        if time.time() - ultimo_prog > PROGRESSO_CICLO_SEG:
            ultimo_prog = time.time()
            print(f"[{utils.agora()}] ⏳ ciclo #{estado.contador_ciclo + 1}: "
                  f"{idx}/{len(alvos)} alvos varridos ({time.time() - t0:.0f}s) "
                  f"— RPC lento, seguindo...")
        res = analise.buscar_sigs_novas(w, config.LIMITE_SIGS)   # v9.7.8 (Etapa 1): cursor until
        if not res:
            continue
        if estado.primeira_passada:
            for s in res:
                estado.marcar_sig(s["signature"])
            estado.avancar_cursor(w, res[0]["signature"])   # v9.7.8 (Etapa 1)
            continue
        if res and res[0].get("blockTime"):
            estado.ultima_atividade[w] = res[0]["blockTime"]
        novas = []
        for s in res:
            if s["signature"] in estado.sigs_vistas:
                continue
            if s.get("err") is not None:
                estado.marcar_sig(s["signature"])
                continue
            novas.append(s["signature"])
        if novas:
            estado.stats_wallet(w)["txs"] += len(novas)
        feitas = 0
        for sig in reversed(novas):
            if feitas >= config.MAX_TXS_POR_ALVO or estourado():
                break
            # v9.7.8 (Etapa 3): teto de tempo POR ALVO — uma wallet pesada
            # nao pode comer metade do orcamento do ciclo (2Xs12nQs..=14.1s
            # de 29.7s em 12/08). Estourou: adia com o motivo registrado;
            # o cursor NAO avanca alem do processado, entao o resto volta
            # no proximo ciclo — nada se perde.
            if time.time() - _t_alvo > config.TETO_SEG_POR_ALVO:
                atas_alvo = sum(1 for dono in estado.atas_vigiadas.values()
                                if dono == w)
                _PRESSAO["alvos_no_teto"] = _PRESSAO.get("alvos_no_teto", 0) + 1
                print(f"[{utils.agora()}] ⏱️ alvo {w[:8]}.. ADIADO: teto "
                      f"{config.TETO_SEG_POR_ALVO}s/alvo ({feitas}/{len(novas)} "
                      f"txs, {atas_alvo} atas) — resto no proximo ciclo")
                break
            # v9.7.12 (Causa 1): o dreno continuo pode estar processando
            # esta sig AGORA — reserva atomica evita processamento duplo.
            if not estado.tentar_reservar_sig(sig):
                estado.avancar_cursor(w, sig)   # esta sendo processada: confirmada
                continue
            try:
                _antes_desc = len(estado.descobertas_no_ciclo)   # v9.7.9 (Etapa 0)
                fila.extend(analise.analisar_tx(sig))
                estado.marcar_sig(sig)
                estado.avancar_cursor(w, sig)   # v9.7.8 (Etapa 1)
                feitas += 1
                txs_novas += 1
                _novas_desc = len(estado.descobertas_no_ciclo) - _antes_desc
                if _novas_desc > 0:
                    _dpo = _PRESSAO.setdefault("descobertas_por_origem", {})
                    _dpo["polling"] = _dpo.get("polling", 0) + _novas_desc
            except Exception as e:
                print(f"Erro em {sig[:16]}: {e}")
                estado.marcar_sig(sig)
                estado.avancar_cursor(w, sig)
            finally:
                estado.liberar_reserva_sig(sig)   # v9.7.12 (Causa 1)

    if _alvo_ant is not None:
        counters.cycle.target_done(_alvo_ant, time.time() - _t_alvo)
        estado.registrar_varredura(_alvo_ant, ciclo_id)
    counters.cycle.add_phase("polling_targets", time.time() - _t_fase)
    _t_fase = time.time()  # fase pesada: replay + alocacoes + atas + autoeval

    if estado.primeira_passada:
        analise.atualizar_atas()
        estado.primeira_passada = False
        return

    # replay de wallets recém-descobertas
    if not estado.em_replay and estado.descobertas_no_ciclo and not estourado():
        estado.em_replay = True
        try:
            pendentes = list(dict.fromkeys(estado.descobertas_no_ciclo))
            for w in pendentes[:config.MAX_REPLAYS_CICLO]:
                if estourado():
                    break
                fila.extend(analise.replay_wallet(w, max_txs=5, estourado=estourado))
                estado.descobertas_no_ciclo.remove(w)
        finally:
            estado.em_replay = False

    # tarefas pesadas em ciclos alternados
    if estado.contador_ciclo % 2 == 0 and not estourado():
        print(f"[{utils.agora()}] 🔍 fase pesada: alocações + lançamentos...")
        try:
            analise.checar_alocacoes(fila, estourado=estourado)
        except Exception as e:
            print(f"[{utils.agora()}] ⚠️ checar_alocacoes falhou (seguindo): {e}")
        for funcao in (analise.checar_lancamentos, analise.checar_segurando):
            if estourado():
                break
            try:
                funcao(fila, estourado=estourado)
            except Exception as e:
                print(f"[{utils.agora()}] ⚠️ {funcao.__name__} falhou (seguindo): {e}")
        if not estourado():
            try:
                analise.checar_moedas(fila, estourado=estourado)
            except Exception as e:
                print(f"[{utils.agora()}] ⚠️ checar_moedas falhou (seguindo): {e}")
    # v9.7.7 (Etapa 3): retencao + tamanho do feature store (1x/hora).
    # O arquivo cresceu 11MB -> 1353MB em um dia SEM retencao — e ja
    # corrompeu duas vezes. Mesmas garantias do DLQ: NUNCA expira item
    # nao processado (sig_backlog pendente e intocavel).
    if estado.contador_ciclo % 720 == 0 and not estourado():
        try:
            import sqlite3 as _sq
            import threading as _th
            from radar.listener import retention as _retfs
            from radar.runtime import dbpath as _dbp
            _confs = _sq.connect(config.DB_HISTORICO)
            _confs.execute("PRAGMA busy_timeout=5000")
            try:
                _retfs.run_cleanup_historico(_confs, _th.Lock())
            finally:
                _confs.close()
            _retfs.checar_tamanho()
            # v9.7.7 (Etapa 3): guarda periodica de espaco em disco.
            _dbp.guard_espaco_disco(config.DB_HISTORICO,
                                    "feature_store")
        except Exception as _e:
            log.warning("[RetentionFS] falhou (seguindo): %s", _e)

    if estado.contador_ciclo % 4 == 0 and not estourado():
        print(f"[{utils.agora()}] 🔍 fase pesada: contas de token (atas)...")
        try:
            analise.atualizar_atas(estourado=estourado)
        except Exception as e:
            print(f"[{utils.agora()}] ⚠️ atualizar_atas falhou (seguindo): {e}")
        if not estourado():
            try:
                analise.vigiar_atas(fila, estourado=estourado)
            except Exception as e:
                print(f"[{utils.agora()}] ⚠️ vigiar_atas falhou (seguindo): {e}")

    # 🧬 v5.0 — autoavaliação
    try:
        autoavaliacao.checar_resultados_pendentes(estourado=estourado)
    except Exception as e:
        print(f"[{utils.agora()}] ⚠️ autoavaliação falhou (seguindo): {e}")

    try:
        # v9.7.12 (Causa 1): alertas produzidos pelo dreno CONTINUO (task
        # live_drain do bridge) entram na fila do ciclo aqui — a entrega
        # ao Telegram segue serializada no ciclo, como antes.
        while True:
            try:
                fila.append(estado.fila_alertas.get_nowait())
            except queue.Empty:
                break
        telegram.enviar_fila(fila)
    except Exception as e:
        print(f"[{utils.agora()}] ⚠️ enviar_fila falhou (seguindo): {e}")

    if estado.dados_sujos or estado.contador_ciclo % config.SAVE_A_CADA_N_CICLOS == 0:
        persistencia.salvar_dados()
        estado.dados_sujos = False

    if (config.RESUMO_ATIVIDADE and not config.MODO_SO_MOEDAS_NOVAS
            and txs_novas > 0 and not fila):
        telegram.tg(f"⚡ <b>Atividade:</b> {txs_novas} tx(s) de rotina. "
           f"Radar: {len(estado.WALLETS)} wallets + {len(estado.atas_vigiadas)} contas. 🛰️")

    if estado._rpc_ok_no_ciclo:
        estado.falhas_rpc_seguidas = 0
    else:
        estado.falhas_rpc_seguidas += 1
        if estado.falhas_rpc_seguidas >= 3 and time.time() - estado.ultimo_aviso_rpc > 900:
            estado.ultimo_aviso_rpc = time.time()
            telegram.tg("⚠️ <b>RPCs p��blicos instáveis!</b> Radar temporariamente cego — "
               "continuo tentando. Se persistir, use chave grátis da Helius "
               "(helius.dev) na lista config.RPCS do bot.py")

    if config.MANTER_PROVA_DE_VIDA and time.time() - estado.ultimo_evento > config.HEARTBEAT_MIN * 60:
        estado.ultimo_evento = time.time()
        # Requisito 32: o heartbeat informa o que ESTÁ acontecendo, não o
        # que a config pediu. LISTENER_ATIVO=True com WebSocket morto
        # produzia "WS ATIVO" em cima de um radar cego.
        _ws_estado = runtime.status_of(health.LEGACY_WEBSOCKET)
        if runtime.is_operational(health.LEGACY_WEBSOCKET):
            ws_status = "📡 WS ATIVO"
        elif _ws_estado in (health.DISABLED, health.STOPPED):
            ws_status = "📵 só polling"
        else:
            ws_status = f"📵 WS {_ws_estado} — só polling"
        telegram.tg(f"🛰️ <b>Radar online</b> — {config.HEARTBEAT_MIN} min sem moedas novas.\n"
           f"👁️ {len(estado.WALLETS)} wallets + {len(estado.atas_vigiadas)} contas vigiadas\n"
           f"🎯 Modo sniper: só moedas novas chegam aqui\n"
           f"{ws_status} | 🕒 {utils.agora()}")

    # 🩺 CYCLE LOG completo (requisito 22): duração, alvos, RPC, backlog,
    # saúde dos componentes e se o deadline foi atingido.
    counters.cycle.add_phase("heavy_e_autoeval", time.time() - _t_fase)

    # v9.6.7 (Tarefa 4): metrica de starvation — idade maxima sem varredura
    # entre as wallets ativas. WARNING alto se passar do limite: alvo preso
    # no fim da fila NUNCA mais passa em branco.
    _idade_max, _pior_alvo = estado.idade_maxima_sem_varredura(ciclo_id)
    if _idade_max > config.MAX_CICLOS_SEM_VARREDURA and _pior_alvo:
        log.warning(
            "STARVATION: alvo %s sem varredura ha %d ciclos (limite %d)",
            utils.curto(_pior_alvo), _idade_max,
            config.MAX_CICLOS_SEM_VARREDURA,
            extra={"event": "scan_starvation", "wallet": _pior_alvo,
                   "age_cycles": _idade_max})

    counters.cycles_completed += 1
    counters.alerts.incr("alerts_created", len(fila))
    runtime.record_success(health.LEGACY_POLLING,
                           processed=counters.cycle.targets_processed)
    runtime.set_queue_depth(health.LEGACY_WEBSOCKET, estado.fila_ws.qsize())

    _status_fs = runtime.status_of(health.FEATURE_STORE)
    _status_graph = runtime.status_of(health.GRAPH)
    _status_bridge = runtime.status_of(health.BRIDGE)
    _modo_ingest = str(getattr(config, "INGESTION_V8_MODE", "off")).upper()

    # v9.7.6: o deadline DESTE ciclo alimenta a classificacao de pressao
    # do proximo; e a pressao/drenagem entram no relatorio (#13).
    _PRESSAO["deadline_prev"] = counters.cycle.deadline_hit
    from radar.runtime import pressure as _pressure_mod
    log.info("\n%s", counters.cycle_report({
        "wallets_active": len(estado.WALLETS),
        "wallets_archived_total": len(estado.wallets_arquivadas),
        "wallets_discovered_cycle": len(estado.descobertas_no_ciclo),
        "tokens_watched": sum(1 for v in estado.moedas.values()
                              if not v.get("morta")),
        "token_accounts": len(estado.atas_vigiadas),
        "alerts_generated": len(fila),
        "alerts_today": estado.alertas_hoje,
        "ws_queue_depth": estado.fila_ws.qsize(),
        "ws_live_depth": estado.fila_live.qsize(),           # v9.7.10 (Etapa 2)
        "ws_live_idade_seg": (estado.ws_live_idade_seg
                              if estado.ws_live_idade_seg is not None
                              else _PRESSAO.get("ws_live_idade_seg")),
        "ws_live_itens": estado.ws_live_processadas,       # v9.7.12 (Causa 1)
        "ws_dedup_entrada": estado.ws_dedup_entrada,       # v9.7.12 (Causa 3)
        "flush_p95_ms": _flush_stats_seguro().get("p95_ms"),   # v9.7.13 (Causa 1)
        "flush_max_ms": _flush_stats_seguro().get("max_ms"),
        "loop_stall_seg": _loop_stall_seguro(),
        "ws_itens": _PRESSAO.get("ws_itens", 0),
        "pressure": _pressure_mod.nivel_atual(),
        "gap_recovery": _pressure_mod.gap_recovery_estado(),
        "dlq_added": _PRESSAO.get("dlq_added", 0),
        "dlq_drained": _PRESSAO.get("dlq_drained", 0),
        "dlq_pending": _PRESSAO.get("dlq_pending"),
        "backlog_pending": _PRESSAO.get("backlog_pending"),
        "alvos_no_teto": _PRESSAO.get("alvos_no_teto", 0),   # v9.7.8 (Etapa 3)
        "ws_origens": _PRESSAO.get("ws_origens", {}),                    # v9.7.9 (Etapa 0)
        "descobertas_por_origem": _PRESSAO.get("descobertas_por_origem", {}),
        "prune_mesmo_ciclo": _PRESSAO.get("prune_mesmo_ciclo", 0),
        "backlog_delta": _PRESSAO.get("backlog_delta"),
        "feature_store": _status_fs,
        "graph": _status_graph,
        "bridge": _status_bridge,
        "ingestion_v8": _modo_ingest,
        "max_scan_age_cycles": _idade_max,
    }), extra={"event": "cycle_complete", "cycle_id": ciclo_id})

    gui.ui_stats({
        "ciclo": estado.contador_ciclo + 1,
        "duracao": f"{time.time() - t0:.0f}s",
        "wallets": len(estado.WALLETS),
        "arquivadas": len(estado.wallets_arquivadas),
        "moedas": sum(1 for v in estado.moedas.values() if not v.get("morta")),
        "atas": len(estado.atas_vigiadas),
        "alertas": estado.alertas_hoje,
        # 🩺 Cada subsistema fala por si: "RPC OK" nunca mais esconde WS morto.
        "rpc": "OK" if estado._rpc_ok_no_ciclo else "FALHOU",
        "ws": runtime.status_of(health.RPC_WS),
        "db": dbhealth.status_of(dbhealth.ROLE_FEATURE_STORE),
        "grafo": _status_graph,
        "ingest": _modo_ingest,
        "lista_wallets": [(n, utils.curto(w)) for w, n in list(estado.WALLETS.items())[:80]],
    })


def rotina_bot():
    """O trabalho do bot: carrega dados, avisa que ligou e roda os ciclos.
    Quando há painel visual, roda numa thread separada (tela nunca trava)."""

    # 🧬 v5.0 — feature store (SQLite imutável) + grafo de relações
    # 🩺 Requisito 16: cair para o "modo v4.8" precisa dizer POR QUE caiu,
    # o que parou de funcionar e o que continua de pé. E os dois são
    # inicializados separadamente: o grafo depende da feature store, então
    # ele degrada junto — mas o radar legado não.
    # 🩺 v9.6.6 — DEPENDENCY BOOT CHECK: valida imports essenciais ANTES de
    # iniciar threads. Dependencia obrigatoria ausente = componente marcado
    # DISABLED com motivo claro, NUNCA HEALTHY falso.
    from radar.runtime import deps as _deps
    # Tarefa 1: loga interpretador/venv ANTES de tudo. Fora de venv =
    # WARNING alto (pip install em outro interpretador nao chega aqui).
    _deps.log_runtime_environment()
    _deps_missing = _deps.apply_boot_check()

    _fs_ok = False
    try:
        db.inicializar()
        _fs_ok = True
    except Exception as e:
        log_exception("feature_store", "inicializar", e,
                      database_role=dbhealth.ROLE_FEATURE_STORE,
                      database_path=db.ARQUIVO_DB)

    if _fs_ok:
        try:
            # v9.6.6: REMOVIDO o mark_healthy incondicional que ficava aqui.
            # Ele sobrescrevia o DISABLED que grafo.inicializar() marca
            # quando networkx esta ausente — a GUI mostrava GRAPH HEALTHY
            # com o componente desligado. Quem decide o status e o proprio
            # grafo.inicializar(): HEALTHY so apos carregar arestas de
            # verdade, DISABLED sem networkx, DEGRADED sem historico.
            grafo.inicializar()
        except Exception as e:
            log_exception("graph", "inicializar", e)
            runtime.mark_degraded(health.GRAPH, f"inicialização falhou: {e}")
    else:
        runtime.mark_degraded(health.GRAPH, "feature store indisponível")
        runtime.mark_degraded(health.AUTOEVALUATION,
                              "feature store indisponível")
        _motivo = ("FEATURE_STORE_CORRUPT"
                   if dbhealth.status_of(dbhealth.ROLE_FEATURE_STORE)
                   == dbhealth.CORRUPT else "FEATURE_STORE_UNAVAILABLE")
        runtime.declare_degraded_mode(
            reason=_motivo,
            # Requisitos 26 e 27: derivado de RuntimeHealth. A lista fixa
            # anterior anunciava "listener WebSocket / bridge" como
            # disponível mesmo quando o bridge estava bloqueado pelo banco
            # corrompido — exatamente a informação errada na pior hora.
            disabled_features=runtime.disabled_features() or [
                "feature store de tokens/alertas",
            ],
            still_available=runtime.still_available() or [
                "estado vivo em radar_dados.json",
            ])

    # 💾 restaura tudo o que já foi coletado
    if persistencia.carregar_dados():
        estado.contador_ciclo = 1

    # 📡 v6.0 — INICIA O LISTENER WEBSOCKET
    # Roda em thread daemon (não bloqueia o bot).
    _iniciar_listener(_deps_missing)

    # 🏗️ v7.0 — FOUNDATION WORKER (Fases 1.2-1.6)
    # Parser, EntityState, Retention, QueryEngine, Observabilidade
    # Roda em thread daemon separada — completamente isolado do bot de trading.
    if not getattr(config, "FOUNDATION_ATIVO", True):
        runtime.mark_disabled(health.FOUNDATION, "config.FOUNDATION_ATIVO=False")
    else:
        try:
            from radar.foundation import start as _foundation_start
            _foundation_start(port=9090)
            runtime.mark_healthy(health.FOUNDATION, "worker na porta 9090")
            log.info("🏗️ Foundation Worker iniciado (parser, state, observabilidade)",
                     extra={"event": "foundation_started", "port": 9090})
        except Exception as e:
            log_exception("foundation", "start", e)
            runtime.mark_failed(health.FOUNDATION, reason=str(e), exc=e,
                                operation="start")

    # 🔬 v8.0 — FASE 1: RAW INGESTION + CANONICAL TRANSACTION
    # Pipeline paralela (NÃO remove o radar legado — compatibilidade mantida).
    # Persiste raw_events ANTES de qualquer parse.
    # Constrói CanonicalTransaction via getTransaction após persistência.
    try:
        import asyncio, threading
        from radar.ingestion import migration as _ing_migration
        from radar.ingestion.pipeline import IngestionPipeline
        from radar.ingestion.store import RawEventStore
        from radar.ingestion.rpc_client import RobustRPCClient
        from radar.ingestion.ws_manager import WSManager
        from radar.ingestion.slot_tracker import SlotTracker

        # 🩺 Requisitos 18 e 19: a v8 nunca é ligada "no escuro". O modo é
        # explícito e a flag booleana antiga, se ligada, cai em SHADOW —
        # nunca direto em operacional, porque a auditoria de double
        # processing precisa vir antes.
        # 🩺 Requisito 45: um único lugar resolve o modo efetivo. A flag
        # booleana antiga não compete mais com INGESTION_V8_MODE.
        _ING_MODE = config.modo_ingestion()
        _ING_ENABLED = _ING_MODE in ("shadow", "active")
        _ING_LIFECYCLE = ingestion_lifecycle.configure(
            mode=_ING_MODE,
            startup_timeout=getattr(config, "INGESTION_STARTUP_TIMEOUT_SECONDS", 30),
            stale_after=getattr(config, "INGESTION_HEARTBEAT_STALE_SECONDS", 180))
        if _ING_ENABLED:
            # Migração versionada (não destrói dados existentes)
            _ing_migration.run(config.DB_FOUNDATION, do_backup=True)

            _rpc_urls = getattr(config, "RPCS", []) or []
            _ws_url   = getattr(config, "WS_URL", None) or ""
            _progs    = getattr(config, "PROGRAMAS", []) or []

            if _ws_url and _rpc_urls:
                _ing_tracker = SlotTracker(gap_threshold=150, lag_threshold=500)
                _ing_rpc     = RobustRPCClient(
                    urls=_rpc_urls,
                    timeout=15.0, max_retries=4,
                    rate_per_second=40.0,
                )
                _ing_store = RawEventStore(config.DB_FOUNDATION,
                                           parser_version="8.0.0")
                # Subscriptions: logsSubscribe para cada programa rastreado
                _subs = [
                    {"jsonrpc": "2.0", "id": i+1, "method": "logsSubscribe",
                     "params": [{"mentions": [p]}, {"commitment": "confirmed"}]}
                    for i, p in enumerate(_progs[:5])  # max 5 subs
                ]
                _ing_ws = WSManager(
                    ws_url=_ws_url,
                    subscriptions=_subs,
                    slot_tracker=_ing_tracker,
                )
                # v9.6.6 — PHASE 4.1 GRAPH ligado ao pipeline canonico:
                # canonical persistido -> FlowNormalizer -> RelationshipDetector
                # -> GraphStore (mesmo data_foundation.db). Sem parsing
                # duplicado, sem segunda fonte de verdade.
                _ing_graph = None
                try:
                    from radar.ingestion.graph_integration import GraphIntelligence
                    _ing_graph = GraphIntelligence(config.DB_FOUNDATION)
                except Exception as _ge:
                    log_exception("graph_intel", "init", _ge)

                _ing_pipeline = IngestionPipeline(
                    ws_manager=_ing_ws,
                    store=_ing_store,
                    rpc=_ing_rpc,
                    slot_tracker=_ing_tracker,
                    parser_version="8.0.0",
                    graph_processor=_ing_graph,
                )

                # Os objetos acima foram construídos com sucesso: isso é
                # prova real de readiness, não suposição.
                _ING_LIFECYCLE.mark_criterion(
                    ingestion_lifecycle.STORE_INITIALIZED)
                _ING_LIFECYCLE.mark_criterion(
                    ingestion_lifecycle.RPC_CLIENT_INITIALIZED)
                _ING_LIFECYCLE.mark_criterion(
                    ingestion_lifecycle.WS_MANAGER_STARTED)

                def _run_ingestion():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        loop.run_until_complete(_ing_pipeline.start())
                        # Só AQUI a pipeline provou que subiu: workers de pé
                        # e event loop vivo. Antes disso, HEALTHY seria
                        # apenas otimismo.
                        _ING_LIFECYCLE.mark_criterion(
                            ingestion_lifecycle.WORKERS_STARTED)
                        _ING_LIFECYCLE.mark_criterion(
                            ingestion_lifecycle.EVENT_LOOP_ALIVE)
                        _ING_LIFECYCLE.beat()
                        _ING_LIFECYCLE.signal_ready()
                        loop.run_forever()
                    finally:
                        loop.close()

                # A thread nasce em STARTING. Quem promove para HEALTHY é o
                # readiness confirmado lá dentro (requisitos 10 a 14).
                _ING_LIFECYCLE.start(_run_ingestion)

                if _ING_MODE == "shadow":
                    # Shadow observa o banco da v8 em somente leitura. Não
                    # existe caminho daqui para Telegram ou estado legado.
                    shadow.start_store_poller(
                        config.DB_FOUNDATION,
                        report_intervalo=getattr(config, "SHADOW_REPORT_SEG", 300),
                        match_window=getattr(
                            config, "SHADOW_MATCH_WINDOW_SECONDS", 90))
                log.info("🔬 Ingestion Pipeline v8.0 iniciada em modo %s "
                         "(raw_events → canonical_transactions)",
                         _ING_MODE.upper(),
                         extra={"event": "ingestion_started",
                                "ingestion_mode": _ING_MODE})
            else:
                runtime.mark_disabled(health.INGESTION_V8,
                                      "WS_URL ou RPCS não configurados")
                log.warning("⚠️ Ingestion Pipeline v8.0: WS_URL ou RPCS não "
                            "configurados — desativada",
                            extra={"event": "ingestion_disabled"})
        else:
            runtime.mark_disabled(health.INGESTION_V8,
                                  "config.INGESTION_V8_MODE=off")
            log.info("ℹ️ Ingestion Pipeline v8.0: desativada "
                     "(config.INGESTION_V8_MODE=off)",
                     extra={"event": "ingestion_disabled"})
    except Exception as e:
        log_exception("ingestion_v8", "start", e)
        runtime.mark_failed(health.INGESTION_V8, reason=str(e), exc=e,
                            operation="start")

    # Requisitos 30 e 31: a mensagem de startup é GERADA a partir do estado
    # real. O texto fixo anterior afirmava "TEMPO REAL" e "durabilidade
    # total" mesmo com WebSocket morto e banco corrompido — o operador lia e
    # acreditava. Marcamos Telegram/Polling saudáveis ANTES de compor, para
    # que o relatório descreva o estado já conhecido.
    # v9.7.6 (#11): sem credencial configurada o Telegram fica DISABLED
    # com o motivo — nunca HEALTHY falso.
    if getattr(config, "BOT_TOKEN", None):
        runtime.mark_healthy(health.TELEGRAM)
    else:
        runtime.mark_disabled(health.TELEGRAM,
                              "BOT_TOKEN ausente — defina no .env")
    runtime.mark_healthy(health.LEGACY_POLLING)
    if config.GUI_ATIVA and TEM_GUI:
        runtime.mark_healthy(health.DASHBOARD, "painel local ativo")
    else:
        runtime.mark_disabled(health.DASHBOARD, "GUI desligada (modo console)")
    runtime.propagate_dependencies()
    # v9.7.7 (Etapa 7): o resumo/alerta de boot sai DEPOIS da janela de
    # estabilizacao — bridge/WS sobem de forma assincrona e o snapshot do
    # meio do boot dizia "Realtime WebSocket: INDISPONIVEL" com o WS ja
    # saudavel 7s depois. O alerta informa o estado REAL.
    runtime.aguardar_estabilizacao(
        (health.BRIDGE, health.LEGACY_WEBSOCKET, health.RPC_WS),
        timeout_seg=getattr(config, "BOOT_ESTABILIZACAO_SEG", 15.0))
    telegram.tg(rt.reports.startup_telegram_text(
        total_wallets=len(estado.WALLETS), intervalo=config.INTERVALO))
    if not (config.GUI_ATIVA and TEM_GUI):
        runtime.mark_disabled(health.DASHBOARD, "modo console")

    # 🩺 Requisito 4: UMA seção clara depois que todos tentaram iniciar.
    # É aqui que fica óbvio, por exemplo, que Bridge=FAILED enquanto o
    # radar legado segue alertando — exatamente o cenário dos logs reais.
    rt.startup_summary()
    rt.start_monitor(
        heartbeat_interval=getattr(config, "HEALTH_HEARTBEAT_SEG", 60),
        error_interval=getattr(config, "ERROR_SUMMARY_SEG", 600),
        slow_rpc_interval=getattr(config, "RPC_SLOW_SUMMARY_SEG", 600))

    _motivo_saida = "graceful"
    try:
        while True:
            # v9.7.3 (Etapa 1): a janela da GUI sinaliza encerramento
            # ORDENADO — o ciclo corrente termina, o finally salva e o
            # processo sai com o codigo deliberado (43). Nada de matar a
            # thread daemon no meio do boot.
            if getattr(estado, "_encerrar_solicitado", False):
                _motivo_saida = (getattr(estado, "_motivo_saida", None)
                                 or "gui_fechada_pelo_usuario")
                break
            try:
                ciclo()
                estado.contador_ciclo += 1
            except Exception as e:
                # Erro de ciclo é recuperável, mas nunca silencioso: vai
                # stack trace inteiro para o log humano e para o JSONL.
                log_exception("legacy_polling", "ciclo", e,
                              cycle_id=estado.contador_ciclo + 1)
                runtime.record_error(health.LEGACY_POLLING, exc=e,
                                     operation="ciclo", log_it=False)
                gui.ui_event(f"⚠️ Erro no ciclo: {e}")
            time.sleep(config.INTERVALO)
    except KeyboardInterrupt:
        _motivo_saida = "keyboard_interrupt"
        log.info("⏹️ Encerrando pelo usuário...",
                 extra={"event": "shutdown_requested"})
    finally:
        estado._motivo_saida = _motivo_saida  # v9.7.1 (Etapa 4): main le p/ o exit log
        persistencia.salvar_dados()
        log.info("💾 dados salvos. Até a próxima caçada! 🎯",
                 extra={"event": "state_saved"})
        # 🩺 Requisito 55: RUN SUMMARY no encerramento gracioso.
        rt.shutdown(_motivo_saida)


def main():
    import sys  # local: bot.py nao importa sys no topo

    # 🩺 Primeira coisa do processo: logging humano + JSONL com rotação,
    # crash handler, inventário de bancos e quick_check. O quick_check roda
    # em somente leitura e abre o circuito ANTES da primeira escrita — é
    # por isso que um banco corrompido não vira mais spam de erro.
    rt.boot()

    # v9.7.0 (Etapa 3): DATA_DIR sob pasta sincronizada (OneDrive etc.) =
    # BLOCKED e o bot RECUSA subir — o mesmo arquivo corrompeu DUAS vezes
    # na mesma pasta. Exit 42: o INICIAR_BOT.bat NAO reinicia.
    from radar.runtime import dbpath
    _provedor = dbpath.sync_provider_for(config.DATA_DIR)
    if _provedor:
        runtime.mark_blocked(
            health.DATABASE,
            reason=f"DATA_DIR sob pasta sincronizada ({_provedor})",
            root_cause=f"SYNC_DIR:{config.DATA_DIR}")
        log.critical(
            "🚫 DATA_DIR sob %s (%s) — bot RECUSA subir. "
            "Rode: scripts\\mover_bancos.py --destino C:\\radar\\data "
            "e mantenha RADAR_DATA_DIR apontado para la.",
            _provedor, config.DATA_DIR)
        sys.exit(dbhealth.CODIGO_SEM_RESTART)
    print("=" * 60)
    print(f"  BOT {BOT_VERSION_LABEL} — LISTENER WEBSOCKET + FEATURE STORE + ANTI-CONGELA")
    print("=" * 60)
    print(f"  Wallets iniciais: {len(estado.WALLETS)} | Intervalo: {config.INTERVALO}s")
    for w, n in estado.WALLETS.items():
        print(f"   • {n}: {utils.curto(w)}")
    ws_url = getattr(config, "WS_URL", None) or "(não configurado)"
    print(f"  📡 WebSocket: {ws_url}")
    print("=" * 60)

    # v9.7.1 (Etapa 4): a causa da saida e SEMPRE nomeada antes do exit
    # — o RUN SUMMARY e o .bat dependem disso p/ nao reiniciar em loop cego.
    _motivo = None
    try:
        if config.GUI_ATIVA and TEM_GUI:
            print("  🖥️ Abrindo painel visual... (feche a janela p/ parar o bot)")
            gui.iniciar_gui(rotina_bot)
        else:
            if config.GUI_ATIVA and not TEM_GUI:
                print("  ⚠️ tkinter indisponível — seguindo em modo console")
            rotina_bot()
        _motivo = getattr(estado, "_motivo_saida", None) or "retorno_sem_motivo_registrado"
    except Exception as e:
        _motivo = f"excecao_nao_tratada:{type(e).__name__}"
        log_exception("bot", "main", e)
    # v9.7.0 (Etapa 1): se o feature store ficou CORRUPT, sai com 42 —
    # o .bat NAO reinicia (sem loop regravando estado derivado a cada 5s).
    # v9.7.3 (Etapa 2): parada DELIBERADA (janela fechada / Ctrl+C) sai
    # com codigo proprio (43) — o .bat encerra SEM reiniciar. Reiniciar
    # so faz sentido para queda inesperada (exit 0); corrupcao segue 42.
    _codigo = dbhealth.exit_code_final(_motivo)
    log.critical("🏁 processo encerrando: motivo=%s exit_code=%d",
                 _motivo, _codigo,
                 extra={"event": "process_exit", "reason": _motivo,
                        "exit_code": _codigo})
    sys.exit(_codigo)


if __name__ == "__main__":
    main()
