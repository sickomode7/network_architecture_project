import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import client
from protocol import Frame, REQUEST, RESPONSE, ProtocolError, encode_frame


class FakeConnection:
    def __init__(self, incoming, send_limit=5, recv_limit=3):
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()
        self.send_limit = send_limit
        self.recv_limit = recv_limit
        self.send_calls = 0
        self.recv_calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        return False

    def send(self, data):
        self.send_calls += 1
        count = min(self.send_limit, len(data))
        self.outgoing.extend(data[:count])
        return count

    def recv(self, size):
        self.recv_calls += 1
        count = min(self.recv_limit, size, len(self.incoming))
        chunk = self.incoming[:count]
        del self.incoming[:count]
        return bytes(chunk)


def response(status, body=b"\x00\xffbody"):
    return encode_frame(
        Frame(RESPONSE, status=status,
              headers=(("content-type", "application/octet-stream"),), body=body)
    )


class ClientTests(unittest.TestCase):
    def run_request(self, wire, *, verbose=False, target="localhost:9000/file.bin"):
        connection = FakeConnection(wire)
        output = io.BytesIO()
        errors = io.StringIO()
        stdout = SimpleNamespace(buffer=output)

        with patch("client.socket.create_connection", return_value=connection) as connect, \
                patch("client.sys.stdout", stdout), \
                patch("client.sys.stderr", errors):
            try:
                exit_code = client.request(target, verbose=verbose)
                error = None
            except (OSError, ValueError, ProtocolError) as caught:
                exit_code = None
                error = caught

        return connection, connect, output.getvalue(), errors.getvalue(), exit_code, error

    def test_one_connection_partial_io_and_unknown_frame_skipping(self):
        unknown = encode_frame(Frame(99, flags=1, body=b"extension"))
        wire_response = response(200, b"\x00\xffsuccess")
        connection, connect, body, errors, exit_code, error = self.run_request(
            unknown + wire_response
        )

        connect.assert_called_once_with(("localhost", 9000))
        self.assertIsNone(error)
        self.assertEqual(exit_code, 0)
        self.assertEqual(body, b"\x00\xffsuccess")
        self.assertEqual(errors, "")
        self.assertEqual(
            connection.outgoing,
            encode_frame(Frame(REQUEST, headers=((":path", "/file.bin"),))),
        )
        self.assertGreater(connection.send_calls, 1)
        self.assertGreater(connection.recv_calls, 20)
        self.assertTrue(connection.closed)

    def test_verbose_dump_is_decoded_and_body_stays_out_of_stderr(self):
        wire_response = response(200, b"BODY\x00\xff")
        connection, connect, body, errors, exit_code, error = self.run_request(
            wire_response, verbose=True
        )

        connect.assert_called_once()
        self.assertIsNone(error)
        self.assertEqual(exit_code, 0)
        self.assertEqual(body, b"BODY\x00\xff")
        self.assertIn("|BODY..|", errors)
        self.assertIn("SENT REQUEST FRAME", errors)
        self.assertIn("RECEIVED FRAME", errors)
        self.assertIn("Decoded:", errors)
        self.assertIn("type=REQUEST", errors)
        self.assertIn("type=RESPONSE", errors)
        self.assertIn("status=200", errors)
        self.assertIn("payload_length=", errors)
        self.assertIn("Header: ':path' = '/file.bin'", errors)
        self.assertIn("Offset   Hex bytes", errors)
        self.assertIn("|BH", errors)
        self.assertEqual(errors.count("SENT REQUEST FRAME"), 1)
        self.assertEqual(errors.count("RECEIVED FRAME"), 1)

    def test_status_exit_policy_is_success_only_for_2xx(self):
        for status, expected_exit in (
            (200, 0), (299, 0), (300, 1), (400, 1), (404, 1), (500, 1), (599, 1)
        ):
            with self.subTest(status=status):
                _conn, connect, body, _errors, exit_code, error = self.run_request(
                    response(status, b"wire body")
                )
                connect.assert_called_once()
                self.assertIsNone(error)
                self.assertEqual(body, b"wire body")
                self.assertEqual(exit_code, expected_exit)

    def test_malformed_complete_response_is_reported_without_crashing(self):
        malformed = bytearray(response(200))
        malformed[0:2] = b"NO"
        connection, connect, body, errors, exit_code, error = self.run_request(
            bytes(malformed), verbose=True
        )

        connect.assert_called_once()
        self.assertIsInstance(error, ProtocolError)
        self.assertIsNone(exit_code)
        self.assertEqual(body, b"")
        self.assertTrue(connection.closed)
        self.assertIn("RECEIVED MALFORMED FRAME", errors)
        self.assertIn("magic=b'NO'", errors)
        self.assertEqual(str(error), "invalid frame magic")

    def test_truncated_response_is_reported_cleanly(self):
        connection, connect, body, errors, exit_code, error = self.run_request(
            response(200)[:8], verbose=True
        )

        connect.assert_called_once()
        self.assertIsInstance(error, ProtocolError)
        self.assertEqual(body, b"")
        self.assertIsNone(exit_code)
        self.assertTrue(connection.closed)
        self.assertIn("SENT REQUEST FRAME", errors)
        self.assertIn("connection ended in the middle of a frame", str(error))

    def test_cli_reports_malformed_response_gracefully(self):
        malformed = bytearray(response(200))
        malformed[0:2] = b"NO"
        connection = FakeConnection(bytes(malformed))
        output = io.BytesIO()
        errors = io.StringIO()
        stdout = SimpleNamespace(buffer=output)

        with patch("client.socket.create_connection", return_value=connection) as connect, \
                patch("client.sys.stdout", stdout), \
                patch("client.sys.stderr", errors):
            exit_code = client.main(["-v", "localhost:9000/file.bin"])

        connect.assert_called_once()
        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), b"")
        self.assertIn("invalid frame magic", errors.getvalue())
        self.assertIn("RECEIVED MALFORMED FRAME", errors.getvalue())

    def test_invalid_response_status_is_rejected(self):
        malformed = bytearray(response(200))
        malformed[6:8] = b"\x00\x63"
        _connection, connect, body, _errors, exit_code, error = self.run_request(
            bytes(malformed)
        )

        connect.assert_called_once()
        self.assertIsInstance(error, ProtocolError)
        self.assertEqual(body, b"")
        self.assertIsNone(exit_code)

    def test_invalid_authority_is_rejected_before_connect(self):
        with patch("client.socket.create_connection") as connect:
            with self.assertRaises(ValueError):
                client.request("localhost:not-a-port/file")
        connect.assert_not_called()

    def test_url_fragment_is_rejected_before_connect(self):
        with patch("client.socket.create_connection") as connect:
            with self.assertRaises(ValueError):
                client.request("localhost:9000/file#fragment")
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
