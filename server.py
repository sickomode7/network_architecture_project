"""TCP server for the Binary HTTP-like protocol."""

import mimetypes
import os
from pathlib import Path
import socket
import stat
import sys
import threading
from urllib.parse import unquote_to_bytes

from protocol import (
    MAX_PAYLOAD_SIZE, ProtocolError, REQUEST, RESPONSE, Frame, encode_frame,
    read_frame, send_all,
)

MAX_ACTIVE_CLIENTS = 64
CLIENT_IO_TIMEOUT_SECONDS = 30


def _response(status, body=b"", content_type="text/plain; charset=utf-8"):
    return Frame(RESPONSE, status=status,
                 headers=(("content-type", content_type),), body=body)


def _send_response(sock, response):
    send_all(sock, encode_frame(response))


def _read_file_limited(file, max_size):
    body = bytearray()
    while len(body) <= max_size:
        remaining = max_size + 1 - len(body)
        chunk = file.read(min(64 * 1024, remaining))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body)


def _resolve_request_path(root, target):
    if not target.startswith("/") or target.startswith("//"):
        raise ValueError("request path must be an absolute path")
    raw_path = target.split("?", 1)[0].split("#", 1)[0]
    for index, char in enumerate(raw_path):
        if char == "%" and (
            index + 2 >= len(raw_path)
            or any(c not in "0123456789abcdefABCDEF"
                   for c in raw_path[index + 1:index + 3])
        ):
            raise ValueError("invalid percent escape in path")
    try:
        decoded = unquote_to_bytes(raw_path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("path is not valid UTF-8") from error
    if "\x00" in decoded or "\\" in decoded:
        raise ValueError("invalid path character")
    parts = decoded.lstrip("/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError("path traversal is not allowed")

    candidate = root.joinpath(*[part for part in parts if part not in ("", ".")])
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _handle_request(sock, root, frame):
    if frame.frame_type != REQUEST:
        _send_response(sock, _response(400, b"expected a request frame\n"))
        return
    if frame.status != 0 or frame.body:
        _send_response(sock, _response(400, b"invalid request metadata or body\n"))
        return
    paths = [value for name, value in frame.headers if name == ":path"]
    if len(paths) != 1:
        _send_response(sock, _response(400, b"request must contain exactly one :path\n"))
        return

    try:
        file_path = _resolve_request_path(root, paths[0])
    except ValueError as error:
        _send_response(sock, _response(400, (str(error) + "\n").encode("utf-8")))
        return
    if file_path is None:
        _send_response(sock, _response(404, b"not found\n"))
        return
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    empty_response = encode_frame(_response(200, b"", content_type))
    max_body_size = MAX_PAYLOAD_SIZE - (len(empty_response) - 20)
    file_descriptor = None
    try:
        open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        open_flags |= getattr(os, "O_NONBLOCK", 0)
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(file_path, open_flags)
        file_info = os.fstat(file_descriptor)
        if not stat.S_ISREG(file_info.st_mode):
            os.close(file_descriptor)
            file_descriptor = None
            _send_response(sock, _response(404, b"not found\n"))
            return
        if file_info.st_size > max_body_size:
            os.close(file_descriptor)
            file_descriptor = None
            _send_response(sock, _response(500, b"file exceeds maximum response size\n"))
            return
        with os.fdopen(file_descriptor, "rb") as file:
            file_descriptor = None
            body = _read_file_limited(file, max_body_size)
    except (OSError, ValueError):
        _send_response(sock, _response(404, b"not found\n"))
        return
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
    if len(body) > max_body_size:
        _send_response(sock, _response(500, b"file exceeds maximum response size\n"))
        return
    _send_response(sock, _response(200, body, content_type))


def handle_connection(sock, root):
    """Serve frames until the peer closes or framing can no longer recover."""
    while True:
        try:
            frame = read_frame(sock)
        except ProtocolError as error:
            try:
                _send_response(sock, _response(400, b"malformed frame\n"))
            except OSError:
                return
            if not error.recoverable:
                return
            continue
        if frame is None:
            return
        if frame.frame_type not in (REQUEST, RESPONSE):
            continue
        _handle_request(sock, root, frame)


def serve(root, port):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("web root must be a directory")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", port))
        listener.listen(MAX_ACTIVE_CLIENTS)
        print(f"bserve: serving {root} on port {port}", flush=True)
        active_clients = threading.BoundedSemaphore(MAX_ACTIVE_CLIENTS)
        while True:
            try:
                connection, _address = listener.accept()
            except InterruptedError:
                continue
            if not active_clients.acquire(blocking=False):
                connection.close()
                continue
            try:
                connection.settimeout(CLIENT_IO_TIMEOUT_SECONDS)
                thread = threading.Thread(
                    target=_serve_client,
                    args=(connection, root, active_clients),
                    daemon=True,
                )
                thread.start()
            except (OSError, RuntimeError) as error:
                connection.close()
                active_clients.release()
                print(f"bserve: unable to start client handler: {error}",
                      file=sys.stderr)


def _serve_client(connection, root, active_clients=None):
    with connection:
        try:
            handle_connection(connection, root)
        except socket.timeout:
            pass
        except OSError as error:
            print(f"bserve: connection error: {error}", file=sys.stderr)
        finally:
            if active_clients is not None:
                active_clients.release()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: bserve WEB_ROOT PORT", file=sys.stderr)
        return 2
    try:
        port = int(argv[1])
        if not 1 <= port <= 65535:
            raise ValueError("port must be in 1..65535")
        serve(argv[0], port)
    except (OSError, ValueError) as error:
        print(f"bserve: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
