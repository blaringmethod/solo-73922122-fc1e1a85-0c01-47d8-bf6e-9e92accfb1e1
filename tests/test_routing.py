"""分流核查单元自测：规则解析、z3 全量去向枚举、漏投/多投/通过/去重、
缺省与显式 null 区分、顶层 anyOf、纯 Python 复核。
运行：python3 tests/test_routing.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from examples import (QC_FW, ROUTING_RULES_DUP, ROUTING_RULES_MISS,  # noqa: E402
                      ROUTING_RULES_OK)
from routing import (MAX_RULES, RoutingError, check_routing,  # noqa: E402
                     parse_rules)
from schema_spec import message_valid, parse_schema  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)


def rules(docs):
    return parse_rules(docs)


def verify_report(report, firmware_doc, raw_rules):
    """对报告里每个组合做纯 Python 独立复核：样本满足固件，命中队列名单与规则重算一致。"""
    fw = parse_schema(firmware_doc)
    parsed = parse_rules(raw_rules)
    for c in report["combos"]:
        msg = c["message"]
        ok(f"复核[{c['queues']}]样本满足固件", message_valid(msg, fw), str(msg))
        expect = sorted({r.queue for r in parsed if message_valid(msg, r.schema)})
        ok(f"复核[{c['queues']}]队列名单一致", expect == c["queues"],
           f"{expect} != {c['queues']}, msg={msg}")
        ok(f"复核[{c['queues']}]类型标注正确", c["size"] == len(c["queues"])
           and c["kind"] == ("none" if not c["queues"] else
                             ("single" if len(c["queues"]) == 1 else "multi")))


# ---------- 规则解析：合法 ----------
ok("解析:单条规则", len(rules([{"queue": "auto", "schema": QC_FW}])) == 1)
ok("解析:8条规则上限内", len(rules([{"queue": f"q{i}", "schema": QC_FW}
                                    for i in range(MAX_RULES)])) == MAX_RULES)

# ---------- 规则解析：非法 ----------
def expect_routing_error(name, raw):
    try:
        parse_rules(raw)
    except RoutingError:
        PASS.append(name)
        return
    raise AssertionError(f"FAILED(应拒绝但未拒绝): {name}")


expect_routing_error("解析:非数组", {"queue": "auto", "schema": QC_FW})
expect_routing_error("解析:空数组", [])
expect_routing_error("解析:超8条", [{"queue": "q", "schema": QC_FW}] * (MAX_RULES + 1))
expect_routing_error("解析:队列名空白", [{"queue": "  ", "schema": QC_FW}])
expect_routing_error("解析:缺schema", [{"queue": "q"}])
expect_routing_error("解析:多余字段", [{"queue": "q", "schema": QC_FW, "x": 1}])
expect_routing_error("解析:schema违反受限语法",
                     [{"queue": "q", "schema": {"type": "object",
                                                 "properties": {"a": {"type": "string"}},
                                                 "additionalProperties": False}}])
expect_routing_error("解析:元素非对象", ["not-an-object"])

# ---------- 场景一：通过（恰好一个队列，含同名队列去重） ----------
rep = check_routing(parse_schema(QC_FW), rules(ROUTING_RULES_OK))
ok("通过场景:状态pass", rep["status"] == "pass", rep.get("reason"))
ok("通过场景:判全", rep["complete"] is True)
ok("通过场景:无异常组合", rep["anomaly_count"] == 0)
ok("通过场景:全部组合为单队列", all(c["size"] == 1 for c in rep["combos"]))
queues_seen = {tuple(c["queues"]) for c in rep["combos"]}
ok("通过场景:两个队列都有去向", queues_seen == {("auto",), ("manual",)}, str(queues_seen))
verify_report(rep, QC_FW, ROUTING_RULES_OK)
# 人工复核两条同名规则：缺省 qc 与显式 null 是不同命中向量，但去向组合都是 manual
# （同名队列只算一个去向，同一去向组合保留一条样本）；两种形态分别用判定器确认
parsed_ok = parse_rules(ROUTING_RULES_OK)
manual_legal = [r for r in parsed_ok if r.queue == "manual"]  # idx1=只收显式null, idx2=只收缺省
miss_msg, null_msg = {"temp": 1, "site": "WQ-01"}, {"temp": 1, "site": "WQ-01", "qc": None}
ok("通过场景:manual两条规则分别覆盖缺省与显式null",
   not message_valid(miss_msg, manual_legal[0].schema)
   and message_valid(null_msg, manual_legal[0].schema)
   and message_valid(miss_msg, manual_legal[1].schema)
   and not message_valid(null_msg, manual_legal[1].schema))

# ---------- 场景二：漏投 ----------
rep = check_routing(parse_schema(QC_FW), rules(ROUTING_RULES_MISS))
ok("漏投场景:状态anomaly", rep["status"] == "anomaly")
none_combos = [c for c in rep["combos"] if c["kind"] == "none"]
ok("漏投场景:存在无队列组合", len(none_combos) == 1)
msg = none_combos[0]["message"]
ok("漏投场景:反例qc缺省或显式null", "qc" not in msg or msg["qc"] is None, str(msg))
ok("漏投场景:反例仍满足固件", message_valid(msg, parse_schema(QC_FW)))
verify_report(rep, QC_FW, ROUTING_RULES_MISS)

# ---------- 场景三：多投 ----------
rep = check_routing(parse_schema(QC_FW), rules(ROUTING_RULES_DUP))
ok("多投场景:状态anomaly", rep["status"] == "anomaly")
multi = [c for c in rep["combos"] if c["kind"] == "multi"]
ok("多投场景:存在双队列组合", any(c["queues"] == ["auto", "manual"] for c in multi))
m = next(c for c in multi if c["queues"] == ["auto", "manual"])["message"]
ok("多投场景:反例qc取N/Q", m.get("qc") in ("N", "Q"), str(m))
# 同时应保留漏投之外的单队列 manual 组合（qc 缺省/null 只命中 manual）
ok("多投场景:仍有单队列manual组合",
   any(c["queues"] == ["manual"] for c in rep["combos"]))
verify_report(rep, QC_FW, ROUTING_RULES_DUP)

# ---------- 同名队列多条规则命中只算一个去向 ----------
dedup_rules = [
    {"queue": "auto", "schema": QC_FW},  # 收全部固件合法报文
    {"queue": "auto", "schema": {  # 只收 qc 显式 N/Q
        "type": "object",
        "properties": {
            "temp": {"type": "integer", "minimum": -40, "maximum": 100},
            "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]},
            "qc": {"enum": ["N", "Q"]}},
        "required": ["temp", "site", "qc"], "additionalProperties": False}},
]
rep = check_routing(parse_schema(QC_FW), rules(dedup_rules))
ok("去重:无异常(只算一个队列)", rep["status"] == "pass" and rep["anomaly_count"] == 0)
ok("去重:所有组合队列名单长度为1", all(len(c["queues"]) == 1 for c in rep["combos"]))
# qc=N 的报文确实同时命中两条规则，但按队列名去重后只有一个去向
dedup_parsed = parse_rules(dedup_rules)
nq_msg = {"temp": 1, "site": "WQ-01", "qc": "N"}
ok("去重:qc=N同时命中两条规则",
   all(message_valid(nq_msg, r.schema) for r in dedup_parsed))
ok("去重:复核器认定只有一个队列",
   sorted({r.queue for r in dedup_parsed if message_valid(nq_msg, r.schema)}) == ["auto"])
verify_report(rep, QC_FW, dedup_rules)

# ---------- 字段缺省 vs 显式 null：三种向量必须分开枚举 ----------
null_missing_rules = [
    {"queue": "qn", "schema": {  # 只收显式 null
        "type": "object",
        "properties": {
            "temp": {"type": "integer", "minimum": -40, "maximum": 100},
            "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]},
            "qc": {"type": "null"}},
        "required": ["temp", "site", "qc"], "additionalProperties": False}},
    {"queue": "qp", "schema": {  # 只收 N/Q
        "type": "object",
        "properties": {
            "temp": {"type": "integer", "minimum": -40, "maximum": 100},
            "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]},
            "qc": {"enum": ["N", "Q"]}},
        "required": ["temp", "site", "qc"], "additionalProperties": False}},
]
rep = check_routing(parse_schema(QC_FW), rules(null_missing_rules))
keys = {tuple(c["queues"]) for c in rep["combos"]}
ok("null区分:qn/qp/空三种组合齐全", keys == {("qn",), ("qp",), ()}, str(keys))
null_msg = next(c for c in rep["combos"] if c["queues"] == ["qn"])["message"]
miss_msg = next(c for c in rep["combos"] if not c["queues"])["message"]
ok("null区分:qn样本显式带null", null_msg.get("qc") is None and "qc" in null_msg)
ok("null区分:漏投样本缺省qc", "qc" not in miss_msg, str(miss_msg))
verify_report(rep, QC_FW, null_missing_rules)

# ---------- 顶层 anyOf 固件 ----------
anyof_fw = {"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 5}},
     "required": ["a"], "additionalProperties": False},
    {"type": "object", "properties": {"b": {"enum": ["x"]}},
     "required": ["b"], "additionalProperties": False}]}
branch_a_rule = [{"queue": "qa", "schema": {
    "type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 5}},
    "required": ["a"], "additionalProperties": False}}]
rep = check_routing(parse_schema(anyof_fw), rules(branch_a_rule))
ok("anyOf:只盖一个分支时漏投", rep["status"] == "anomaly")
none_msg = next(c for c in rep["combos"] if c["kind"] == "none")["message"]
ok("anyOf:漏投反例来自另一分支", none_msg == {"b": "x"}, str(none_msg))
both_rules = branch_a_rule + [{"queue": "qb", "schema": {
    "type": "object", "properties": {"b": {"enum": ["x"]}},
    "required": ["b"], "additionalProperties": False}}]
rep = check_routing(parse_schema(anyof_fw), rules(both_rules))
ok("anyOf:两分支各有队列则通过", rep["status"] == "pass" and rep["anomaly_count"] == 0)
verify_report(rep, anyof_fw, both_rules)

# ---------- 单规则与固件完全相同：全部报文恰好一个队列 ----------
rep = check_routing(parse_schema(QC_FW), rules([{"queue": "only", "schema": QC_FW}]))
ok("全等规则:通过", rep["status"] == "pass")
ok("全等规则:仅一个组合", rep["enumerated_vectors"] == 1
   and rep["combos"][0]["queues"] == ["only"])

print(f"全部 {len(PASS)} 项分流核查单元自测通过")
