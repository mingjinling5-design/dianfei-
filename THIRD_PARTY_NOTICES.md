# Third-party notices

`ysp/security_token.py` is a Python port of the `app_security_token` algorithm in
DumpTime/yxy (`crates/core/src/bind/campus/login.rs`), licensed under Apache-2.0.
Original source: https://github.com/DumpTime/yxy/tree/dev
Audited revision: 5b5ae94a453f5ceae9771f43e0da2c42309046b6.
Changes: Python implementation using cryptography, validated padding, optional
timestamp injection for deterministic testing. The original license is preserved
in `licenses/yxy-Apache-2.0.txt`.

The direct API workflow is based on the user's supplied eschool.py. It uses
Python's standard-library transport and manual captcha entry instead of aiohttp
and automated OCR. No user credentials or third-party account are bundled.

`ysp/app_sign.py` ports the signing algorithm from zjutjh/yxy-go
(`internal/utils/yxyClient/sign.go`), licensed under AGPL-3.0.
Source: https://github.com/zjutjh/yxy-go
Audited revision: f2ff145fb94239a254ec969716f6d71e236b8b89.
Changes: Python implementation with the original public signing test vectors.
The modern electricity authorization flow in `ysp/eschool.py` also follows that
client's `internal/manager/auth/electricityAuthManager.go` protocol.
Original license: `licenses/yxy-go-AGPL-3.0.txt`. This local integration has not
been published; any redistribution or hosted version must retain and comply
with the applicable licenses, including corresponding source obligations.
