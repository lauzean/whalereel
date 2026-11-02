"""密码散列与访问令牌工具（用户体系）。

纯标准库实现：密码用 PBKDF2-HMAC-SHA256（随机盐 + 固定迭代次数），
令牌用 secrets 随机 256bit 十六进制串。散列串格式：
pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""

import hashlib
import hmac
import secrets

# PBKDF2 迭代次数（安全与登录耗时折中，登录单次 <100ms）
PBKDF2_ITERATIONS = 200_000
# PBKDF2 盐长度（字节）
PBKDF2_SALT_BYTES = 16
# 散列算法标识（散列串首段，算法升级时向后兼容判别用）
PBKDF2_ALGO_ID = "pbkdf2_sha256"
# 访问令牌长度（字节；hex 编码后翻倍）
TOKEN_BYTES = 32
# 令牌有效期（秒）：30 天
TOKEN_TTL_SECONDS = 30 * 24 * 3600
# 内置管理员账号（仅 users 表为空时种子创建；首次登录后可在界面改密）
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
# 用户名规则：2-32 位中文/字母/数字/下划线/连字符
USERNAME_PATTERN = r"^[A-Za-z0-9_\-一-龥]{2,32}$"
# 密码最短长度
PASSWORD_MIN_LENGTH = 6


def hash_password(password: str) -> str:
    """明文密码 → PBKDF2 散列串（随机盐）。"""
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_ALGO_ID}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与散列串是否匹配（常数时间比较，防时序侧信道）。"""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != PBKDF2_ALGO_ID:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    """生成新访问令牌（256bit 随机十六进制串）。"""
    return secrets.token_hex(TOKEN_BYTES)
