# Annotated BHTTP/1 wire exchange

This is a real exchange captured by running the current `bserve` and `bcurl`
implementations. The server was started with the equivalent Windows command
`python bserve .\www 9000`; the client was run as
`python bcurl -v localhost:9000/index.html`. The client opened one TCP
connection and exited with status 0.

The served file, [index.html](../www/index.html), contains
`Binary protocol submission example.` followed by CRLF. Its exact body is
37 bytes:

```text
42 69 6e 61 72 79 20 70 72 6f 74 6f 63 6f 6c 20
73 75 62 6d 69 73 73 69 6f 6e 20 65 78 61 6d 70
6c 65 2e 0d 0a
```

## Request frame

The client sent one complete 44-byte request frame. Offsets are zero-based
from the beginning of this frame.

### Raw hexadecimal bytes

```text
0000  42 48 01 01 00 00 00 00 00 00 00 18 00 00 00 00
0010  00 00 00 00 00 01 00 05 00 00 00 0b 3a 70 61 74
0020  68 2f 69 6e 64 65 78 2e 68 74 6d 6c
```

### Fixed header, bytes 00–19 (20 bytes total)

| Offset | Raw bytes | Field | Decoded value |
|---|---|---|---|
| `00-01` | `42 48` | MAGIC | ASCII `BH` |
| `02` | `01` | VERSION | 1 |
| `03` | `01` | FRAME_TYPE | 1 = REQUEST |
| `04-05` | `00 00` | FLAGS | 0 |
| `06-07` | `00 00` | STATUS_CODE | 0 (required for requests) |
| `08-11` | `00 00 00 18` | PAYLOAD_LENGTH | 24 bytes |
| `12-15` | `00 00 00 00` | BODY_LENGTH | 0 bytes |
| `16-19` | `00 00 00 00` | RESERVED | 0 |

The fixed header ends at byte offset 19. The payload therefore starts at
offset 20 and occupies offsets 20–43 (24 bytes).

### Decoded payload, bytes 20–43

| Offset | Raw bytes | Payload field | Decoded value |
|---|---|---|---|
| `20-21` | `00 01` | HEADER_COUNT | 1 |
| `22-23` | `00 05` | HEADER_NAME_LENGTH | 5 bytes |
| `24-27` | `00 00 00 0b` | HEADER_VALUE_LENGTH | 11 bytes |
| `28-32` | `3a 70 61 74 68` | HEADER_NAME | UTF-8 `:path` |
| `33-43` | `2f 69 6e 64 65 78 2e 68 74 6d 6c` | HEADER_VALUE | UTF-8 `/index.html` |

There is one header pair. No body bytes follow it; `BODY_LENGTH` is zero.

### Request byte-range account

```text
00-01  magic
02     version
03     frame type
04-05  flags
06-07  status code
08-11  payload length
12-15  body length
16-19  reserved
20-21  header count
22-23  header name length
24-27  header value length
28-32  header name (:path)
33-43  header value (/index.html)
```

These ranges cover every byte from 00 through 43 exactly once:
20 fixed-header bytes + 24 payload bytes = 44 total bytes.

## Response frame

The server returned one complete 86-byte response frame.

### Raw hexadecimal bytes

```text
0000  42 48 01 02 00 00 00 c8 00 00 00 42 00 00 00 25
0010  00 00 00 00 00 01 00 0c 00 00 00 09 63 6f 6e 74
0020  65 6e 74 2d 74 79 70 65 74 65 78 74 2f 68 74 6d
0030  6c 42 69 6e 61 72 79 20 70 72 6f 74 6f 63 6f 6c
0040  20 73 75 62 6d 69 73 73 69 6f 6e 20 65 78 61 6d
0050  70 6c 65 2e 0d 0a
```

### Fixed header, bytes 00–19 (20 bytes total)

| Offset | Raw bytes | Field | Decoded value |
|---|---|---|---|
| `00-01` | `42 48` | MAGIC | ASCII `BH` |
| `02` | `01` | VERSION | 1 |
| `03` | `02` | FRAME_TYPE | 2 = RESPONSE |
| `04-05` | `00 00` | FLAGS | 0 |
| `06-07` | `00 c8` | STATUS_CODE | 200 |
| `08-11` | `00 00 00 42` | PAYLOAD_LENGTH | 66 bytes |
| `12-15` | `00 00 00 25` | BODY_LENGTH | 37 bytes |
| `16-19` | `00 00 00 00` | RESERVED | 0 |

The fixed header ends at byte offset 19. The payload starts at offset 20 and
occupies offsets 20–85 (66 bytes).

### Decoded payload, bytes 20–85

| Offset | Raw bytes | Payload field | Decoded value |
|---|---|---|---|
| `20-21` | `00 01` | HEADER_COUNT | 1 |
| `22-23` | `00 0c` | HEADER_NAME_LENGTH | 12 bytes |
| `24-27` | `00 00 00 09` | HEADER_VALUE_LENGTH | 9 bytes |
| `28-39` | `63 6f 6e 74 65 6e 74 2d 74 79 70 65` | HEADER_NAME | UTF-8 `content-type` |
| `40-48` | `74 65 78 74 2f 68 74 6d 6c` | HEADER_VALUE | UTF-8 `text/html` |
| `49-85` | `42 69 6e 61 72 79 20 70 72 6f 74 6f 63 6f 6c 20 73 75 62 6d 69 73 73 69 6f 6e 20 65 78 61 6d 70 6c 65 2e 0d 0a` | BODY | 37 raw bytes; ASCII `Binary protocol submission example.` plus CRLF |

There is one header pair. The body is exactly the final 37 payload bytes,
offsets 49–85.

### Response byte-range account

```text
00-01  magic
02     version
03     frame type
04-05  flags
06-07  status code
08-11  payload length
12-15  body length
16-19  reserved
20-21  header count
22-23  header name length
24-27  header value length
28-39  header name (content-type)
40-48  header value (text/html)
49-85  body
```

These ranges cover every byte from 00 through 85 exactly once:
20 fixed-header bytes + 66 payload bytes = 86 total bytes. The payload length
is the sum of 2 count bytes, 6 field-length bytes, 12 name bytes, 9 value
bytes, and 37 body bytes: `2 + 6 + 12 + 9 + 37 = 66`.

## Captured client decoding

The actual `bcurl -v` output decoded the response as:

```text
type=RESPONSE status=200 payload_length=66 body_length=37
Header: 'content-type' = 'text/html'
Body: 37 bytes
```

The client wrote only the 37 response-body bytes to stdout; the frame dumps
and decoded fields were written separately to stderr.
