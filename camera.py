#!/usr/bin/env python3
import argparse
import os
import socket
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
from requests.auth import HTTPDigestAuth
from onvif import ONVIFCamera
from onvif.exceptions import ONVIFError
from zeep.transports import Transport


ROOT = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path):
    try:
        with open(path, "r", encoding="utf-8") as env_file:
            lines = env_file.read().splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(os.path.join(ROOT, ".env"))


DEFAULT_IP = os.getenv("CAMERA_IP", "")
DEFAULT_PORT = int(os.getenv("CAMERA_PORT", "5000"))
DEFAULT_USER = os.getenv("CAMERA_USER", "")
DEFAULT_PASS = os.getenv("CAMERA_PASS", "")
DEFAULT_PROFILE = os.getenv("CAMERA_PROFILE", "IPCProfilesToken0")

DIRECTIONS = {
    "left": (-1.0, 0.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "up": (0.0, 1.0, 0.0),
    "down": (0.0, -1.0, 0.0),
    "zoom-in": (0.0, 0.0, 1.0),
    "zoom-out": (0.0, 0.0, -1.0),
    "zoom_in": (0.0, 0.0, 1.0),
    "zoom_out": (0.0, 0.0, -1.0),
    "aproximar": (0.0, 0.0, 1.0),
    "afastar": (0.0, 0.0, -1.0),
}

def clamp(value, minimum=-1.0, maximum=1.0):
    return max(minimum, min(maximum, value))


def connect(args):
    transport = Transport(timeout=args.timeout, operation_timeout=args.timeout)

    return ONVIFCamera(
        args.host,
        args.port,
        args.user,
        args.password,
        encrypt=not args.no_digest,
        adjust_time=args.adjust_time,
        transport=transport,
    )


def get_services(camera):
    media = camera.create_media_service()
    ptz = camera.create_ptz_service()
    return media, ptz


def get_profile(media, profile_token=None):
    profiles = media.GetProfiles()
    if not profiles:
        raise RuntimeError("A camera nao retornou nenhum profile ONVIF.")

    if profile_token:
        for profile in profiles:
            if profile.token == profile_token:
                return profile
        tokens = ", ".join(profile.token for profile in profiles)
        raise RuntimeError(f"Profile '{profile_token}' nao encontrado. Profiles: {tokens}")

    for profile in profiles:
        if getattr(profile, "PTZConfiguration", None):
            return profile

    return profiles[0]


def stop(ptz, profile_token):
    request = ptz.create_type("Stop")
    request.ProfileToken = profile_token
    request.PanTilt = True
    request.Zoom = True
    ptz.Stop(request)


def move(ptz, profile_token, pan, tilt, zoom, duration):
    request = ptz.create_type("ContinuousMove")
    request.ProfileToken = profile_token
    request.Velocity = {}

    if pan or tilt:
        request.Velocity["PanTilt"] = {"x": clamp(pan), "y": clamp(tilt)}

    if zoom:
        request.Velocity["Zoom"] = {"x": clamp(zoom)}

    ptz.ContinuousMove(request)

    if duration > 0:
        time.sleep(duration)
        stop(ptz, profile_token)


def soap_ptz_url(args):
    return f"http://{args.host}:{args.port}/onvif/ptz_service"


def post_soap(args, url, namespace_alias, namespace_url, action_url, body):
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:{namespace_alias}="{namespace_url}"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
{body}
  </s:Body>
</s:Envelope>
"""
    response = requests.post(
        url,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{action_url}"',
        },
        auth=HTTPDigestAuth(args.user, args.password),
        timeout=args.timeout,
    )
    response.raise_for_status()
    return response


def post_ptz_soap(args, action, body):
    return post_soap(
        args,
        soap_ptz_url(args),
        "tptz",
        "http://www.onvif.org/ver20/ptz/wsdl",
        f"http://www.onvif.org/ver20/ptz/wsdl/{action}",
        body,
    )


def stop_raw(args, profile_token):
    body = f"""    <tptz:ContinuousMove>
      <tptz:ProfileToken>{profile_token}</tptz:ProfileToken>
      <tptz:Velocity>
        <tt:PanTilt x="0" y="0"/>
        <tt:Zoom x="0"/>
      </tptz:Velocity>
    </tptz:ContinuousMove>"""
    post_ptz_soap(args, "ContinuousMove", body)


def move_raw(args, profile_token, pan, tilt, zoom, duration):
    velocity = []
    if pan or tilt:
        velocity.append(f'        <tt:PanTilt x="{clamp(pan)}" y="{clamp(tilt)}"/>')
    if zoom:
        velocity.append(f'        <tt:Zoom x="{clamp(zoom)}"/>')

    body = f"""    <tptz:ContinuousMove>
      <tptz:ProfileToken>{profile_token}</tptz:ProfileToken>
      <tptz:Velocity>
{chr(10).join(velocity)}
      </tptz:Velocity>
    </tptz:ContinuousMove>"""
    post_ptz_soap(args, "ContinuousMove", body)

    if duration > 0:
        time.sleep(duration)
        stop_raw(args, profile_token)


def list_profiles(media):
    profiles = media.GetProfiles()
    for profile in profiles:
        has_ptz = "sim" if getattr(profile, "PTZConfiguration", None) else "nao"
        name = getattr(profile, "Name", "")
        print(f"token={profile.token} name={name} ptz={has_ptz}")


def print_status(ptz, profile_token):
    status = ptz.GetStatus({"ProfileToken": profile_token})
    print(status)


def discover(timeout=4.0):
    message_id = uuid.uuid4()
    probe = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>""".encode()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)

    sock.sendto(probe, ("239.255.255.250", 3702))

    found = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock.settimeout(max(0.1, deadline - time.monotonic()))
        try:
            data, address = sock.recvfrom(65535)
        except socket.timeout:
            break

        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue

        xaddrs = []
        scopes = []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            text = (element.text or "").strip()
            if tag == "XAddrs" and text:
                xaddrs.extend(text.split())
            elif tag == "Scopes" and text:
                scopes.extend(text.split())

        found.append((address[0], xaddrs, scopes))

    if not found:
        print("Nenhum dispositivo ONVIF respondeu ao discovery.")
        return

    for host, xaddrs, scopes in found:
        print(f"host={host}")
        for xaddr in xaddrs:
            parsed = urlparse(xaddr)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            print(f"  xaddr={xaddr}")
            print(f"  teste: CAMERA_IP={parsed.hostname} CAMERA_PORT={port} python camera.py profiles")
        if scopes:
            print(f"  scopes={' '.join(scopes)}")


