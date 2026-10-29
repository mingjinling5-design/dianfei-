"""YUNMA_APP request signing, ported from zjutjh/yxy-go (AGPL-3.0).

Source: internal/utils/yxyClient/sign.go; see THIRD_PARTY_NOTICES.md.
"""
import base64
import hashlib
import hmac


def sign_request(params):
    values = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value,str) and not value:
            continue
        if isinstance(value,bool):
            value = 'true' if value else 'false'
        values.append(str(value))
    uid = str(params.get('ymId',''))
    device = str(params.get('deviceId',''))
    material = (uid[-13:] if len(uid)>=13 else '') + (device[-29:] if len(device)>=29 else '')
    key = hashlib.sha256(material.encode()).hexdigest()[32:].encode()
    return base64.b64encode(hmac.new(key,'|'.join(values).encode(),hashlib.sha256).digest()).decode()
