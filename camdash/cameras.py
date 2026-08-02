from __future__ import annotations

import base64
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, urlparse, urlunparse
from xml.etree import ElementTree

import requests

from .config import CameraConfig


LOG = logging.getLogger(__name__)
SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"


class CameraError(RuntimeError):
    pass


def credentialed(url: str, username: str, password: str) -> str:
    if not username or "@" in urlparse(url).netloc:
        return url
    parsed = urlparse(url)
    auth = quote(username, safe="") + ":" + quote(password, safe="") + "@"
    return urlunparse(parsed._replace(netloc=auth + parsed.netloc))


@dataclass(slots=True)
class CameraProbe:
    ok: bool
    services: list[str]
    profiles: list[dict[str, Any]]
    ptz: bool
    events: bool
    error: str = ""


class CameraAdapter:
    def __init__(self, config: CameraConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "CAM-Dashboard/0.1"

    def snapshot_url(self, main: bool = True) -> str:
        value = self.config.snapshot_main if main else self.config.snapshot_sub
        if not value:
            raise CameraError("snapshot URL is not configured")
        return self._template(value)

    def mjpeg_url(self, main: bool = False) -> str:
        value = self.config.mjpeg_main if main else self.config.mjpeg_sub
        if not value:
            raise CameraError("MJPEG URL is not configured")
        return self._template(value)

    def rtsp_url(self, main: bool = True) -> str:
        value = self.config.rtsp_main if main else self.config.rtsp_sub
        if not value:
            raise CameraError("RTSP URL is not configured")
        return credentialed(self._template(value), self.config.username, self.config.password)

    def fetch_snapshot(self, main: bool = True, timeout: int = 12) -> tuple[bytes, str]:
        url = self.snapshot_url(main)
        response = self.session.get(url, auth=self._auth(url), timeout=timeout)
        if response.status_code >= 400:
            raise CameraError(f"snapshot HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0]
        if not response.content or not content_type.startswith("image/"):
            raise CameraError("camera returned an invalid snapshot")
        return response.content, content_type

    def open_mjpeg(self, main: bool = False):
        url = self.mjpeg_url(main)
        response = self.session.get(url, auth=self._auth(url), timeout=(8, 60), stream=True)
        if response.status_code >= 400:
            response.close()
            raise CameraError(f"MJPEG HTTP {response.status_code}")
        return response

    def probe(self) -> CameraProbe:
        return probe_onvif(self.config)

    def ptz(self, command: str, coarse: bool = False) -> None:
        raise CameraError("PTZ is not supported")

    def sd_status(self) -> dict[str, Any]:
        return {"supported": False}

    def _template(self, value: str) -> str:
        return value.format(host=self.config.host, token=quote(self.config.token, safe=""))

    def _auth(self, url: str):
        if self.config.username and "@" not in urlparse(url).netloc:
            return self.config.username, self.config.password
        return None


class ThinginoAdapter(CameraAdapter):
    def snapshot_url(self, main: bool = True) -> str:
        explicit = self.config.snapshot_main if main else self.config.snapshot_sub
        if explicit:
            return self._template(explicit)
        channel = 0 if main else 1
        return f"http://{self.config.host}/x/ch{channel}.jpg?token={quote(self.config.token, safe='')}"

    def mjpeg_url(self, main: bool = False) -> str:
        explicit = self.config.mjpeg_main if main else self.config.mjpeg_sub
        if explicit:
            return self._template(explicit)
        channel = 0 if main else 1
        return f"http://{self.config.host}/x/ch{channel}.mjpg?token={quote(self.config.token, safe='')}"

    def rtsp_url(self, main: bool = True) -> str:
        explicit = self.config.rtsp_main if main else self.config.rtsp_sub
        if explicit:
            return credentialed(self._template(explicit), self.config.username or "thingino", self.config.password)
        channel = 0 if main else 1
        return credentialed(f"rtsp://{self.config.host}/ch{channel}", self.config.username or "thingino", self.config.password)

    def ptz(self, command: str, coarse: bool = False) -> None:
        try:
            OnvifAdapter(self.config).ptz(command, coarse)
            return
        except Exception:
            pass
        directions = {
            "up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
            "up-left": (-1, 1), "up-right": (1, 1), "down-left": (-1, -1), "down-right": (1, -1),
        }
        if command in directions:
            x, y = directions[command]
            response = self.session.get(
                f"http://{self.config.host}/x/json-motor-params.cgi",
                params={"token": self.config.token}, timeout=8,
            )
            response.raise_for_status()
            params = response.json()
            divisor = 10 if coarse else 100
            args = {
                "d": "g", "x": x * float(params.get("steps_pan", 0)) / divisor,
                "y": y * float(params.get("steps_tilt", 0)) / divisor,
            }
        elif command == "center":
            params = self.session.get(
                f"http://{self.config.host}/x/json-motor-params.cgi",
                params={"token": self.config.token}, timeout=8,
            ).json()
            args = {"d": "x", "x": float(params.get("steps_pan", 0)) / 2, "y": float(params.get("steps_tilt", 0)) / 2}
        elif command == "home":
            args = {"d": "r"}
        else:
            raise CameraError("invalid PTZ command")
        args["token"] = self.config.token
        response = self.session.get(f"http://{self.config.host}/x/json-motor.cgi", params=args, timeout=8)
        if response.status_code >= 400:
            raise CameraError(f"PTZ HTTP {response.status_code}")

    def sd_status(self) -> dict[str, Any]:
        response = self.session.get(
            f"http://{self.config.host}/x/tool-sdcard.cgi", params={"token": self.config.token}, timeout=8,
        )
        response.raise_for_status()
        try:
            value = response.json()
            return {"supported": True, "configured": self.config.sd_redundancy, "status": value}
        except ValueError:
            text = response.text
            return {
                "supported": True, "configured": self.config.sd_redundancy,
                "mounted": "/mnt/mmcblk0p1" in text or "mmcblk0p1" in text,
                "message": "SD endpoint reachable",
            }


class OnvifAdapter(CameraAdapter):
    def __init__(self, config: CameraConfig):
        super().__init__(config)
        self._profiles: list[Any] | None = None
        self._media = None
        self._ptz = None

    def _client(self):
        try:
            from onvif import ONVIFCamera
        except ImportError as exc:
            raise CameraError("onvif-zeep is not installed") from exc
        return ONVIFCamera(self.config.host, self.config.onvif_port, self.config.username, self.config.password)

    def _ensure_media(self) -> None:
        if self._media is None:
            client = self._client()
            self._media = client.create_media_service()
            self._profiles = self._media.GetProfiles()

    def _profile(self, main: bool):
        self._ensure_media()
        if not self._profiles:
            raise CameraError("camera has no ONVIF media profiles")
        ranked = sorted(self._profiles, key=lambda p: _profile_pixels(p), reverse=True)
        return ranked[0] if main or len(ranked) == 1 else ranked[-1]

    def snapshot_url(self, main: bool = True) -> str:
        explicit = self.config.snapshot_main if main else self.config.snapshot_sub
        if explicit:
            return self._template(explicit)
        profile = self._profile(main)
        uri = self._media.GetSnapshotUri({"ProfileToken": profile.token}).Uri
        return uri.replace("0.0.0.0", self.config.host)

    def rtsp_url(self, main: bool = True) -> str:
        explicit = self.config.rtsp_main if main else self.config.rtsp_sub
        if explicit:
            return credentialed(self._template(explicit), self.config.username, self.config.password)
        profile = self._profile(main)
        uri = self._media.GetStreamUri({
            "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
            "ProfileToken": profile.token,
        }).Uri.replace("0.0.0.0", self.config.host)
        return credentialed(uri, self.config.username, self.config.password)

    def ptz(self, command: str, coarse: bool = False) -> None:
        try:
            if self._ptz is None:
                self._ptz = self._client().create_ptz_service()
            profile = self._profile(True)
            amount = .35 if coarse else .12
            vectors = {
                "up": (0, amount), "down": (0, -amount), "left": (-amount, 0), "right": (amount, 0),
                "up-left": (-amount, amount), "up-right": (amount, amount),
                "down-left": (-amount, -amount), "down-right": (amount, -amount),
            }
            if command == "home":
                self._ptz.GotoHomePosition({"ProfileToken": profile.token})
            elif command == "center":
                self._ptz.AbsoluteMove({"ProfileToken": profile.token, "Position": {"PanTilt": {"x": 0, "y": 0}}})
            elif command in vectors:
                x, y = vectors[command]
                self._ptz.RelativeMove({"ProfileToken": profile.token, "Translation": {"PanTilt": {"x": x, "y": y}}})
            else:
                raise CameraError("invalid PTZ command")
        except Exception as exc:
            raise CameraError(f"ONVIF PTZ failed: {exc}") from exc


def adapter_for(config: CameraConfig) -> CameraAdapter:
    return ThinginoAdapter(config) if config.adapter == "thingino" else OnvifAdapter(config)


def _profile_pixels(profile: Any) -> int:
    try:
        resolution = profile.VideoEncoderConfiguration.Resolution
        return int(resolution.Width) * int(resolution.Height)
    except Exception:
        return 0


def probe_onvif(config: CameraConfig) -> CameraProbe:
    endpoint = f"http://{config.host}:{config.onvif_port}/onvif/device_service"
    body = f'''<s:Envelope xmlns:s="{SOAP}"><s:Body><tds:GetServices xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices></s:Body></s:Envelope>'''
    try:
        response = requests.post(endpoint, data=body, headers={"Content-Type": "application/soap+xml"}, timeout=8)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        namespaces = [n.text or "" for n in root.iter() if n.tag.endswith("Namespace")]
        services = sorted({n.rstrip("/").split("/")[-2] for n in namespaces})
        profiles: list[dict[str, Any]] = []
        if config.username or not config.needs_credentials:
            try:
                adapter = OnvifAdapter(config)
                adapter._ensure_media()
                for profile in adapter._profiles or []:
                    profiles.append({"token": profile.token, "name": getattr(profile, "Name", profile.token), "pixels": _profile_pixels(profile)})
            except Exception as exc:
                LOG.info("ONVIF profile probe incomplete for %s: %s", config.id, exc)
        return CameraProbe(True, services, profiles, "ptz" in services, "events" in services)
    except Exception as exc:
        return CameraProbe(False, [], [], False, False, str(exc))


def discover_onvif(timeout: float = 3.0) -> list[dict[str, Any]]:
    message_id = f"uuid:{uuid.uuid4()}"
    probe = f'''<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="{SOAP}" xmlns:w="{WSA}" xmlns:d="{WSD}" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>{message_id}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action>{WSD}/Probe</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>'''.encode()
    found: dict[str, dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(.35)
    try:
        sock.sendto(probe, ("239.255.255.250", 3702))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, address = sock.recvfrom(65535)
                root = ElementTree.fromstring(data)
                xaddrs = next((n.text or "" for n in root.iter() if n.tag.endswith("XAddrs")), "")
                scopes = next((n.text or "" for n in root.iter() if n.tag.endswith("Scopes")), "")
                key = xaddrs or address[0]
                found[key] = {"host": address[0], "xaddrs": xaddrs.split(), "scopes": scopes.split()}
            except socket.timeout:
                continue
            except ElementTree.ParseError:
                continue
    finally:
        sock.close()
    return list(found.values())
