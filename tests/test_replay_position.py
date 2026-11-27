"""Optional recorded positions remain explicit and independent of replay timing."""

import csv

import pytest

from core.replay_source import ReplayRow, ReplaySource


@pytest.mark.parametrize("schema_version", [3, 4])
@pytest.mark.parametrize("column", ["position_mm", " Position_MM ", None, "position"])
def test_optional_finite_recorded_position(tmp_path, schema_version, column):
    path = tmp_path / "positions.csv"
    header = ["schema_version", "相对时间/s", "帧号", "状态", "valid_mask",
              "i2c_err", "ch01_delta_signed_mv", "ch01_wire_mv"]
    if column is not None:
        header.append(column)
    values = ["0", "-2.5", "1.25", "", "nan", "inf", "-inf", "bad", "1e309"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for index, position in enumerate(values):
            row = [schema_version, index * 0.29, index, 0, 1, 0, 0.125, 424.625]
            if column is not None:
                row.append(position)
            writer.writerow(row)

    source = ReplaySource()
    source.load(str(path))
    rows = source.inspection_rows
    assert isinstance(rows, tuple)
    assert len(rows) == len(values)
    expected = [0.0, -2.5, 1.25] + [None] * 6
    if column not in ("position_mm", " Position_MM "):
        expected = [None] * len(values)
    assert [row.position_mm for row in rows] == expected
    assert rows[0].wire_mv == [424.625]
    assert rows[0].delta_signed == [0.125]
    assert source.current_index == 0
    assert source.state == "stopped"
    # Existing positional/keyword constructors need not supply the new field.
    old_fields = {key: value for key, value in vars(rows[0]).items() if key != "position_mm"}
    assert ReplayRow(**old_fields).position_mm is None


def test_inspection_conditions_normalized_conflicts_and_reset(tmp_path):
    path = tmp_path / "conditions.csv"
    header = ["schema_version", "相对时间/s", "帧号", "状态", "valid_mask",
              "i2c_err", "ch01_delta_signed_mv", "激励频率", "频率单位",
              "增益", "线圈", "铁板", "baseline_device_id"]

    def write(conditions):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for index, values in enumerate(conditions):
                writer.writerow([4, index, index, 0, 1, 0, 0.125] + values + ["other-baseline-board"])

    source = ReplaySource()
    write([["", "", "", "", ""],
           [" 1000 ", " KHZ ", " 1 ", " coil-a ", " steel "],
           ["1e3", "kHz", "1", "coil-a", "steel"],
           ["nan", "", "", "", ""]])
    source.load(str(path))
    expected = {"frequency": 1000.0, "freq_unit": "kHz", "gain": "1",
                "coil": "coil-a", "plate": "steel"}
    assert source.inspection_conditions == expected
    assert not source.inspection_conditions_conflict
    source.inspection_conditions["coil"] = "changed-outside"
    assert source.inspection_conditions == expected

    write([["1000", "kHz", "1", "coil-a", "steel"],
           ["2000", "kHz", "1", "coil-a", "steel"]])
    source.load(str(path))
    assert source.inspection_conditions_conflict
    assert source.inspection_conditions["frequency"] == 1000.0

    write([["-1", "", "", "", ""], ["inf", "", "", "", ""],
           ["invalid", "", "", "", ""], ["0", "", "", "", ""]])
    source.load(str(path))
    assert source.inspection_conditions == {"frequency": 0.0}
    assert not source.inspection_conditions_conflict

    write([["", "", "", "coil-a", ""], ["", "", "", "coil-b", ""]])
    source.load(str(path))
    assert source.inspection_conditions_conflict
