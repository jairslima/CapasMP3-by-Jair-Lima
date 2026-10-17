# CapasMP3 by Jair Lima

Um script automatizado para recuperar capas de álbuns MP3 ausentes e embuti-las nos arquivos de áudio.

## Características
- Analisa o diretório de músicas do usuário por padrão (`%USERPROFILE%\Music`) ou outro caminho informado via CLI.
- Identifica "álbuns" ignorando pastas com menos de 2 arquivos MP3.
- Utiliza a API do iTunes para buscar a arte com base nas tags ID3 (`Artist` e `Album`).
- Salva um arquivo `cover.jpg` e embute a imagem apenas nos arquivos `.mp3` que ainda estão sem capa.
- **Continuidade segura**: mantém um registro (`progresso.json`) para retomar a varredura exatamente de onde parou em caso de interrupções.
- Não marca falhas transitórias de rede como concluídas, permitindo nova tentativa em execuções futuras.
- Exibe mensagens de erro mais úteis para depuração.
- Possui modo `audit` para diagnosticar pastas mistas, tags ausentes e agrupamentos lógicos por álbum sem alterar os arquivos.
- Possui modo `fix-tags` para preencher tags ausentes quando a inferência pelo nome do arquivo for confiável.
- Possui modo `fix-covers` para corrigir capas ausentes ou divergentes por grupo lógico de álbum, inclusive em pastas mistas.

## Instalação
```bash
pip install -r requirements.txt
```

## Como usar
Basta rodar o script principal:
```bash
python recuperar_capas.py
```

Para informar caminhos customizados:
```bash
python recuperar_capas.py --music-dir "D:\Musicas" --progress-file "D:\Capas\progresso.json"
```

Para controlar a verbosidade:
```bash
python recuperar_capas.py --quiet
python recuperar_capas.py --verbose
```

Para gerar um diagnóstico sem modificar nada:
```bash
python recuperar_capas.py --mode audit --music-dir "D:\Musicas"
```

Isso gera por padrão:
- `relatorio_audit.md`
- `relatorio_audit.json`

Para preencher tags ausentes a partir do nome do arquivo:
```bash
python recuperar_capas.py --mode fix-tags --music-dir "D:\Musicas"
```

Para corrigir capas por álbum lógico:
```bash
python recuperar_capas.py --mode fix-covers --music-dir "D:\Musicas"
```
