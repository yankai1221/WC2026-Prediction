"""VaultKeyProvider —— vault key 来源契约。

修复 S1：Kompany 原 engine.py:177-191 把 ``vault_key=""`` 当合法值，
凭据库会拿空串作为 master key 解密，等于实际未加密。

本契约要求 provider 要么返回**非空** key，要么直接抛
``VaultKeyMissing`` 让上层 fail-loud。
"""
from __future__ import annotations

from typing import Protocol


class VaultKeyProvider(Protocol):
    def resolve(self) -> str:
        """返回非空 vault key。

        实现合约：
        - 解析成功：返回非空字符串。
        - 解析失败：raise ``lottery_kernel.errors.VaultKeyMissing``。
        - 绝不允许返回空串、None 或被 ``except: pass`` 吞掉。
        """
        ...
