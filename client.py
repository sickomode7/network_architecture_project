"""Single-connection command-line client for the binary protocol."""

import socket
import struct
import sys
from urllib.parse import urlsplit

from protocol import (
    HEADER_SIZE, REQUEST, RESPONSE, Frame, ProtocolError, encode_frame, read_frame,
    send_all,
)

_FRAME_HEADER = struct.Struct(">2sBBHHIII")
_TYPE_NAMES = {REQUEST: "REQUEST", RESPONSE: "RESPONSE"}


def _parse_target(target):
    parsed = urlsplit("//" + target)
    if not parsed.hostname or parsed.port is None:
        raise ValueError("target must be HOST:PORT/PATH")
    if parsed.scheme or parsed.username or parsed.password:
        raise ValueError("target must be HOST:PORT/PATH")
    if parsed.fragment:
        raise ValueError("URL fragments are not supported")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return parsed.hostname, parsed.port, path


def _verbose(direction, raw, frame=None):
    print(f"--- {direction} ({len(raw)} bytes) ---", file=sys.stderr)
    if len(raw) >= HEADER_SIZE:
        magic, version, frame_type, flags, status, payload_length, body_length, reserved = (
            _FRAME_HEADER.unpack_from(raw)
        )
        type_name = _TYPE_NAMES.get(frame_type, f"UNKNOWN ({frame_type})")
        print(
            "Decoded: "
            f"magic={magic!r} version={version} type={type_name} "
            f"flags=0x{flags:04x} status={status} "
            f"payload_length={payload_length} body_length={body_length} "
            f"reserved=0x{reserved:08x}",
            file=sys.stderr,
        )
        if frame is not None:
            for name, value in frame.headers:
                print(f"Header: {name!r} = {value!r}", file=sys.stderr)
            print(f"Body: {len(frame.body)} bytes", file=sys.stderr)

    print("Offset   Hex bytes                                         ASCII",
          file=sys.stderr)
    for offset in range(0, len(raw), 16):
        chunk = raw[offset:offset + 16]
        hex_bytes = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_bytes = "".join(chr(byte) if 0x20 <= byte <= 0x7e else "." for byte in chunk)
        print(f"{offset:08x}  {hex_bytes:<47}  |{ascii_bytes}|",
              file=sys.stderr)


def request(target, verbose=False):
    host, port, path = _parse_target(target)
    frame = Frame(REQUEST, headers=((":path", path),))
    raw_request = encode_frame(frame)

    # The context manager creates exactly one TCP connection for this request.
    with socket.create_connection((host, port)) as connection:
        send_all(connection, raw_request)
        if verbose:
            _verbose("SENT REQUEST FRAME", raw_request, frame)
        while True:
            try:
                response = read_frame(connection)
            except ProtocolError as error:
                if verbose and error.raw_frame:
                    _verbose("RECEIVED MALFORMED FRAME", error.raw_frame)
                raise
            if response is None:
                raise OSError("server closed without a response")
            if verbose:
                _verbose("RECEIVED FRAME", response.raw_bytes, response)
            # A complete unknown frame was consumed; continue on this socket.
            if response.frame_type not in (REQUEST, RESPONSE):
                continue
            if response.frame_type != RESPONSE:
                raise ProtocolError("received a request frame instead of a response")
            break

    sys.stdout.buffer.write(response.body)
    sys.stdout.buffer.flush()
    return 0 if 200 <= response.status <= 299 else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    verbose = False
    if argv and argv[0] == "-v":
        verbose = True
        argv = argv[1:]
    if len(argv) != 1:
        print("usage: bcurl [-v] HOST:PORT/PATH", file=sys.stderr)
        return 2
    try:
        return request(argv[0], verbose)
    except (OSError, ValueError, ProtocolError) as error:
        print(f"bcurl: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
