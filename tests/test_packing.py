"""POST /v1/packing/audit 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

FNC1 = "\x1d"  # ASCII 29


def make_gtin(body13: str) -> str:
    """由 13 位本体计算模 10 校验位,生成合法 14 位 GTIN。"""
    assert len(body13) == 13 and body13.isdigit()
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body13)))
    return body13 + str((10 - total % 10) % 10)


GTIN = make_gtin("0950600112345")
GTIN2 = make_gtin("0950600112346")
GTIN3 = make_gtin("0950600112347")


def scan(gtin=GTIN, serial="SN1", batch=None, expiry=None):
    """按 01/17/10/21 顺序拼接扫描串(变长字段后视需要补 FNC1)。"""
    s = "01" + gtin
    if expiry is not None:
        s += "17" + expiry
    if batch is not None:
        s += "10" + batch + FNC1
    if serial is not None:
        s += "21" + serial
    return s


def pack(parent, child):
    return {"action": "pack", "parent_scan": parent, "child_scan": child}


def unpack(parent, child):
    return {"action": "unpack", "parent_scan": parent, "child_scan": child}


def ref(gtin, serial):
    return {"gtin": gtin, "serial": serial}


def post(operations):
    resp = client.post("/v1/packing/audit", json={"operations": operations})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------- 正常路径 ----------

def test_pack_single_item_into_container():
    body = post([pack(scan(serial="BOX"), scan(serial="SN1"))])
    assert body["errors"] == []
    assert body["items"] == [
        {
            "gtin": GTIN,
            "serial": "BOX",
            "parent": None,
            "children": [ref(GTIN, "SN1")],
            "outermost": ref(GTIN, "BOX"),
        },
        {
            "gtin": GTIN,
            "serial": "SN1",
            "parent": ref(GTIN, "BOX"),
            "children": [],
            "outermost": ref(GTIN, "BOX"),
        },
    ]


def test_nested_packing_outermost_container():
    # SN1 装入 BOX,BOX 再装入 PALLET:最外层容器逐层向上
    body = post(
        [
            pack(scan(serial="BOX"), scan(serial="SN1")),
            pack(scan(serial="PALLET"), scan(serial="BOX")),
        ]
    )
    assert body["errors"] == []
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["SN1"]["parent"] == ref(GTIN, "BOX")
    assert by_serial["SN1"]["outermost"] == ref(GTIN, "PALLET")
    assert by_serial["BOX"]["parent"] == ref(GTIN, "PALLET")
    assert by_serial["BOX"]["children"] == [ref(GTIN, "SN1")]
    assert by_serial["BOX"]["outermost"] == ref(GTIN, "PALLET")
    assert by_serial["PALLET"]["parent"] is None
    assert by_serial["PALLET"]["children"] == [ref(GTIN, "BOX")]
    assert by_serial["PALLET"]["outermost"] == ref(GTIN, "PALLET")


def test_unpack_removes_relation_but_items_stay_listed():
    body = post(
        [
            pack(scan(serial="BOX"), scan(serial="SN1")),
            unpack(scan(serial="BOX"), scan(serial="SN1")),
        ]
    )
    assert body["errors"] == []
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["BOX"]["children"] == []
    assert by_serial["BOX"]["outermost"] == ref(GTIN, "BOX")
    assert by_serial["SN1"]["parent"] is None
    assert by_serial["SN1"]["outermost"] == ref(GTIN, "SN1")  # 无父件时为自身


def test_unpack_then_repack_into_another_container():
    body = post(
        [
            pack(scan(serial="BOX1"), scan(serial="SN1")),
            unpack(scan(serial="BOX1"), scan(serial="SN1")),
            pack(scan(serial="BOX2"), scan(serial="SN1")),
        ]
    )
    assert body["errors"] == []
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["BOX1"]["children"] == []
    assert by_serial["SN1"]["parent"] == ref(GTIN, "BOX2")
    assert by_serial["SN1"]["outermost"] == ref(GTIN, "BOX2")


def test_batch_and_expiry_ignored_for_identity():
    # 同一 GTIN+序列号,批号 / 有效期不同仍视为同一实物
    body = post(
        [
            pack(scan(serial="BOX", batch="LOT1", expiry="251231"), scan(serial="SN1")),
            unpack(scan(serial="BOX"), scan(serial="SN1", batch="LOT2", expiry="260101")),
        ]
    )
    assert body["errors"] == []
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["BOX"]["children"] == []
    assert by_serial["SN1"]["parent"] is None


def test_children_sorted_by_gtin_then_serial():
    body = post(
        [
            pack(scan(serial="BOX"), scan(gtin=GTIN2, serial="SN2")),
            pack(scan(serial="BOX"), scan(serial="SN2")),
            pack(scan(serial="BOX"), scan(serial="SN1")),
        ]
    )
    assert body["errors"] == []
    box = next(item for item in body["items"] if item["serial"] == "BOX")
    assert box["children"] == [
        ref(GTIN, "SN1"),
        ref(GTIN, "SN2"),
        ref(GTIN2, "SN2"),
    ]


def test_items_sorted_by_gtin_then_serial():
    body = post(
        [
            pack(scan(gtin=GTIN3, serial="SN2"), scan(gtin=GTIN2, serial="SN2")),
            pack(scan(gtin=GTIN2, serial="SN1"), scan(serial="SN1")),
        ]
    )
    assert body["errors"] == []
    assert [(i["gtin"], i["serial"]) for i in body["items"]] == [
        (GTIN, "SN1"),
        (GTIN2, "SN1"),
        (GTIN2, "SN2"),
        (GTIN3, "SN2"),
    ]


# ---------- 装入约束 ----------

def test_pack_self_rejected():
    body = post([pack(scan(serial="A"), scan(serial="A"))])
    assert body["items"] == []
    assert [e["index"] for e in body["errors"]] == [0]
    assert "本身" in body["errors"][0]["reason"]


def test_pack_child_with_existing_parent_rejected():
    body = post(
        [
            pack(scan(serial="BOX1"), scan(serial="SN1")),
            pack(scan(serial="BOX2"), scan(serial="SN1")),  # SN1 已有父件
        ]
    )
    assert [e["index"] for e in body["errors"]] == [1]
    assert "父件" in body["errors"][0]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["SN1"]["parent"] == ref(GTIN, "BOX1")  # 失败操作不改变关系
    assert "BOX2" not in by_serial  # 仅被失败操作涉及,不出现在结果中


def test_pack_direct_cycle_rejected():
    body = post(
        [
            pack(scan(serial="A"), scan(serial="B")),
            pack(scan(serial="B"), scan(serial="A")),  # B 是 A 的父件,装入会成环
        ]
    )
    assert [e["index"] for e in body["errors"]] == [1]
    assert "循环" in body["errors"][0]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["B"]["parent"] == ref(GTIN, "A")
    assert by_serial["A"]["parent"] is None


def test_pack_indirect_cycle_rejected():
    # A→B→C 之后把 C 装入 A:C 是 A 的祖先
    body = post(
        [
            pack(scan(serial="A"), scan(serial="B")),
            pack(scan(serial="B"), scan(serial="C")),
            pack(scan(serial="C"), scan(serial="A")),
        ]
    )
    assert [e["index"] for e in body["errors"]] == [2]
    assert "循环" in body["errors"][0]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["A"]["parent"] is None
    assert by_serial["A"]["outermost"] == ref(GTIN, "A")


# ---------- 取出约束 ----------

def test_unpack_without_parent_rejected():
    body = post([unpack(scan(serial="BOX"), scan(serial="SN1"))])
    assert body["items"] == []
    assert [e["index"] for e in body["errors"]] == [0]
    assert "无父件" in body["errors"][0]["reason"]


def test_unpack_must_name_current_direct_parent():
    body = post(
        [
            pack(scan(serial="BOX1"), scan(serial="SN1")),
            unpack(scan(serial="BOX2"), scan(serial="SN1")),  # 指定的父件不符
            unpack(scan(serial="BOX1"), scan(serial="SN1")),  # 失败操作不改变关系,本条成功
        ]
    )
    assert [e["index"] for e in body["errors"]] == [1]
    assert "直接父件" in body["errors"][0]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["SN1"]["parent"] is None
    assert by_serial["BOX1"]["children"] == []


def test_unpack_indirect_parent_rejected():
    # SN1 在 BOX 里、BOX 在 PALLET 里:不能直接从 PALLET 取出 SN1
    body = post(
        [
            pack(scan(serial="BOX"), scan(serial="SN1")),
            pack(scan(serial="PALLET"), scan(serial="BOX")),
            unpack(scan(serial="PALLET"), scan(serial="SN1")),
        ]
    )
    assert [e["index"] for e in body["errors"]] == [2]
    assert "直接父件" in body["errors"][0]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["SN1"]["parent"] == ref(GTIN, "BOX")


# ---------- 扫描问题 ----------

def test_invalid_parent_scan_reported_and_skipped():
    body = post(
        [
            pack("01" + GTIN + "99AB", scan(serial="SN1")),  # 未知 AI '99'
            pack(scan(serial="BOX"), scan(serial="SN1")),
        ]
    )
    assert [e["index"] for e in body["errors"]] == [0]
    assert "父件" in body["errors"][0]["reason"]
    assert "未知 AI" in body["errors"][0]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["SN1"]["parent"] == ref(GTIN, "BOX")


def test_invalid_child_scan_reported_and_skipped():
    body = post([pack(scan(serial="BOX"), "01" + GTIN + "99AB")])
    assert body["items"] == []
    assert [e["index"] for e in body["errors"]] == [0]
    assert "子件" in body["errors"][0]["reason"]
    assert "未知 AI" in body["errors"][0]["reason"]


def test_missing_serial_reported_and_skipped():
    body = post(
        [
            pack(scan(serial=None), scan(serial="SN1")),  # 父件缺序列号
            pack(scan(serial="BOX"), scan(serial=None)),  # 子件缺序列号
            pack(scan(serial="BOX"), scan(serial="SN1")),
        ]
    )
    assert [e["index"] for e in body["errors"]] == [0, 1]
    assert "父件" in body["errors"][0]["reason"] and "序列号" in body["errors"][0]["reason"]
    assert "子件" in body["errors"][1]["reason"] and "序列号" in body["errors"][1]["reason"]
    by_serial = {item["serial"]: item for item in body["items"]}
    assert by_serial["SN1"]["parent"] == ref(GTIN, "BOX")


def test_both_scans_invalid_reports_single_error():
    body = post([pack("01" + GTIN + "99AB", "01" + GTIN + "98AB")])
    assert len(body["errors"]) == 1
    assert body["errors"][0]["index"] == 0
    assert "父件" in body["errors"][0]["reason"]


def test_errors_sorted_by_original_index():
    body = post(
        [
            unpack(scan(serial="BOX"), scan(serial="SN1")),  # 无父件
            pack("01" + GTIN + "99AB", scan(serial="SN2")),  # 父件扫描非法
            pack(scan(serial="A"), scan(serial="A")),  # 子件是父件本身
        ]
    )
    assert body["items"] == []
    assert [e["index"] for e in body["errors"]] == [0, 1, 2]


# ---------- 请求体验证(422) ----------

def _expect_422(payload):
    resp = client.post("/v1/packing/audit", json=payload)
    assert resp.status_code == 422, resp.text


def test_validation_operation_count():
    _expect_422({"operations": []})
    _expect_422({"operations": [pack(scan(serial="BOX"), scan())] * 301})
    resp = client.post(
        "/v1/packing/audit",
        json={"operations": [pack(scan(serial="BOX"), scan())] * 300},
    )
    assert resp.status_code == 200, resp.text


def test_validation_action_must_be_pack_or_unpack():
    _expect_422({"operations": [dict(pack(scan(), scan()), action="repack")]})
    _expect_422({"operations": [{"parent_scan": scan(), "child_scan": scan()}]})


def test_validation_required_fields_and_no_extra():
    _expect_422({"operations": [{"action": "pack", "child_scan": scan()}]})
    _expect_422({"operations": [{"action": "pack", "parent_scan": scan()}]})
    _expect_422({"operations": [dict(pack(scan(), scan()), extra=1)]})
