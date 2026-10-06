"""Binary framing shared by bserve and bcurl."""

from dataclasses import dataclass
import socket
import struct

MAGIC = b"BH"
VERSION = 1
REQUEST = 1
RESPONSE = 2
HEADER_SIZE = 20
MAX_FRAME_SIZE = 8 * 1024 * 1024
MAX_PAYLOAD_SIZE = MAX_FRAME_SIZE - HEADER_SIZE
MAX_HEADER_COUNT = 64
MAX_NAME_SIZE = 255
MAX_VALUE_SIZE = 8192

_FRAME_HEADER = struct.Struct(">2sBBHHIII")
_FIELD_HEADER = struct.Struct(">HI")
_COUNT = struct.Struct(">H")


class ProtocolError(Exception):
    """A malformed or truncated frame, with stream recovery information."""

    def __init__(self, message, recoverable=True, raw_frame=None):
        super().__init__(message)
        self.recoverable = recoverable
        self.raw_frame = raw_frame


@dataclass
class Frame:
    frame_type: int
    flags: int = 0
    status: int = 0
    headers: tuple = ()
    body: bytes = b""
    raw_bytes: bytes = None


def _encode_headers(headers):
    if len(headers) > MAX_HEADER_COUNT:
        raise ValueError("too many headers")
    encoded = bytearray(_COUNT.pack(len(headers)))
    for name, value in headers:
        name_bytes = name.encode("utf-8")
        value_bytes = value.encode("utf-8")
        if not name_bytes or len(name_bytes) > MAX_NAME_SIZE:
            raise ValueError("invalid header name length")
        if len(value_bytes) > MAX_VALUE_SIZE:
            raise ValueError("header value is too long")
        encoded.extend(_FIELD_HEADER.pack(len(name_bytes), len(value_bytes)))
        encoded.extend(name_bytes)
        encoded.extend(value_bytes)
    return bytes(encoded)


def encode_frame(frame):
    header_section = _encode_headers(frame.headers)
    if len(frame.body) > MAX_PAYLOAD_SIZE:
        raise ValueError("body exceeds maximum frame size")
    payload_length = len(header_section) + len(frame.body)
    if payload_length > MAX_PAYLOAD_SIZE:
        raise ValueError("payload exceeds maximum frame size")
    if not 0 <= frame.flags <= 0xFFFF or not 0 <= frame.status <= 0xFFFF:
        raise ValueError("flags and status must fit in 16 bits")
    if not 0 <= frame.frame_type <= 0xFF:
        raise ValueError("frame type must fit in 8 bits")
    header = _FRAME_HEADER.pack(
        MAGIC, VERSION, frame.frame_type, frame.flags, frame.status,
        payload_length, len(frame.body), 0,
    )
    return header + header_section + frame.body


def _recv_exact(sock, size, *, allow_clean_eof=False):
    data = bytearray()
    while len(data) < size:
        try:
            chunk = sock.recv(size - len(data))
        except InterruptedError:
            continue
        if not chunk:
            if allow_clean_eof and not data:
                return None
            raise ProtocolError("connection ended in the middle of a frame",
                                recoverable=False)
        data.extend(chunk)
    return bytes(data)


def read_frame(sock):
    header = _recv_exact(sock, HEADER_SIZE, allow_clean_eof=True)
    if header is None:
        return None

    magic, version, frame_type, flags, status, payload_length, body_length, reserved = (
        _FRAME_HEADER.unpack(header)
    )
    if payload_length > MAX_PAYLOAD_SIZE:
        raise ProtocolError("payload exceeds maximum frame size", recoverable=False)

    # Read the bounded payload before validating fields so a bad frame with a
    # trustworthy, in-range length does not leave its payload on the stream.
    payload = _recv_exact(sock, payload_length)
    raw_frame = header + payload
    if magic != MAGIC:
        raise ProtocolError("invalid frame magic", raw_frame=raw_frame)
    if version != VERSION:
        raise ProtocolError("unsupported protocol version", raw_frame=raw_frame)

    # The length is sufficient to skip any extension frame, even if it uses
    # flags or reserved values that this version does not interpret.
    if frame_type not in (REQUEST, RESPONSE):
        return Frame(frame_type, flags, status, (), b"", raw_frame)

    if reserved != 0:
        raise ProtocolError("reserved field must be zero", raw_frame=raw_frame)
    if flags != 0:
        raise ProtocolError("unsupported frame flags", raw_frame=raw_frame)
    if body_length > payload_length:
        raise ProtocolError("body length exceeds payload length", raw_frame=raw_frame)
    if frame_type == REQUEST and (status != 0 or body_length != 0):
        raise ProtocolError("request status and body length must be zero",
                            raw_frame=raw_frame)
    if frame_type == RESPONSE and not 100 <= status <= 599:
        raise ProtocolError("response status must be in the range 100..599",
                            raw_frame=raw_frame)

    if len(payload) < _COUNT.size:
        raise ProtocolError("missing header count", raw_frame=raw_frame)
    header_count = _COUNT.unpack_from(payload)[0]
    if header_count > MAX_HEADER_COUNT:
        raise ProtocolError("too many headers", raw_frame=raw_frame)

    offset = _COUNT.size
    end_of_headers = len(payload) - body_length
    headers = []
    for _ in range(header_count):
        if end_of_headers - offset < _FIELD_HEADER.size:
            raise ProtocolError("truncated header length", raw_frame=raw_frame)
        name_length, value_length = _FIELD_HEADER.unpack_from(payload, offset)
        offset += _FIELD_HEADER.size
        if not name_length or name_length > MAX_NAME_SIZE:
            raise ProtocolError("invalid header name length", raw_frame=raw_frame)
        if value_length > MAX_VALUE_SIZE:
            raise ProtocolError("header value is too long", raw_frame=raw_frame)
        field_end = offset + name_length + value_length
        if field_end > end_of_headers:
            raise ProtocolError("header exceeds header section", raw_frame=raw_frame)
        try:
            name = payload[offset:offset + name_length].decode("utf-8")
            value = payload[offset + name_length:field_end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ProtocolError("header is not valid UTF-8",
                                raw_frame=raw_frame) from error
        headers.append((name, value))
        offset = field_end

    if offset != end_of_headers:
        raise ProtocolError("unexpected bytes in header section", raw_frame=raw_frame)
    body = payload[end_of_headers:]
    return Frame(frame_type, flags, status, tuple(headers), body, raw_frame)


def send_all(sock, data):
    view = memoryview(data)
    while view:
        try:
            sent = sock.send(view)
        except InterruptedError:
            continue
        if sent == 0:
            raise OSError("socket connection closed during send")
        view = view[sent:]


def frame_hex(frame_bytes):
    """Format a complete raw frame for the client's verbose output."""
    return "\n".join(
        f"{offset:04x}  " + " ".join(f"{byte:02x}" for byte in frame_bytes[offset:offset + 16])
        for offset in range(0, len(frame_bytes), 16)
    )
