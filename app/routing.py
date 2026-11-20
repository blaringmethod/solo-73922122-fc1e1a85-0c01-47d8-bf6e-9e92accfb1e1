"""入仓分流核查：对一版已发布固件允许的**全部**合法报文，证明每条报文的队列去向。

输入：固件写入格式（受限 JSON Schema，支持顶层 anyOf）与至多 8 条分流规则
（每条 = 队列名 + 一份受限 JSON Schema）。报文命中某条规则当且仅当它满足该规则
Schema；同一队列名下多条规则对同一报文同时命中时只算一个去向（按队列名去重）。

核查方式（z3 逻辑证明，非抽样）：在「固件合法」约束下枚举全部可实现的命中向量
（2^n 个，n≤8），每个向量取一条合法报文作样本，并用纯 Python 判定器独立复核：
- 命中 0 个队列            -> 漏投（没有队列收件）
- 命中 ≥2 个**不同**队列   -> 多投（多个队列同时收件）
- 每个可实现向量都恰好命中 1 个队列 -> 通过（证明全部合法报文恰好进入一个队列）
求解超时/未知无法判全时给出「待核」，并附已枚举到的组合。

字段缺省与显式 null 由编码中的 present / kind 两组变量区分（见 solver._Encoder）：
required 字段缺省即不命中；allow_null 只接受显式 null，二者不会混淆。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from z3 import Not, Or, Solver, is_true, sat

from schema_spec import Schema, SchemaError, message_valid, parse_schema
from solver import KIND_INT, KIND_NULL, _Encoder

MAX_RULES = 8
QUEUE_NAME_RE = re.compile(r"^[^\s]{1,64}$")
TOTAL_BUDGET_SECONDS = 20.0  # 整次核查的总求解预算，超时即判「待核」


class RoutingError(ValueError):
    """分流规则或核查输入不合法。"""


@dataclass
class Rule:
    index: int          # 在粘贴数组中的下标（0 起）
    queue: str
    schema: Schema


def parse_rules(raw) -> list[Rule]:
    """解析并校验粘贴进来的规则 JSON 数组（1~8 条，每条含 queue 与 schema）。"""
    if not isinstance(raw, list):
        raise RoutingError("规则必须是 JSON 数组，每个元素为 {\"queue\": 队列名, \"schema\": 受限JSON Schema}")
    if not raw:
        raise RoutingError("规则数组不能为空，至少需要 1 条规则")
    if len(raw) > MAX_RULES:
        raise RoutingError(f"规则最多 {MAX_RULES} 条，收到 {len(raw)} 条")
    rules = []
    for i, item in enumerate(raw):
        where = f"规则[{i}]"
        if not isinstance(item, dict):
            raise RoutingError(f"{where}: 必须是 JSON 对象")
        extra = set(item) - {"queue", "schema"}
        if extra:
            raise RoutingError(f"{where}: 含多余字段 {sorted(extra)}，只允许 queue / schema")
        queue = item.get("queue")
        if not isinstance(queue, str) or not QUEUE_NAME_RE.match(queue.strip()):
            raise RoutingError(f"{where}: 队列名必须是 1~64 字符的非空白字符串")
        if "schema" not in item:
            raise RoutingError(f"{where}: 缺少 schema")
        try:
            schema = parse_schema(item["schema"])
        except SchemaError as e:
            raise RoutingError(f"{where}.schema 不符合受限语法：{e}") from e
        rules.append(Rule(index=i, queue=queue.strip(), schema=schema))
    return rules


def _build_encoder(firmware: Schema, rules: list[Rule]):
    """汇总固件与全部规则涉及的字段及字符串枚举表，构造统一报文编码器。"""
    names = set()
    for sch in [firmware] + [r.schema for r in rules]:
        for b in sch.branches:
            names |= set(b.properties)
    fields = sorted(names)
    str_tables = {}
    for f in fields:
        strs = set()
        for sch in [firmware] + [r.schema for r in rules]:
            for b in sch.branches:
                spec = b.properties.get(f)
                if spec and spec.str_set:
                    strs |= set(spec.str_set)
        if strs:
            str_tables[f] = sorted(strs)
    return _Encoder(fields, str_tables), fields, str_tables


def _extract_message(enc, model, fields, str_tables) -> dict:
    """从 z3 模型还原报文；缺省字段不写入键，显式 null 写为 null（二者区分保留）。"""
    msg = {}
    for f in fields:
        if is_true(model.eval(enc.present[f], model_completion=True)):
            kind = model.eval(enc.kind[f], model_completion=True).as_long()
            if kind == KIND_NULL:
                msg[f] = None
            elif kind == KIND_INT:
                msg[f] = model.eval(enc.ival[f], model_completion=True).as_long()
            else:
                msg[f] = str_tables[f][
                    model.eval(enc.sval[f], model_completion=True).as_long()]
    return msg


def _verify_witness(msg: dict, firmware: Schema, rules: list[Rule], bits: list[bool]) -> None:
    """纯 Python 独立复核：报文确实满足固件，且每条规则的命中情况与 z3 向量一致。"""
    if not message_valid(msg, firmware):
        raise RoutingError(f"内部错误：样本报文不满足固件格式 {msg!r}")
    for rule, bit in zip(rules, bits):
        if message_valid(msg, rule.schema) != bit:
            raise RoutingError(f"内部错误：规则[{rule.index}] 命中复核失败 {msg!r}")


def check_routing(firmware: Schema, rules: list[Rule]) -> dict:
    """枚举固件合法报文的全部异常/正常去向组合，返回可 JSON 序列化的核查报告。"""
    enc, fields, str_tables = _build_encoder(firmware, rules)
    bits_expr = [enc.schema_ok(r.schema) for r in rules]

    s = Solver()
    s.add(*enc.wellformed())
    s.add(enc.schema_ok(firmware))  # 只在固件允许的合法报文空间内枚举

    combos = {}          # 队列名元组(去重后) -> 组合信息
    vector_count = 0     # 已枚举到的原始命中向量数（同名队列去重前）
    complete = True
    reason = None
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    cap = 2 ** len(rules)

    for _ in range(cap):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            complete, reason = False, "求解总预算用尽，未能判全全部合法报文"
            break
        s.set("timeout", int(remaining * 1000))
        result = s.check()
        if result == sat:
            pass
        elif str(result) == "unknown":
            complete, reason = False, "z3 求解超时/未知，未能判全全部合法报文"
            break
        else:  # unsat：所有可实现的命中向量已枚举完毕
            break

        model = s.model()
        bits = [is_true(model.eval(b, model_completion=True)) for b in bits_expr]
        msg = _extract_message(enc, model, fields, str_tables)
        _verify_witness(msg, firmware, rules, bits)
        vector_count += 1

        # 同名队列多条规则命中只算一个去向
        queues = sorted({r.queue for r, bit in zip(rules, bits) if bit})
        key = tuple(queues)
        matched_rules = [{"index": r.index, "queue": r.queue}
                         for r, bit in zip(rules, bits) if bit]
        if key not in combos:
            combos[key] = {
                "queues": queues,
                "size": len(queues),
                "kind": "none" if not queues else ("single" if len(queues) == 1 else "multi"),
                "message": msg,
                "matched_rules": matched_rules,
            }
        # 封锁该命中向量，继续枚举其它向量（至多 2^n 个）
        s.add(Or([b if not val else Not(b) for b, val in zip(bits_expr, bits)]))
    # 退出循环只有三种情况：unsat（向量已枚举完）、跑满 cap 次 SAT（2^n 个向量全部
    # 找到，同样判全）、或上面的超时/未知分支（complete 已置 False）。

    combo_list = sorted(combos.values(), key=lambda c: (c["size"], c["queues"]))
    anomalies = [c for c in combo_list if c["size"] != 1]

    if not complete:
        status = "pending"
    elif not combo_list:
        status, reason = "pending", "该固件格式不存在任何合法报文，无从核查去向"
    elif not anomalies:
        status = "pass"
    else:
        status = "anomaly"

    return {
        "status": status,          # pass | anomaly | pending
        "complete": complete,
        "reason": reason,
        "rule_count": len(rules),
        "rules": [{"index": r.index, "queue": r.queue} for r in rules],
        "combos": combo_list,
        "anomaly_count": len(anomalies),
        "enumerated_vectors": len(combos),
        "vector_count": vector_count,
    }
