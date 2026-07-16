import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_fernet_instance: Fernet | None = None


def _write_private_key_file(path: Path, key: bytes) -> None:
    """Create a key file with owner-only permissions where supported."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, key)
    finally:
        os.close(descriptor)
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows ACLs are not represented by POSIX mode bits. The file still
        # remains local; a platform keychain can replace this in a later phase.
        pass


def get_fernet() -> Fernet:
    """Return the configured local encryption primitive.

    An invalid configured key is a startup/configuration error. We never derive
    a predictable replacement from MAC addresses or silently change keys.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    key_value = settings.SECRET_ENCRYPTION_KEY
    if key_value:
        key_bytes = key_value.encode()
    else:
        key_file = Path(settings.DATA_DIR) / ".key"
        if key_file.exists():
            key_bytes = key_file.read_bytes().strip()
        else:
            key_bytes = Fernet.generate_key()
            try:
                _write_private_key_file(key_file, key_bytes)
            except FileExistsError:
                # Another process won the creation race.
                key_bytes = key_file.read_bytes().strip()

    try:
        _fernet_instance = Fernet(key_bytes)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            "PDM_SECRET_ENCRYPTION_KEY or data/.key is not a valid Fernet key"
        ) from exc
    return _fernet_instance


def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    return get_fernet().encrypt(secret.encode()).decode()


def decrypt_secret(encrypted: str) -> str:
    if not encrypted:
        return ""
    try:
        return get_fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Stored provider secret cannot be decrypted with the current key"
        ) from exc
