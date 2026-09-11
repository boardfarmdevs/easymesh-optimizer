from __future__ import annotations

import json
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .engine import RoomEngine
from .events import EventHistoryGap, EventStore
from .interactions import InteractionError


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler):
        self.connections = threading.BoundedSemaphore(64)
        self.event_streams = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        request.settimeout(5)
        if not self.connections.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


class RoomDemoServer:
    def __init__(
        self,
        address: tuple[str, int],
        store: EventStore,
        viewer_root: Path,
        interactions: RoomEngine | None = None,
        *,
        replay: bool = False,
    ):
        self.store = store
        self.viewer_root = viewer_root.resolve()
        self.interactions = interactions
        self.viewer_mode = "replay" if replay else "interactive" if interactions is not None else "live"
        handler = self._handler()
        self.httpd = BoundedHTTPServer(address, handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="room-demo-http",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _handler(self):
        store = self.store
        viewer_root = self.viewer_root
        interactions = self.interactions
        viewer_mode = self.viewer_mode

        class Handler(BaseHTTPRequestHandler):
            server_version = "EasyMeshRoomDemo/0.1"

            def log_message(self, format, *args):
                return

            def _headers(
                self,
                status: int,
                content_type: str,
                length: int | None = None,
                extra: dict[str, str] | None = None,
            ):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                if length is not None:
                    self.send_header("Content-Length", str(length))
                self.end_headers()

            def _json(
                self,
                value,
                status: int = HTTPStatus.OK,
                *,
                revision: int | None = None,
            ):
                body = (json.dumps(value, sort_keys=True) + "\n").encode()
                extra = None if revision is None else {
                    "ETag": f'"world-revision-{int(revision)}"'
                }
                self._headers(
                    status, "application/json; charset=utf-8", len(body), extra
                )
                self.wfile.write(body)

            def _interaction_json(self, value, status: int = HTTPStatus.OK):
                revision = value.get("revision") if isinstance(value, dict) else None
                self._json(value, status, revision=revision)

            def _body(self, maximum: int = 64 * 1024) -> dict:
                media_type = self.headers.get("Content-Type", "").split(";", 1)[0]
                if media_type.strip().lower() != "application/json":
                    raise InteractionError(
                        415,
                        "unsupported_media_type",
                        "interactive writes require Content-Type: application/json",
                    )
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as error:
                    raise InteractionError(400, "invalid_length", "invalid Content-Length") from error
                if length <= 0 or length > maximum:
                    raise InteractionError(400, "invalid_body", "a JSON body is required")
                try:
                    value = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise InteractionError(400, "invalid_json", "request body is not valid JSON") from error
                if not isinstance(value, dict):
                    raise InteractionError(400, "invalid_body", "request body must be an object")
                return value

            def _require_same_origin(self) -> None:
                origin = self.headers.get("Origin")
                if not origin:
                    return
                if urlparse(origin).netloc != self.headers.get("Host"):
                    raise InteractionError(
                        403,
                        "origin_mismatch",
                        "cross-origin interactive writes are not allowed",
                    )

            def _expected_revision(self, body: dict) -> int:
                raw = self.headers.get("If-Match")
                if raw is None:
                    raise InteractionError(
                        428,
                        "revision_required",
                        "world mutation requires If-Match: \"world-revision-N\"",
                    )
                value = raw.strip()
                if value.startswith('W/'):
                    value = value[2:].strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                if value.startswith("world-revision-"):
                    value = value[len("world-revision-"):]
                try:
                    revision = int(value)
                except ValueError as error:
                    raise InteractionError(
                        400, "invalid_revision", "If-Match has an invalid revision"
                    ) from error
                supplied = body.get("expected_revision")
                if supplied is not None:
                    try:
                        supplied_revision = int(supplied)
                    except (TypeError, ValueError) as error:
                        raise InteractionError(
                            400, "invalid_revision", "expected_revision is invalid"
                        ) from error
                    if supplied_revision != revision:
                        raise InteractionError(
                            400,
                            "revision_mismatch",
                            "If-Match and expected_revision do not match",
                        )
                return revision

            def _interaction_error(self, error: Exception):
                if isinstance(error, InteractionError):
                    status, code, message = error.status, error.code, str(error)
                else:
                    status, code, message = 502, "medium_error", str(error)
                self._json({"error": code, "message": message}, status)

            @staticmethod
            def _role_path(path: str) -> tuple[str, str] | None:
                parts = path.strip("/").split("/")
                if len(parts) != 5 or parts[:3] != ["api", "demo", "roles"]:
                    return None
                return unquote(parts[3]), parts[4]

            @staticmethod
            def _movement_path(path: str, *, action: bool) -> tuple[str, str | None] | None:
                parts = path.strip("/").split("/")
                expected = 5 if action else 4
                if len(parts) != expected or parts[:3] != ["api", "demo", "movements"]:
                    return None
                return unquote(parts[3]), (parts[4] if action else None)

            def _static(self, relative: str):
                target = (viewer_root / relative).resolve()
                if viewer_root not in target.parents and target != viewer_root:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                body = target.read_bytes()
                if relative == "index.html":
                    body = body.replace(
                        b'<meta name="room-viewer-mode" content="no-connect">',
                        f'<meta name="room-viewer-mode" content="{viewer_mode}">'.encode(),
                        1,
                    )
                content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                self._headers(HTTPStatus.OK, content_type, len(body))
                self.wfile.write(body)

            def _events(self, parsed):
                query = parse_qs(parsed.query)
                raw = self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]
                try:
                    sequence = max(0, int(raw))
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST, "invalid event sequence")
                    return
                if not self.server.event_streams.acquire(blocking=False):
                    self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "event stream capacity reached")
                    return
                try:
                    self._headers(HTTPStatus.OK, "text/event-stream; charset=utf-8")
                    while True:
                        try:
                            events = store.wait_after(sequence, 10)
                        except EventHistoryGap:
                            current = store.current()
                            sequence = current["sequence"]
                            encoded = json.dumps({"reason": "history_expired", "sequence": sequence,
                                                  "current_url": "/api/demo/current"})
                            self.wfile.write(f"id: {sequence}\nevent: reset\ndata: {encoded}\n\n".encode())
                            self.wfile.flush()
                            continue
                        if not events:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        for event in events:
                            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
                            self.wfile.write(f"id: {event['sequence']}\ndata: {encoded}\n\n".encode())
                            sequence = event["sequence"]
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    return
                finally:
                    self.server.event_streams.release()

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    location = "/viewer/" + (f"?{parsed.query}" if parsed.query else "")
                    self._headers(HTTPStatus.FOUND, "text/plain; charset=utf-8", 0, {"Location": location})
                elif parsed.path == "/healthz":
                    current = store.current()
                    complete = store.storage_status()["journal"].get("complete", True)
                    self._json({"status": "ok" if complete else "degraded", "run_id": store.run_id,
                                "evidence_complete": complete,
                                "state": current["state"]})
                elif parsed.path == "/api/demo/current":
                    current = store.current()
                    self._json(current, revision=current["world_revision"])
                elif parsed.path == "/api/demo/world":
                    self._json(store.initial_world if parse_qs(parsed.query).get("initial") == ["1"] else store.current_world())
                elif parsed.path == "/api/demo/worlds" and interactions is not None:
                    self._json(interactions.world_catalog())
                elif parsed.path == "/api/demo/events":
                    self._events(parsed)
                elif parsed.path == "/api/demo/events.json":
                    self._json({
                        "schema": "easymesh.room-demo.events.v1",
                        "run_id": store.run_id,
                        "events": store.all(),
                        "history": store.storage_status()["history"],
                    })
                elif parsed.path == "/api/demo/storage":
                    self._json(store.storage_status())
                elif parsed.path == "/api/demo/interactions" and interactions is not None:
                    snapshot = interactions.snapshot()
                    self._json(snapshot, revision=snapshot["revision"])
                elif parsed.path == "/api/demo/recording/world" and interactions is not None:
                    try:
                        self._json(interactions.recorded_world())
                    except Exception as error:
                        self._interaction_error(error)
                elif parsed.path in {"/viewer", "/viewer/", "/viewer/index.html"}:
                    self._static("index.html")
                elif parsed.path.startswith("/viewer/"):
                    self._static(parsed.path[len("/viewer/"):])
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self):
                parsed = urlparse(self.path)
                if interactions is None:
                    self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only milestone")
                    return
                try:
                    self._require_same_origin()
                    body = self._body(4 * 1024 * 1024 if parsed.path == "/api/demo/world/apply" else 64 * 1024)
                    command_id = str(body.get("command_id") or "")
                    if parsed.path == "/api/demo/traffic-probe":
                        self._interaction_json(interactions.select_traffic_probe(
                            str(body.get("role") or ""), token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body), command_id=command_id,
                        ))
                        return
                    if parsed.path == "/api/demo/playback":
                        self._interaction_json(interactions.playback_control(
                            str(body.get("action") or ""), token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body), command_id=command_id,
                        ))
                        return
                    if parsed.path == "/api/demo/world/apply":
                        self._interaction_json(interactions.apply_world(
                            body.get("world"), token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body), command_id=command_id,
                        ))
                        return
                    if parsed.path == "/api/demo/interactions/lease":
                        if body.get("token"):
                            self._interaction_json(interactions.renew(
                                str(body["token"]), command_id=command_id
                            ))
                        else:
                            self._interaction_json(interactions.acquire(
                                str(body.get("owner") or ""),
                                command_id=command_id,
                            ), 201)
                        return
                    if parsed.path == "/api/demo/recording/start":
                        self._interaction_json(interactions.start_recording(
                            token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body),
                            name=body.get("name"),
                            command_id=command_id,
                        ), 201)
                        return
                    if parsed.path == "/api/demo/recording/stop":
                        self._interaction_json(interactions.stop_recording(
                            token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body),
                            command_id=command_id,
                        ))
                        return
                    matched = self._role_path(parsed.path)
                    if matched is not None and matched[1] == "move":
                        role, _ = matched
                        self._interaction_json(interactions.move(
                            role,
                            token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body),
                            destination=body.get("destination"),
                            speed_mps=body.get("speed_mps"),
                            client_sequence=body.get("client_sequence"),
                            command_id=command_id,
                        ), 201)
                        return
                    movement = self._movement_path(parsed.path, action=True)
                    if movement is not None and movement[1] in {"pause", "resume"}:
                        self._interaction_json(interactions.movement_control(
                            movement[0], movement[1],
                            token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body),
                            command_id=command_id,
                        ))
                        return
                    raise InteractionError(404, "unknown_operation", "unknown interaction operation")
                except Exception as error:
                    self._interaction_error(error)

            def do_PUT(self):
                parsed = urlparse(self.path)
                matched = self._role_path(parsed.path)
                if matched is None or interactions is None:
                    self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only milestone")
                    return
                role, operation = matched
                try:
                    self._require_same_origin()
                    body = self._body()
                    common = {
                        "token": str(body.get("token") or ""),
                        "expected_revision": self._expected_revision(body),
                        "client_sequence": body.get("client_sequence"),
                        "command_id": str(body.get("command_id") or ""),
                    }
                    if operation == "position":
                        result = interactions.position(
                            role, position=body.get("position"),
                            final=bool(body.get("final")), **common,
                        )
                    elif operation == "presence":
                        result = interactions.presence(
                            role, present=body.get("present"), **common,
                        )
                    else:
                        raise InteractionError(404, "unknown_operation", "unknown role operation")
                    self._interaction_json(result)
                except Exception as error:
                    self._interaction_error(error)

            def do_DELETE(self):
                parsed = urlparse(self.path)
                if interactions is None:
                    self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "read-only milestone")
                    return
                try:
                    self._require_same_origin()
                    body = self._body()
                    command_id = str(body.get("command_id") or "")
                    if parsed.path == "/api/demo/interactions/lease":
                        self._interaction_json(interactions.release(
                            str(body.get("token") or ""),
                            command_id=command_id,
                        ))
                        return
                    movement = self._movement_path(parsed.path, action=False)
                    if movement is not None:
                        self._interaction_json(interactions.movement_control(
                            movement[0], "cancel",
                            token=str(body.get("token") or ""),
                            expected_revision=self._expected_revision(body),
                            command_id=command_id,
                        ))
                        return
                    raise InteractionError(404, "unknown_operation", "unknown interaction operation")
                except Exception as error:
                    self._interaction_error(error)

        return Handler
