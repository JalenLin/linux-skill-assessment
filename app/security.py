import hashlib
import hmac
import secrets
import string


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(calc.hex(), h)


def random_password(n: int = 8) -> str:
    # 去掉容易混淆的字元，方便作答者照紙條輸入
    alphabet = "".join(c for c in string.ascii_lowercase + string.digits if c not in "il1o0")
    return "".join(secrets.choice(alphabet) for _ in range(n))
