# Requisitos para rodar o Camera UI

Este projeto roda um backend Python local para controlar uma camera IP/PTZ via ONVIF/SOAP e exibir video, audio, snapshots e gravacoes no navegador.

## Requisitos gerais

- A camera e o computador precisam estar na mesma rede local.
- A porta ONVIF/PTZ da camera precisa estar acessivel.
- A porta RTSP da camera precisa estar acessivel.
- O sistema deve ter Python e FFmpeg instalados.

Configuracao testada da camera:

```text
IP: 192.168.1.2
ONVIF/PTZ: 5000
RTSP/video: 554
Usuario: admin
Profile PTZ: IPCProfilesToken0
Profile video: IPCProfilesToken1
Profile audio: IPCProfilesToken0
```

O FFmpeg e obrigatorio para:

- converter RTSP para video no navegador;
- tocar audio no navegador;
- salvar snapshots;
- gerar thumbnails;
- salvar gravacoes MP4.

## Windows

### 1. Instalar Python

Recomendado: Python 3.12.

```powershell
winget install --id Python.Python.3.12 -e
```

Feche e abra o PowerShell. Valide:

```powershell
python --version
py --version
```

Se `python` abrir a Microsoft Store, desative os aliases:

```text
Configuracoes > Apps > Configuracoes avancadas de apps > Aliases de execucao de app
```

Desligue:

```text
python.exe
python3.exe
```

### 2. Instalar FFmpeg

```powershell
winget install --id Gyan.FFmpeg -e
```

Feche e abra o PowerShell. Valide:

```powershell
ffmpeg -version
```

Se `python`, `py` ou `ffmpeg` nao forem encontrados, adicione os caminhos ao Path do usuario.

### 3. Instalar dependencias Python

Dentro da pasta do projeto:

```powershell
cd /caminho/para/camera-ui
python -m pip install -r requirements.txt
```

### 4. Criar e editar o `.env`

```powershell
copy .env-example .env
```

Edite `.env` e preencha os dados da camera, principalmente `CAMERA_PASS`.

### 5. Rodar no Windows

```powershell
cd D:\projetos\camera-ui
python app.py
```

Acesse:

```text
http://127.0.0.1:8080
```

### 6. Acessar pela rede local no Windows

No `.env`, deixe:

```text
CAMERA_UI_HOST=0.0.0.0
CAMERA_UI_PORT=8080
```

Descubra o IP do computador:

```powershell
ipconfig
```

Procure o `Endereco IPv4` da placa Wi-Fi ou Ethernet, por exemplo:

```text
192.168.1.50
```

Em outro dispositivo da mesma rede, acesse:

```text
http://192.168.1.50:8080
```

Se nao abrir, libere a porta no Firewall do Windows. Abra o PowerShell como administrador e rode:

```powershell
New-NetFirewallRule `
  -DisplayName "Camera UI 8080" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8080 `
  -Action Allow
```

Alternativa pela interface:

```text
Seguranca do Windows > Firewall e protecao de rede > Configuracoes avancadas > Regras de Entrada > Nova Regra
```

Escolha:

```text
Porta > TCP > 8080 > Permitir a conexao > Privada
```

### 7. Testes de rede no Windows

Teste a porta RTSP:

```powershell
Test-NetConnection 192.168.1.2 -Port 554
```

Teste a porta ONVIF/PTZ:

```powershell
Test-NetConnection 192.168.1.2 -Port 5000
```

## Linux

Em Linux nativo, o projeto deve rodar normalmente. Em geral, Linux nativo e melhor que WSL para RTSP/UDP, porque evita a camada de NAT do WSL.

### 1. Instalar Python e FFmpeg

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg
```

Valide:

```bash
python3 --version
ffmpeg -version
```

### 2. Instalar dependencias Python

Dentro da pasta do projeto:

```bash
cd /caminho/para/camera-ui
python3 -m pip install -r requirements.txt
```

### 3. Criar e editar o `.env`

```bash
cp .env-example .env
nano .env
```

Preencha os dados da camera, principalmente `CAMERA_PASS`.

### 4. Rodar no Linux

```bash
cd /caminho/para/camera-ui
python3 app.py
```

Acesse:

```text
http://127.0.0.1:8080
```

### 5. Acessar pela rede local no Linux

No `.env`, deixe:

```text
CAMERA_UI_HOST=0.0.0.0
CAMERA_UI_PORT=8080
```

Descubra o IP do computador Linux:

```bash
hostname -I
```

Ou:

```bash
ip addr
```

Em outro dispositivo da mesma rede, acesse:

```text
http://IP-DO-LINUX:8080
```

Se usar `ufw`, libere a porta:

```bash
sudo ufw allow 8080/tcp
```

### 6. Testes de rede no Linux

Se `nc` nao existir:

```bash
sudo apt install netcat-openbsd
```

Teste a porta RTSP:

```bash
nc -vz 192.168.1.2 554
```

Teste a porta ONVIF/PTZ:

```bash
nc -vz 192.168.1.2 5000
```

### Observacao sobre WSL

O projeto pode iniciar no WSL, mas video RTSP por UDP pode ficar instavel por causa de NAT e portas dinamicas. Para este projeto, prefira Windows nativo ou Linux nativo quando o objetivo for video em tempo real.

## Configuracao do `.env`

Modelo recomendado:

```text
CAMERA_IP=192.168.1.2
CAMERA_PORT=5000
CAMERA_USER=admin
CAMERA_PASS=sua-senha
CAMERA_PROFILE=IPCProfilesToken0

