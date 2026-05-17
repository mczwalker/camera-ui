import os
from types import SimpleNamespace
from urllib.parse import quote, urlparse, urlunparse

from zeep.helpers import serialize_object

import core_config as config
from camera import (
    DEFAULT_IP,
    DEFAULT_PASS,
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    DEFAULT_USER,
    connect,
    get_services,
)


def camera_args(timeout=5.0):
    return SimpleNamespace(
        host=os.getenv("CAMERA_IP", DEFAULT_IP),
        port=int(os.getenv("CAMERA_PORT", str(DEFAULT_PORT))),
        user=os.getenv("CAMERA_USER", DEFAULT_USER),
        password=os.getenv("CAMERA_PASS", DEFAULT_PASS),
        profile=os.getenv("CAMERA_PROFILE", DEFAULT_PROFILE),
        timeout=timeout,
        no_digest=False,
        adjust_time=False,
    )


def normalize_for_json(value):
    value = serialize_object(value)
    if isinstance(value, dict):
        return {str(key): normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_for_json(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "tag") and hasattr(value, "attrib"):
        return str(value)
    return value


def get_rtsp_uri(profile_token=None):
    args = camera_args(timeout=4.0)
    profile = profile_token or config.VIDEO_PROFILE or args.profile
    try:
        camera = connect(args)
        media, _ = get_services(camera)
        request = media.create_type("GetStreamUri")
        request.ProfileToken = profile
        request.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        return media.GetStreamUri(request).Uri
    except Exception:
        path = "onvif2" if profile == "IPCProfilesToken1" else "onvif1"
        return f"rtsp://{args.host}:554/{path}"


def rtsp_url_with_auth(url, args):
    parsed = urlparse(url)
    if parsed.scheme != "rtsp" or parsed.username:
        return url

    user = quote(args.user, safe="")
    password = quote(args.password, safe="")
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def mask_url(url):
    parsed = urlparse(url)
    if not parsed.password:
        return url

    username = parsed.username or ""
    netloc = f"{username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def profile_supports_zoom(profile):
    ptz_config = getattr(profile, "PTZConfiguration", None)
    if not ptz_config:
        return False

    zoom_fields = [
        "DefaultAbsoluteZoomPositionSpace",
        "DefaultRelativeZoomTranslationSpace",
        "DefaultContinuousZoomVelocitySpace",
        "ZoomLimits",
    ]
    return any(getattr(ptz_config, field, None) for field in zoom_fields)


def camera_supports_zoom():
    if config.ENABLE_ZOOM in {"1", "true", "yes", "on"}:
        return True
    if config.ENABLE_ZOOM in {"0", "false", "no", "off"}:
        return False

    args = camera_args(timeout=4.0)
    try:
        camera = connect(args)
        media, _ = get_services(camera)
        profiles = media.GetProfiles()
    except Exception:
        return False

    for profile in profiles:
        if profile.token == args.profile:
            return profile_supports_zoom(profile)

    return any(profile_supports_zoom(profile) for profile in profiles)


def basic_device_info():
    args = camera_args(timeout=4.0)
    try:
        camera = connect(args)
        info = normalize_for_json(camera.devicemgmt.GetDeviceInformation())
    except Exception:
        return {}

    if not isinstance(info, dict):
        return {}

    return {
        "manufacturer": info.get("Manufacturer"),
        "model": info.get("Model"),
        "firmwareVersion": info.get("FirmwareVersion"),
        "serialNumber": info.get("SerialNumber"),
        "hardwareId": info.get("HardwareId"),
    }


def summarize_profiles(profiles):
    if not isinstance(profiles, list):
        return []

    summary = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        video = profile.get("VideoEncoderConfiguration") or {}
        resolution = video.get("Resolution") if isinstance(video, dict) else {}
        width = resolution.get("Width") if isinstance(resolution, dict) else None
        height = resolution.get("Height") if isinstance(resolution, dict) else None
        summary.append(
            {
                "token": profile.get("token"),
                "name": profile.get("Name"),
                "videoEncoding": video.get("Encoding") if isinstance(video, dict) else None,
                "resolution": f"{width}x{height}" if width and height else None,
                "hasPTZ": bool(profile.get("PTZConfiguration")),
                "hasAudio": bool(profile.get("AudioEncoderConfiguration")),
            }
        )
    return summary


def basic_profiles_info():
    args = camera_args(timeout=4.0)
    try:
        camera = connect(args)
        media, _ = get_services(camera)
        profiles = normalize_for_json(media.GetProfiles())
    except Exception:
        return []

    return summarize_profiles(profiles)


def safe_onvif_call(label, callback):
    try:
        return {"ok": True, "label": label, "value": normalize_for_json(callback())}
    except Exception as exc:
        return {"ok": False, "label": label, "error": str(exc)}


def section_value(sections, label):
    for section in sections:
        if section.get("label") == label and section.get("ok"):
            return section.get("value")
    return None


def summarize_capabilities(capabilities):
    if not isinstance(capabilities, dict):
        return []
    return [str(key) for key, value in capabilities.items() if value]


def summarize_services(services):
    if not isinstance(services, list):
        return []

    summary = []
    for service in services:
        if not isinstance(service, dict):
            continue
        version = service.get("Version", {})
        version_text = ""
        if isinstance(version, dict):
            major = version.get("Major")
            minor = version.get("Minor")
            if major is not None and minor is not None:
                version_text = f"{major}.{minor}"
        summary.append(
            {
                "namespace": service.get("Namespace", ""),
                "version": version_text,
                "xaddr": service.get("XAddr", ""),
            }
        )
    return summary


def identification_summary(args, sections, network_status):
    device_info = section_value(sections, "Device information") or {}
    capabilities = section_value(sections, "Capabilities")
    services = section_value(sections, "Services")
    profiles = section_value(sections, "Profiles")

    return {
        "connection": {
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "profile": args.profile,
            "videoProfile": config.VIDEO_PROFILE or args.profile,
            "audioProfile": config.AUDIO_PROFILE,
        },
        "ptz": {
            "zoomMode": config.ENABLE_ZOOM,
            "supportsZoom": camera_supports_zoom(),
        },
        "network": network_status,
        "device": {
            "manufacturer": device_info.get("Manufacturer"),
            "model": device_info.get("Model"),
            "firmwareVersion": device_info.get("FirmwareVersion"),
            "serialNumber": device_info.get("SerialNumber"),
            "hardwareId": device_info.get("HardwareId"),
        },
        "capabilities": summarize_capabilities(capabilities),
        "services": summarize_services(services),
        "profiles": summarize_profiles(profiles),
    }


def camera_identification(network_status):
    args = camera_args(timeout=6.0)
    camera = connect(args)
    device = camera.devicemgmt
    sections = [
        safe_onvif_call("Device information", device.GetDeviceInformation),
        safe_onvif_call("Hostname", device.GetHostname),
        safe_onvif_call("System date and time", device.GetSystemDateAndTime),
        safe_onvif_call("Network interfaces", device.GetNetworkInterfaces),
        safe_onvif_call("Network protocols", device.GetNetworkProtocols),
        safe_onvif_call("DNS", device.GetDNS),
        safe_onvif_call("NTP", device.GetNTP),
        safe_onvif_call("Scopes", device.GetScopes),
        safe_onvif_call("Capabilities", lambda: device.GetCapabilities({"Category": "All"})),
        safe_onvif_call("Services", lambda: device.GetServices({"IncludeCapability": True})),
    ]

    try:
        media, _ = get_services(camera)
        sections.append(safe_onvif_call("Profiles", media.GetProfiles))
    except Exception as exc:
        sections.append({"ok": False, "label": "Profiles", "error": str(exc)})

    return {
        "target": f"{args.host}:{args.port}",
        "user": args.user,
        "profile": args.profile,
        "summary": identification_summary(args, sections, network_status),
        "sections": sections,
    }
