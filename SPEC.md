# BHTTP/1 Protocol Specification

## 1. Protocol overview

BHTTP/1 is a small HTTP-like request/response protocol carried directly over a
TCP byte stream. It is a project-specific protocol, not HTTP and not HTTP/2.
It borrows the general idea of a fixed binary frame header followed by a
length-delimited payload, but defines its own magic, fields, message types,
header representation, limits, and connection behavior.

Every frame consists of exactly one 20-byte fixed header followed by exactly
the number of payload bytes declared in that header. All integer fields are
unsigned and use network byte order (big-endian). Header text is UTF-8; body
bytes are opaque.

## 2. TCP connection behavior

TCP provides an ordered byte stream, not message boundaries. A sender must
write all bytes of a frame, handling partial writes. A receiver must loop
until it has read the full 20-byte fixed header and then the full bounded
payload. One `recv()` may return part of a frame, one frame, or bytes spanning
multiple frames; a receive call must never be treated as a frame boundary.

The server accepts a TCP connection and reads frames sequentially. For each
request frame it sends one response before processing the next request frame.
The server leaves the connection open after a response and can serve multiple
sequential request frames over that same connection. There is no multiplexing
or request identifier; frame order pairs each response with its request.
Unknown frame types are skipped without a response.

The command-line client opens exactly one TCP connection per invocation,
sends one request, skips any unknown frames received before the response, and
reads a response on that same connection. It does not reconnect or open a
second connection for that request.

## 3. Fixed header layout

The fixed header is **exactly 20 bytes**. Offsets are zero-based from the first
byte of the frame. Each row specifies the complete field width; there are no
implicit padding bytes.

| Offset | Size | Field | Meaning |
|---:|---:|---|---|
| 0 | 2 bytes | Magic | ASCII `BH` (`42 48` hex). Identifies the BHTTP frame envelope. |
| 2 | 1 byte | Version | Protocol version. Version 1 is `01`. |
| 3 | 1 byte | Frame type | `01` request, `02` response. Other values are unknown/extension types. |
| 4 | 2 bytes | Flags | Version 1 known frames require `0000`. See flag rules below. |
| 6 | 2 bytes | Status code | Requests require zero. Responses encode the numeric status here. |
| 8 | 4 bytes | Payload length | Number of bytes after the fixed header, including header count, encoded fields, and body. |
| 12 | 4 bytes | Body length | Number of final payload bytes that are the body. |
| 16 | 4 bytes | Reserved | Must be zero (`00000000`) for known version 1 frames. |

Byte ranges do not overlap: `0..1 + 2 + 3 + 4..5 + 6..7 + 8..11 + 12..15 + 16..19` totals **20 bytes**.

### Byte order

All multi-byte integers (flags, status, payload/body lengths, reserved value,
header count, and individual field lengths) are unsigned big-endian integers.
The magic bytes and UTF-8 strings are byte sequences, not integers.

### Version 1 flags

The flags field is 16 bits wide to leave a stable extension point in the fixed
header. **No flag bits are currently defined.** For known types 1 and 2, the
only valid value in version 1 is zero; any nonzero flags value makes a known
frame malformed. Receivers skip an unknown frame type by its bounded payload
length without interpreting its flags.

### Design rationale

* **Magic (2 bytes):** a short recognizable marker detects traffic that is
  not a BHTTP frame without spending substantial header space.
* **Version (1 byte):** versions 0–255 can be named directly; a receiver can
  reject an unsupported envelope before parsing a version-specific payload.
* **Frame type (1 byte):** supports 256 type numbers while making requests,
  responses, and future extension types cheap to distinguish.
* **Flags (2 bytes):** reserves 16 independent future option bits while
  version 1 keeps behavior deterministic by requiring zero for known frames.
* **Status (2 bytes):** directly stores the numeric status (100–599) without
  converting it to decimal text; zero has a clear request-only meaning.
* **Payload length (4 bytes):** permits a large length-delimited payload and
  establishes the next frame boundary independently of TCP read boundaries.
* **Body length (4 bytes):** separates arbitrary binary body bytes from the
  preceding variable-length headers without scanning body contents.
* **Reserved (4 bytes):** keeps a fixed 20-byte envelope with room for a
  future 32-bit common field; requiring zero avoids accidental interpretations
  in version 1.

The 20-byte fixed layout and explicit payload length are inspired by binary
framing concepts discussed in class. BHTTP does not copy HTTP/2's frame
layout, stream identifiers, flags, or multiplexing behavior.

## 4. Frame types and flags