CAMERA_UI_HOST=0.0.0.0
CAMERA_UI_PORT=8080

CAMERA_RTSP_TRANSPORT=udp
CAMERA_VIDEO_PROFILE=IPCProfilesToken1
CAMERA_AUDIO_PROFILE=IPCProfilesToken0
CAMERA_ENABLE_ZOOM=auto
CAMERA_SNAPSHOT_DIR=snapshots
CAMERA_RECORDING_DIR=recordings
```

Observacoes:

- `CAMERA_PROFILE` e usado para controle PTZ.
- `CAMERA_VIDEO_PROFILE` e usado para o quadro de video e snapshots.
- `CAMERA_AUDIO_PROFILE` e usado para audio e gravacoes com audio.
- `CAMERA_ENABLE_ZOOM=auto` detecta se a camera anuncia suporte a zoom PTZ. Use `1` para forcar ou `0` para desativar.
- `CAMERA_RTSP_TRANSPORT=udp` foi o modo aceito por esta camera.
- `CAMERA_STREAM_URL` e `CAMERA_DIRECT_STREAM` so sao necessarios se existir um stream HTTP/MJPEG pronto.

## Validacao pelo navegador

Teste a configuracao do backend:

```text
http://127.0.0.1:8080/api/config
```

Teste se o FFmpeg consegue ler um frame RTSP:

```text
http://127.0.0.1:8080/api/stream-test
```

O retorno esperado deve incluir:

```json
{
  "ok": true
}
```

## Dependencias Python

Dependencias atuais:

```text
requests==2.34.2
onvif_zeep==0.2.12
zeep==4.3.2
```

O arquivo `requirements.txt` deve ser mantido no projeto e versionado no Git. Ele e a referencia para instalar as bibliotecas Python em outra maquina ou em um ambiente novo.

Arquivos e pastas gerados automaticamente, como `__pycache__`, `snapshots`, `recordings` e `.thumbs`, nao precisam ser versionados.

## Funcionalidades

- Controle PTZ: botoes de direcao, zoom e stop.
- Video no navegador: RTSP convertido para MJPEG via FFmpeg.
- Audio no navegador: RTSP convertido para MP3 via FFmpeg.
- Snapshot: salva JPG em `snapshots`.
- Gravacao: salva MP4 com video e audio em `recordings`.
- Galeria: visualiza snapshots e recordings.
- Identification: consulta e exibe informacoes ONVIF da camera.
- Setup: descobre profiles da camera e salva configuracoes de stream/PTZ/audio no `.env`.

## Pastas geradas

Estas pastas sao criadas automaticamente quando usadas:

```text
snapshots
recordings
.thumbs
```

## Problemas comuns

### `ffmpegAvailable` aparece como `false`

O FFmpeg nao esta no Path do sistema. Feche e abra o terminal depois da instalacao. Se continuar, adicione o caminho do FFmpeg ao Path.

### Video preto ou instavel

- Confirme `CAMERA_RTSP_TRANSPORT=udp`.
- Confirme `CAMERA_VIDEO_PROFILE=IPCProfilesToken1`.
- Acesse `/api/stream-test` para ver o erro real do FFmpeg.

### Audio sem sincronismo perfeito na gravacao

A camera entrega video e audio em perfis/streams diferentes. A gravacao combina essas entradas no FFmpeg, mas pode haver pequena diferenca perceptivel dependendo da estabilidade do RTSP.

### Porta em uso

Altere no `.env`:

```text
CAMERA_UI_PORT=8081
```

Depois reinicie:

Windows:

```powershell
python app.py
```

Linux:

```bash
python3 app.py
```

## Seguranca

O acesso pela rede local e recomendado apenas em ambiente confiavel. O sistema nao possui login/autenticacao para exposicao direta na internet.