def parse_args():
    parser = argparse.ArgumentParser(description="Move camera PTZ via ONVIF.")
    parser.add_argument(
        "command",
        choices=[
            *DIRECTIONS.keys(),
            "stop",
            "profiles",
            "status",
            "discover",
        ],
        nargs="?",
        default="profiles",
        help="Comando PTZ. Padrao: profiles",
    )
    parser.add_argument("--speed", type=float, default=0.35, help="Velocidade de 0.0 a 1.0.")
    parser.add_argument("--duration", type=float, default=0.5, help="Tempo de movimento em segundos.")
    parser.add_argument(
        "--profile",
        default=DEFAULT_PROFILE,
        help="ProfileToken especifico. Use 'profiles' para listar.",
    )
    parser.add_argument("--host", default=DEFAULT_IP, help="IP/host da camera.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Porta ONVIF da camera.")
    parser.add_argument("--user", default=DEFAULT_USER, help="Usuario ONVIF.")
    parser.add_argument("--password", default=DEFAULT_PASS, help="Senha ONVIF.")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout das chamadas ONVIF em segundos.")
    parser.add_argument(
        "--adjust-time",
        action="store_true",
        help="Ajustar diferenca de relogio para autenticacao WSSE.",
    )
    parser.add_argument(
        "--no-adjust-time",
        action="store_true",
        help="Mantido por compatibilidade; o ajuste de relogio ja fica desligado por padrao.",
    )
    parser.add_argument(
        "--no-digest",
        action="store_true",
        help="Usar senha WSSE em texto em vez de digest. Algumas cameras baratas exigem isso.",
    )
    parser.add_argument(
        "--raw-ptz",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enviar PTZ por SOAP manual. Ativado por padrao nesta camera.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        if args.command == "discover":
            discover()
            return 0

        camera = connect(args)
        media, ptz = get_services(camera)

        if args.command == "profiles":
            list_profiles(media)
            return 0

        profile = get_profile(media, args.profile)
        profile_token = profile.token

        if args.command == "stop":
            if args.raw_ptz:
                stop_raw(args, profile_token)
            else:
                stop(ptz, profile_token)
            print(f"Parado. profile={profile_token}")
            return 0

        if args.command == "status":
            print_status(ptz, profile_token)
            return 0

        pan, tilt, zoom = DIRECTIONS[args.command]
        speed = clamp(args.speed, 0.0, 1.0)
        if args.raw_ptz:
            move_raw(args, profile_token, pan * speed, tilt * speed, zoom * speed, args.duration)
        else:
            move(ptz, profile_token, pan * speed, tilt * speed, zoom * speed, args.duration)
        print(
            f"Movimento enviado: {args.command} "
            f"speed={speed} duration={args.duration}s profile={profile_token}"
        )
        return 0
    except ONVIFError as exc:
        print(f"Erro ONVIF: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)

    print(
        "Dica: se nao conectar, teste outra porta ONVIF, por exemplo:\n"
        "  CAMERA_PORT=8899 python camera.py profiles\n"
        "  CAMERA_PORT=8080 python camera.py profiles\n"
        "A porta 554 geralmente e do RTSP/video, nao do ONVIF/PTZ.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