| Type value | Meaning | Version 1 behavior |
|---:|---|---|
| `1` (`01`) | Request | Parsed as a request; status must be zero and body length must be zero. |
| `2` (`02`) | Response | Parsed as a response; status must be in the range 100–599. |
| Any other value | Unknown/extension | Read and skipped using the fixed header's payload length; no response is generated by the server. |

For known types, nonzero flags or reserved bytes are malformed. For unknown
types, after checking magic, supported version, and a bounded payload length,
the receiver consumes exactly the payload length and skips the frame. It does
not parse the unknown type's flags, status, body length, reserved field, or
payload. This permits an extension to reuse those fields without a version 1
receiver losing synchronization.

## 5. Request frame format

A request is a type-1 frame with zero flags, zero status, zero reserved value,
and zero body length. Its payload contains a header count and encoded headers.
There must be exactly one header named `:path`; other headers may be present
and are ignored by this server. `:path` contains an origin-form path, optionally
followed by a query. The server uses the path portion to select a file below
the configured web root.

The server rejects a request with a nonzero status/body length, malformed
header section, or anything other than exactly one `:path` with a 400 response.
A path that is not absolute, contains invalid UTF-8 or forbidden path
characters or malformed percent escapes, includes a `..` path segment, or
resolves outside the web root is also rejected with 400. A missing/non-file
target returns 404.

## 6. Response frame format

A response is a type-2 frame with zero flags and zero reserved value. The
status code field contains an unsigned numeric status from 100 through 599.
The payload contains a header count, encoded headers, and then the body.
Response body bytes are written unchanged to the client output; they need not
be text or UTF-8. The server currently returns one `content-type` header.

The client exits zero only for 2xx statuses. All other valid response status
values, including 1xx and 3xx, produce a nonzero exit.

## 7. Payload and header encoding

The payload has this exact layout:

```text
header-count (2 bytes)
header 1
header 2
...
header N
body (body-length bytes)
```

The header count is an unsigned 16-bit integer, big-endian, counting complete
name/value pairs. For each header, in order:

| Part | Size | Encoding |
|---|---:|---|
| Name length | 2 bytes | Unsigned big-endian byte count |
| Value length | 4 bytes | Unsigned big-endian byte count |
| Name | Name length bytes | UTF-8 |
| Value | Value length bytes | UTF-8 |

Lengths count encoded **bytes**, not Unicode characters. Names must contain at
least one byte. Values may be empty. No NUL terminator, delimiter, padding, or
implicit whitespace is added. Header names are compared exactly and
case-sensitively by this implementation.

After exactly the declared number of pairs, the header section must end
exactly at `payload length - body length`. Any missing length bytes, field
extending past that boundary, invalid UTF-8, or leftover bytes before the body
makes a known frame malformed. The body is the final `body length` bytes.

### String encoding

Header names and values, including the `:path` value, are UTF-8 without a
terminating NUL. The server percent-decodes the URL path as UTF-8 before file
resolution. The binary response body is not decoded.

### Status code encoding

The status code is the unsigned 16-bit integer at offset 6, in big-endian
order. Requests must use zero. Responses must use a value from 100 through
599; for example, 200 is `00 c8`, 400 is `01 90`, and 404 is `01 94`.
It is a binary number, not the ASCII string `"200"`.

### Body encoding

The body is opaque bytes, and its exact size is given by the 32-bit body
length. For requests in version 1, that size must be zero. For responses, it
may be zero or positive. Text bodies are not given special treatment.

## 8. Maximum sizes

| Limit | Version 1 value |
|---|---:|
| Complete frame, fixed header included | 8,388,608 bytes (8 MiB) |
| Payload | 8,388,588 bytes (8 MiB minus the 20-byte fixed header) |
| Header pairs per frame | 64 |
| Header name | 255 UTF-8 bytes |
| Header value | 8,192 UTF-8 bytes |

The payload limit includes the two-byte count, every field-length word, every
name and value, and the body. Receivers check the payload length against the
limit before allocating or reading the payload. The server further limits a
file response so its headers plus body fit within that payload limit; an
oversized file receives status 500.

## 9. Malformed frame behavior

A malformed known frame is rejected; its fields are never treated as a valid
request or response.

* If the fixed header is complete, the advertised payload is within the
  maximum, and the payload has been consumed, a malformed frame receives a
  400 response. The server can then continue at the next frame boundary.
  This includes invalid magic/version, nonzero known-frame flags/reserved
  fields, invalid known-frame status/body metadata, and invalid payload fields.
