"""ENTER_PAGE 的共享参数合同。

能力提示和发送前校验必须复用这里的语法定义，避免模型看到的命令模板
与主机实际接受的命令结构发生漂移。页面参数的业务含义只在已有源码证据时
登记；未知页面不得猜测 ``0`` 或其他取值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


ENTER_PAGE_COMMAND = "ENTER_PAGE"
ENTER_PAGE_PARAM_TYPE = "uint32"
ENTER_PAGE_UINT32_MAX = 0xFFFFFFFF
_WINDOW_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_UINT32_DECIMAL = re.compile(r"[0-9]+")
_FORMAT_EXAMPLE = ":ENTER_PAGE:CALCULATOR,0"


@dataclass(frozen=True)
class EnterPageParamValue:
    """一个已有源码或用例证据支持的页面参数值。"""

    value: int
    meaning: str


@dataclass(frozen=True)
class EnterPageParamContract:
    """某个注册页面对 ENTER_PAGE 第二参数的解释。"""

    meaning: str
    legal_values: tuple[EnterPageParamValue, ...]
    canonical_value: int | None
    complete: bool


_KNOWN_WINDOW_CONTRACTS = {
    "CALCULATOR": EnterPageParamContract(
        meaning="计算器页面不使用启动用户数据；0 仅作为必填占位值",
        legal_values=(EnterPageParamValue(0, "规范占位值"),),
        canonical_value=0,
        complete=True,
    ),
}


def enter_page_param_contract(
    window_name: str,
    *,
    special_values: Iterable[EnterPageParamValue] = (),
) -> EnterPageParamContract:
    """返回页面参数合同；仅把已有源码证据转换为确定结论。"""

    extracted = tuple(special_values)
    if extracted:
        return EnterPageParamContract(
            meaning="特殊页面模式，由页面源码的 switch(param) 解释",
            legal_values=extracted,
            canonical_value=extracted[0].value,
            complete=True,
        )
    known = _KNOWN_WINDOW_CONTRACTS.get(window_name)
    if known is not None:
        return known
    return EnterPageParamContract(
        meaning="页面特定启动用户数据；当前源码目录尚未解析其业务含义",
        legal_values=(),
        canonical_value=None,
        complete=False,
    )


def render_enter_page_knowledge(
    window_name: str,
    *,
    special_values: Iterable[EnterPageParamValue] = (),
) -> str:
    """生成给执行 Agent 的完整页面参数说明。"""

    contract = enter_page_param_contract(
        window_name,
        special_values=special_values,
    )
    common = (
        f"参数1=window_name:{window_name}（注册页面名，必填） | "
        f"参数2=param（{ENTER_PAGE_PARAM_TYPE}，必填） | "
        f"param含义={contract.meaning}"
    )
    if not contract.complete or contract.canonical_value is None:
        return (
            f"语法=:ENTER_PAGE:{window_name},<uint32_param> | {common} | "
            "合法值=未知，禁止猜测 | 完整示例=无"
        )

    command = f":ENTER_PAGE:{window_name},{contract.canonical_value}"
    legal_values = ", ".join(
        f"{item.value}={item.meaning}" for item in contract.legal_values
    )
    return (
        f"命令={command} | {common} | 合法值={legal_values} | "
        f"完整示例={command}"
    )


def parse_enter_page_args(args: str) -> tuple[str, int]:
    """解析 ``window_name,uint32_param`` 并校验已有的确定合同。"""

    if args.count(",") != 1:
        raise ValueError(
            "ENTER_PAGE 必须包含页面名和一个 uint32 参数，完整格式为 "
            f"{_FORMAT_EXAMPLE}"
        )
    window_name, param_text = args.split(",", 1)
    if not _WINDOW_NAME.fullmatch(window_name):
        raise ValueError(
            "ENTER_PAGE 页面名为空或格式错误，完整格式为 "
            f"{_FORMAT_EXAMPLE}"
        )
    if not _UINT32_DECIMAL.fullmatch(param_text):
        raise ValueError(
            "ENTER_PAGE param 必须是非负十进制 uint32，完整格式为 "
            f"{_FORMAT_EXAMPLE}"
        )
    param = int(param_text)
    if param > ENTER_PAGE_UINT32_MAX:
        raise ValueError(
            f"ENTER_PAGE param 超出 uint32 范围，完整格式为 {_FORMAT_EXAMPLE}"
        )
    known = _KNOWN_WINDOW_CONTRACTS.get(window_name)
    if known is not None and param not in {
        item.value for item in known.legal_values
    }:
        legal_values = ", ".join(str(item.value) for item in known.legal_values)
        raise ValueError(
            f"ENTER_PAGE 页面 {window_name} 的合法 param 为 {legal_values}，"
            f"完整格式为 {_FORMAT_EXAMPLE}"
        )
    return window_name, param


__all__ = [
    "ENTER_PAGE_COMMAND",
    "ENTER_PAGE_PARAM_TYPE",
    "EnterPageParamContract",
    "EnterPageParamValue",
    "enter_page_param_contract",
    "parse_enter_page_args",
    "render_enter_page_knowledge",
]
