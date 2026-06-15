"""Kernel-level exceptions.

Fail-loud 是核心约定：任何配置/凭据缺失都必须抛出本模块中的具体异常，
绝不允许 ``except Exception: pass`` 形式的静默回退（修复 S1）。
"""
from __future__ import annotations


class KernelConfigError(RuntimeError):
    """配置层错误的基类。"""


class VaultKeyMissing(KernelConfigError):
    """vault_key 为空或解析失败。

    决不允许使用空字符串作为加密 master key（修复 S1：
    Kompany 原 engine.py:177-191 的静默回退路径）。
    """


class StorageContractError(KernelConfigError):
    """LedgerStorage 未满足 Protocol 契约。"""


class HostContractError(KernelConfigError):
    """TickHost 未满足 Protocol 契约。"""
