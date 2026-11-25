"""tls_peek - what a host's TLS handshake hands THIS Python, and what this Python trusts (v4.271).

Run on the machine whose HTTPS call fails, with the same Python the app uses:

    py -3.12 deploy\\tls_peek.py api.telegram.org

Prints, with verification OFF (so the handshake completes whatever the chain is): the
TLS version, every certificate the server sent (subject, issuer, validity) - the last one
is the root, which tells a real public chain from an intercepting firewall or proxy -
then what this Python verifies against (the OpenSSL default paths, SSL_CERT_FILE /
SSL_CERT_DIR if set, the Windows store count, certifi's bundle if installed) and finally
a VERIFIED handshake like the app's, with the exact error if it fails. No token, no
account: it only looks at certificates. Diagnostic only - never changes anything.
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
import tempfile


def _decode(der: bytes) -> dict:
    """Subject / issuer / validity of one DER certificate via the stdlib (no cryptography package)."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    fd, path = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(pem)
        return ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]  # noqa: SLF001 - diagnostic
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _name(rdns) -> str:
    return ", ".join("=".join(kv) for rdn in (rdns or ()) for kv in rdn)


def _chain(sock) -> list[bytes]:
    obj = getattr(sock, "_sslobj", None)
    for attr in ("get_unverified_chain", "get_verified_chain"):
        fn = getattr(sock, attr, None) or getattr(obj, attr, None)
        if fn:
            try:
                out = fn()
                if out:
                    return [c if isinstance(c, bytes) else c.public_bytes(ssl._ssl.ENCODING_DER) for c in out]  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    leaf = sock.getpeercert(binary_form=True)
    return [leaf] if leaf else []


def main(host: str, port: int = 443) -> int:
    print(f"python {sys.version.split()[0]}  openssl {ssl.OPENSSL_VERSION}")
    print(f"host {host}:{port}\n")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with ctx.wrap_socket(socket.create_connection((host, port), timeout=15), server_hostname=host) as s:
            print(f"handshake (unverified): {s.version()}  cipher {s.cipher()[0]}")
            chain = _chain(s)
    except Exception as exc:  # noqa: BLE001
        print(f"handshake (unverified) FAILED: {exc!r}")
        return 2
    print(f"the server sent {len(chain)} certificate(s):")
    for i, der in enumerate(chain):
        try:
            d = _decode(der)
            print(f"  [{i}] subject: {_name(d.get('subject'))}")
            print(f"      issuer:  {_name(d.get('issuer'))}")
            print(f"      valid:   {d.get('notBefore')}  ->  {d.get('notAfter')}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}] (could not decode: {exc!r})")
    if chain:
        try:
            last = _decode(chain[-1])
            if _name(last.get("subject")) == _name(last.get("issuer")):
                print("  the last certificate is SELF-SIGNED (a root): if its name is not a public authority"
                      " (GlobalSign, DigiCert, Go Daddy, Let's Encrypt ...) something in between is intercepting")
        except Exception:  # noqa: BLE001
            pass

    print("\nwhat this Python verifies against:")
    vp = ssl.get_default_verify_paths()
    print(f"  openssl defaults: cafile={vp.openssl_cafile}  capath={vp.openssl_capath}")
    for k in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY"):
        if os.environ.get(k):
            print(f"  env {k} = {os.environ[k]}")
    dflt = ssl.create_default_context()
    print(f"  loaded by create_default_context(): {dflt.cert_store_stats()}")
    try:
        import certifi  # type: ignore

        print(f"  certifi (httpx uses it): {certifi.where()}")
    except Exception:  # noqa: BLE001
        print("  certifi: not installed")

    print("\nverified handshake (as the app does it):")
    try:
        with dflt.wrap_socket(socket.create_connection((host, port), timeout=15), server_hostname=host) as s:
            print(f"  OK - {s.version()} - the chain verifies; the app's call should work")
            return 0
    except ssl.SSLCertVerificationError as exc:
        print(f"  FAILED - {exc.verify_message} (code {exc.verify_code})")
        print("  -> the chain above is not trusted by this Python's store; see the last certificate's issuer")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED - {exc!r}")
        return 1


if __name__ == "__main__":
    h = sys.argv[1] if len(sys.argv) > 1 else "api.telegram.org"
    p = int(sys.argv[2]) if len(sys.argv) > 2 else 443
    sys.exit(main(h, p))
