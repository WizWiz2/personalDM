import os
import base64
import uuid
from cryptography.fernet import Fernet
from app.config import settings

_fernet_instance = None

def get_fernet() -> Fernet:
    """Lazily initializes and returns a Fernet instance.

    Uses SECRET_ENCRYPTION_KEY if configured, or falls back to a locally stored key.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
        
    key_str = settings.SECRET_ENCRYPTION_KEY
    if not key_str:
        # Fallback to key file in data directory
        key_file = os.path.join(settings.DATA_DIR, ".key")
        if os.path.exists(key_file):
            with open(key_file, "r") as f:
                key_str = f.read().strip()
        else:
            # Generate a new key and save it
            os.makedirs(settings.DATA_DIR, exist_ok=True)
            key_bytes = Fernet.generate_key()
            key_str = key_bytes.decode()
            with open(key_file, "w") as f:
                f.write(key_str)
                
    # Fernet requires url-safe base64 32-byte key
    try:
        _fernet_instance = Fernet(key_str.encode())
    except Exception:
        # If key is invalid base64 or length, derive one using node UUID
        node_uuid = str(uuid.getnode())
        # Pad or hash to get 32 bytes
        derived = (node_uuid * 3).encode()[:32]
        key_str = base64.urlsafe_b64encode(derived).decode()
        _fernet_instance = Fernet(key_str.encode())
        
    return _fernet_instance

def encrypt_secret(secret: str) -> str:
    """Encrypts a plaintext secret string."""
    if not secret:
        return ""
    f = get_fernet()
    return f.encrypt(secret.encode()).decode()

def decrypt_secret(encrypted: str) -> str:
    """Decrypts an encrypted secret string."""
    if not encrypted:
        return ""
    f = get_fernet()
    try:
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        # In case of corruption or key change, return empty to prevent crash
        return ""
