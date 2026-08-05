from __future__ import annotations

import base64
import logging
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse, urlunparse
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


class StreamingHttpResponse:
    def __init__(self, sock: socket.socket, raw, status_code: int, headers: dict[str, str]):
        self._socket = sock
        self.raw = raw
        self.status_code = status_code
        self.headers = headers

    def iter_content(self, chunk_size: int):
        while True:
            chunk = self.raw.read1(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        try:
            self.raw.close()
        finally:
            self._socket.close()


def open_streaming_http(
    url: str, auth: tuple[str, str] | None, connect_timeout: int, read_timeout: int,
) -> StreamingHttpResponse:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CameraError("MJPEG URL must use HTTP or HTTPS")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.create_connection((parsed.hostname, port), timeout=connect_timeout)
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
    sock.settimeout(read_timeout)
    response = None
    try:
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        host = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
        headers = [
            f"GET {target} HTTP/1.1", f"Host: {host}",
            "Accept: multipart/x-mixed-replace", "Connection: close",
        ]
        credentials = auth
        if credentials is None and parsed.username is not None:
            credentials = (unquote(parsed.username), unquote(parsed.password or ""))
        if credentials:
            token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode("ascii")
            headers.append(f"Authorization: Basic {token}")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = HTTPResponse(sock)
        response.begin()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        if response.status >= 400:
            raise CameraError(f"MJPEG HTTP {response.status}")
        return StreamingHttpResponse(sock, response, response.status, response_headers)
    except Exception:
        if response is not None:
            response.close()
        sock.close()
        raise


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

    def open_mjpeg(self, main: bool = False, read_timeout: int = 60):
        url = self.mjpeg_url(main)
        try:
            return open_streaming_http(url, self._auth(url), 8, read_timeout)
        except (OSError, ssl.SSLError) as exc:
            raise CameraError(f"MJPEG connection failed: {type(exc).__name__}") from exc

    def probe(self) -> CameraProbe:
        return probe_onvif(self.config)

    def ptz(self, command: str, coarse: bool = False) -> dict[str, Any]:
        raise CameraError("PTZ is not supported")

    def motor_status(self) -> dict[str, Any]:
        return {"supported": False, "healthy": False, "error": "motor control is not supported"}

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

    def ptz(self, command: str, coarse: bool = False) -> dict[str, Any]:
        directions = {
            "up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
            "up-left": (-1, 1), "up-right": (1, 1), "down-left": (-1, -1), "down-right": (1, -1),
        }
        if command in directions:
            x, y = directions[command]
            params = self._motor_params()
            divisor = 10 if coarse else 100
            args = {
                "d": "g", "x": self._motor_delta(params, "steps_pan", x, divisor),
                "y": self._motor_delta(params, "steps_tilt", y, divisor),
            }
        elif command in {"center", "home"}:
            return self._motor_helper_request({"action": "center"}, timeout=50)
        else:
            raise CameraError("invalid PTZ command")
        args["token"] = self.config.token
        try:
            response = self.session.get(
                f"http://{self.config.host}/x/json-motor.cgi", params=args, timeout=8,
            )
        except requests.RequestException as exc:
            raise CameraError(f"Thingino PTZ request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise CameraError(f"Thingino PTZ HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CameraError("Thingino PTZ returned invalid JSON") from exc
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict) or "xpos" not in message or "ypos" not in message:
            raise CameraError("Thingino PTZ returned no motor position")
        requested = {key: value for key, value in args.items() if key != "token"}
        return {
            "driver": "thingino", "requested": requested,
            "position": {"x": message["xpos"], "y": message["ypos"]},
            "status": message.get("status"),
        }

    def _motor_params(self) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"http://{self.config.host}/x/json-motor-params.cgi",
                params={"token": self.config.token}, timeout=8,
            )
        except requests.RequestException as exc:
            raise CameraError(f"Thingino motor parameters failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise CameraError(f"Thingino motor parameters HTTP {response.status_code}")
        try:
            params = response.json()
        except ValueError as exc:
            raise CameraError("Thingino motor parameters returned invalid JSON") from exc
        if not isinstance(params, dict):
            raise CameraError("Thingino motor parameters returned an invalid payload")
        return params

    def motor_status(self) -> dict[str, Any]:
        return self._motor_helper_request()

    def _motor_helper_request(
        self, extra_params: dict[str, Any] | None = None, timeout: int = 8,
    ) -> dict[str, Any]:
        params = {"token": self.config.token}
        if extra_params:
            params.update(extra_params)
        try:
            response = self.session.get(
                f"http://{self.config.host}/x/camdash-motor.cgi", params=params, timeout=timeout,
            )
        except requests.RequestException as exc:
            raise CameraError(f"Thingino motor health request failed: {type(exc).__name__}") from exc
        if response.status_code == 404:
            raise CameraError("Thingino motor recovery helper is not installed")
        if response.status_code >= 400:
            raise CameraError(f"Thingino motor health HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CameraError("Thingino motor health returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("supported") is not True:
            raise CameraError("Thingino motor health returned an invalid payload")
        return payload

    @staticmethod
    def _motor_limit(params: dict[str, Any], name: str) -> float:
        try:
            value = float(params.get(name, 0))
        except (TypeError, ValueError) as exc:
            raise CameraError(f"Thingino motor calibration {name} is invalid") from exc
        if value <= 0:
            raise CameraError(f"Thingino motor calibration {name} is not configured")
        return value

    @classmethod
    def _motor_delta(cls, params: dict[str, Any], name: str, direction: int, divisor: int) -> int:
        if direction == 0:
            return 0
        amount = max(1, round(cls._motor_limit(params, name) / divisor))
        return direction * amount

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

    def ptz(self, command: str, coarse: bool = False) -> dict[str, Any]:
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
            return {"driver": "onvif", "requested": {"command": command, "coarse": coarse}}
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
