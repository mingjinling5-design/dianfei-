"""Python port of DumpTime/yxy app_security_token (Apache-2.0).

Source: crates/core/src/bind/campus/login.rs, see THIRD_PARTY_NOTICES.md.
"""
import base64
import hashlib
import time
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class SecurityTokenCodec:
    def generate_app_security_token(self, security_token, device_id, app_schema_version='4.1.6', timestamp=None):
        key = security_token[:16].encode('ascii')
        encrypted = base64.b64decode(security_token[32:], validate=True)
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        challenge = (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')
        stamp = int(time.time()) if timestamp is None else int(timestamp)
        value = f'{device_id}|YUNMA_APP|{challenge}|{stamp}|{app_schema_version}'
        digest = hashlib.md5(hashlib.md5(value.encode()).hexdigest().upper().encode()).hexdigest().upper()
        padder = padding.PKCS7(128).padder()
        plain = (value + '|' + digest).encode()
        data = padder.update(plain) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()
