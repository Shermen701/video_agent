from __future__ import annotations

import base64
import binascii
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def ensure_rsa_key_pair(private_key_path: str | Path, public_key_path: str | Path) -> None:
    private_path = Path(private_key_path)
    public_path = Path(public_key_path)
    if private_path.exists() and public_path.exists():
        return

    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def decrypt_rsa_credential(ciphertext: str, private_key_path: str | Path) -> str:
    encrypted = _decode_ciphertext(ciphertext)
    private_key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
    paddings = [
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
        padding.PKCS1v15(),
    ]
    last_error: Exception | None = None
    for rsa_padding in paddings:
        try:
            return private_key.decrypt(encrypted, rsa_padding).decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - try next supported RSA padding.
            last_error = exc
    raise RuntimeError("failed to decrypt RSA credential") from last_error


def encrypt_rsa_credential(plaintext: str, public_key_path: str | Path) -> str:
    public_key = serialization.load_pem_public_key(Path(public_key_path).read_bytes())
    encrypted = public_key.encrypt(
        plaintext.encode("utf-8"),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_rsa_password(ciphertext: str, private_key_path: str | Path) -> str:
    return decrypt_rsa_credential(ciphertext, private_key_path)


def encrypt_rsa_password(plaintext: str, public_key_path: str | Path) -> str:
    return encrypt_rsa_credential(plaintext, public_key_path)


def looks_like_rsa_ciphertext(value: str) -> bool:
    try:
        encrypted = _decode_ciphertext(value)
    except ValueError:
        return False
    return len(encrypted) >= 128


def _decode_ciphertext(value: str) -> bytes:
    text = value.strip()
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("RSA ciphertext must be base64 encoded") from exc