* If a fixed header or payload is truncated, or the advertised payload exceeds
  the maximum, the server attempts a 400 response and then closes the
  connection. It cannot safely identify the next frame boundary.
* A frame that cannot be read because the peer has already closed may not be
  able to receive the 400; the server still closes cleanly.
* A complete request with syntactically valid framing but invalid request
  semantics (for example, missing or duplicate `:path`) receives 400 and the
  connection remains open.

The 400 error response is a normal type-2 response frame and carries a short
UTF-8 diagnostic body.

## 10. Unknown frame behavior

To process a candidate unknown frame, a receiver reads its fixed 20-byte
header and first checks that payload length is at most 8 MiB minus 20 bytes.
It then consumes exactly that many bytes before validating the magic and
version. If those envelope fields are valid, it skips the frame without
parsing the payload or relying on body length, flags, status, or reserved
bytes. Consuming the bounded payload before rejecting an invalid magic or
version allows the stream to recover at the next frame boundary. The server
sends no response for a valid unknown frame and continues reading. A truncated
fixed header, truncated payload, or over-limit payload cannot be skipped
under this version's envelope rules.

## 11. Connection lifecycle

1. A client opens a TCP connection.
2. It writes a complete request frame, handling partial writes.
3. The server accumulates and parses one complete frame at a time.
4. The server skips unknown frames, or processes a request and writes one
   response frame.
5. The connection remains open for further sequential requests.
6. Either peer may close the TCP connection; clean EOF between frames ends the
   session. There is no protocol-level close frame or keep-alive text header.
7. An unrecoverable frame boundary error results in a best-effort 400 followed
   by server-side connection closure.

The current server implementation handles up to 64 active client connections.
Additional accepted connections are closed when all slots are busy. Each
accepted socket has a 30-second blocking-I/O timeout; an idle or stalled
connection is closed after that timeout. These are server resource-protection
limits, not fields in the wire protocol.

## 12. Example request frame

Request for `/index.html`, no body, with one `:path` header:

| Bytes | Meaning |
|---|---|
| `42 48` | Magic `BH` |
| `01` | Version 1 |
| `01` | Request type |
| `00 00` | Flags zero |
| `00 00` | Request status zero |
| `00 00 00 18` | Payload length 24 |
| `00 00 00 00` | Body length zero |
| `00 00 00 00` | Reserved zero |
| `00 01` | One header |
| `00 05` | Name length: 5 bytes |
| `00 00 00 0b` | Value length: 11 bytes |
| `3a 70 61 74 68` | UTF-8 name `:path` |
| `2f 69 6e 64 65 78 2e 68 74 6d 6c` | UTF-8 value `/index.html` |

## 13. Example response frame

Response status 200, header `content-type: text/plain`, body bytes `OK`:

| Bytes | Meaning |
|---|---|
| `42 48` | Magic `BH` |
| `01` | Version 1 |
| `02` | Response type |
| `00 00` | Flags zero |
| `00 c8` | Status 200 |
| `00 00 00 20` | Payload length 32 |
| `00 00 00 02` | Body length 2 |
| `00 00 00 00` | Reserved zero |
| `00 01` | One header |
| `00 0c` | Name length: 12 bytes |
| `00 00 00 0a` | Value length: 10 bytes |
| `63 6f 6e 74 65 6e 74 2d 74 79 70 65` | `content-type` |
| `74 65 78 74 2f 70 6c 61 69 6e` | `text/plain` |
| `4f 4b` | Body `OK` |

## 14. Complete annotated hexadecimal example

The following is the complete request frame for `/index.html`. Offsets are
hexadecimal and each line contains at most 16 bytes. The frame is 44 bytes:
20 fixed-header bytes plus 24 payload bytes.

```text
0000  42 48 01 01 00 00 00 00 00 00 00 18 00 00 00 00
0010  00 00 00 00 00 01 00 05 00 00 00 0b 3a 70 61 74
0020  68 2f 69 6e 64 65 78 2e 68 74 6d 6c
```

The complete response shown above is 52 bytes: 20 fixed-header bytes plus 32
payload bytes.

```text
0000  42 48 01 02 00 00 00 c8 00 00 00 20 00 00 00 02
0010  00 00 00 00 00 01 00 0c 00 00 00 0a 63 6f 6e 74
0020  65 6e 74 2d 74 79 70 65 74 65 78 74 2f 70 6c 61
0030  69 6e 4f 4b
```

In both examples the payload length begins at offset 8 and counts only the
bytes after offset 19. Therefore `20 + payload length` is the complete frame
length.
