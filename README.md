# Camera UI

Interface web local para controlar uma camera IP/PTZ pela rede e visualizar o stream no navegador.

O projeto fornece um painel simples para mover a camera, controlar zoom, assistir ao video, ouvir audio, salvar snapshots e gravar videos com audio. Tambem inclui galerias para visualizar e excluir imagens e gravacoes salvas.

## Funcionalidades

- Controle PTZ com botoes de direcao, zoom e parada.
- Video no navegador a partir de RTSP convertido por FFmpeg.
- Audio no navegador a partir de RTSP convertido por FFmpeg.
- Snapshot do quadro atual salvo em `snapshots`.
- Gravacao MP4 salva em `recordings`.
- Galeria de snapshots com visualizador em pop-up.
- Galeria de recordings com thumbnails, player em pop-up e exclusao.
- Tela Identification com resumo, profiles, capacidades e detalhes ONVIF da camera.
- Tela Setup para descobrir profiles e salvar a configuracao da camera no `.env`.
- Configuracao por `.env`.

## Tecnologias

- Python 3.12
- `http.server` da biblioteca padrao
- ONVIF/SOAP para PTZ
- FFmpeg para video, audio, snapshots, thumbnails e gravacoes
- HTML, CSS e JavaScript sem framework

## Camera testada

O app foi testado com uma camera ONVIF identificada como:

```text
Fabricante: Technology
Modelo: IPC
Firmware: 21.00.01.36
Hardware: Ver 2.1
```

Profiles usados nos testes:

```text
IPCProfilesToken0 · MainStream · H264 · 1280x720
IPCProfilesToken1 · SubStream · H264 · 320x180
```

## Como rodar

Instale as dependencias:

```powershell
python -m pip install -r requirements.txt
```

Crie o `.env` a partir do exemplo:

```powershell
copy .env-example .env
```

Edite o `.env` com os dados da camera e rode:

```powershell
python app.py
```

Acesse:

```text
http://127.0.0.1:8080
```

## Documentacao

- [REQUISITOS.md](REQUISITOS.md): requisitos, instalacao, configuracao, acesso pela rede e comandos de validacao.

## Arquivos principais

- `app.py`: backend HTTP local e endpoints da interface.
- `camera.py`: comandos ONVIF/SOAP para PTZ.
- `index.html`: interface web.
- `.env-example`: modelo de configuracao.
- `requirements.txt`: dependencias Python.

## Observacao de seguranca

O sistema foi pensado para uso em rede local. Ele nao possui login ou controle de acesso, entao nao deve ser exposto diretamente na internet.
