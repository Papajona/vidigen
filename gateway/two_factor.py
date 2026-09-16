"""TOTP-based 2FA logic, kept free of any database or FastAPI dependency so it can be
tested deterministically — pyotp's TOTP codes are time-based, so tests fix the timestamp
rather than depending on wall-clock time (which would make tests flaky by definition)."""
import secrets as _secrets
from datetime import datetime, timedelta, timezone
import pyotp


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_email: str, issuer: str = 'Vidigen') -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=issuer)


def verify_code(secret: str, code: str, for_time: datetime | None = None) -> bool:
    """valid_window=1 allows one 30s step of clock drift either side — standard practice
    for TOTP verification, not a security weakening (an attacker still needs the secret,
    this just tolerates normal clock skew between the admin's phone and this server)."""
    if not code or not code.isdigit():
        return False
    totp = pyotp.TOTP(secret)
    if for_time is not None:
        return totp.verify(code, for_time=for_time, valid_window=1)
    return totp.verify(code, valid_window=1)


def generate_session_token() -> str:
    return _secrets.token_urlsafe(32)


def session_expiry(hours: int = 4) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def is_session_valid(expires_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return expires_at > now
