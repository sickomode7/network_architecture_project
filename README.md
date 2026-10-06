# HTTP, in Binary: Two Tracks, One Protocol

A small HTTP-like protocol over raw TCP. The complete wire format and an
annotated request/response hexdump are in [SPEC.md](SPEC.md).
The implementation uses only the Python standard library and does not use an
HTTP client or server library.

## Run

Python 3.10 or newer is recommended.

On Unix-like systems, from this directory:

```sh
./bserve ./www 9000
./bcurl -v localhost:9000/index.html
```

The second command opens one TCP connection, sends one request, prints its
body to stdout, and writes sent/received frame hexdumps to stderr. The server
keeps each connection open and can process sequential request frames on it.
If needed, make the launchers executable first with `chmod +x bserve bcurl`.

On Windows, use:

```powershell
py bserve .\www 9000
py bcurl -v localhost:9000/index.html
```

Create a `www` directory and put files beneath it before starting the server.
The server binds to all IPv4 interfaces on the selected port.
The server handles up to 64 active clients and closes connections whose
blocking socket operation is idle for 30 seconds.

The client returns status 0 only for 2xx responses. All other response
statuses, along with argument/connection/protocol failures, return nonzero.
An example real client/server exchange with decoded frame fields and
hexadecimal bytes is in [examples/hexdump.txt](examples/hexdump.txt).

## Tests

Run the standard-library test suite with:

```sh
python3 -m unittest discover -s tests -v
```

Tests cover binary bodies, sequential requests over one socket, unknown frame
skipping, malformed frames, and web-root path traversal protection.
