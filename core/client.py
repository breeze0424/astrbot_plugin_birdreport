"""中国观鸟记录中心公开接口的加密 HTTP 客户端（异步）。

协议依据网站公开页面脚本：请求体为「表单参数按 ASCII 键序排序 → 压缩 JSON →
按 117 字节分段用 1024 位 RSA 公钥 PKCS#1 v1.5 加密 → 拼接 Base64」，
请求头带毫秒 `timestamp`、32 位 `requestId` 与 `sign=MD5(明文JSON + requestId + timestamp)`。
列表/详情接口的加密 `data` 用 AES-CBC/PKCS7 解密；部分端点（如 adcode 省市）返回明文。

网站更新加密逻辑后需重新核对页面脚本，此时会抛 `ProtocolChangedError`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urljoin

import httpx
from cryptography.hazmat.primitives import padding as symmetric_padding
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .errors import (
    BirdReportError,
    NetworkError,
    ProtocolChangedError,
    VerificationRequiredError,
)

BASE = "https://www.birdreport.cn"
REPORT_PAGE = BASE + "/home/search/report.html"
API_BASE = "https://api.birdreport.cn/"
PUBLIC_KEY_DER_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCvxXa98E1uWXnBzXkS2yHUfnBM6n3PCwLdfIox"
    "03T91joBvjtoDqiQ5x3tTOfpHs3LtiqMMEafls6b0YWtgB1dse1W5m+FpeusVkCOkQxB4SZDH6tuer"
    "IknnmB/Hsq5wgEkIvO5Pff9biig6AyoAkdWpSek/1/B7zYIepYY0lxKQIDAQAB"
)
AES_KEY = b"C8EB5514AF5ADDB94B2207B08C66601C"
AES_IV = b"55DD79C6F04E1A67"
PAGE_SIZE = 20

# 触发验证码的业务码与关键词
VERIFICATION_CODES = {405, 505}
VERIFICATION_KEYWORDS = ("频繁", "验证码", "验证", "稍后再试", "访问过于频繁", "请输入")


def request_id() -> str:
    """生成与网站 getUuid() 一致的 32 位十六进制 ID。"""
    digits = "0123456789abcdef"
    chars = [secrets.choice(digits) for _ in range(32)]
    chars[14] = "4"
    chars[19] = digits[((int(chars[19], 16) if chars[19].isdigit() else 0) & 3) | 8]
    chars[8] = chars[13] = chars[18] = chars[23]
    return "".join(chars)


def encrypt_request(params: dict, public_key) -> tuple[str, str]:
    """把请求参数加密成 POST 表单体，返回 (明文 JSON 文本, Base64 密文)。"""
    form = urlencode(params)
    if form:
        parsed = dict(pair.split("=", 1) for pair in form.split("&"))
    else:
        parsed = {}
    plain = json.dumps(dict(sorted(parsed.items())), ensure_ascii=False, separators=(",", ":"))
    max_chunk = (public_key.key_size + 7) // 8 - 11
    raw = plain.encode("utf-8")
    cipher = b"".join(
        public_key.encrypt(raw[pos:pos + max_chunk], asymmetric_padding.PKCS1v15())
        for pos in range(0, len(raw), max_chunk)
    )
    return plain, base64.b64encode(cipher).decode("ascii")


def decrypt_data(ciphertext: str):
    """AES-CBC/PKCS7 解密接口返回的加密 `data`。失败即视为协议变化。"""
    try:
        raw = base64.b64decode(ciphertext, validate=True)
        decryptor = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).decryptor()
        padded = decryptor.update(raw) + decryptor.finalize()
        unpadder = symmetric_padding.PKCS7(128).unpadder()
        plain = unpadder.update(padded) + unpadder.finalize()
        return json.loads(plain.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolChangedError("接口响应无法解密；网站可能已更新加密协议") from exc


def report_url(filters: dict) -> str:
    """根据筛选条件生成网页查询链接（仅用于复现/展示）。"""
    encoded = base64.b64encode(
        json.dumps(filters, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    return REPORT_PAGE + "?" + urlencode({"search": encoded})


def detail_url(report_id: str) -> str:
    """生成报告详情页链接（带时间戳，只作展示，不作为永久 ID）。"""
    param = {"reportId": report_id, "ts": int(time.time() * 1000)}
    encoded = base64.b64encode(json.dumps(param, separators=(",", ":")).encode()).decode()
    sign = hashlib.md5(b"").hexdigest()
    return BASE + "/home/record/page.html?" + urlencode({"param": encoded, "sign": sign})


class BirdReportClient:
    """异步加密 HTTP 客户端。"""

    def __init__(self, timeout: float = 25):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5),
            follow_redirects=True,
        )
        self.public_key = serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY_DER_B64))

    async def post(self, path: str, params: dict) -> dict:
        """加密 POST。返回外层 JSON；`data` 是否加密由各端点决定，调用方自行解密。"""
        plain, body = encrypt_request(params, self.public_key)
        requestid = request_id()
        timestamp = str(int(time.time() * 1000))
        sign = hashlib.md5((plain + requestid + timestamp).encode("utf-8")).hexdigest()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": BASE + "/",
            "Origin": BASE,
            "timestamp": timestamp,
            "requestId": requestid,
            "sign": sign,
        }
        try:
            response = await self._client.post(
                urljoin(API_BASE, path.lstrip("/")), data=body, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise NetworkError(f"接口 {path} 请求失败：{exc}") from exc
        except ValueError as exc:
            raise ProtocolChangedError(f"接口 {path} 没有返回 JSON") from exc
        if not isinstance(payload, dict):
            raise ProtocolChangedError(f"接口 {path} 返回格式异常")
        code = payload.get("code")
        if code not in (None, 0):
            msg = str(payload.get("msg") or code)
            if code in VERIFICATION_CODES or any(k in msg for k in VERIFICATION_KEYWORDS):
                raise VerificationRequiredError(f"接口 {path} 触发验证码：{msg}")
            raise BirdReportError(f"接口 {path} 报错：{msg}", category="business")
        return payload

    async def close(self):
        await self._client.aclose()
