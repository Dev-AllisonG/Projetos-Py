import os
# -*- coding: utf-8 -*-
"""
============================================================
 DESCOBRIR O CHAT_ID DO SEU GRUPO (rode no seu PC)
============================================================
 COMO USAR:
   1. Adicione o seu bot no grupo do Telegram
   2. Envie QUALQUER mensagem no grupo (ex: "oi")
      → se não funcionar, envie a mensagem RESPONDENDO ao bot
        ou digite /start dentro do grupo
   3. Rode:  python pegar_chat_id.py
   4. O script mostra o ID do grupo (número negativo)
   5. Copie esse número para a linha CHAT_ID do bot.py
============================================================
"""

import time
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

print("=" * 55)
print("  Procurando mensagens recebidas pelo seu bot...")
print("  (envie uma mensagem no grupo AGORA, se ainda não enviou)")
print("=" * 55)

offset = None
encontrados = {}

for tentativa in range(20):  # tenta por ~60 segundos
    try:
        params = {"timeout": 5}
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params=params, timeout=15)
        dados = r.json()
        if not dados.get("ok"):
            print("Erro da API:", dados)
            break

        for upd in dados.get("result", []):
            offset = upd["update_id"] + 1
            # mensagem normal ou evento de bot adicionado ao grupo
            msg = (upd.get("message") or upd.get("my_chat_member")
                   or upd.get("channel_post") or {})
            chat = msg.get("chat") or {}
            if chat:
                encontrados[chat["id"]] = (
                    chat.get("type"), chat.get("title") or chat.get("first_name"))

        if encontrados:
            break
    except Exception as e:
        print("Erro de conexão:", e)

    print(f"  aguardando mensagem... ({tentativa+1}/20)")
    time.sleep(3)

print()
if not encontrados:
    print("❌ Nenhum chat encontrado.")
    print()
    print("Tente isto:")
    print("  1. Confirme que o bot ESTÁ DENTRO do grupo")
    print("  2. Envie /start DENTRO do grupo")
    print("  3. Se não funcionar: no @BotFather use /setprivacy,")
    print("     escolha seu bot e selecione 'Disable', depois")
    print("     envie outra mensagem no grupo e rode de novo")
else:
    print("✅ CHATS ENCONTRADOS:\n")
    for cid, (tipo, nome) in encontrados.items():
        marca = "  ⬅️ ESTE É O ID DO GRUPO!" if tipo in ("group", "supergroup") else ""
        print(f"  CHAT_ID: {cid}   | tipo: {tipo} | nome: {nome}{marca}")
    print()
    print("Copie o número do GRUPO (negativo) para o bot.py:")
    print('  CHAT_ID = "-100xxxxxxxxxx"')
