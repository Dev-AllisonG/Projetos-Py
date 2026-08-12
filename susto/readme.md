# 👻 Susto

Um pequeno prank/jumpscare feito em **Python** com **Pygame**. Abre em tela cheia, mostra uma arte ASCII tremendo e se distorcendo na tela, exibe mensagens de "susto" e dispara efeitos sonoros — tudo isso e se fecha sozinho depois de alguns segundos.

> ⚠️ **Aviso:** este projeto é uma brincadeira (prank). Ele abre em **tela cheia** e toca som alto de propósito. Use com responsabilidade e avise quem for "vítima" 😄 — ou pelo menos tenha o volume sob controle.

---

## ✨ Como funciona

- Abre em **fullscreen**, preenchendo a tela com fundo preto
- Renderiza uma arte ASCII em vermelho, que treme cada vez mais forte conforme o tempo passa (`shake_x` / `shake_y`)
- Exibe as mensagens **"EU ESTOU AQUI"** e **"VOCÊ NÃO PODE SAIR"**
- Aplica efeitos de **glitch** (retângulos coloridos aleatórios) e **distorção** (deslocamento de fatias horizontais da tela), que aumentam de intensidade com o tempo
- Em torno dos 5 segundos, a tela pisca branco e toca dois efeitos sonoros (`jumpscare1.wav` e `jumpscare2.wav`)
- Encerra automaticamente depois de ~8 segundos (ou antes, se apertar **ESC** ou fechar a janela)

---

## 🎮 Controles

| Tecla / Ação      | Efeito                  |
|-------------------|--------------------------|
| `ESC`             | Fecha o programa imediatamente |
| Fechar a janela   | Encerra o programa       |
| *(automático)*    | Encerra sozinho após ~8 segundos |

---

## 🧱 Estrutura do projeto

```
susto/
├── main.py              # script principal
├── requirements.txt      # dependências (pygame)
├── jumpscare1.wav        # efeito sonoro 1
├── jumpscare2.wav        # efeito sonoro 2
├── main.spec              # spec do PyInstaller (para gerar o .exe)
├── dist/main.exe          # executável já compilado (Windows)
└── build/                  # arquivos temporários gerados pelo PyInstaller
```

> `resource_path()` no código trata os caminhos dos `.wav` tanto rodando via `python main.py` quanto rodando o `.exe` empacotado com PyInstaller (usa `sys._MEIPASS` quando congelado).

---

## 🔧 Requisitos

- Python 3.8+
- [Pygame](https://www.pygame.org/)

Instale as dependências:
```bash
pip install -r requirements.txt
```

---

## ▶️ Como executar

**Direto com Python:**
```bash
cd susto
python main.py
```

**Usando o executável já compilado (Windows):**
```bash
cd susto/dist
main.exe
```

---

## 📦 Gerando o executável (PyInstaller)

O repositório já inclui um `main.spec` pronto. Para gerar seu próprio `.exe`:

```bash
pip install pyinstaller
pyinstaller main.spec
```

O executável final aparece em `dist/main.exe`, com os arquivos `.wav` embutidos.

---

## 🚧 Possíveis melhorias futuras

- Tornar a duração do susto e a intensidade dos efeitos configuráveis
- Adicionar suporte multiplataforma para o build (atualmente o `.exe` é só Windows)
- Trocar a arte ASCII fixa por um asset de imagem
- Opção de rodar em janela (não fullscreen) para testes

---

## 📄 Licença

Projeto pessoal de estudo / brincadeira. Sinta-se livre para usar como referência.
