import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

from protocol import (
    HEADER_SIZE, MAX_PAYLOAD_SIZE, REQUEST, RESPONSE, Frame, ProtocolError,
    encode_frame, read_frame, send_all,
)
from server import _read_file_limited, handle_connection


class LimitedSocket:
    """Socket adapter that deterministically fragments reads and writes."""

    def __init__(self, sock, recv_limit=None, send_limit=None,
                 interrupt_recv=False, interrupt_send=False):
        self.sock = sock
        self.recv_limit = recv_limit
        self.send_limit = send_limit
        self.recv_calls = 0
        self.send_calls = 0
        self.interrupt_recv = interrupt_recv
        self.interrupt_send = interrupt_send

    def recv(self, size):
        self.recv_calls += 1
        if self.interrupt_recv:
            self.interrupt_recv = False
            raise InterruptedError()
        if self.recv_limit is not None:
            size = min(size, self.recv_limit)
        return self.sock.recv(size)

    def send(self, data):
        self.send_calls += 1
        if self.interrupt_send:
            self.interrupt_send = False
            raise InterruptedError()
        if self.send_limit is not None:
            data = data[:self.send_limit]
        return self.sock.send(data)


def raw_frame(frame_type=REQUEST, *, flags=0, status=0, payload=b"",
              body_length=0, magic=b"BH", version=1, reserved=0,
              payload_length=None):
    if payload_length is None:
        payload_length = len(payload)
    return struct.pack(
        ">2sBBHHIII", magic, version, frame_type, flags, status,
        payload_length, body_length, reserved,
    ) + payload


def request(path, headers=()):
    return encode_frame(Frame(REQUEST, headers=((":path", path),) + tuple(headers)))


class ServerConnection:
    """A live loopback TCP connection, allowing frame-by-frame exchanges."""

    def __init__(self, root, recv_limit=None, send_limit=None):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client.settimeout(3)
        self.client.connect(listener.getsockname())
        server_sock, _address = listener.accept()
        listener.close()
        self.server_sock = LimitedSocket(server_sock, recv_limit, send_limit)
        self.server_errors = []

        def serve():
            try:
                handle_connection(self.server_sock, root)
            except BaseException as error:
                self.server_errors.append(error)
            finally:
                server_sock.close()

        self.worker = threading.Thread(target=serve, daemon=True)
        self.worker.start()

    def write(self, data, chunk_size=None, use_send_all=False):
        if use_send_all:
            send_all(LimitedSocket(self.client, send_limit=chunk_size), data)
            return
        if chunk_size is None:
            self.client.sendall(data)
            return
        for offset in range(0, len(data), chunk_size):
            self.client.sendall(data[offset:offset + chunk_size])

    def response(self):
        frame = read_frame(self.client)
        if frame is None:
            raise AssertionError("server closed without a response frame")
        return frame

    def finish_input(self):
        try:
            self.client.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def wait_closed(self):
        self.worker.join(timeout=2)
        if self.worker.is_alive():
            self.client.close()
            self.worker.join(timeout=2)
            raise AssertionError("server did not close the connection")
        if self.server_errors:
            raise self.server_errors[0]

    def close(self):
        self.finish_input()
        self.client.close()
        self.worker.join(timeout=2)


class ProtocolServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "www"
        self.root.mkdir()
        (self.root / "text.txt").write_bytes(b"hello, binary HTTP\n")
        (self.root / "100%.txt").write_bytes(b"literal percent")
        (self.root / "image.bin").write_bytes(b"\x00\xff\x10BIN\x80")
        (self.root / "empty.dat").write_bytes(b"")
        self.connections = []

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp.cleanup()

    def connect(self, recv_limit=None, send_limit=None):
        connection = ServerConnection(self.root, recv_limit, send_limit)
        self.connections.append(connection)
        return connection

    def assert_response(self, response, status, body):
        self.assertEqual(response.frame_type, RESPONSE)
        self.assertEqual(response.status, status)
        self.assertEqual(response.body, body)
        self.assertEqual(response.raw_bytes, encode_frame(response))

    def test_normal_get_existing_text_file_wire_response(self):
        connection = self.connect()
        connection.write(request("/text.txt"))
        response = connection.response()
        self.assert_response(response, 200, b"hello, binary HTTP\n")
        self.assertIn(("content-type", "text/plain"), response.headers)

    def test_normal_get_binary_file_preserves_all_body_bytes(self):
        expected = b"\x00\xff\x10BIN\x80"
        connection = self.connect()
        connection.write(request("/image.bin"))
        self.assert_response(connection.response(), 200, expected)

    def test_limited_file_reader_handles_short_reads(self):
        class ShortReader:
            def __init__(self, data):
                self.data = bytearray(data)

            def read(self, size):
                size = min(size, 2, len(self.data))
                chunk = self.data[:size]
                del self.data[:size]
                return bytes(chunk)

        self.assertEqual(
            _read_file_limited(ShortReader(b"\x00\xffbinary"), 20),
            b"\x00\xffbinary",
        )
        self.assertEqual(
            _read_file_limited(ShortReader(b"larger than limit"), 5),
            b"larger",
        )

    def test_missing_file_returns_404_frame(self):
        connection = self.connect()
        connection.write(request("/missing.txt"))
        self.assert_response(connection.response(), 404, b"not found\n")

    def test_malformed_header_count_returns_400_wire_frame(self):
        # Count says one field, but the header section contains no field bytes.
        frame = raw_frame(payload=b"\x00\x01")
        connection = self.connect()
        connection.write(frame)
        self.assert_response(connection.response(), 400, b"malformed frame\n")

    def test_truncated_fixed_header_returns_400_then_closes(self):
        connection = self.connect()
        connection.write(b"BH\x01\x01\x00\x00")
        connection.finish_input()
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        connection.wait_closed()

    def test_truncated_payload_returns_400_then_closes(self):
        frame = raw_frame(payload=b"\x00\x00", payload_length=12)
        connection = self.connect()
        connection.write(frame)
        connection.finish_input()
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        connection.wait_closed()

    def test_invalid_magic_returns_400_and_recovers_at_next_boundary(self):
        connection = self.connect()
        connection.write(raw_frame(magic=b"XX") + request("/text.txt"))
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_invalid_version_returns_400_and_recovers(self):
        connection = self.connect()
        connection.write(raw_frame(version=2) + request("/text.txt"))
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_invalid_body_length_returns_400_and_recovers(self):
        # body_length cannot exceed payload_length on a known request.
        bad = raw_frame(payload=b"\x00\x00", body_length=3)
        connection = self.connect()
        connection.write(bad + request("/text.txt"))
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_excessively_large_payload_returns_400_then_closes(self):
        oversized = raw_frame(payload_length=MAX_PAYLOAD_SIZE + 1)
        connection = self.connect()
        connection.write(oversized)
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        connection.wait_closed()

    def test_invalid_header_count_returns_400_and_recovers(self):
        bad_payload = struct.pack(">H", 65)
        connection = self.connect()
        connection.write(raw_frame(payload=bad_payload) + request("/text.txt"))
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_invalid_name_and_value_lengths_return_400(self):
        malformed_payloads = (
            b"\x00\x01\x00\x00\x00\x00\x00",  # empty name length
            b"\x00\x01\x00\x01\x00\x00\x20\x01x",  # value length > 8192
            b"\x00\x01\x00\x05\x00\x00\x00\x01x",  # declared name truncated
            b"\x00\x01\x00\x01\x00\x00\x00\x03nx",  # declared value truncated
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload.hex()):
                connection = self.connect()
                connection.write(raw_frame(payload=payload))
                self.assert_response(connection.response(), 400, b"malformed frame\n")

    def test_unknown_frame_is_skipped_exactly_then_next_request_is_parsed(self):
        extension_payload = b"\x00\xffNOT-A-HEADER-SECTION\x00\x01"
        unknown = raw_frame(
            frame_type=99, flags=0xA55A, status=0xEEEE,
            payload=extension_payload, body_length=0xFFFFFFFF,
            reserved=0x01020304,
        )
        next_request = request("/text.txt")
        connection = self.connect()
        connection.write(unknown + next_request)
        # No response belongs to the unknown frame. The first response must
        # be for the immediately following request, proving exact skip length.
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_unknown_frame_followed_by_valid_frame_keeps_same_connection(self):
        connection = self.connect()
        identity = connection.client.fileno()
        connection.write(raw_frame(frame_type=250, payload=b"\x00\x01junk"))
        connection.write(request("/text.txt"))
        response = connection.response()
        self.assert_response(response, 200, b"hello, binary HTTP\n")
        self.assertEqual(connection.client.fileno(), identity)

    def test_request_response_request_response_order_on_same_connection(self):
        connection = self.connect()
        identity = connection.client.fileno()

        connection.write(request("/text.txt"))
        first = connection.response()
        self.assert_response(first, 200, b"hello, binary HTTP\n")
        self.assertEqual(connection.client.fileno(), identity)

        connection.write(request("/image.bin"))
        second = connection.response()
        self.assert_response(second, 200, b"\x00\xff\x10BIN\x80")
        self.assertEqual(connection.client.fileno(), identity)

    def test_several_sequential_requests_without_reconnecting(self):
        connection = self.connect()
        identity = connection.client.fileno()
        cases = (
            ("/text.txt", 200, b"hello, binary HTTP\n"),
            ("/empty.dat", 200, b""),
            ("/missing", 404, b"not found\n"),
            ("/image.bin", 200, b"\x00\xff\x10BIN\x80"),
        )
        for path, status, body in cases:
            connection.write(request(path))
            self.assert_response(connection.response(), status, body)
            self.assertEqual(connection.client.fileno(), identity)

    def test_partial_tcp_writes_are_reassembled(self):
        connection = self.connect(recv_limit=3)
        connection.write(request("/text.txt"), chunk_size=1)
        self.assertGreater(connection.server_sock.recv_calls, HEADER_SIZE)
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_send_all_handles_partial_socket_writes(self):
        connection = self.connect()
        encoded = request("/text.txt")
        limited = LimitedSocket(connection.client, send_limit=2)
        send_all(limited, encoded)
        self.assertGreater(limited.send_calls, 1)
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_server_handles_partial_tcp_reads_and_writes(self):
        connection = self.connect(recv_limit=2, send_limit=3)
        connection.write(request("/text.txt"))
        response = connection.response()
        self.assertGreater(connection.server_sock.recv_calls, HEADER_SIZE)
        self.assertGreater(connection.server_sock.send_calls, 1)
        self.assert_response(response, 200, b"hello, binary HTTP\n")

    def test_interrupted_tcp_reads_and_writes_are_retried(self):
        connection = self.connect()
        connection.server_sock.interrupt_recv = True
        connection.server_sock.interrupt_send = True
        connection.write(request("/text.txt"))
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")
        self.assertGreater(connection.server_sock.recv_calls, 1)
        self.assertGreater(connection.server_sock.send_calls, 1)

    def test_client_disconnect_halfway_through_frame_is_clean(self):
        connection = self.connect()
        connection.write(request("/text.txt")[:HEADER_SIZE + 4])
        connection.finish_input()
        self.assert_response(connection.response(), 400, b"malformed frame\n")
        connection.wait_closed()

    def test_empty_file_returns_empty_binary_body(self):
        connection = self.connect()
        connection.write(request("/empty.dat"))
        self.assert_response(connection.response(), 200, b"")

    def test_parent_path_traversal_returns_400(self):
        secret = self.root.parent / "secret.txt"
        secret.write_bytes(b"secret")
        connection = self.connect()
        connection.write(request("/../secret.txt"))
        self.assert_response(connection.response(), 400, b"path traversal is not allowed\n")

    def test_encoded_parent_path_traversal_returns_400(self):
        connection = self.connect()
        connection.write(request("/%2e%2e/secret.txt"))
        self.assert_response(connection.response(), 400, b"path traversal is not allowed\n")

    def test_invalid_percent_escape_returns_400(self):
        for path in ("/bad%2", "/bad%GG"):
            with self.subTest(path=path):
                connection = self.connect()
                connection.write(request(path))
                self.assert_response(connection.response(), 400,
                                     b"invalid percent escape in path\n")

    def test_percent_encoded_percent_filename_is_supported(self):
        connection = self.connect()
        connection.write(request("/100%25.txt"))
        self.assert_response(connection.response(), 200, b"literal percent")

    def test_symlink_outside_root_is_not_served(self):
        secret = self.root.parent / "secret.txt"
        secret.write_bytes(b"secret")
        link = self.root / "outside.txt"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        connection = self.connect()
        connection.write(request("/outside.txt"))
        self.assert_response(connection.response(), 404, b"not found\n")

    def test_symlink_loop_is_not_served_or_crashing(self):
        link = self.root / "loop"
        try:
            link.symlink_to(link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        connection = self.connect()
        connection.write(request("/loop"))
        self.assert_response(connection.response(), 404, b"not found\n")

    def test_oversized_file_returns_500_without_reading_entire_file(self):
        oversized = self.root / "large.bin"
        with oversized.open("wb") as file:
            file.truncate(MAX_PAYLOAD_SIZE)
        connection = self.connect()
        connection.write(request("/large.bin"))
        self.assert_response(connection.response(), 500,
                             b"file exceeds maximum response size\n")

    def test_absolute_path_attempts_return_400(self):
        for target in ("//etc/passwd", "C:/Windows/win.ini", "http://example/a"):
            with self.subTest(target=target):
                connection = self.connect()
                connection.write(request(target))
                self.assert_response(connection.response(), 400,
                                     b"request path must be an absolute path\n")

    def test_very_long_path_is_not_truncated_or_crashed(self):
        target = "/" + ("a" * 4000)
        connection = self.connect()
        connection.write(request(target))
        self.assert_response(connection.response(), 404, b"not found\n")

    def test_multiple_headers_with_one_path_are_accepted(self):
        connection = self.connect()
        connection.write(request("/text.txt", (("x-note", "hello"),)))
        self.assert_response(connection.response(), 200, b"hello, binary HTTP\n")

    def test_zero_headers_is_a_valid_payload_but_invalid_request(self):
        connection = self.connect()
        connection.write(raw_frame(payload=b"\x00\x00"))
        self.assert_response(connection.response(), 400,
                             b"request must contain exactly one :path\n")


if __name__ == "__main__":
    unittest.main()
