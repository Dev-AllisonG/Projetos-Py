@echo off
REM ============================================================
REM  INICIAR BOT COM AUTO-REINICIO (Windows) — versao em radar/version.py
REM  SEMPRE inicia pelo python ABSOLUTO da venv dedicada.
REM  Nunca por `python` solto: o pip de um interpretador NAO
REM  instala para outro (causa raiz do ModuleNotFoundError).
REM  Se o bot travar ou cair, ele volta sozinho em 5 segundos.
REM  Deixe esta janela aberta. Para parar: feche a janela.
REM ============================================================
title Bot Insider - RODANDO (nao feche esta janela)
cd /d "%~dp0"

REM --- Interpretador: venv dedicada FORA do OneDrive (Tarefa 3) ---
set "VENV_PY=C:\radar\.venv\Scripts\python.exe"

REM --- Dados/bancos FORA do OneDrive (v9.7.0): o bot RECUSA subir  ---
REM --- se RADAR_DATA_DIR apontar para pasta sincronizada (exit 42). ---
if not defined RADAR_DATA_DIR set "RADAR_DATA_DIR=C:\radar\data"

if not exist "%VENV_PY%" (
    echo ============================================================
    echo   ERRO: venv nao encontrada em %VENV_PY%
    echo.
    echo   Crie a venv UMA VEZ com estes comandos no PowerShell:
    echo     py -0
    echo     py -3.12 -m venv C:\radar\.venv
    echo     C:\radar\.venv\Scripts\python.exe -m pip install --upgrade pip
    echo     C:\radar\.venv\Scripts\python.exe -m pip install -r "%~dp0requirements.txt"
    echo.
    echo   Se py -3.12 nao existir, use py -3.11 ^(o projeto exige 3.11+^).
    echo   NAO use o pythoncore-3.14 solto da Microsoft Store.
    echo ============================================================
    pause
    exit
)

REM --- Verifica se o bot.py esta na mesma pasta ---
if not exist "bot.py" (
    echo ============================================================
    echo   ERRO: o arquivo bot.py NAO foi encontrado nesta pasta:
    echo   %cd%
    echo.
    echo   COMO RESOLVER:
    echo   1. Baixe o arquivo bot.py
    echo   2. Coloque ele NA MESMA PASTA deste INICIAR_BOT.bat
    echo   3. Atencao: se o nome estiver "bot.py.txt", renomeie
    echo      para apenas "bot.py"
    echo   4. Execute este arquivo de novo
    echo ============================================================
    pause
    exit
)

REM --- Verifica as dependencias obrigatorias NA VENV ---
"%VENV_PY%" -c "import websockets, networkx" 2>nul
if errorlevel 1 (
    echo Instalando dependencias obrigatorias na venv...
    "%VENV_PY%" -m pip install -r "%~dp0requirements.txt"
)

echo ============================================================
echo   BOT INSIDER - INICIANDO...
echo   Interpretador: %VENV_PY%
"%VENV_PY%" -c "import sys; print('   Python:', sys.version.split()[0], '| venv:', sys.prefix != sys.base_prefix)"
echo   Pasta: %cd%
echo   Esta janela precisa ficar aberta.
echo   Se o bot cair, ele reinicia com backoff ^(teto: 5 seguidos^).
echo ============================================================

REM --- v9.7.1 (Etapa 4): backoff + teto de reinicios seguidos.      ---
REM --- Saida NAO-graceful NAO pode virar loop cego a cada 5s.        ---
set /a RESTARTS=0

:loop
"%VENV_PY%" bot.py
REM --- v9.7.3 (Etapa 2): parada DELIBERADA (janela fechada / Ctrl+C) ---
REM --- exit 43 = NAO reiniciar. Checado ANTES do 42 (errorlevel e >=). ---
if errorlevel 43 (
    echo ============================================================
    echo   PARADA DELIBERADA ^(codigo 43^): voce fechou a janela ou
    echo   pediu para parar ^(Ctrl+C^). O bot NAO vai reiniciar
    echo   sozinho — reiniciar seria desobedecer o pedido.
    echo ============================================================
    exit /b 0
)
if errorlevel 42 (
    echo ============================================================
    echo   PARADA PROTEGIDA ^(codigo 42^): banco CORRUPT ou dados sob
    echo   pasta sincronizada. O bot NAO vai reiniciar sozinho para
    echo   nao agravar o dano. Siga as instrucoes do log acima.
    echo ============================================================
    pause
    exit /b 42
)
set /a RESTARTS+=1
if %RESTARTS% GEQ 5 (
    echo ============================================================
    echo   5 reinicios seguidos sem estabilizar — loop interrompido.
    echo   O motivo da saida esta nomeado no log acima ^(RUN SUMMARY^
    echo   e a linha "processo encerrando: motivo=..."^).
    echo   Resolva a causa antes de tentar de novo.
    echo ============================================================
    pause
    exit /b 1
)
set /a ESPERA=5*%RESTARTS%
echo.
echo [%date% %time%] O bot parou. Reinicio #%RESTARTS% em %ESPERA%s ^(backoff^)...
timeout /t %ESPERA% /nobreak >nul
goto loop
