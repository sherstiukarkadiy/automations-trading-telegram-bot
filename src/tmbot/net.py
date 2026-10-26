"""Network settings shared by all exchange clients.

TLS is verified against the operating system certificate store (truststore) instead of
Python's bundled certifi set. This matters on machines where a corporate proxy or antivirus
re-signs HTTPS traffic with its own root (Zscaler, Netskope, ESET, ...): the OS trusts that
root, certifi does not. Certificate verification is never disabled.
"""

from __future__ import annotations

import ssl

import truststore

TLS_ERROR_HINT = (
    "TLS: сертифікат сервера не пройшов перевірку. Найчастіше причина — корпоративний "
    "проксі чи антивірус, що перевіряє HTTPS. Запусти з іншої мережі/машини або додай "
    "кореневий сертифікат проксі в системне сховище. Перевірку сертифікатів не вимикай."
)


def ssl_context() -> ssl.SSLContext:
    """TLS context backed by the OS certificate store."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def is_tls_error(exc: BaseException) -> bool:
    """True if a connection failed on certificate verification (retrying won't help)."""
    text = str(exc)
    return "CERTIFICATE_VERIFY_FAILED" in text or isinstance(exc, ssl.SSLCertVerificationError)
